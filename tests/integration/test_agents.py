import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.agents import get_db
from backend.app.main import create_app
from backend.app.models import Agent, Operation

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
        connection.execute(text("TRUNCATE agents, workload_incarnations, operations CASCADE"))
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
    with TestClient(api_for(session_maker)) as client:
        yield client


def create(client, key="create-one", name="Sales helper"):
    return client.post(
        "/api/v1/agents",
        json={"display_name": name, "employee_label": "Alex"},
        headers={"Idempotency-Key": key},
    )


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
    with TestClient(api_for(session_maker)) as restarted:
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
