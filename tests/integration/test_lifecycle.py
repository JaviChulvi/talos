import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
import test_agents
from docker.errors import NotFound
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_diagnostics import FakeDriver

from backend.app.models import Agent, Operation, Run, WorkloadIncarnation
from gateway.identity import validate_token
from worker.lifecycle import (
    StorageFullError,
    Worker,
    configure_inference,
    credential_path,
    read_credentials,
    worker_lock,
)
from worker.main import run
from worker.runtime import IMAGE

pytestmark = pytest.mark.integration
database_engine = test_agents.database_engine
session_maker = test_agents.session_maker
client = test_agents.client
create = test_agents.create
REAL_WAIT_READY = Worker.wait_ready


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
            "Config": {"Image": options.get("image", IMAGE), "Hostname": resource.id[:12]},
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

    def prepare(client, state, config, payload, labels, *, native=False, runtime_kind="openclaw"):
        for name in (state, config):
            try:
                client.volumes.get(name)
            except NotFound:
                client.volumes.create(name=name, labels=labels)

    monkeypatch.setattr("worker.lifecycle.prepare_volumes", prepare)
    monkeypatch.setattr("worker.lifecycle.release_stopped_gateway_lease", Mock())
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


def test_native_model_operation_retries_without_stopping_or_granting_early(
    client,
    worker,
    session_maker,
    monkeypatch,
):
    from gateway.identity import native_selection

    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        agent.runtime_mode = "native"
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.model_route, incarnation.image_digest = "native", IMAGE
        incarnation_id = incarnation.id
    runtime = worker.owned_container(incarnation)
    runtime.attrs["Config"]["Env"] = ["no_proxy=localhost,talos-gateway"]
    credentials = read_credentials(incarnation)
    assert native_selection(credentials["agent_token"]) is None

    async def catalog():
        return [{"id": "test/model", "name": "Test", "context_length": 32000}]

    monkeypatch.setattr("backend.app.inference.catalog", catalog)
    writer = Mock(side_effect=RuntimeError("synthetic failure"))
    monkeypatch.setattr("worker.lifecycle.apply_native_model", writer)
    response = client.post(
        f"/api/v1/inference/agents/{agent_id}/native",
        json={"model_id": "test/model"},
        headers={"Idempotency-Key": "model-change"},
    )
    assert response.status_code == 202
    operation_id = UUID(response.json()["id"])
    worker.process_one()
    with session_maker.begin() as session:
        operation = session.get(Operation, operation_id)
        assert operation.status == "retry_wait"
        operation.next_retry_at = None
        assert session.get(Agent, UUID(agent_id)).observed_state == "ready"
    assert native_selection(credentials["agent_token"]) is None
    assert runtime.status == "running"
    writer.side_effect = None
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        assert agent.inference_override["model_id"] == "test/model"
        assert agent.current_incarnation_id == incarnation_id and agent.observed_state == "ready"
        assert session.get(Operation, operation_id).status == "succeeded"
    assert writer.call_count == 2
    assert native_selection(credentials["agent_token"])["model_id"] == "test/model"
    assert native_selection("unrecognized-agent-identity") is None
    with session_maker.begin() as session:
        session.get(WorkloadIncarnation, incarnation_id).revoked_at = datetime.now(UTC)
    assert native_selection(credentials["agent_token"]) is None


@pytest.mark.parametrize("stopped", [True, False])
def test_native_reset_clears_persisted_test_session_before_next_turn(
    client, worker, session_maker, monkeypatch, stopped
):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    if stopped:
        response = client.post(
            f"/api/v1/agents/{agent_id}/stop", headers={"Idempotency-Key": "stop"}
        )
        assert response.status_code == 202
        worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        agent.runtime_mode = "native"
        agent.inference_override = {"model_id": "previous/model"}
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.model_route = "native"
        incarnation_id = incarnation.id
    writer = Mock()
    monkeypatch.setattr("worker.lifecycle.apply_native_model", writer)
    connection = AsyncMock()
    monkeypatch.setattr("worker.lifecycle.connect_runtime", connection)
    response = client.post(
        f"/api/v1/inference/agents/{agent_id}/native",
        content="null",
        headers={"Idempotency-Key": "reset-model", "Content-Type": "application/json"},
    )
    assert response.status_code == 202
    worker.process_one()
    with session_maker() as session:
        assert session.get(Operation, UUID(response.json()["id"])).status == "succeeded"
        assert session.get(Agent, UUID(agent_id)).inference_override is None
    writer.assert_called_once()
    connection.assert_not_called()  # Do not mutate the session during an in-flight turn.
    runtime = AsyncMock()
    asyncio.run(
        configure_inference(
            session_maker,
            SimpleNamespace(
                agent_id=UUID(agent_id), incarnation_id=incarnation_id, session_key=None
            ),
            runtime,
        )
    )
    runtime.request.assert_awaited_once_with(
        "sessions.patch", {"key": f"agent:main:talos:{agent_id}", "model": None}
    )


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


@pytest.mark.parametrize("disk_full", [True, False])
def test_exited_runtime_reports_disk_full_without_exposing_logs(
    client, worker, session_maker, monkeypatch, disk_full
):
    agent_id, operation_id = provision(client, worker)
    # Exercise the real readiness path; bootstrap has exited before the first probe.
    monkeypatch.setattr(worker, "wait_ready", REAL_WAIT_READY.__get__(worker))
    original_create = worker.client.containers.create

    def create_runtime(*args, **kwargs):
        container = original_create(*args, **kwargs)
        container.start = lambda: setattr(container, "status", "exited")
        container.logs = lambda **_: (
            b"ENOSPC: cannot create state; private-token-must-not-escape"
            if disk_full
            else b"Unexpected exit: private-token-must-not-escape"
        )
        return container

    monkeypatch.setattr(worker.client.containers, "create", create_runtime)
    assert worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, UUID(operation_id))
        agent = session.get(Agent, UUID(agent_id))
        assert operation.status == ("failed" if disk_full else "retry_wait")
        assert operation.attempts == 1
        assert operation.error == (
            StorageFullError.message
            if disk_full
            else "The runtime exited during startup. "
            "Inspect its native configuration and Docker logs."
        )
        assert agent.last_error == operation.error
        assert "private-token" not in operation.error
        if disk_full:
            assert operation.next_retry_at is None
            assert session.get(WorkloadIncarnation, agent.current_incarnation_id).revoked_at
    if disk_full:
        assert not worker.process_one()


def test_reconciliation_reattaches_gateway_recovers_and_detects_exit(client, worker, session_maker):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker() as session:
        incarnation = session.scalar(select(WorkloadIncarnation))
        token = read_credentials(incarnation)["agent_token"]
    network = worker.client.networks.get(worker.names(UUID(agent_id))[1])
    old_gateway = worker.client.containers.get("platform-gateway")
    network.disconnect(old_gateway)
    old_gateway.remove()
    worker.recover()
    assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "degraded"
    assert not validate_token(token)
    replacement = worker.client.containers.create(
        name="platform-gateway", labels=worker.service_labels("gateway")
    )
    replacement.start()
    worker.recover()
    assert replacement.id in network.attrs["Containers"]
    assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "ready"
    assert validate_token(token)
    runtime = worker.client.containers.get(incarnation.container_name)
    runtime.stop()
    worker.recover()
    worker.recover()
    assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "degraded"
    assert runtime.status == "exited"
    assert not validate_token(token)


@pytest.mark.parametrize("gateway_state", ["missing", "mismatched", "multiple"])
def test_missing_gateway_retries_but_ownership_conflicts_are_terminal(
    client, worker, session_maker, gateway_state
):
    operation = create(client).json()
    gateway = worker.client.containers.get("platform-gateway")
    if gateway_state == "missing":
        gateway.remove()
    elif gateway_state == "mismatched":
        gateway.labels["io.talos.installation"] = "foreign"
    else:
        worker.client.containers.create(
            name="duplicate-gateway", labels=worker.service_labels("gateway")
        ).start()
    worker.process_one()
    with session_maker() as session:
        operation = session.get(Operation, UUID(operation["id"]))
        assert operation.status == ("retry_wait" if gateway_state == "missing" else "failed")


def test_reconciliation_skips_agents_with_active_start(client, worker, session_maker, monkeypatch):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).observed_state = "degraded"
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/start", headers={"Idempotency-Key": "another-start"}
        ).status_code
        == 202
    )
    ensure_network = Mock(side_effect=AssertionError("Active lifecycle must not be probed"))
    monkeypatch.setattr(worker, "ensure_network", ensure_network)
    worker.recover()
    ensure_network.assert_not_called()
    assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "degraded"


def test_reconciliation_yields_to_unrelated_lifecycle_work(client, worker, monkeypatch):
    provision(client, worker)
    worker.process_one()
    operation = create(client, key="unrelated-agent").json()
    probe = AsyncMock(side_effect=AssertionError("Pending work must run before probing"))
    monkeypatch.setattr(worker, "wait_ready", probe)
    worker.recover()
    probe.assert_not_called()
    assert worker.process_one()
    assert client.get(f"/api/v1/operations/{operation['id']}").json()["status"] == "succeeded"


@pytest.mark.parametrize("probe_fails", [False, True])
def test_reconciliation_does_not_overwrite_concurrent_stop(
    client, worker, session_maker, monkeypatch, probe_fails
):
    agent_id, _ = provision(client, worker)
    worker.process_one()

    async def probe(*_, **__):
        assert (
            client.post(
                f"/api/v1/agents/{agent_id}/stop", headers={"Idempotency-Key": "concurrent-stop"}
            ).status_code
            == 202
        )
        if probe_fails:
            raise OSError("Probe lost connection")

    monkeypatch.setattr(worker, "wait_ready", probe)
    worker.recover()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        assert agent.desired_state == "stopped"
        assert agent.revision == 3
        assert agent.observed_state == "ready"
        assert agent.last_error is None


@pytest.mark.parametrize("failure_stage", ["recover", "claim", "execute", "record"])
def test_worker_loop_retries_database_outages_without_replacing_operation(
    client, worker, session_maker, monkeypatch, failure_stage
):
    created = create(client).json()
    operation_id = UUID(created["id"])
    outage = OperationalError("fixture", {}, OSError("Database restarting"))
    failed = False
    fail_transaction = failure_stage == "claim"
    original_begin = session_maker.begin
    original_recover = worker.recover
    original_ensure_network = worker.ensure_network

    @contextmanager
    def begin():
        nonlocal fail_transaction, failed
        if fail_transaction:
            fail_transaction, failed = False, True
            raise outage
        with original_begin() as session:
            yield session

    def recover(**kwargs):
        nonlocal failed
        if failure_stage == "recover" and not failed:
            failed = True
            raise outage
        original_recover(**kwargs)

    def ensure_network(agent_id, **kwargs):
        nonlocal fail_transaction, failed
        if not failed and failure_stage == "execute":
            failed = True
            raise outage
        if not failed and failure_stage == "record":
            fail_transaction = True
            raise OSError("Docker temporarily unavailable")
        return original_ensure_network(agent_id, **kwargs)

    monkeypatch.setattr(session_maker, "begin", begin)
    monkeypatch.setattr(worker, "recover", recover)
    monkeypatch.setattr(worker, "ensure_network", ensure_network)
    worker.client.close = Mock()
    monkeypatch.setattr("worker.main.Worker", lambda: worker)
    monkeypatch.setattr(
        "worker.main.get_settings", lambda: SimpleNamespace(lifecycle_poll_seconds=0.01)
    )
    original_wait_for = asyncio.wait_for
    delays = []

    async def wait_for(awaitable, timeout):
        delays.append(timeout)
        return await original_wait_for(awaitable, min(timeout, 0.01))

    monkeypatch.setattr("worker.main.asyncio.wait_for", wait_for)

    async def scenario():
        loop = asyncio.get_running_loop()
        stop_callbacks = []
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda _, callback: stop_callbacks.append(callback)
        )
        task = asyncio.create_task(run())
        try:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if task.done():
                    task.result()
                with session_maker() as session:
                    operation = session.get(Operation, operation_id)
                    if operation.status == "succeeded":
                        break
            else:
                pytest.fail("Worker did not resume the durable operation")
        finally:
            stop_callbacks[0]()
            await task

    asyncio.run(scenario())
    assert failed
    assert 2 in delays
    worker.client.close.assert_called_once()
    with session_maker() as session:
        operations = session.scalars(select(Operation)).all()
        assert len(operations) == 1
        assert operations[0].id == operation_id
        assert operations[0].status == "succeeded"


def test_replacement_worker_recovers_network_before_queued_diagnostic(
    client, worker, session_maker, monkeypatch
):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    run_id = client.post(
        f"/api/v1/agents/{agent_id}/diagnostic-runs",
        json={"message": "Queued before replacement"},
        headers={"Idempotency-Key": "before-replacement"},
    ).json()["id"]
    # Ordinary periodic recovery yields to this unrelated operation; startup
    # must still attach the replacement worker before dispatching the run.
    assert create(client, key="unrelated-agent").status_code == 202
    network = worker.client.networks.get(worker.names(UUID(agent_id))[1])
    previous = worker.client.containers.get("platform-worker")
    network.disconnect(previous)
    previous.remove()
    replacement = worker.client.containers.create(
        name="platform-worker", labels=worker.service_labels("worker")
    )
    replacement.start()

    class ProfileDriver(FakeDriver):
        async def request(self, method, params):
            if method == "models.list":
                return {"models": [{"id": "default", "provider": "foundation"}]}
            assert method == "sessions.patch"
            assert params["model"] == "foundation/default"
            return {}

    driver = ProfileDriver()

    async def connector(_sessions, _agent_id):
        assert replacement.id in network.attrs["Containers"]
        return driver

    worker.client.close = Mock()
    monkeypatch.setattr("worker.main.Worker", lambda: worker)
    monkeypatch.setattr("worker.main.connect_runtime", connector)
    monkeypatch.setattr(
        "worker.main.get_settings", lambda: SimpleNamespace(lifecycle_poll_seconds=0.01)
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        stop_callbacks = []
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda _, callback: stop_callbacks.append(callback)
        )
        task = asyncio.create_task(run())
        try:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if task.done():
                    task.result()
                with session_maker() as session:
                    if session.get(Run, UUID(run_id)).status == "completed":
                        break
            else:
                pytest.fail("Queued diagnostic did not complete after worker replacement")
        finally:
            stop_callbacks[0]()
            await task

    asyncio.run(scenario())
    assert driver.sent == 1


def test_dashboard_handoff_rewrites_both_addresses_without_changing_agent_state(
    client, worker, session_maker, monkeypatch
):
    import json
    from urllib.parse import parse_qs, urlsplit

    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        agent.runtime_mode = "native"
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.model_route = "native"
        incarnation.image_digest = IMAGE
        revision = agent.revision
        container_name = incarnation.container_name
    container = worker.client.containers.get(container_name)
    container.exec_run = lambda _: SimpleNamespace(
        exit_code=0,
        output=(
            "plugin log\n"
            + json.dumps(
                {
                    "ok": True,
                    "browserBootstrapExpiresAtMs": 9999999999999,
                    "browserUrl": "http://127.0.0.1:18789/#bootstrapToken=one-use-test&gatewayUrl=ws%3A%2F%2F127.0.0.1%3A18789",
                }
            )
        ).encode(),
    )
    proxy = SimpleNamespace(
        status="running",
        attrs={
            "NetworkSettings": {
                "Ports": {"18789/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32123"}]}
            }
        },
    )
    monkeypatch.setattr(worker, "ui_proxy", lambda _, **kwargs: proxy)
    response = client.post(
        f"/api/v1/agents/{agent_id}/dashboard", headers={"Idempotency-Key": "open"}
    )
    assert response.status_code == 202
    worker.process_one()
    operation = client.get("/api/v1/operations/" + response.json()["id"]).json()
    assert operation["status"] == "succeeded"
    target = urlsplit(operation["dashboard_url"])
    assert target.netloc == "127.0.0.1:32123"
    assert parse_qs(target.fragment) == {
        "bootstrapToken": ["one-use-test"],
        "gatewayUrl": ["ws://127.0.0.1:32123"],
    }
    after = client.get(f"/api/v1/agents/{agent_id}").json()
    assert (after["revision"], after["observed_state"]) == (revision, "ready")

    proxy.attrs["NetworkSettings"]["Ports"]["18789/tcp"][0]["HostIp"] = "0.0.0.0"
    rejected = client.post(
        f"/api/v1/agents/{agent_id}/dashboard", headers={"Idempotency-Key": "bad-bind"}
    )
    worker.process_one()
    assert client.get("/api/v1/operations/" + rejected.json()["id"]).json()["dashboard_url"] is None
    assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "ready"
    with session_maker() as session:
        assert session.get(WorkloadIncarnation, incarnation.id).revoked_at is None


@pytest.fixture
def role_agent(client, worker, session_maker, monkeypatch):
    role = client.post("/api/v1/roles", json={"name": "Sales"}).json()
    employee = client.post("/api/v1/employees", json={"name": "Alex", "role_id": role["id"]}).json()
    created = client.post(
        "/api/v1/agents",
        json={
            "display_name": "Alex helper",
            "employee_id": employee["id"],
        },
        headers={"Idempotency-Key": "role-agent"},
    ).json()
    monkeypatch.setattr(worker, "ensure_network", lambda *a, **kw: None)
    monkeypatch.setattr(worker.client.images, "get", lambda _: SimpleNamespace(id=IMAGE))
    proxy = SimpleNamespace(
        attrs={
            "NetworkSettings": {
                "Ports": {"18789/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}]}
            }
        },
        status="running",
        stop=lambda **kw: None,
        remove=lambda: None,
    )
    monkeypatch.setattr(worker, "ui_proxy", lambda *a, **kw: proxy)
    writer = Mock()
    monkeypatch.setattr("worker.lifecycle.apply_setup", writer)
    monkeypatch.setattr("worker.lifecycle.prepare_setup", Mock())
    monkeypatch.setattr("worker.lifecycle.verify_setup", Mock())
    worker.process_one()
    return created["agent_id"], role, writer


def test_role_application_captures_snapshot_recovers_and_leaves_new_edit_pending(
    client, worker, session_maker, role_agent, monkeypatch
):
    agent_id, role, writer = role_agent
    path = "/api/v1/agents/" + agent_id
    operation = client.post(path + "/start", headers={"Idempotency-Key": "start-role"}).json()
    # Saving during an operation must not mutate its captured policy.
    client.put(
        "/api/v1/roles/" + role["id"], json={"name": "Sales", "capabilities": ["web_research"]}
    )
    complete = worker.complete
    monkeypatch.setattr(worker, "complete", Mock(side_effect=ProcessDied()))
    with pytest.raises(ProcessDied):
        worker.process_one()
    monkeypatch.setattr(worker, "complete", complete)
    assert worker.process_one()
    assert writer.call_count == 1  # Adopts the same configured incarnation on recovery.
    agent = client.get(path).json()
    assert agent["applied_role"]["revision"] == 1 and agent["permissions_pending"]
    replay = client.post(path + "/start", headers={"Idempotency-Key": "start-role"}).json()
    assert replay["id"] == operation["id"] and replay["status"] == "succeeded"
    applied = client.post(path + "/apply-role", headers={"Idempotency-Key": "apply"}).json()
    assert applied["action"] == "apply_role"
    assert worker.process_one()
    agent = client.get(path).json()
    assert agent["observed_state"] == "ready" and not agent["permissions_pending"]
    assert agent["applied_role"]["capabilities"] == ["web_research"]
    assert writer.call_count == 2


def test_stopped_role_application_never_starts_and_failure_stays_stopped(
    client, worker, session_maker, role_agent
):
    agent_id, role, writer = role_agent
    path = "/api/v1/agents/" + agent_id
    client.post(path + "/apply-role", headers={"Idempotency-Key": "stopped-apply"})
    worker.process_one()
    assert client.get(path).json()["observed_state"] == "stopped"
    assert not client.get(path).json()["permissions_pending"]
    # A validation failure must never launch an unrestricted container.
    writer.side_effect = RuntimeError("Invalid native configuration")
    op = client.post(path + "/start", headers={"Idempotency-Key": "failed-start"}).json()
    for _ in range(5):
        worker.process_one()
        with session_maker.begin() as session:
            session.get(Operation, UUID(op["id"])).next_retry_at = None
    agent = client.get(path).json()
    assert agent["observed_state"] == agent["desired_state"] == "stopped"
    assert client.get("/api/v1/operations/" + op["id"]).json()["status"] == "failed"
    assert "check native configuration" in agent["last_error"]
    assert not [
        c for c in worker.client.containers.items.values() if "io.talos.incarnation" in c.labels
    ]


@pytest.mark.parametrize("ownership_conflict", [False, True])
def test_role_stop_failure_is_durable_and_does_not_block_other_agents(
    client, worker, session_maker, role_agent, monkeypatch, ownership_conflict
):
    agent_id, _, _ = role_agent
    path = "/api/v1/agents/" + agent_id
    client.post(path + "/start", headers={"Idempotency-Key": "start-before-stop-failure"})
    worker.process_one()
    with session_maker() as session:
        incarnation = session.get(
            WorkloadIncarnation, session.get(Agent, UUID(agent_id)).current_incarnation_id
        )
    container = worker.owned_container(incarnation)
    if ownership_conflict:
        container.labels["io.talos.agent"] = "foreign"
    else:
        monkeypatch.setattr(container, "stop", Mock(side_effect=OSError("Docker unavailable")))
    operation = client.post(
        path + "/apply-role", headers={"Idempotency-Key": "stop-failure"}
    ).json()
    attempts = 1 if ownership_conflict else 5
    for attempt in range(1, attempts + 1):
        assert worker.process_one()
        with session_maker.begin() as session:
            current = session.get(Operation, UUID(operation["id"]))
            assert current.status == ("failed" if attempt == attempts else "retry_wait")
            assert "stop could not be confirmed" in current.error
            current.next_retry_at = None
    agent = client.get(path).json()
    assert agent["observed_state"] == "error"  # Never claim an unconfirmed stop succeeded.
    assert agent["desired_state"] == "stopped"
    assert container.status == "running"
    other = create(client, key="after-stop-failure").json()
    assert worker.process_one()
    assert client.get("/api/v1/operations/" + other["id"]).json()["status"] == "succeeded"


def test_gateway_lease_cleanup_requires_confirmed_stop(client, worker, session_maker, monkeypatch):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker() as session:
        incarnation = session.scalar(select(WorkloadIncarnation))
    container = worker.owned_container(incarnation)
    cleanup = Mock()
    monkeypatch.setattr("worker.lifecycle.release_stopped_gateway_lease", cleanup)
    monkeypatch.setattr(container, "stop", lambda **_: None)
    with pytest.raises(RuntimeError, match="Runtime did not stop"):
        worker.stop_incarnation(incarnation, remove=True)
    cleanup.assert_not_called()
    container.status = "exited"
    worker.stop_incarnation(incarnation, remove=True)
    cleanup.assert_called_once_with(
        worker.client,
        worker.names(UUID(agent_id))[0],
        container.attrs["Config"]["Hostname"],
        worker.labels(UUID(agent_id)),
        image=incarnation.image_digest,
    )


@pytest.mark.parametrize("suffix", ["-init", "-permissions", "-setup", "-model", "-capture"])
@pytest.mark.parametrize("action", ["start", "delete"])
def test_replacement_and_deletion_remove_orphaned_helpers(
    client, worker, session_maker, suffix, action
):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        agent.observed_state = "degraded"
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
    helper = worker.client.containers.create(
        name=incarnation.config_volume + suffix, labels=worker.labels(UUID(agent_id))
    )
    # Docker refuses to delete even a stopped helper's mounted volumes.
    for name in (incarnation.config_volume, worker.names(UUID(agent_id))[0]):
        volume = worker.client.volumes.get(name)
        remove = volume.remove

        def remove_unmounted(*, remove=remove):
            assert helper.name not in worker.client.containers.items
            remove()

        volume.remove = remove_unmounted
    path = f"/api/v1/agents/{agent_id}"
    response = (
        client.post(path + "/start", headers={"Idempotency-Key": "replace"})
        if action == "start"
        else client.delete(path, headers={"Idempotency-Key": "delete"})
    )
    worker.process_one()
    result = client.get("/api/v1/operations/" + response.json()["id"]).json()
    assert result["status"] == "succeeded", result
    assert helper.name not in worker.client.containers.items


@pytest.mark.parametrize("suffix", ["-setup", "-model", "-capture"])
def test_helper_cleanup_preserves_foreign_containers(client, worker, session_maker, suffix):
    from worker.runtime import OwnershipError

    agent_id, _ = provision(client, worker)
    worker.process_one()
    with session_maker() as session:
        incarnation = session.get(Agent, UUID(agent_id)).current_incarnation
    helper = worker.client.containers.create(
        name=incarnation.config_volume + suffix, labels={"owner": "someone-else"}
    )
    with pytest.raises(OwnershipError):
        worker.remove_config(incarnation)
    assert worker.client.containers.get(helper.name) is helper
    assert worker.client.volumes.get(incarnation.config_volume)


@pytest.mark.parametrize("stop_fails", [False, True])
def test_replacement_releases_old_run_only_after_runtime_stops(
    client, worker, session_maker, monkeypatch, stop_fails
):
    agent_id, _ = provision(client, worker)
    worker.process_one()
    turn = client.post(
        f"/api/v1/agents/{agent_id}/diagnostic-runs",
        json={"message": "old turn"},
        headers={"Idempotency-Key": "old-turn"},
    ).json()
    with session_maker.begin() as session:
        session.get(Run, UUID(turn["id"])).status = "unknown"
        agent = session.get(Agent, UUID(agent_id))
        agent.observed_state = "degraded"
        old = agent.current_incarnation_id
    if stop_fails:
        monkeypatch.setattr(worker, "stop_incarnation", Mock(side_effect=RuntimeError("busy")))
    restart = client.post(
        f"/api/v1/agents/{agent_id}/start", headers={"Idempotency-Key": "restart"}
    ).json()
    worker.process_one()
    result = client.get("/api/v1/operations/" + restart["id"]).json()
    with session_maker() as session:
        agent = session.get(Agent, UUID(agent_id))
        run = session.get(Run, UUID(turn["id"]))
        if stop_fails:
            assert result["status"] == "retry_wait"
            assert agent.current_incarnation_id == old
            assert run.status == "unknown"
            return
        assert result["status"] == "succeeded"
        assert agent.current_incarnation_id != old
        assert run.status == "interrupted"
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/diagnostic-runs",
            json={"message": "new turn"},
            headers={"Idempotency-Key": "new-turn"},
        ).status_code
        == 202
    )


@pytest.mark.parametrize("kind", ["openclaw", "hermes"])
def test_native_restart_retains_image_after_tag_and_catalog_change(
    client, worker, session_maker, role_agent, monkeypatch, kind
):
    from copy import deepcopy

    from backend.app.config import get_settings
    from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS, RUNTIME_RELEASES
    from worker.lifecycle import release_stopped_gateway_lease
    from worker.runtime import NATIVE_IMAGES

    release = RUNTIME_RELEASES[kind]
    original, replacement = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    images = {
        NATIVE_IMAGES[kind]: SimpleNamespace(
            id=original, labels={"io.talos.runtime-release": release}
        ),
        original: SimpleNamespace(id=original, labels={"io.talos.runtime-release": release}),
    }
    lookup = Mock(side_effect=images.__getitem__)
    monkeypatch.setattr(worker.client.images, "get", lookup)
    payload = {"display_name": "Pinned", "employee_label": "Alex", "runtime_kind": kind}
    if kind == "hermes":
        payload["dashboard_password"] = "synthetic-version-password"
    created = client.post(
        "/api/v1/agents", json=payload, headers={"Idempotency-Key": "versioned-agent"}
    )
    assert created.status_code == 202
    agent_id = UUID(created.json()["agent_id"])
    worker.process_one()
    path = f"/api/v1/agents/{agent_id}"
    assert (
        client.post(path + "/start", headers={"Idempotency-Key": "first-start"}).status_code == 202
    )
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, agent_id)
        assert agent.observed_state == "ready"
        assert agent.runtime_image == original
        first_id = agent.current_incarnation_id
    images[NATIVE_IMAGES[kind]] = SimpleNamespace(
        id=replacement, labels={"io.talos.runtime-release": release}
    )
    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    catalog[kind] = {"2026.9.10" if kind == "openclaw" else "0.22.0": replacement}
    monkeypatch.setattr(get_settings(), "runtime_versions", catalog)
    assert (
        client.post(path + "/stop", headers={"Idempotency-Key": "stop-versioned"}).status_code
        == 202
    )
    worker.process_one()
    assert (
        client.post(path + "/start", headers={"Idempotency-Key": "restart-versioned"}).status_code
        == 202
    )
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, agent_id)
        assert agent.observed_state == "ready"
        assert agent.runtime_release == release and agent.runtime_image == original
        assert agent.current_incarnation_id != first_id
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        assert incarnation.image_digest == original
        assert worker.owned_container(incarnation).attrs["Config"]["Image"] == original
    assert lookup.call_args_list[-1].args == (original,)
    if kind == "hermes":
        release_stopped_gateway_lease.assert_not_called()
    else:
        assert release_stopped_gateway_lease.call_args.kwargs["image"] == original


@pytest.mark.parametrize("declared", [None, "openclaw-2026.9.6"])
def test_added_version_requires_matching_installed_image_before_start(
    client, worker, session_maker, role_agent, monkeypatch, declared
):
    from copy import deepcopy

    from backend.app.config import get_settings
    from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS

    catalog = deepcopy(DEFAULT_RUNTIME_VERSIONS)
    image = "sha256:" + "a" * 64
    catalog["openclaw"]["2026.9.10"] = image
    monkeypatch.setattr(get_settings(), "runtime_versions", catalog)
    monkeypatch.setattr(
        worker.client.images,
        "get",
        lambda _: SimpleNamespace(
            id=image,
            labels={} if declared is None else {"io.talos.runtime-release": declared},
        ),
    )
    created = client.post(
        "/api/v1/agents",
        json={"display_name": "Mismatch", "employee_label": "Alex", "runtime_version": "2026.9.10"},
        headers={"Idempotency-Key": "mismatched-version"},
    )
    assert created.status_code == 202
    agent_id = UUID(created.json()["agent_id"])
    worker.process_one()
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/start", headers={"Idempotency-Key": "mismatched-start"}
        ).status_code
        == 202
    )
    creations = worker.client.containers.creations
    worker.process_one()
    with session_maker() as session:
        agent = session.get(Agent, agent_id)
        assert agent.current_incarnation_id is None
        assert "does not match" in agent.last_error
    assert worker.client.containers.creations == creations
