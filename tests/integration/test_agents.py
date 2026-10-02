import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.agents import get_db
from backend.app.config import get_settings
from backend.app.main import create_app
from backend.app.models import Agent, Operation, WorkloadIncarnation
from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS
from tests.admin_client import administrator_client

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def database_engine():
    url = os.environ.get("TALOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TALOS_TEST_DATABASE_URL to run PostgreSQL integration checks")
    schema = f"talos_api_test_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def session_maker(database_engine):
    # Every table is inside this module's random schema; other database users and
    # the development stack are unaffected.
    with database_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE agents, workload_incarnations, operations, employees, roles CASCADE")
        )
    return sessionmaker(database_engine, expire_on_commit=False)


def api_for(session_maker):
    app = create_app()

    def database():
        with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = database
    return app


@pytest.fixture
def client(session_maker):
    with administrator_client(api_for(session_maker), session_maker) as client:
        yield client


def create(client, key="create-one", name="Sales helper"):
    return client.post(
        "/api/v1/agents",
        json={"display_name": name, "employee_label": "Alex", "runtime_mode": "managed"},
        headers={"Idempotency-Key": key},
    )


def test_native_model_creation_and_durable_selection(client, session_maker, monkeypatch):
    async def catalog():
        return [
            {
                "id": "test/model",
                "name": "Test",
                "context_length": 32000,
                "supported_parameters": [],
            }
        ]

    monkeypatch.setattr("backend.app.inference.catalog", catalog)
    response = client.post(
        "/api/v1/agents",
        json={
            "display_name": "Native",
            "employee_label": "Alex",
            "model_id": "test/model",
        },
        headers={"Idempotency-Key": "native-model-create"},
    )
    assert response.status_code == 202
    agent_id = response.json()["agent_id"]
    assert (
        client.get(f"/api/v1/agents/{agent_id}").json()["inference_override"]["model_id"]
        == "test/model"
    )
    complete_operation(session_maker, response.json()["id"])
    path = f"/api/v1/inference/agents/{agent_id}/native"
    response = client.post(
        path, json={"model_id": "test/model"}, headers={"Idempotency-Key": "native-model-set"}
    )
    assert response.status_code == 202
    assert response.json()["action"] == "configure_model"
    assert (
        client.post(
            path, json={"model_id": "test/model"}, headers={"Idempotency-Key": "native-model-set"}
        ).json()
        == response.json()
    )
    assert (
        client.post(
            path,
            json={"model_id": "test/model"},
            headers={"Idempotency-Key": "native-model-conflict"},
        ).status_code
        == 409
    )
    complete_operation(session_maker, response.json()["id"])
    assert (
        client.post(
            path, json={"model_id": "unknown"}, headers={"Idempotency-Key": "unknown"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            path, json={"model_id": "fixture"}, headers={"Idempotency-Key": "fixture"}
        ).status_code
        == 400
    )
    response = client.post(
        path,
        content="null",
        headers={"Content-Type": "application/json", "Idempotency-Key": "reset"},
    )
    assert response.status_code == 202
    with session_maker() as session:
        assert session.get(Operation, UUID(response.json()["id"])).model_selection == {}
    # Native selections must not silently fall through the managed settings API.
    assert client.delete(f"/api/v1/inference/agents/{agent_id}").status_code == 409


def complete_operation(session_maker, operation_id, observed_state="stopped"):
    with session_maker.begin() as session:
        operation = session.get(Operation, UUID(operation_id))
        operation.status = "succeeded"
        operation.step = "complete"
        session.get(Agent, operation.agent_id).observed_state = observed_state


def test_create_replay_validation_and_restart_persistence(client, session_maker):
    response = create(client)
    assert response.status_code == 202
    operation = response.json()
    assert operation["status"] == "queued"
    assert operation["target_revision"] == 1
    assert create(client).json() == operation
    assert create(client, name="Different agent").status_code == 409
    assert len(client.get("/api/v1/agents").json()) == 1
    agent = client.get(f"/api/v1/agents/{operation['agent_id']}").json()
    assert agent["desired_state"] == "stopped"
    assert agent["observed_state"] == "pending"
    assert agent["runtime_release"] == "openclaw-2026.9.6"
    assert agent["current_incarnation_id"] is None
    assert "idempotency_key" not in operation
    assert "request_hash" not in operation
    assert "gateway_token_hash" not in agent

    assert (
        client.post(
            "/api/v1/agents",
            headers={"Idempotency-Key": "override"},
            json={"display_name": "Agent", "employee_label": "Alex", "runtime_release": "unsafe"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/agents", json={"display_name": "Agent", "employee_label": "Alex"}
        ).status_code
        == 422
    )
    assert create(client, key="blank", name="   ").status_code == 422
    assert create(client, key=" ").status_code == 422
    for field in ("display_name", "employee_label"):
        payload = {"display_name": "Agent", "employee_label": "Alex", field: "A\x00B"}
        assert (
            client.post(
                "/api/v1/agents", json=payload, headers={"Idempotency-Key": "invalid-text"}
            ).status_code
            == 422
        )
    assert len(client.get("/api/v1/agents").json()) == 1
    with administrator_client(api_for(session_maker), session_maker) as restarted:
        assert restarted.get(f"/api/v1/operations/{operation['id']}").json() == operation
        assert create(restarted).json() == operation


def test_concurrent_create_reuses_one_agent_and_operation(client, session_maker):
    barrier = Barrier(6)

    def submit(_):
        barrier.wait(timeout=10)
        return create(client, key="concurrent-create")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, range(6)))
    assert [response.status_code for response in responses] == [202] * 6
    assert len({response.json()["id"] for response in responses}) == 1
    with session_maker() as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1
        assert session.scalar(select(func.count()).select_from(Operation)) == 1


def test_concurrent_mismatched_create_rolls_back_losing_agent(client, session_maker):
    barrier = Barrier(2)

    def submit(name):
        barrier.wait(timeout=10)
        return create(client, key="same-key", name=name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["First", "Second"]))
    assert sorted(response.status_code for response in responses) == [202, 409]
    with session_maker() as session:
        assert session.scalar(select(func.count()).select_from(Agent)) == 1


def test_concurrent_lifecycle_conflict_and_replay(client, session_maker):
    created = create(client).json()
    complete_operation(session_maker, created["id"])
    agent_id = created["agent_id"]
    barrier = Barrier(2)

    def submit(action):
        barrier.wait(timeout=10)
        return client.post(
            f"/api/v1/agents/{agent_id}/{action}", headers={"Idempotency-Key": action}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ["start", "stop"]))
    assert sorted(response.status_code for response in responses) == [202, 409]
    operation = next(response.json() for response in responses if response.status_code == 202)
    action = operation["action"]
    assert operation["target_revision"] == 2
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/{action}", headers={"Idempotency-Key": action}
        ).json()
        == operation
    )
    with session_maker.begin() as session:
        session.get(Operation, UUID(operation["id"])).status = "retry_wait"
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/stop", headers={"Idempotency-Key": "new-key"}
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/agents/{agent_id}").json()["revision"] == 2


def test_delete_tombstone_keeps_idempotency_and_operation_history(client, session_maker):
    created = create(client).json()
    agent_id = created["agent_id"]
    complete_operation(session_maker, created["id"])
    path = f"/api/v1/agents/{agent_id}"
    deletion = client.delete(path, headers={"Idempotency-Key": "delete-one"})
    assert deletion.status_code == 202
    assert (
        client.post(f"{path}/start", headers={"Idempotency-Key": "cannot-start"}).status_code == 409
    )
    complete_operation(session_maker, deletion.json()["id"], "deleted")
    replay = client.delete(path, headers={"Idempotency-Key": "delete-one"})
    assert replay.status_code == 202
    assert replay.json()["id"] == deletion.json()["id"]
    assert replay.json()["status"] == "succeeded"
    assert create(client).json()["id"] == created["id"]
    assert client.get(path).status_code == 404
    assert client.get("/api/v1/agents").json() == []
    assert client.delete(path, headers={"Idempotency-Key": "new-delete"}).status_code == 404
    assert client.get(f"/api/v1/operations/{uuid4()}").status_code == 404


def test_start_does_not_replace_a_ready_runtime(client, session_maker):
    created = create(client).json()
    complete_operation(session_maker, created["id"])
    path = f"/api/v1/agents/{created['agent_id']}/start"
    started = client.post(path, headers={"Idempotency-Key": "start-one"}).json()
    complete_operation(session_maker, started["id"], "ready")
    assert client.post(path, headers={"Idempotency-Key": "start-one"}).json()["id"] == started["id"]
    assert client.post(path, headers={"Idempotency-Key": "start-again"}).status_code == 409
    assert client.get(f"/api/v1/agents/{created['agent_id']}").json()["revision"] == 2


def test_native_default_dashboard_admission_and_idempotency(client, session_maker):
    response = client.post(
        "/api/v1/agents",
        json={"display_name": "Native", "employee_label": "Owner"},
        headers={"Idempotency-Key": "native"},
    )
    assert response.status_code == 202
    created = response.json()
    agent_id = created["agent_id"]
    complete_operation(session_maker, created["id"])
    agent = client.get(f"/api/v1/agents/{agent_id}").json()
    assert agent["runtime_mode"] == "native"
    url = f"/api/v1/agents/{agent_id}/dashboard"
    headers = {"Idempotency-Key": "open"}
    assert client.post(url, headers=headers).status_code == 409
    with session_maker.begin() as session:
        row = session.get(Agent, UUID(agent_id))
        row.desired_state, row.observed_state = "running", "ready"
    opened = client.post(url, headers=headers)
    assert opened.status_code == 202
    assert opened.headers["Cache-Control"] == "no-store"
    assert client.post(url, headers=headers).json() == opened.json()
    assert client.get(f"/api/v1/agents/{agent_id}").json()["revision"] == agent["revision"]
    complete_operation(session_maker, opened.json()["id"], "ready")
    result = client.get("/api/v1/operations/" + opened.json()["id"])
    assert result.headers["Cache-Control"] == "no-store"
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/diagnostic-runs",
            json={"message": "hello"},
            headers={"Idempotency-Key": "native-chat"},
        ).status_code
        == 409
    )
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).runtime_mode = "managed"
    assert client.post(url, headers={"Idempotency-Key": "managed-open"}).status_code == 409


def test_hermes_password_is_hashed_private_and_part_of_idempotency(client, session_maker):
    import base64
    import hashlib

    payload = {
        "display_name": "Hermes helper",
        "employee_label": "Owner",
        "runtime_kind": "hermes",
        "dashboard_password": "hermes-test-password-only",
    }
    headers = {"Idempotency-Key": "hermes-create"}
    response = client.post("/api/v1/agents", json=payload, headers=headers)
    assert response.status_code == 202
    created = response.json()
    assert client.post("/api/v1/agents", json=payload, headers=headers).json() == created
    assert (
        client.post(
            "/api/v1/agents",
            json={**payload, "dashboard_password": "different-test-password"},
            headers=headers,
        ).status_code
        == 409
    )
    agent = client.get("/api/v1/agents/" + created["agent_id"]).json()
    assert agent["runtime_kind"] == "hermes"
    assert agent["runtime_mode"] == "native"
    assert agent["runtime_release"] == "hermes-0.21.5"
    assert "dashboard_password" not in agent and "dashboard_password_hash" not in agent
    with session_maker() as session:
        stored = session.get(Agent, UUID(created["agent_id"])).dashboard_password_hash
    scheme, n, r, p, salt, key = stored.split("$")
    assert scheme == "scrypt" and payload["dashboard_password"] not in stored
    assert hashlib.scrypt(
        payload["dashboard_password"].encode(),
        salt=base64.b64decode(salt),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=32,
    ) == base64.b64decode(key)
    for bad in [
        {**payload, "runtime_mode": "managed"},
        {k: v for k, v in payload.items() if k != "dashboard_password"},
        {**payload, "runtime_kind": "unknown"},
        {**payload, "runtime_kind": "openclaw"},
    ]:
        rejected = client.post(
            "/api/v1/agents", json=bad, headers={"Idempotency-Key": "bad-runtime"}
        )
        assert rejected.status_code == 422
        assert payload["dashboard_password"] not in rejected.text


def test_employee_role_assignment_preserves_legacy_agents(client, session_maker):
    legacy = create(client).json()
    complete_operation(session_maker, legacy["id"])
    role = client.post("/api/v1/roles", json={"name": "Sales"}).json()
    assert role["capabilities"] == [] and role["revision"] == 1
    assert client.post("/api/v1/roles", json={"name": "Sales"}).status_code == 409
    assert (
        client.post(
            "/api/v1/roles", json={"name": "Bad", "capabilities": ["send_mail"]}
        ).status_code
        == 422
    )
    employee = client.post("/api/v1/employees", json={"name": "Alex", "role_id": role["id"]}).json()
    path = f"/api/v1/agents/{legacy['agent_id']}"
    before = client.get(path).json()
    assert before["employee_id"] is None and before["employee_label"] == "Alex"
    assigned = client.put(path + "/employee", json={"employee_id": employee["id"]})
    assert assigned.status_code == 200
    assert assigned.json()["role"]["id"] == role["id"]
    assert assigned.json()["employee_label"] == "Alex"
    assert client.delete("/api/v1/roles/" + role["id"]).status_code == 409
    assert client.delete("/api/v1/employees/" + employee["id"]).status_code == 409
    updated = client.put(
        "/api/v1/roles/" + role["id"], json={"name": "Sales", "capabilities": ["web_research"]}
    ).json()
    assert updated["revision"] == 2
    assert client.get(path).json()["role"]["capabilities"] == ["web_research"]
    created = client.post(
        "/api/v1/agents",
        json={"display_name": "Assigned", "employee_id": employee["id"]},
        headers={"Idempotency-Key": "assigned"},
    )
    assert created.status_code == 202
    assert (
        client.put(
            f"/api/v1/agents/{created.json()['agent_id']}/employee",
            json={"employee_id": employee["id"]},
        ).status_code
        == 409
    )
    assert len(client.get("/api/v1/capabilities").json()) == 3


def test_employee_and_role_edits_and_deletion(client):
    role = client.post("/api/v1/roles", json={"name": "Sales"}).json()
    path = "/api/v1/roles/" + role["id"]
    assert client.put(path, json={"name": "Renamed"}).json()["revision"] == 1
    employee = client.post("/api/v1/employees", json={"name": "Alex", "role_id": role["id"]}).json()
    ep = "/api/v1/employees/" + employee["id"]
    assert (
        client.put(
            ep, json={"name": "Alexander", "email": "alex@example.com", "role_id": role["id"]}
        ).status_code
        == 200
    )
    assert client.put(ep, json={"name": "Alex", "role_id": str(uuid4())}).status_code == 404
    assert client.delete(ep).status_code == 204
    assert client.delete(path).status_code == 204


@pytest.mark.parametrize("kind,version", [("openclaw", "2026.9.10"), ("hermes", "0.22.0")])
@pytest.mark.parametrize("requested", [None, "latest", "specific"])
def test_native_version_selection_and_replay_after_catalog_change(
    client, session_maker, monkeypatch, kind, version, requested
):
    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    image = "sha256:" + "a" * 64
    catalog[kind][version] = image
    monkeypatch.setattr(get_settings(), "runtime_versions", catalog)
    payload = {"display_name": "Versioned", "employee_label": "Alex", "runtime_kind": kind}
    if kind == "hermes":
        payload["dashboard_password"] = "synthetic-version-password"
    if requested is not None:
        payload["runtime_version"] = version if requested == "specific" else requested
    response = client.post("/api/v1/agents", json=payload, headers={"Idempotency-Key": "version"})
    assert response.status_code == 202
    operation = response.json()
    with session_maker() as session:
        agent = session.get(Agent, UUID(operation["agent_id"]))
        assert agent.runtime_release == f"{kind}-{version}"
        assert agent.runtime_image == image
    targets = client.get("/api/v1/setups/runtime-targets").json()
    assert {"runtime_kind": kind, "runtime_release": f"{kind}-{version}"} in targets
    # A timed-out create must recover the admitted selection even after removal.
    monkeypatch.setattr(get_settings(), "runtime_versions", deepcopy(DEFAULT_RUNTIME_VERSIONS))
    assert (
        client.post("/api/v1/agents", json=payload, headers={"Idempotency-Key": "version"}).json()
        == operation
    )
    assert len(client.get("/api/v1/agents").json()) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"runtime_version": "2026.9.99"},
        {"runtime_version": "v2026.9.6"},
        {"runtime_version": "2026.9.6", "runtime_mode": "managed"},
        {"runtime_image": "unapproved-image:latest"},
    ],
)
def test_unapproved_version_or_caller_image_is_rejected(client, extra):
    response = client.post(
        "/api/v1/agents",
        json={"display_name": "Rejected", "employee_label": "Alex", **extra},
        headers={"Idempotency-Key": "bad-version"},
    )
    assert response.status_code == 422
    assert client.get("/api/v1/agents").json() == []


def test_explicit_bundled_version_does_not_float_to_latest(client, session_maker, monkeypatch):
    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog["openclaw"]["2026.9.10"] = "sha256:" + "a" * 64
    monkeypatch.setattr(get_settings(), "runtime_versions", catalog)
    response = client.post(
        "/api/v1/agents",
        json={"display_name": "Pinned", "employee_label": "Alex", "runtime_version": "2026.9.6"},
        headers={"Idempotency-Key": "pinned-version"},
    )
    assert response.status_code == 202
    with session_maker() as session:
        agent = session.get(Agent, UUID(response.json()["agent_id"]))
        assert agent.runtime_release == "openclaw-2026.9.6"
        assert agent.runtime_image == "talos-openclaw-native:local"


def test_upgrade_backfills_only_existing_native_image_pins(database_engine, session_maker):
    image = "sha256:" + "a" * 64
    with session_maker.begin() as session:
        agents = [
            Agent(display_name=mode, employee_label="Alex", runtime_mode=mode)
            for mode in ("native", "managed", "native")
        ]
        session.add_all(agents)
        session.flush()
        for agent in agents[:2]:
            incarnation = WorkloadIncarnation(
                agent_id=agent.id,
                generation=1,
                runtime_release=agent.runtime_release,
                image_digest=image,
            )
            session.add(incarnation)
            session.flush()
            agent.current_incarnation_id = incarnation.id
        ids = [agent.id for agent in agents]
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    with database_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0024")
        command.upgrade(config, "head")
    with session_maker() as session:
        assert [session.get(Agent, agent_id).runtime_image for agent_id in ids] == [
            image,
            None,
            None,
        ]
