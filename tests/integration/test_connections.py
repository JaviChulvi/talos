import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app import connections
from backend.app.connections import (
    Connection,
    ConnectionBindingError,
    ConnectionVersion,
    load_bound_secrets,
    resolve_bindings,
)
from backend.app.db import get_db
from backend.app.main import create_app
from backend.app.models import Agent, Employee, Operation, Role, Setup, SetupRevision

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = {"connection_slots": [{"id": "crm", "label": "CRM", "fields": ["token"]}]}


@pytest.fixture(scope="module")
def database_engine():
    url = os.environ.get("TALOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TALOS_TEST_DATABASE_URL to run PostgreSQL integration checks")
    schema = f"talos_connection_test_{uuid4().hex}"
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
    with database_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE connections, connection_versions, agents, operations, "
                "employees, roles CASCADE"
            )
        )
    return sessionmaker(database_engine, expire_on_commit=False)


@pytest.fixture
def client(session_maker, tmp_path, monkeypatch):
    monkeypatch.setattr(
        connections, "get_settings", lambda: SimpleNamespace(connection_secrets_dir=tmp_path)
    )
    app = create_app()

    def database():
        with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client


def create(client, name="CRM", fields=None, token="test-token"):
    response = client.post(
        "/api/v1/connections", json={"name": name, "fields": fields or ["token"]}
    )
    assert response.status_code == 201, response.text
    connection = response.json()
    if token is not None:
        response = client.put(
            f"/api/v1/connections/{connection['id']}/credentials", json={"values": {"token": token}}
        )
        assert response.status_code == 200, response.text
        connection = response.json()
    return connection


def test_metadata_rotation_is_write_only_and_keeps_old_version(client, session_maker):
    first = create(client, token="sensitive-first-token")
    identifier = first["id"]
    path = f"/api/v1/connections/{identifier}"
    with session_maker.begin() as session:
        selected = resolve_bindings(session, MANIFEST, {"crm": identifier}, {})
    assert load_bound_secrets(selected) == {"crm": {"token": "sensitive-first-token"}}
    response = client.put(path + "/credentials", json={"values": {"token": "sensitive-next-token"}})
    assert response.status_code == 200
    assert response.json()["current_version"] == 2
    assert response.json()["current_version_id"] != first["current_version_id"]
    assert load_bound_secrets(selected) == {"crm": {"token": "sensitive-first-token"}}
    assert "sensitive" not in response.text
    assert "sensitive" not in client.get(path).text
    assert "sensitive" not in client.get("/api/v1/connections").text
    with session_maker.begin() as session:
        current = resolve_bindings(session, MANIFEST, {"crm": identifier}, {})
        versions = session.scalars(select(ConnectionVersion)).all()
        assert len(versions) == 2
        assert all("sensitive" not in str(vars(version)) for version in versions)
    assert load_bound_secrets(current) == {"crm": {"token": "sensitive-next-token"}}
    assert client.put(path, json={"name": "CRM renamed", "fields": ["token"]}).status_code == 200
    assert client.put(path, json={"name": "CRM", "fields": ["different"]}).status_code == 409


def test_defaults_overrides_and_unusable_override_never_falls_back(client, session_maker):
    default = create(client, name="Default", token="default-token")
    override = create(client, name="Employee", token="employee-token")
    empty = create(client, name="Empty", token=None)
    with session_maker.begin() as session:
        selected = resolve_bindings(
            session, MANIFEST, {"crm": default["id"]}, {"crm": override["id"]}
        )
        assert load_bound_secrets(selected)["crm"]["token"] == "employee-token"
        for bad in (str(uuid4()), "invalid", None, empty["id"]):
            with pytest.raises(ConnectionBindingError):
                resolve_bindings(session, MANIFEST, {"crm": default["id"]}, {"crm": bad})
        with pytest.raises(ConnectionBindingError, match="Connection required"):
            resolve_bindings(session, MANIFEST, {}, {})
        with pytest.raises(ConnectionBindingError, match="fields missing"):
            resolve_bindings(
                session,
                {"connection_slots": [{"id": "crm", "fields": ["account", "token"]}]},
                {"crm": default["id"]},
                {},
            )
        assert resolve_bindings(session, {"connection_slots": []}, {}, {}) == {}


def test_deletion_blocks_bindings_snapshots_and_active_operations(client, session_maker):
    connection = create(client)
    identifier = connection["id"]
    path = f"/api/v1/connections/{identifier}"
    with session_maker.begin() as session:
        role = Role(name="Sales", connection_bindings={"crm": identifier})
        session.add(role)
        session.flush()
        role_id = role.id
    assert client.delete(path).status_code == 409
    with session_maker.begin() as session:
        session.get(Role, role_id).connection_bindings = {}
        application = {"connections": {"crm": {"connection_id": identifier}}}
        agent = Agent(display_name="A", employee_label="E", selected_application=application)
        session.add(agent)
        session.flush()
        agent_id = agent.id
    assert client.delete(path).status_code == 409
    with session_maker.begin() as session:
        session.get(Agent, agent_id).selected_application = None
        session.add(
            Operation(
                agent_id=agent_id,
                role_application=application,
                action="apply_role",
                target_revision=1,
                idempotency_scope="test",
                idempotency_key="test",
                request_hash="hash",
            )
        )
    assert client.delete(path).status_code == 409
    with session_maker.begin() as session:
        session.scalar(select(Operation)).status = "succeeded"
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    with session_maker() as session:
        assert session.scalars(select(ConnectionVersion)).all() == []


def test_validation_and_storage_failure_never_disclose_credentials(
    client, session_maker, monkeypatch
):
    connection = create(client, token=None)
    path = f"/api/v1/connections/{connection['id']}/credentials"
    for value in ({"token": "sensitive\ncredential"}, {"other": "sensitive-token"}, {"token": 123}):
        response = client.put(path, json={"values": value})
        assert response.status_code == 422
        assert "sensitive" not in response.text
    monkeypatch.setattr(
        connections,
        "write_credential_version",
        lambda *args: (_ for _ in ()).throw(OSError("sensitive-token")),
    )
    response = client.put(path, json={"values": {"token": "sensitive-token"}})
    assert response.status_code == 503 and "sensitive" not in response.text
    with session_maker() as session:
        assert session.get(Connection, UUID(connection["id"])).current_version == 0
        assert session.scalars(select(ConnectionVersion)).all() == []


def test_concurrent_rotations_create_distinct_immutable_versions(client, session_maker):
    connection = create(client, token=None)
    barrier = Barrier(3)

    def rotate(index):
        barrier.wait(timeout=10)
        return client.put(
            f"/api/v1/connections/{connection['id']}/credentials",
            json={"values": {"token": f"token-{index}"}},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(rotate, range(3)))
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert sorted(response.json()["current_version"] for response in responses) == [1, 2, 3]
    with session_maker() as session:
        assert len(session.scalars(select(ConnectionVersion)).all()) == 3


def test_agent_restart_preserves_credential_version_until_explicit_apply(client, session_maker):
    from backend.app.applications import desired_application, normalize_application
    from backend.app.setups import empty_manifest

    connection = create(client, token="sensitive-original")
    with session_maker.begin() as session:
        setup = Setup(name="Sales")
        session.add(setup)
        session.flush()
        manifest = {
            **empty_manifest(),
            "targets": [
                {
                    "runtime_kind": "openclaw",
                    "runtime_release": "openclaw-2026.9.6",
                    "architecture": "arm64",
                }
            ],
            "connectors": [
                {
                    "id": "crm",
                    "enabled": True,
                    "tools": ["lookup"],
                    "headers": {"Authorization": {"slot": "crm", "field": "token"}},
                },
                {
                    "id": "denied",
                    "enabled": True,
                    "tools": ["write"],
                    "headers": {"Authorization": {"slot": "unbound", "field": "token"}},
                },
            ],
            "connection_slots": [
                {"id": "crm", "label": "CRM", "fields": ["token"]},
                {"id": "unbound", "label": "Denied connector", "fields": ["token"]},
            ],
        }
        revision = SetupRevision(
            setup_id=setup.id, version=1, manifest=manifest, artifact_hash="a" * 64
        )
        session.add(revision)
        session.flush()
        role = Role(
            name="Sales",
            setup_revision_id=revision.id,
            connector_grants=["crm"],
            connection_bindings={"crm": connection["id"]},
        )
        session.add(role)
        session.flush()
        employee = Employee(name="Alex", role_id=role.id)
        session.add(employee)
        session.flush()
        agent = Agent(
            display_name="Sales helper",
            employee_label="Alex",
            employee_id=employee.id,
            runtime_mode="native",
            observed_state="stopped",
        )
        session.add(agent)
        session.flush()
        agent_id = agent.id
        selected = desired_application(session, agent)
        # The denied connector does not demand the missing unbound connection.
        assert list(selected["connections"]) == ["crm"]
        agent.selected_application = selected
        agent.applied_application = selected
    path = f"/api/v1/agents/{agent_id}"
    rotated = client.put(
        f"/api/v1/connections/{connection['id']}/credentials",
        json={"values": {"token": "sensitive-rotated"}},
    )
    assert rotated.status_code == 200
    after_rotation = client.get(path)
    assert after_rotation.status_code == 200
    assert "sensitive" not in after_rotation.text
    assert after_rotation.json()["setup_status"] == "update_available"
    assert after_rotation.json()["selected_application"] == selected
    preview = client.post(path + "/setup-preview")
    assert preview.status_code == 200 and "sensitive" not in preview.text
    assert "Account connections changed" in preview.json()["changes"]
    assert (
        preview.json()["application"]["connections"]["crm"]["version_id"]
        == rotated.json()["current_version_id"]
    )
    started = client.post(path + "/start", headers={"Idempotency-Key": "restart-selected"})
    assert started.status_code == 202, started.text
    with session_maker.begin() as session:
        operation = session.get(Operation, UUID(started.json()["id"]))
        assert normalize_application(operation.role_application, "openclaw") == selected
        assert load_bound_secrets(operation.role_application["connections"]) == {
            "crm": {"token": "sensitive-original"}
        }
        operation.status = "succeeded"
        session.get(Agent, agent_id).observed_state = "ready"
    applied = client.post(path + "/apply-role", headers={"Idempotency-Key": "choose-rotation"})
    assert applied.status_code == 202, applied.text
    with session_maker() as session:
        operation = session.get(Operation, UUID(applied.json()["id"]))
        latest = session.get(Agent, agent_id).selected_application
        assert latest["connections"]["crm"]["version_id"] == rotated.json()["current_version_id"]
        assert operation.role_application["connections"] == latest["connections"]
        assert load_bound_secrets(latest["connections"]) == {"crm": {"token": "sensitive-rotated"}}
        assert session.get(Agent, agent_id).applied_application == selected


def test_deleted_agent_audit_snapshots_do_not_retain_connection_secrets(client, session_maker):
    from worker.lifecycle import Worker

    connection = create(client)
    path = f"/api/v1/connections/{connection['id']}"
    with session_maker.begin() as session:
        application = {
            "connections": resolve_bindings(session, MANIFEST, {"crm": connection["id"]}, {})
        }
        agent = Agent(
            display_name="Retired helper",
            employee_label="Alex",
            observed_state="stopped",
            selected_application=application,
            applied_application=application,
        )
        session.add(agent)
        session.flush()
        agent_id = agent.id
    assert client.delete(path).status_code == 409
    deletion = client.delete(
        f"/api/v1/agents/{agent_id}", headers={"Idempotency-Key": "retire-agent"}
    )
    assert deletion.status_code == 202
    with session_maker() as session:
        assert session.get(Agent, agent_id).desired_state == "deleted"
        assert session.get(Agent, agent_id).observed_state == "stopped"
    # Admission alone cannot release the selected credential files.
    assert client.delete(path).status_code == 409
    with session_maker.begin() as session:
        session.get(Agent, agent_id).observed_state = "deleting"
        operation = session.get(Operation, UUID(deletion.json()["id"]))
        operation.status = "running"
    assert client.delete(path).status_code == 409
    # Use the real completion path, which preserves snapshots as audit history.
    Worker(sessions=session_maker, client=object()).complete(operation, "deleted")
    with session_maker() as session:
        retired = session.get(Agent, agent_id)
        assert retired.observed_state == "deleted"
        assert retired.selected_application == retired.applied_application == application
        assert session.get(Operation, operation.id).status == "succeeded"
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    with session_maker() as session:
        assert session.scalars(select(ConnectionVersion)).all() == []
