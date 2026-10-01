"""Regression checks for the durable setup stop boundary and runtime adoption."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from docker.errors import NotFound
from sqlalchemy import func, select

from backend.app.applications import application_fingerprint
from backend.app.models import Agent, Operation, WorkloadIncarnation
from gateway.identity import validate_token

# The existing harness supplies a real PostgreSQL schema and owned fake Docker resources.
# ruff: noqa: F401, F811
from tests.integration.test_lifecycle import (
    ProcessDied,
    client,
    database_engine,
    role_agent,
    session_maker,
    worker,
)
from worker.lifecycle import Worker, read_credentials
from worker.runtime import RuntimeReadinessError

pytestmark = pytest.mark.integration


@pytest.fixture
def running_role_agent(client, worker, session_maker, role_agent):
    agent_id, role, writer = role_agent
    path = f"/api/v1/agents/{agent_id}"
    started = client.post(path + "/start", headers={"Idempotency-Key": "initial-start"})
    assert started.status_code == 202
    assert worker.process_one()
    assert client.get("/api/v1/operations/" + started.json()["id"]).json()["status"] == "succeeded"
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        application = deepcopy(agent.applied_application)
    token = read_credentials(incarnation)["agent_token"]
    assert validate_token(token)
    writer.reset_mock()
    return SimpleNamespace(
        id=UUID(agent_id),
        path=path,
        role=role,
        writer=writer,
        incarnation=incarnation,
        container=worker.owned_container(incarnation),
        application=application,
        token=token,
    )


def apply_role(client, running, key):
    response = client.post(running.path + "/apply-role", headers={"Idempotency-Key": key})
    assert response.status_code == 202
    return UUID(response.json()["id"])



def require_prepared_volume(monkeypatch, writer):
    """Match the adapter's preparation timing and dependency on both volumes."""
    def prepare(client, state, incarnation, *args, **kwargs):
        client.volumes.get(state)
        client.volumes.get(incarnation.config_volume)
        return {"config_volume": incarnation.config_volume}

    preparation = Mock(side_effect=prepare)

    def apply(*args, prepared=None, **kwargs):
        if prepared is None:
            preparation(*args, **kwargs)

    monkeypatch.setattr("worker.lifecycle.prepare_setup", preparation)
    writer.side_effect = apply
    return preparation


def test_preflight_failure_preserves_running_identity_and_applied_snapshot(
    client, worker, session_maker, running_role_agent, monkeypatch
):
    running = running_role_agent
    client.put(
        "/api/v1/roles/" + running.role["id"],
        json={"name": "Sales", "capabilities": ["web_research"]},
    )
    prepare = Mock(side_effect=RuntimeReadinessError("Unsupported setup architecture"))
    monkeypatch.setattr("worker.lifecycle.prepare_setup", prepare)
    creations = worker.client.containers.creations
    operation_id = apply_role(client, running, "bad-preflight")
    assert worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        agent = session.get(Agent, running.id)
        incarnation = session.get(WorkloadIncarnation, running.incarnation.id)
        assert operation.status == "failed" and operation.step == "apply_role"
        assert operation.next_retry_at is None
        assert agent.desired_state == "running" and agent.observed_state == "ready"
        assert agent.current_incarnation_id == incarnation.id == running.incarnation.id
        assert incarnation.revoked_at is None
        assert agent.applied_application == running.application
        assert agent.selected_application != agent.applied_application
        assert session.scalar(select(func.count()).select_from(WorkloadIncarnation)) == 1
    assert running.container.status == "running" and validate_token(running.token)
    assert worker.client.containers.creations == creations
    prepare.assert_called_once()
    running.writer.assert_not_called()


def test_application_error_after_stop_boundary_stays_stopped_across_retry(
    client, worker, session_maker, running_role_agent, monkeypatch
):
    running = running_role_agent
    running.writer.side_effect = RuntimeReadinessError("Native setup validation failed")
    operation_id = apply_role(client, running, "bad-after-stop")
    assert worker.process_one()
    with session_maker.begin() as session:
        operation = session.get(Operation, operation_id)
        agent = session.get(Agent, running.id)
        assert operation.status == "retry_wait" and operation.step == "applying_setup"
        assert agent.observed_state == "stopped"
        assert agent.applied_application == running.application
        assert session.get(WorkloadIncarnation, running.incarnation.id).revoked_at is not None
        operation.next_retry_at = None
    assert running.container.status == "exited" and not validate_token(running.token)
    assert not [
        c
        for c in worker.client.containers.items.values()
        if "io.talos.incarnation" in c.labels and c.status == "running"
    ]
    # A subsequent preparation error must not erase the persisted stop boundary.
    preparation = require_prepared_volume(monkeypatch, running.writer)
    preparation.side_effect = RuntimeReadinessError("Partial installation needs recovery")
    restarted = Worker(sessions=session_maker, client=worker.client)
    monkeypatch.setattr(restarted, "ensure_network", worker.ensure_network)
    monkeypatch.setattr(restarted, "ui_proxy", worker.ui_proxy)
    assert restarted.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        assert operation.status == "retry_wait" and operation.step == "applying_setup"
        agent = session.get(Agent, running.id)
        assert agent.observed_state == "stopped"
        assert agent.applied_application == running.application
    preparation.assert_called_once()
    assert running.writer.call_count == 2


def test_persisted_stop_boundary_survives_crash_before_first_docker_mutation(
    client, worker, session_maker, running_role_agent, monkeypatch
):
    running = running_role_agent
    operation_id = apply_role(client, running, "crash-at-stop-boundary")
    with monkeypatch.context() as patch:
        patch.setattr(worker, "ensure_incarnation", Mock(side_effect=ProcessDied()))
        with pytest.raises(ProcessDied):
            worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        assert operation.status == "running" and operation.step == "applying_setup"
    assert running.container.status == "running"
    preparation = require_prepared_volume(monkeypatch, running.writer)
    preparation.side_effect = RuntimeReadinessError("Setup recovery failed")
    restarted = Worker(sessions=session_maker, client=worker.client)
    monkeypatch.setattr(restarted, "ensure_network", worker.ensure_network)
    monkeypatch.setattr(restarted, "ui_proxy", worker.ui_proxy)
    assert restarted.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        agent = session.get(Agent, running.id)
        assert operation.status == "retry_wait" and operation.step == "applying_setup"
        assert agent.observed_state == "stopped"
        assert agent.applied_application == running.application
    assert running.container.status == "exited" and not validate_token(running.token)
    preparation.assert_called_once()
    running.writer.assert_called_once()


@pytest.mark.parametrize("crash_point", ["retired_config", "first_allocation", "next_allocation"])
def test_retry_prepares_only_after_successor_config_volume_exists(
    client, worker, session_maker, role_agent, monkeypatch, crash_point
):
    agent_id, _, writer = role_agent
    path = f"/api/v1/agents/{agent_id}"
    previous = None
    if crash_point != "first_allocation":
        started = client.post(path + "/start", headers={"Idempotency-Key": "before-crash"})
        assert started.status_code == 202
        assert worker.process_one()
        with session_maker() as session:
            agent = session.get(Agent, UUID(agent_id))
            previous = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        writer.reset_mock()
    preparation = require_prepared_volume(monkeypatch, writer)
    response = client.post(path + "/apply-role", headers={"Idempotency-Key": "volume-crash"})
    assert response.status_code == 202
    operation_id = UUID(response.json()["id"])
    with monkeypatch.context() as patch:
        if crash_point == "retired_config":
            stop = worker.stop_incarnation

            def stop_then_crash(incarnation, remove=False):
                stop(incarnation, remove=remove)
                if remove and incarnation.id == previous.id:
                    raise ProcessDied()

            patch.setattr(worker, "stop_incarnation", stop_then_crash)
        else:
            # ensure_incarnation has committed, but prepare_volumes has not run.
            patch.setattr(worker, "ensure_credentials", Mock(side_effect=ProcessDied()))
        with pytest.raises(ProcessDied):
            worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        agent = session.get(Agent, UUID(agent_id))
        interrupted = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        selected = deepcopy(agent.selected_application)
        assert operation.status == "running" and operation.step == "applying_setup"
        if previous:
            assert session.get(WorkloadIncarnation, previous.id).revoked_at is not None
        if crash_point == "retired_config":
            assert interrupted.id == previous.id
        else:
            assert interrupted.generation == operation.target_revision
    with pytest.raises(NotFound):
        worker.client.volumes.get(interrupted.config_volume)
    writer.assert_not_called()

    restarted = Worker(sessions=session_maker, client=worker.client)
    monkeypatch.setattr(restarted, "ensure_network", worker.ensure_network)
    monkeypatch.setattr(restarted, "ui_proxy", worker.ui_proxy)
    assert restarted.process_one()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        agent = session.get(Agent, UUID(agent_id))
        successor = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        assert operation.status == "succeeded"
        assert successor.generation == operation.target_revision
        assert successor.revoked_at is None
        assert agent.applied_application == agent.selected_application == selected
        assert agent.observed_state == ("ready" if previous else "stopped")
        assert session.scalar(select(func.count()).select_from(WorkloadIncarnation)) == (
            2 if previous else 1
        )
        if crash_point != "retired_config":
            assert successor.id == interrupted.id
        if previous:
            assert successor.id != previous.id
    writer.assert_called_once()
    assert writer.call_args.kwargs["prepared"] is None
    assert preparation.call_count == (2 if previous else 1)
    assert preparation.call_args.args[2].config_volume == successor.config_volume



def test_new_apply_recovers_revoked_incarnation_without_a_config_volume(
    client, worker, session_maker, role_agent, monkeypatch
):
    agent_id, _, writer = role_agent
    path = f"/api/v1/agents/{agent_id}"
    preparation = require_prepared_volume(monkeypatch, writer)
    response = client.post(path + "/apply-role", headers={"Idempotency-Key": "exhaust-identity"})
    assert response.status_code == 202
    operation_id = UUID(response.json()["id"])
    with session_maker.begin() as session:
        # Exercise the final bounded attempt without waiting through backoff.
        session.get(Operation, operation_id).attempts = 4
    with monkeypatch.context() as patch:
        patch.setattr(
            worker,
            "ensure_credentials",
            Mock(side_effect=RuntimeReadinessError("Identity publication failed")),
        )
        assert worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        revoked = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        selected = deepcopy(agent.selected_application)
        assert session.get(Operation, operation_id).status == "failed"
        assert revoked.revoked_at is not None
        assert agent.observed_state == agent.desired_state == "stopped"
        assert agent.applied_application is None
    with pytest.raises(NotFound):
        worker.client.volumes.get(revoked.config_volume)
    preparation.assert_not_called()
    writer.assert_not_called()

    response = client.post(path + "/apply-role", headers={"Idempotency-Key": "new-apply"})
    assert response.status_code == 202
    assert worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, UUID(response.json()["id"]))
        agent = session.get(Agent, UUID(agent_id))
        successor = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        assert operation.status == "succeeded"
        assert successor.id != revoked.id and successor.revoked_at is None
        assert successor.generation == operation.target_revision
        assert agent.applied_application == agent.selected_application == selected
        assert agent.observed_state == agent.desired_state == "stopped"
        assert session.scalar(select(func.count()).select_from(WorkloadIncarnation)) == 2
    writer.assert_called_once()
    preparation.assert_called_once()
    assert writer.call_args.kwargs["prepared"] is None
    assert preparation.call_args.args[2].config_volume == successor.config_volume


def test_failed_first_install_can_be_removed_with_an_empty_application(
    client, worker, session_maker, role_agent
):
    agent_id, _, writer = role_agent
    path = f"/api/v1/agents/{agent_id}"
    response = client.post(path + "/apply-role", headers={"Idempotency-Key": "first-install"})
    assert response.status_code == 202
    operation_id = UUID(response.json()["id"])
    with session_maker.begin() as session:
        operation = session.get(Operation, operation_id)
        selected = deepcopy(operation.role_application)
        # Artifact I/O is mocked by the lifecycle harness. This records a failed
        # first setup without relying on the bundle API in a lifecycle-only test.
        selected["setup"] = {"artifact_hash": "a" * 64, "manifest": {"instructions": "Fixture"}}
        selected["fingerprint"] = application_fingerprint(selected)
        operation.role_application = selected
        operation.attempts = 4  # Exercise the final bounded attempt directly.
        session.get(Agent, UUID(agent_id)).selected_application = {
            k: v for k, v in selected.items() if k != "restart"
        }
    writer.side_effect = RuntimeReadinessError("Partial setup installation failed")
    assert worker.process_one()
    with session_maker() as session:
        assert session.get(Operation, operation_id).status == "failed"
        agent = session.get(Agent, UUID(agent_id))
        assert agent.applied_application is None
        assert agent.selected_application["setup"] is not None
        assert agent.observed_state == agent.desired_state == "stopped"
    assert writer.call_args.args[4]["setup"] is not None
    writer.reset_mock(side_effect=True)
    removal = client.post(path + "/apply-role", headers={"Idempotency-Key": "remove-partial"})
    assert removal.status_code == 202
    assert worker.process_one()
    writer.assert_called_once()
    assert writer.call_args.args[4]["setup"] is None
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        assert session.get(Operation, UUID(removal.json()["id"])).status == "succeeded"
        assert agent.applied_application["setup"] is None
        assert agent.selected_application == agent.applied_application
        assert agent.observed_state == agent.desired_state == "stopped"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "error, expected",
    [
        (
            RuntimeReadinessError("Restore edited setup files before Apply", code="edited"),
            "Restore edited setup files before Apply",
        ),
        (RuntimeError("private-provider-details"), "RuntimeError: runtime recovery failed"),
    ],
)
def test_recovery_checks_applied_receipt_and_grandfathers_legacy_agents(
    client, worker, session_maker, running_role_agent, monkeypatch, legacy, error, expected, caplog
):
    running = running_role_agent
    _, network = worker.names(running.id)
    worker.client.networks.create(name=network, labels=worker.labels(running.id), internal=True)
    if legacy:
        with session_maker.begin() as session:
            agent = session.get(Agent, running.id)
            agent.applied_application = {**agent.applied_application, "legacy_receipt": True}
            agent.selected_application = agent.applied_application
    inspect = Mock(side_effect=error)
    monkeypatch.setattr("worker.lifecycle.verify_setup", inspect)
    worker.recover(yield_to_operations=False)
    with session_maker() as session:
        agent = session.get(Agent, running.id)
        assert agent.observed_state == ("ready" if legacy else "degraded")
        assert agent.current_incarnation_id == running.incarnation.id
        assert session.get(WorkloadIncarnation, running.incarnation.id).revoked_at is None
        if not legacy:
            assert agent.last_error == expected
            assert expected in caplog.text
    assert "private-provider-details" not in caplog.text
    assert running.container.status == "running"
    if legacy:
        inspect.assert_not_called()
        # The first successful adapter application retires the migration marker.
        operation_id = apply_role(client, running, "upgrade-legacy-receipt")
        inspect.side_effect = None
        assert worker.process_one()
        with session_maker() as session:
            agent = session.get(Agent, running.id)
            assert session.get(Operation, operation_id).status == "succeeded"
            assert "legacy_receipt" not in agent.applied_application
            assert "legacy_receipt" not in agent.selected_application
    else:
        inspect.assert_called_once()
        assert inspect.call_args.kwargs == {"discover": False}
        assert inspect.call_args.args[4] == running.application
        assert not validate_token(running.token)
        inspect.side_effect = None
        worker.recover(yield_to_operations=False)
        with session_maker() as session:
            agent = session.get(Agent, running.id)
            assert agent.observed_state == "ready" and agent.last_error is None
        assert validate_token(running.token)


def test_migrated_legacy_container_recreation_recovers_crash_after_create(
    client, worker, session_maker, running_role_agent, monkeypatch
):
    running = running_role_agent
    # Reconstruct the durable pre-0014 state after Docker creation but before
    # database completion: captured old role keys and no application receipt.
    legacy = {
        "role": {
            key: value
            for key, value in running.application["role"].items()
            if key in {"id", "name", "revision", "capabilities"}
        },
        "permissions": running.application["permissions"],
        "employee_id": running.application["employee_id"],
        "restart": True,
        "legacy_receipt": True,
    }
    with session_maker.begin() as session:
        operation = session.scalar(
            select(Operation).where(
                Operation.agent_id == running.id,
                Operation.action == "start",
                Operation.target_revision == running.incarnation.generation,
            )
        )
        operation.status, operation.step = "running", "start"
        operation.role_application = legacy
        operation_id = operation.id
        agent = session.get(Agent, running.id)
        agent.applied_application = None
        agent.applied_role = None
        agent.selected_application = {
            key: value for key, value in legacy.items() if key != "restart"
        }
        agent.observed_state = "starting"

    def require_new_receipt(*args, **kwargs):
        if not running.writer.call_count:
            raise RuntimeReadinessError("Legacy container has no application receipt")

    monkeypatch.setattr("worker.lifecycle.verify_setup", Mock(side_effect=require_new_receipt))
    worker.client.containers.crash_after_create = True
    with pytest.raises(ProcessDied):
        worker.process_one()
    assert running.container.status == "exited"
    running.writer.assert_called_once()
    with session_maker() as session:
        operation = session.get(Operation, operation_id)
        incarnation = session.get(WorkloadIncarnation, running.incarnation.id)
        assert operation.status == "running" and operation.step == "applying_setup"
        assert operation.role_application["setup"] is None
        assert operation.role_application["connector_grants"] == []
        assert operation.role_application["role"]["setup_revision_id"] is None
        assert operation.role_application["fingerprint"] == application_fingerprint(
            operation.role_application
        )
        # A recorded old ID would make adoption of the replacement fail ownership.
        assert incarnation.container_id is None
    restarted = Worker(sessions=session_maker, client=worker.client)
    # Match the original native harness while exercising a fresh worker instance.
    monkeypatch.setattr(restarted, "ensure_network", worker.ensure_network)
    monkeypatch.setattr(restarted, "ui_proxy", worker.ui_proxy)
    assert restarted.process_one()
    with session_maker() as session:
        agent = session.get(Agent, running.id)
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        assert session.get(Operation, operation_id).status == "succeeded"
        assert agent.observed_state == "ready"
        assert incarnation.id == running.incarnation.id
        assert incarnation.container_id != running.container.id
        assert "legacy_receipt" not in agent.applied_application
        assert agent.selected_application == agent.applied_application
    assert validate_token(running.token)
    assert (
        len(
            [
                c
                for c in worker.client.containers.items.values()
                if "io.talos.incarnation" in c.labels and c.status == "running"
            ]
        )
        == 1
    )
