"""Single-worker, restartable Docker lifecycle operations."""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import secrets
import socket
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import docker
from docker.errors import NotFound
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError

from backend.app.config import get_settings
from backend.app.db import session_factory
from backend.app.diagnostics import mark_runs_stopped
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    RUNTIME_RELEASE,
    Agent,
    Operation,
    WorkloadIncarnation,
)
from worker.openclaw import DeviceIdentity, GatewayError, OpenClawClient
from worker.runtime import IMAGE, approve_device, launch_options, prepare_volumes, runtime_config

MAX_ATTEMPTS = 5


class OwnershipError(RuntimeError):
    pass


class StorageFullError(RuntimeError):
    message = "Docker storage is full. Free Docker disk space, then start the agent again."


def require_labels(actual: dict, expected: dict):
    if any(actual.get(key) != value for key, value in expected.items()):
        raise OwnershipError("Docker resource does not belong to this agent installation")


@contextmanager
def worker_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Talos worker owns this state directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def credential_path(incarnation_id: UUID) -> Path:
    return get_settings().worker_state_dir / "incarnations" / f"{incarnation_id}.json"


def read_credentials(incarnation: WorkloadIncarnation) -> dict:
    credentials = json.loads(credential_path(incarnation.id).read_text())
    digest = hashlib.sha256(credentials["agent_token"].encode()).hexdigest()
    if digest != incarnation.gateway_token_hash:
        raise RuntimeError("Private agent identity does not match its database record")
    return credentials


async def connect_runtime(sessions, agent_id: UUID) -> OpenClawClient:
    """Connect diagnostics using the worker-only identity, never a browser token."""
    with sessions() as session:
        agent = session.get(Agent, agent_id)
        if agent is None or agent.desired_state != "running" or agent.observed_state != "ready":
            raise RuntimeError("Agent is not ready")
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        if (
            incarnation is None
            or incarnation.revoked_at is not None
            or incarnation.expires_at is None
            or incarnation.expires_at <= datetime.now(UTC)
        ):
            raise RuntimeError("Agent identity is inactive")
        credentials = read_credentials(incarnation)
        name = incarnation.container_name
    identity = DeviceIdentity.load_or_create(get_settings().worker_state_dir / "control-device.pem")
    return await OpenClawClient(
        f"ws://{name}:18789", credentials["control_token"], identity
    ).connect()


class Worker:
    def __init__(self, sessions=None, client=None):
        self.sessions = sessions or session_factory()
        self.client = client or docker.from_env(timeout=30)
        self.settings = get_settings()
        self.owner = str(uuid4())

    def labels(self, agent_id: UUID) -> dict:
        return {
            "io.talos.project": self.settings.compose_project,
            "io.talos.installation": self.settings.installation_id,
            "io.talos.agent": str(agent_id),
        }

    def names(self, agent_id: UUID) -> tuple[str, str]:
        prefix = (
            f"talos-{self.settings.compose_project}-{self.settings.installation_id}-{agent_id.hex}"
        )
        return prefix + "-state", prefix + "-net"

    def service_labels(self, service: str) -> dict:
        return {
            "com.docker.compose.project": self.settings.compose_project,
            "com.docker.compose.service": service,
            "io.talos.installation": self.settings.installation_id,
        }

    def platform_containers(self):
        worker = self.client.containers.get(self.settings.worker_container or socket.gethostname())
        require_labels(worker.labels, self.service_labels("worker"))
        gateways = self.client.containers.list(
            filters={
                "label": [
                    f"com.docker.compose.project={self.settings.compose_project}",
                    "com.docker.compose.service=gateway",
                ]
            }
        )
        if not gateways:
            raise RuntimeError("Gateway is temporarily unavailable")
        if len(gateways) != 1:
            raise OwnershipError("Expected exactly one running gateway for this installation")
        require_labels(gateways[0].labels, self.service_labels("gateway"))
        return worker, gateways[0]

    def ensure_network(self, agent_id: UUID):
        _, name = self.names(agent_id)
        labels = self.labels(agent_id)
        try:
            network = self.client.networks.get(name)
            require_labels(network.attrs.get("Labels") or {}, labels)
            if not network.attrs.get("Internal") or network.attrs.get("Driver") != "bridge":
                raise OwnershipError("Agent network must be an internal Docker bridge")
        except NotFound:
            network = self.client.networks.create(
                name, driver="bridge", internal=True, labels=labels
            )
        for container, alias in zip(
            self.platform_containers(), ("talos-worker", "talos-gateway"), strict=True
        ):
            network.reload()
            if container.id not in (network.attrs.get("Containers") or {}):
                network.connect(container, aliases=[alias])
        return network

    def ensure_state(self, agent_id: UUID):
        name, _ = self.names(agent_id)
        labels = self.labels(agent_id)
        try:
            volume = self.client.volumes.get(name)
            require_labels(volume.attrs.get("Labels") or {}, labels)
        except NotFound:
            self.client.volumes.create(name=name, labels=labels)

    def owned_container(self, incarnation: WorkloadIncarnation):
        if not incarnation.container_name:
            return None
        try:
            container = self.client.containers.get(incarnation.container_name)
        except NotFound:
            return None
        require_labels(
            container.labels,
            {
                **self.labels(incarnation.agent_id),
                "io.talos.incarnation": str(incarnation.id),
                "io.talos.config": incarnation.config_hash,
            },
        )
        if incarnation.container_id and container.id != incarnation.container_id:
            raise OwnershipError("Recorded container identity changed")
        if container.attrs["Config"]["Image"] != IMAGE:
            raise OwnershipError("Runtime image does not match the approved digest")
        return container

    def stop_incarnation(self, incarnation: WorkloadIncarnation, remove=False):
        container = self.owned_container(incarnation)
        if container is not None:
            container.reload()
            if container.status in {"running", "restarting", "paused"}:
                container.stop(timeout=15)
                container.reload()
            if container.status in {"running", "restarting", "paused"}:
                raise RuntimeError("Runtime did not stop")
            if remove:
                container.remove()
        if remove and incarnation.config_volume:
            self.remove_config(incarnation)

    def remove_config(self, incarnation: WorkloadIncarnation):
        labels = self.labels(incarnation.agent_id)
        try:
            initializer = self.client.containers.get(incarnation.config_volume + "-init")
        except NotFound:
            pass
        else:
            require_labels(initializer.labels, labels)
            initializer.remove(force=True)
        self.remove_volume(incarnation.config_volume, labels)

    def remove_volume(self, name, labels):
        try:
            volume = self.client.volumes.get(name)
        except NotFound:
            return
        require_labels(volume.attrs.get("Labels") or {}, labels)
        volume.remove()

    def revoke_all(self, agent_id: UUID) -> list[WorkloadIncarnation]:
        with self.sessions.begin() as session:
            incarnations = session.scalars(
                select(WorkloadIncarnation).where(WorkloadIncarnation.agent_id == agent_id)
            ).all()
            for incarnation in incarnations:
                if incarnation.revoked_at is None:
                    incarnation.revoked_at = datetime.now(UTC)
            return incarnations

    def ensure_incarnation(self, operation: Operation) -> WorkloadIncarnation:
        # The target revision is the durable identity for this particular start.
        with self.sessions() as session:
            current = session.scalar(
                select(WorkloadIncarnation).where(
                    WorkloadIncarnation.agent_id == operation.agent_id,
                    WorkloadIncarnation.generation == operation.target_revision,
                )
            )
            if current:
                return current
        for previous in self.revoke_all(operation.agent_id):
            self.stop_incarnation(previous, remove=True)
        with self.sessions.begin() as session:
            agent = session.get(Agent, operation.agent_id, with_for_update=True)
            incarnation = WorkloadIncarnation(
                id=uuid4(),
                agent_id=agent.id,
                generation=operation.target_revision,
                runtime_release=RUNTIME_RELEASE,
                image_digest=IMAGE,
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            prefix = f"talos-{incarnation.id.hex}"
            incarnation.container_name = prefix
            incarnation.config_volume = prefix + "-config"
            session.add(incarnation)
            session.flush()
            agent.current_incarnation_id = incarnation.id
            return incarnation

    def ensure_credentials(self, incarnation: WorkloadIncarnation) -> tuple[dict, dict]:
        path = credential_path(incarnation.id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            credentials = json.loads(path.read_text())
        else:
            if incarnation.gateway_token_hash:
                raise RuntimeError("Existing runtime has lost its private identity")
            credentials = {
                "agent_token": secrets.token_urlsafe(32),
                "control_token": secrets.token_urlsafe(32),
            }
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
                os.chmod(temporary.name, 0o600)
                json.dump(credentials, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, path)
            descriptor = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        config = runtime_config(
            credentials["control_token"], credentials["agent_token"], "http://talos-gateway:8001"
        )
        digest = hashlib.sha256(credentials["agent_token"].encode()).hexdigest()
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if incarnation.gateway_token_hash and (
            incarnation.gateway_token_hash != digest or incarnation.config_hash != config_hash
        ):
            raise RuntimeError("Runtime credentials or configuration changed")
        with self.sessions.begin() as session:
            row = session.get(WorkloadIncarnation, incarnation.id)
            row.gateway_token_hash, row.config_hash = digest, config_hash
        incarnation.gateway_token_hash, incarnation.config_hash = digest, config_hash
        return credentials, config

    async def wait_ready(self, container, token: str, *, retry=True):
        identity = DeviceIdentity.load_or_create(
            self.settings.worker_state_dir / "control-device.pem"
        )
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.to_thread(container.reload)
            if container.status != "running":
                output = await asyncio.to_thread(container.logs, tail=40)
                if b"ENOSPC" in output:
                    raise StorageFullError()
                raise RuntimeError("Runtime exited before readiness")
            client = OpenClawClient(
                f"ws://{container.name}:18789", token, identity, timeout=10 if retry else 2
            )
            try:
                await client.connect()
                await client.close()
                return
            except GatewayError as error:
                if not retry:
                    raise
                if error.code == "NOT_PAIRED" or error.details.get("code") == "PAIRING_REQUIRED":
                    await asyncio.to_thread(approve_device, container, identity)
                elif error.code != "UNAVAILABLE":
                    raise
            except (OSError, TimeoutError, EOFError):
                if not retry:
                    raise
            await asyncio.sleep(1)
        raise TimeoutError("OpenClaw readiness deadline exceeded")

    def mark_stopped(self, session, agent: Agent):
        agent.observed_state = "stopped"
        mark_runs_stopped(session, agent.id)

    def recover(self, *, yield_to_operations=True):
        """Reconcile existing runtimes without restarting them or replaying work."""
        active = (
            select(Operation.id)
            .where(
                Operation.agent_id == Agent.id,
                Operation.status.in_(ACTIVE_OPERATION_STATUSES),
            )
            .exists()
        )
        with self.sessions() as session:
            agents = session.scalars(
                select(Agent).where(
                    Agent.desired_state == "running",
                    Agent.observed_state.in_(("ready", "degraded")),
                    ~active,
                )
            ).all()
        for agent in agents:
            error_message = None
            try:
                with self.sessions() as session:
                    # Yield between probes when lifecycle work becomes runnable.
                    if yield_to_operations and session.scalar(
                        select(Operation.id)
                        .where(
                            Operation.status.in_(ACTIVE_OPERATION_STATUSES),
                            or_(
                                Operation.next_retry_at.is_(None),
                                Operation.next_retry_at <= datetime.now(UTC),
                            ),
                        )
                        .limit(1)
                    ):
                        return
                    incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
                self.client.networks.get(self.names(agent.id)[1])
                self.ensure_network(agent.id)
                if (
                    incarnation is None
                    or incarnation.revoked_at is not None
                    or incarnation.expires_at is None
                    or incarnation.expires_at <= datetime.now(UTC)
                ):
                    raise RuntimeError("Runtime identity is inactive")
                container = self.owned_container(incarnation)
                if container is None:
                    raise RuntimeError("Runtime is missing")
                container.reload()
                if container.status != "running":
                    raise RuntimeError("Runtime is not running")
                credentials = read_credentials(incarnation)
                asyncio.run(self.wait_ready(container, credentials["control_token"], retry=False))
            except OperationalError:
                raise
            except Exception as error:
                error_message = f"{type(error).__name__}: runtime recovery failed"
                logging.error("Recovery failed for agent %s: %s", agent.id, type(error).__name__)
            with self.sessions.begin() as session:
                current = session.get(Agent, agent.id, with_for_update=True)
                if (
                    current is not None
                    and current.revision == agent.revision
                    and current.desired_state == "running"
                    and current.current_incarnation_id == agent.current_incarnation_id
                    and current.observed_state == agent.observed_state
                    and not session.scalar(select(active).where(Agent.id == agent.id))
                ):
                    current.observed_state = "degraded" if error_message else "ready"
                    current.last_error = error_message

    def execute(self, operation: Operation):
        with self.sessions() as session:
            agent = session.get(Agent, operation.agent_id)
            if (
                agent.runtime_release != RUNTIME_RELEASE
                or agent.revision != operation.target_revision
            ):
                raise OwnershipError("Operation does not match the approved agent revision")
        if operation.action == "create":
            self.ensure_network(operation.agent_id)
            self.ensure_state(operation.agent_id)
            observed = "stopped"
        elif operation.action == "start":
            self.client.images.get(
                IMAGE
            )  # Pull/install the approved image before starting the worker.
            incarnation = self.ensure_incarnation(operation)
            if incarnation.revoked_at is not None or incarnation.expires_at <= datetime.now(UTC):
                raise RuntimeError("Start identity is revoked or expired")
            credentials, config = self.ensure_credentials(incarnation)
            self.ensure_network(operation.agent_id)
            state, network = self.names(operation.agent_id)
            container = self.owned_container(incarnation)
            if container is None:
                prepare_volumes(
                    self.client,
                    state,
                    incarnation.config_volume,
                    config,
                    self.labels(operation.agent_id),
                )
                container = self.client.containers.create(
                    **launch_options(
                        incarnation.container_name,
                        state,
                        network,
                        incarnation.config_volume,
                        {
                            **self.labels(operation.agent_id),
                            "io.talos.incarnation": str(incarnation.id),
                            "io.talos.config": incarnation.config_hash,
                        },
                    )
                )
            # A crash immediately above is recovered by looking up this exact
            # labeled name; no second container or state volume is allocated.
            with self.sessions.begin() as session:
                session.get(WorkloadIncarnation, incarnation.id).container_id = container.id
            container.reload()
            if container.status != "running":
                container.start()
            asyncio.run(self.wait_ready(container, credentials["control_token"]))
            observed = "ready"
        else:
            for incarnation in self.revoke_all(operation.agent_id):
                self.stop_incarnation(incarnation, remove=operation.action == "delete")
            observed = "stopped"
            if operation.action == "delete":
                self.delete_resources(operation.agent_id)
                observed = "deleted"
        with self.sessions.begin() as session:
            agent = session.get(Agent, operation.agent_id, with_for_update=True)
            if observed in {"stopped", "deleted"}:
                self.mark_stopped(session, agent)
            agent.observed_state, agent.last_error = observed, None
            if observed == "deleted":
                agent.current_incarnation_id = None
            current = session.get(Operation, operation.id)
            current.status, current.step, current.error = "succeeded", "complete", None
            current.heartbeat_at = datetime.now(UTC)

    def delete_resources(self, agent_id: UUID):
        state, name = self.names(agent_id)
        try:
            network = self.client.networks.get(name)
        except NotFound:
            pass
        else:
            require_labels(network.attrs.get("Labels") or {}, self.labels(agent_id))
            network.reload()
            for container_id in list((network.attrs.get("Containers") or {}).keys()):
                container = self.client.containers.get(container_id)
                service = container.labels.get("com.docker.compose.service")
                if service not in {"worker", "gateway"}:
                    raise OwnershipError("Unexpected container on agent network")
                require_labels(container.labels, self.service_labels(service))
                network.disconnect(container)
            network.remove()
        self.remove_volume(state, self.labels(agent_id))
        with self.sessions() as session:
            ids = session.scalars(
                select(WorkloadIncarnation.id).where(WorkloadIncarnation.agent_id == agent_id)
            ).all()
        for incarnation_id in ids:
            credential_path(incarnation_id).unlink(missing_ok=True)

    def process_one(self) -> bool:
        with self.sessions.begin() as session:
            operation = session.scalar(
                select(Operation)
                .where(
                    Operation.status.in_(ACTIVE_OPERATION_STATUSES),
                    or_(
                        Operation.next_retry_at.is_(None),
                        Operation.next_retry_at <= datetime.now(UTC),
                    ),
                )
                .order_by(Operation.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if operation is None:
                return False
            operation.status, operation.owner = "running", self.owner
            operation.attempts += 1
            operation.next_retry_at = None
            operation.heartbeat_at = datetime.now(UTC)
            operation.step = operation.action
            agent = session.get(Agent, operation.agent_id)
            agent.observed_state = {
                "create": "provisioning",
                "start": "starting",
                "stop": "stopping",
                "delete": "deleting",
            }[operation.action]
        try:
            self.execute(operation)
        except OperationalError:
            raise
        except Exception as error:
            message = (
                StorageFullError.message
                if isinstance(error, StorageFullError)
                else f"{type(error).__name__}: lifecycle operation failed"
            )
            logging.error(
                "Lifecycle %s failed for operation %s: %s",
                operation.action,
                operation.id,
                message,
            )
            with self.sessions.begin() as session:
                current = session.get(Operation, operation.id)
                terminal = current.attempts >= MAX_ATTEMPTS or isinstance(
                    error, (OwnershipError, StorageFullError)
                )
                current.status = "failed" if terminal else "retry_wait"
                current.error = message
                current.next_retry_at = (
                    None
                    if terminal
                    else datetime.now(UTC) + timedelta(seconds=min(2**current.attempts, 30))
                )
                current.heartbeat_at = datetime.now(UTC)
                agent = session.get(Agent, operation.agent_id)
                agent.observed_state, agent.last_error = "error", current.error
                if agent.current_incarnation_id and terminal:
                    session.get(
                        WorkloadIncarnation, agent.current_incarnation_id
                    ).revoked_at = datetime.now(UTC)
        return True
