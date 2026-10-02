"""Maintenance admission is durable and serialized against every new writer."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.installation import (
    ensure_writable,
    enter_maintenance,
    installation_status,
    leave_maintenance,
)
from backend.app.models import Agent, InferenceCall, InstallationState, Run, WorkloadIncarnation
from gateway.identity import AdmissionDenied, admit_inference, validate_token

# ruff: noqa: F401, F811
from tests.integration.test_lifecycle import (
    client,
    database_engine,
    provision,
    role_agent,
    session_maker,
    worker,
)
from worker.lifecycle import credential_path, read_credentials

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def reset_gate(session_maker):
    with session_maker.begin() as session:
        state = session.get(InstallationState, 1)
        state.maintenance_operation_id = None
        state.maintenance_kind = None
        state.maintenance_started_at = None
    yield
    with session_maker.begin() as session:
        state = session.get(InstallationState, 1)
        state.maintenance_operation_id = None
        state.maintenance_kind = None
        state.maintenance_started_at = None


def test_gate_survives_sessions_blocks_mutations_and_preserves_reads(client, session_maker):
    with session_maker.begin() as session:
        enter_maintenance(session, "backup-one", "backup")
    # A replacement process sees the same durable fence and can resume it.
    with session_maker.begin() as session:
        enter_maintenance(session, "backup-one", "backup")
        with pytest.raises(HTTPException) as error:
            leave_maintenance(session, "different-operation")
        assert error.value.status_code == 409
    status = client.get("/api/v1/installation")
    assert status.status_code == 200
    assert status.json()["maintenance"]["active"] is True
    assert client.get("/api/v1/roles").status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 200
    response = client.post("/api/v1/roles", json={"name": "Blocked", "capabilities": []})
    assert response.status_code == 503
    with session_maker.begin() as session:
        leave_maintenance(session, "backup-one")
    assert (
        client.post("/api/v1/roles", json={"name": "Allowed", "capabilities": []}).status_code
        == 201
    )


def test_entry_waits_for_admitted_writer_then_rechecks_quiescence(session_maker):
    entered = Event()

    def pause():
        with session_maker.begin() as session:
            entered.set()
            with pytest.raises(HTTPException) as error:
                enter_maintenance(session, "racing-backup", "backup")
            return error.value.status_code

    with ThreadPoolExecutor(max_workers=1) as executor:
        with session_maker.begin() as writer:
            ensure_writable(writer)
            future = executor.submit(pause)
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            writer.add(
                Agent(display_name="Concurrent", employee_label="Test", desired_state="running")
            )
        assert future.result(timeout=5) == 409
    with session_maker() as session:
        assert not installation_status(session)["maintenance"]["active"]


def test_unknown_work_and_running_agents_prevent_maintenance(client, worker, session_maker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        with pytest.raises(HTTPException, match="Stop every agent"):
            enter_maintenance(session, "backup", "backup")
        agent = session.get(Agent, UUID(agent_id))
        agent.desired_state = agent.observed_state = "stopped"
        session.add(
            Run(
                agent_id=agent.id,
                incarnation_id=agent.current_incarnation_id,
                message="Unknown",
                status="unknown",
                idempotency_key="unknown",
                request_hash="a" * 64,
            )
        )
    with session_maker.begin() as session:
        with pytest.raises(HTTPException, match="uncertain work"):
            enter_maintenance(session, "backup", "backup")


def test_new_gateway_work_is_fenced_without_cutting_existing_validation(
    client,
    worker,
    session_maker,
    monkeypatch,
):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        incarnation = session.scalar(select(WorkloadIncarnation))
        token = read_credentials(incarnation)["agent_token"]
        # Simulate a persisted fence after admission, bypassing the normal idle
        # precondition solely to verify that stream validation remains untouched.
        session.get(InstallationState, 1).maintenance_operation_id = "pause"
    assert validate_token(token)
    with pytest.raises(AdmissionDenied) as error:
        admit_inference(token)
    assert error.value.code == "maintenance_active"
    assert worker.process_one() is False


@pytest.mark.parametrize(
    "blocker", ["unknown", "revoked", "expired", "stopped", "image", "credentials", "inference"]
)
def test_recovery_does_not_restart_unsafe_incarnations(client, worker, session_maker, blocker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        if blocker == "unknown":
            session.add(
                Run(
                    agent_id=agent.id,
                    incarnation_id=incarnation.id,
                    message="Uncertain",
                    status="unknown",
                    idempotency_key=str(uuid4()),
                    request_hash="b" * 64,
                )
            )
        elif blocker == "revoked":
            incarnation.revoked_at = datetime.now(UTC)
        elif blocker == "expired":
            incarnation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif blocker == "inference":
            session.add(
                InferenceCall(
                    agent_id=agent.id,
                    incarnation_id=incarnation.id,
                    model="synthetic/model",
                )
            )
        elif blocker == "credentials":
            credential_path(incarnation.id).unlink()
        elif blocker == "stopped":
            agent.desired_state = "stopped"
    container = worker.client.containers.get(incarnation.container_name)
    container.stop()
    if blocker == "image":
        container.attrs["Config"]["Image"] = "foreign-image"
    worker.recover()
    assert container.status == "exited"
    with session_maker() as session:
        assert session.get(Agent, UUID(agent_id)).current_incarnation_id == incarnation.id
        if blocker == "unknown":
            assert session.scalar(select(Run.status)) == "unknown"


def test_reboot_recovery_requires_current_permissions(
    client,
    worker,
    session_maker,
    role_agent,
):
    agent_id, role, _ = role_agent
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/start",
            headers={"Idempotency-Key": "native-start"},
        ).status_code
        == 202
    )
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
    # The fake native fixture omits its network because start doesn't need one.
    worker.client.networks.create(worker.names(UUID(agent_id))[1])
    runtime = worker.client.containers.get(incarnation.container_name)
    runtime.stop()
    assert (
        client.put(
            f"/api/v1/roles/{role['id']}",
            json={"name": "Sales", "capabilities": ["web_research"]},
        ).status_code
        == 200
    )
    worker.recover()
    assert runtime.status == "exited"
    with session_maker() as session:
        assert "Permissions or credentials changed" in session.get(Agent, UUID(agent_id)).last_error


def test_reboot_can_resume_known_unsent_queue_without_new_identity(client, worker, session_maker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        session.add(
            Run(
                agent_id=agent.id,
                incarnation_id=incarnation.id,
                message="Not sent yet",
                status="queued",
                idempotency_key="unsent",
                request_hash="c" * 64,
            )
        )
    runtime = worker.client.containers.get(incarnation.container_name)
    runtime.stop()
    worker.recover()
    assert runtime.status == "running"
    with session_maker() as session:
        assert session.get(Agent, UUID(agent_id)).current_incarnation_id == incarnation.id
        assert session.scalar(select(Run.status)) == "queued"
