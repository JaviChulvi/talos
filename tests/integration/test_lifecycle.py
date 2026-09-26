from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import test_agents
from docker.errors import NotFound
from sqlalchemy import select

from backend.app.models import Agent, Operation, WorkloadIncarnation
from gateway.identity import validate_token
from worker.lifecycle import Worker, credential_path, read_credentials, worker_lock
from worker.runtime import IMAGE

pytestmark = pytest.mark.integration
database_engine = test_agents.database_engine
session_maker = test_agents.session_maker
client = test_agents.client
create = test_agents.create


class ProcessDied(BaseException):
    pass


class Resources:
    def __init__(self, kind):
        self.kind, self.items = kind, {}
        self.crash_after_create = False
        self.creations = 0

    def get(self, name):
        for item in self.items.values():
            if name in (item.name, item.id):
                return item
        raise NotFound("Missing test resource")

    def create(self, name, **options):
        assert name not in self.items
        resource = SimpleNamespace(
            id=uuid4().hex, name=name, labels=options.get("labels", {}), status="created"
        )
        resource.attrs = {
            "Labels": resource.labels,
            "Internal": options.get("internal", False),
            "Driver": options.get("driver", "bridge"),
            "Containers": {},
            "Config": {"Image": options.get("image", IMAGE)},
        }
        resource.reload = lambda: None
        resource.remove = lambda **_: self.items.pop(name)
        resource.start = lambda: setattr(resource, "status", "running")
        resource.stop = lambda **_: setattr(resource, "status", "exited")
        resource.connect = lambda container, **_: resource.attrs["Containers"].update(
            {container.id: {}}
        )
        resource.disconnect = lambda container, **_: resource.attrs["Containers"].pop(container.id)
        self.items[name] = resource
        self.creations += 1
        if self.crash_after_create:
            self.crash_after_create = False
            raise ProcessDied()
        return resource

    def list(self, filters):
        return [
            item
            for item in self.items.values()
            if all(
                item.labels.get(key) == value
                for key, value in (label.split("=", 1) for label in filters["label"])
            )
        ]


@pytest.fixture
def worker(session_maker, monkeypatch, tmp_path):
    settings = SimpleNamespace(
        worker_state_dir=tmp_path,
        compose_project="talos-test",
        installation_id="test",
        worker_container="platform-worker",
        readiness_timeout_seconds=10,
    )
    monkeypatch.setattr("worker.lifecycle.get_settings", lambda: settings)
    containers, volumes, networks = (
        Resources("containers"),
        Resources("volumes"),
        Resources("networks"),
    )
    docker = SimpleNamespace(
        containers=containers,
        volumes=volumes,
        networks=networks,
        images=SimpleNamespace(get=lambda _: object()),
    )
    worker = Worker(sessions=session_maker, client=docker)
    for service in ("worker", "gateway"):
        containers.create(name=f"platform-{service}", labels=worker.service_labels(service)).start()

    def prepare(client, state, config, payload, labels):
        for name in (state, config):
            try:
                client.volumes.get(name)
            except NotFound:
                client.volumes.create(name=name, labels=labels)

    monkeypatch.setattr("worker.lifecycle.prepare_volumes", prepare)
    monkeypatch.setattr(Worker, "wait_ready", AsyncMock())
    monkeypatch.setattr("gateway.identity.session_factory", lambda: session_maker)
    return worker


def provision(client, worker):
    operation = create(client).json()
    worker.process_one()
    agent_id = operation["agent_id"]
    response = client.post(f"/api/v1/agents/{agent_id}/start", headers={"Idempotency-Key": "start"})
    assert response.status_code == 202
    return agent_id, response.json()["id"]


def test_crash_after_creation_adopts_exact_container_and_state(client, worker, session_maker):
    agent_id, operation_id = provision(client, worker)
    worker.client.containers.crash_after_create = True
    with pytest.raises(ProcessDied):
        worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, UUID(operation_id))
        assert operation.status == "running"
        incarnation = session.scalar(select(WorkloadIncarnation))
        assert incarnation.container_id is None
        expected_container = worker.client.containers.get(incarnation.container_name).id
        token = read_credentials(incarnation)["agent_token"]
        assert not validate_token(token)  # Not ready yet.
    before_containers, before_volumes = (
        worker.client.containers.creations,
        worker.client.volumes.creations,
    )
    restarted = Worker(sessions=session_maker, client=worker.client)
    restarted.recover()
    assert restarted.process_one()
    assert worker.client.containers.creations == before_containers
    assert worker.client.volumes.creations == before_volumes
    with session_maker() as session:
        assert session.get(Operation, UUID(operation_id)).status == "succeeded"
        assert session.scalar(select(WorkloadIncarnation)).container_id == expected_container
        assert session.get(Agent, UUID(agent_id)).observed_state == "ready"
    assert validate_token(token)
    assert not validate_token("invalid-token-value-does-not-match")


def test_stop_revoke_restart_and_owned_delete(client, worker, session_maker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker() as session:
        original = session.scalar(select(WorkloadIncarnation))
        original_token = read_credentials(original)["agent_token"]
    unrelated = worker.client.volumes.create(name="unrelated", labels={"owner": "someone-else"})
    state_name = worker.names(UUID(agent_id))[0]
    state_id = worker.client.volumes.get(state_name).id
    path = f"/api/v1/agents/{agent_id}"
    assert client.post(path + "/stop", headers={"Idempotency-Key": "stop"}).status_code == 202
    assert not validate_token(original_token)
    worker.process_one()
    assert worker.client.volumes.get(state_name).id == state_id
    assert client.post(path + "/start", headers={"Idempotency-Key": "restart"}).status_code == 202
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        current = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        assert current.id != original.id
        assert session.get(WorkloadIncarnation, original.id).revoked_at is not None
        assert validate_token(read_credentials(current)["agent_token"])
        assert not validate_token(original_token)
    assert worker.client.volumes.get(state_name).id == state_id
    assert client.delete(path, headers={"Idempotency-Key": "delete"}).status_code == 202
    worker.process_one()
    assert client.get(path).status_code == 404
    assert worker.client.volumes.get("unrelated").id == unrelated.id
    assert state_name not in worker.client.volumes.items
    assert not credential_path(original.id).exists()


def test_ownership_conflict_is_terminal_and_preserves_foreign_resource(
    client, worker, session_maker
):
    operation = create(client).json()
    state, _ = worker.names(UUID(operation["agent_id"]))
    foreign = worker.client.volumes.create(name=state, labels={"owner": "someone-else"})
    worker.process_one()
    with session_maker() as session:
        result = session.get(Operation, UUID(operation["id"]))
        assert result.status == "failed"
        assert result.error.startswith("OwnershipError")
    assert worker.client.volumes.get(state).id == foreign.id


def test_expired_and_revoked_identity_fail_closed(client, worker, session_maker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        incarnation = session.scalar(select(WorkloadIncarnation))
        token = read_credentials(incarnation)["agent_token"]
        incarnation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not validate_token(token)
    with session_maker.begin() as session:
        incarnation = session.scalar(select(WorkloadIncarnation))
        incarnation.expires_at = datetime.now(UTC) + timedelta(days=1)
        incarnation.revoked_at = datetime.now(UTC)
    assert not validate_token(token)


def test_second_worker_lock_is_rejected(tmp_path):
    with worker_lock(tmp_path), pytest.raises(RuntimeError, match="Another Talos worker"):
        with worker_lock(tmp_path):
            pytest.fail("A second worker acquired the same lock")


def test_transient_docker_failures_stop_after_bounded_retries(
    client, worker, session_maker, monkeypatch
):
    operation = create(client).json()

    def unavailable(_):
        raise OSError("Docker unavailable")

    monkeypatch.setattr(worker, "ensure_network", unavailable)
    for attempt in range(1, 6):
        assert worker.process_one()
        with session_maker.begin() as session:
            current = session.get(Operation, UUID(operation["id"]))
            assert current.attempts == attempt
            assert current.status == ("failed" if attempt == 5 else "retry_wait")
            current.next_retry_at = None
    assert not worker.process_one()
