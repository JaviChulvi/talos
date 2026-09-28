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
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import docker
import httpx
from docker.errors import APIError, NotFound
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError

from backend.app.config import get_settings
from backend.app.db import session_factory
from backend.app.diagnostics import mark_runs_stopped
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    HERMES_RELEASE,
    RUNTIME_RELEASES,
    Agent,
    Operation,
    WorkloadIncarnation,
)
from worker.hermes import HermesClient
from worker.openclaw import DeviceIdentity, GatewayError, OpenClawClient
from worker.runtime import (
    IMAGE,
    NATIVE_IMAGES,
    OwnershipError,
    RuntimeReadinessError,
    apply_native_model,
    approve_device,
    launch_options,
    model_profile,
    native_config,
    prepare_volumes,
    release_stopped_gateway_lease,
    require_labels,
    runtime_config,
    runtime_error_message,
)
from worker.setup_runtime import apply_setup, prepare_setup, verify_setup

MAX_ATTEMPTS = 5


class StorageFullError(RuntimeError):
    message = "Docker storage is full. Free Docker disk space, then start the agent again."


class NativeModelError(RuntimeError):
    pass


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


async def connect_runtime(sessions, agent_id: UUID) -> OpenClawClient | HermesClient:
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
        hermes = agent.runtime_kind == "hermes"
    if hermes:
        worker = Worker(sessions=sessions)
        try:
            container = await asyncio.to_thread(worker.owned_container, incarnation)
            if container is None:
                raise RuntimeError("Hermes container is missing")
            return HermesClient(worker.client, container.id)
        except (APIError, RuntimeError, asyncio.CancelledError):
            worker.client.close()
            raise
    identity = DeviceIdentity.load_or_create(get_settings().worker_state_dir / "control-device.pem")
    return await OpenClawClient(
        f"ws://{name}:18789", credentials["control_token"], identity
    ).connect()


async def configure_inference(sessions, run, client):
    """Publish a model profile before send, then wait for the runtime's active catalog."""
    with sessions() as session:
        incarnation = session.get(WorkloadIncarnation, run.incarnation_id)
        if incarnation.model_route == "native":
            agent = session.get(Agent, run.agent_id)
            model_id = agent.inference_override["model_id"] if agent.inference_override else None
            if agent.runtime_kind == "hermes":
                client.model_id = model_id
            else:
                if model_id:
                    deadline = time.monotonic() + 30
                    while True:
                        catalog = await client.request("models.list", {})
                        if any(
                            m.get("id") == model_id and m.get("provider") == "talos-openrouter"
                            for m in catalog.get("models", [])
                        ):
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Native runtime has not loaded the selected model")
                        await asyncio.sleep(0.25)
                await client.request(
                    "sessions.patch",
                    {
                        "key": f"agent:main:talos:{run.agent_id}",
                        "model": "talos-openrouter/" + model_id if model_id else None,
                    },
                )
            return
        if incarnation.model_route == "fixture":
            return
        credentials = read_credentials(incarnation)
    config, identifier = model_profile(
        runtime_config(
            credentials["control_token"],
            credentials["agent_token"],
            "http://talos-gateway:8001",
            model_route="default",
        ),
        run.model_id,
        run.inference.get("capabilities", {}),
    )

    async def loaded():
        try:
            result = await client.request("models.list", {})
        except GatewayError as error:
            if "Model catalog is not ready" in str(error):
                return False
            raise
        return any(
            m.get("id") == identifier and m.get("provider") == "foundation"
            for m in result.get("models", [])
        )

    if not await loaded():

        def publish():
            worker = Worker(sessions=sessions)
            try:
                # Reuse the same labeled, network-isolated volume writer as startup.
                worker.owned_container(incarnation)
                state, _ = worker.names(run.agent_id)
                prepare_volumes(
                    worker.client,
                    state,
                    incarnation.config_volume,
                    config,
                    worker.labels(run.agent_id),
                )
            finally:
                worker.client.close()

        await asyncio.to_thread(publish)
        deadline = time.monotonic() + 30
        while not await loaded():
            if time.monotonic() >= deadline:
                raise TimeoutError("Runtime did not load the selected model profile")
            await asyncio.sleep(0.25)
    await client.request(
        "sessions.patch",
        {
            "key": f"agent:main:talos:{run.agent_id}",
            "model": f"foundation/{identifier}",
            "thinkingLevel": None,
        },
    )


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

    def ensure_network(self, agent_id: UUID, *, native=False):
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
        services = list(
            zip(self.platform_containers(), ("talos-worker", "talos-gateway"), strict=True)
        )
        if native:
            egress = self.client.containers.list(
                filters={
                    "label": [
                        f"com.docker.compose.project={self.settings.compose_project}",
                        "com.docker.compose.service=egress",
                        f"io.talos.installation={self.settings.installation_id}",
                    ]
                }
            )
            if len(egress) != 1:
                raise RuntimeReadinessError("Start the Talos egress service before native agents")
            require_labels(egress[0].labels, self.service_labels("egress"))
            services.append((egress[0], "talos-egress"))
        for container, alias in services:
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
        if container.attrs["Config"]["Image"] != (
            incarnation.image_digest if incarnation.model_route == "native" else IMAGE
        ):
            raise OwnershipError("Runtime image does not match the approved digest")
        return container

    def ui_proxy(self, incarnation: WorkloadIncarnation, *, ensure=False):
        """A fixed TCP relay publishes the UI without giving the agent an external bridge."""
        name = incarnation.container_name + "-ui"
        labels = {
            **self.labels(incarnation.agent_id),
            "io.talos.incarnation": str(incarnation.id),
            "io.talos.role": "ui-proxy",
        }
        try:
            proxy = self.client.containers.get(name)
            require_labels(proxy.labels, labels)
            if proxy.attrs["Config"]["Image"] != IMAGE:
                raise OwnershipError("UI relay image changed")
        except NotFound:
            if not ensure:
                return None
            networks = self.client.networks.list(
                filters={
                    "label": [
                        f"com.docker.compose.project={self.settings.compose_project}",
                        "com.docker.compose.network=ingress",
                    ]
                }
            )
            if len(networks) != 1:
                raise OwnershipError("Expected one platform ingress network") from None
            proxy = self.client.containers.create(
                IMAGE,
                name=name,
                labels=labels,
                network=networks[0].name,
                entrypoint=["node", "-e"],
                command=[
                    "const net=require('net');net.createServer(down=>{"
                    "const up=net.connect(Number(process.argv[2]),process.argv[1]);"
                    "down.on('error',()=>up.destroy());up.on('error',()=>down.destroy());"
                    "down.on('close',()=>up.destroy());up.on('close',()=>down.destroy());"
                    "down.pipe(up);up.pipe(down);}).listen(18789,'0.0.0.0');",
                    incarnation.container_name,
                    "9119" if incarnation.runtime_release == HERMES_RELEASE else "18789",
                ],
                ports={"18789/tcp": ("127.0.0.1", 20000 + secrets.randbelow(40000))},
                user="1000:1000",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="128m",
                pids_limit=32,
                healthcheck={"test": ["NONE"]},
            )
        if ensure:
            network = self.client.networks.get(self.names(incarnation.agent_id)[1])
            network.reload()
            if proxy.id not in (network.attrs.get("Containers") or {}):
                network.connect(proxy)
            if proxy.status != "running":
                try:
                    proxy.start()
                except APIError:
                    # A host-port collision on a new relay must choose a fresh port on retry.
                    if proxy.status == "created":
                        proxy.remove(force=True)
                    raise
            proxy.reload()
        return proxy

    def stop_incarnation(self, incarnation: WorkloadIncarnation, remove=False):
        if incarnation.model_route == "native":
            proxy = self.ui_proxy(incarnation)
            if proxy is not None:
                if proxy.status in {"running", "restarting", "paused"}:
                    proxy.stop(timeout=5)
                if remove:
                    proxy.remove()
        container = self.owned_container(incarnation)
        if container is not None:
            container.reload()
            if container.status in {"running", "restarting", "paused"}:
                container.stop(timeout=15)
                container.reload()
            if container.status in {"running", "restarting", "paused"}:
                raise RuntimeError("Runtime did not stop")
            if incarnation.runtime_release != HERMES_RELEASE:
                # The successor has a different hostname/PID namespace, so
                # OpenClaw cannot prove this predecessor dead by itself.
                release_stopped_gateway_lease(
                    self.client,
                    self.names(incarnation.agent_id)[0],
                    container.attrs["Config"]["Hostname"],
                    self.labels(incarnation.agent_id),
                )
            if remove:
                container.remove()
        if remove and incarnation.config_volume:
            self.remove_config(incarnation)

    def remove_config(self, incarnation: WorkloadIncarnation):
        labels = self.labels(incarnation.agent_id)
        for suffix in ("-init", "-permissions"):
            try:
                initializer = self.client.containers.get(incarnation.config_volume + suffix)
            except NotFound:
                continue
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
                model_route="native" if agent.runtime_mode == "native" else "default",
                runtime_release=agent.runtime_release,
                image_digest=self.client.images.get(NATIVE_IMAGES[agent.runtime_kind]).id
                if agent.runtime_mode == "native"
                else IMAGE,
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
        with self.sessions() as session:
            agent = session.get(Agent, incarnation.agent_id)
        config = (
            native_config(agent.runtime_kind, agent.dashboard_password_hash)
            if incarnation.model_route == "native"
            else runtime_config(
                credentials["control_token"],
                credentials["agent_token"],
                "http://talos-gateway:8001",
                model_route=incarnation.model_route,
            )
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

    async def wait_ready(self, container, token: str, *, retry=True, runtime_kind="openclaw"):
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
                raise RuntimeReadinessError(
                    runtime_error_message(output.decode(errors="replace"))
                    or "The runtime exited during startup. "
                    "Inspect its native configuration and Docker logs."
                )
            try:
                if runtime_kind == "hermes":
                    async with httpx.AsyncClient(
                        timeout=10 if retry else 2, trust_env=False
                    ) as client:
                        response = await client.get(f"http://{container.name}:9119/api/health")
                        response.raise_for_status()
                        health = response.json()
                        if health.get("ok") is not True or health.get("auth_required") is not True:
                            raise RuntimeError("Hermes dashboard authentication is not ready")
                else:
                    client = OpenClawClient(
                        f"ws://{container.name}:18789", token, identity, timeout=10 if retry else 2
                    )
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
            except (OSError, TimeoutError, EOFError, httpx.HTTPError):
                if not retry:
                    raise
            await asyncio.sleep(1)
        raise RuntimeReadinessError(
            "The runtime did not become ready in time. "
            "Check Docker resources and native startup logs."
        )

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
                self.ensure_network(agent.id, native=agent.runtime_mode == "native")
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
                if incarnation.model_route == "native":
                    proxy = self.ui_proxy(incarnation)
                    if proxy is None or proxy.status != "running":
                        raise RuntimeError("UI relay is not running")
                if (
                    agent.applied_application
                    and not agent.applied_application.get("legacy_receipt")
                    and incarnation.model_route == "native"
                ):
                    state, _ = self.names(agent.id)
                    verify_setup(
                        self.client,
                        state,
                        incarnation,
                        agent.runtime_kind,
                        agent.applied_application,
                        self.labels(agent.id),
                        discover=False,
                    )
                credentials = read_credentials(incarnation)
                asyncio.run(
                    self.wait_ready(
                        container,
                        credentials["control_token"],
                        retry=False,
                        runtime_kind=agent.runtime_kind,
                    )
                )
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
                agent.runtime_release != RUNTIME_RELEASES.get(agent.runtime_kind)
                or agent.revision != operation.target_revision
            ):
                raise OwnershipError("Operation does not match the approved agent revision")
        if operation.action == "configure_model":
            if agent.current_incarnation_id:
                with self.sessions() as session:
                    incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
                credentials = read_credentials(incarnation)
                container = self.owned_container(incarnation)
                if (
                    container is not None
                    and agent.observed_state == "ready"
                    and operation.model_selection
                ):
                    environment = container.attrs["Config"].get("Env", [])
                    if not any(
                        v.startswith("no_proxy=") and "talos-gateway" in v for v in environment
                    ):
                        raise NativeModelError(
                            "Stop and start this older agent once to enable OpenRouter routing, "
                            "then save the model again."
                        )
                state, _ = self.names(agent.id)
                apply_native_model(
                    self.client,
                    state,
                    incarnation,
                    agent.runtime_kind,
                    operation.model_selection,
                    credentials["agent_token"],
                    self.labels(agent.id),
                )
            with self.sessions.begin() as session:
                session.get(Agent, agent.id).inference_override = operation.model_selection or None
                current = session.get(Operation, operation.id)
                current.status, current.step, current.error = "succeeded", "complete", None
            return
        if operation.action == "dashboard":
            if (
                agent.runtime_mode != "native"
                or agent.desired_state != "running"
                or agent.observed_state != "ready"
            ):
                raise RuntimeError("Native agent is not ready")
            with self.sessions() as session:
                incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
            container = self.owned_container(incarnation)
            proxy = self.ui_proxy(incarnation)
            if proxy is None or proxy.status != "running":
                raise RuntimeError("UI relay is unavailable; stop and start the agent")
            bindings = proxy.attrs["NetworkSettings"]["Ports"]["18789/tcp"]
            if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                raise OwnershipError("Native UI must be published on loopback only")
            if agent.runtime_kind == "hermes":
                url = f"http://{agent.id.hex}.localhost:" + bindings[0]["HostPort"]
            else:
                result = container.exec_run(["node", "openclaw.mjs", "dashboard", "--json"])
                if result.exit_code:
                    raise RuntimeError("OpenClaw dashboard handoff failed")
                # --json can be preceded by plugin diagnostics. Never log the handoff.
                output = result.output.decode()
                data = None
                for index, char in enumerate(output):
                    if char == "{":
                        try:
                            data = json.loads(output[index:])
                            break
                        except ValueError:
                            continue
                if not data or not data.get("ok") or not data.get("browserBootstrapExpiresAtMs"):
                    raise RuntimeError("OpenClaw did not issue a browser handoff")
                target = urlsplit(data["browserUrl"])
                if (
                    target.scheme != "http"
                    or target.hostname not in {"127.0.0.1", "localhost"}
                    or not target.fragment
                ):
                    raise RuntimeError("Unexpected OpenClaw dashboard URL")
                authority = "127.0.0.1:" + bindings[0]["HostPort"]
                fragment = dict(parse_qsl(target.fragment))
                if not fragment.get("bootstrapToken"):
                    raise RuntimeError("OpenClaw handoff has no bootstrap token")
                fragment["gatewayUrl"] = "ws://" + authority
                url = urlunsplit(
                    (
                        "http",
                        authority,
                        target.path,
                        target.query,
                        urlencode(fragment),
                    )
                )
            with self.sessions.begin() as session:
                current = session.get(Operation, operation.id)
                current.dashboard_url = url
                current.status, current.step, current.error = "succeeded", "complete", None
                current.heartbeat_at = datetime.now(UTC)
            return
        if operation.action == "create":
            self.ensure_network(operation.agent_id, native=agent.runtime_mode == "native")
            self.ensure_state(operation.agent_id)
            observed = "stopped"
        elif operation.action in {"start", "apply_role"}:
            self.client.images.get(
                NATIVE_IMAGES[agent.runtime_kind] if agent.runtime_mode == "native" else IMAGE
            )  # Pull/install the approved image before starting the worker.
            application = operation.role_application
            if application:
                from backend.app.applications import normalize_application

                application = {
                    **normalize_application(application, agent.runtime_kind),
                    "restart": application["restart"],
                }
                operation.role_application = application
                with self.sessions.begin() as session:
                    session.get(Operation, operation.id).role_application = application
            # Empty applications must also remove any partially installed setup.
            has_setup = bool(application)
            prepared_setup = None
            # Once the stop boundary is durable, an interrupted predecessor may
            # have lost its config volume, or its successor may not have one yet.
            # Let apply_setup prepare after prepare_volumes on those retries.
            if (
                has_setup
                and agent.current_incarnation_id
                and operation.step != "applying_setup"
            ):
                with self.sessions() as session:
                    previous = session.get(WorkloadIncarnation, agent.current_incarnation_id)
                if previous and previous.revoked_at is None and previous.config_volume:
                    state, _ = self.names(agent.id)
                    prepared_setup = prepare_setup(
                        self.client,
                        state,
                        SimpleNamespace(
                            config_volume=previous.config_volume,
                            image_digest=self.client.images.get(
                                NATIVE_IMAGES[agent.runtime_kind]
                            ).id,
                        ),
                        agent.runtime_kind,
                        application,
                        self.labels(agent.id),
                    )
            if application:
                # Persist the stop boundary before mutating Docker. A preflight
                # failure preserves the old running incarnation; later failures
                # must leave the runtime stopped, including after worker restart.
                with self.sessions.begin() as session:
                    session.get(Operation, operation.id).step = "applying_setup"
                operation.step = "applying_setup"
            incarnation = self.ensure_incarnation(operation)
            if incarnation.revoked_at is not None or incarnation.expires_at <= datetime.now(UTC):
                raise RuntimeError("Start identity is revoked or expired")
            if operation.role_application:
                if application.get("legacy_receipt"):
                    # A pre-upgrade operation may already have created a container
                    # without a receipt. Recreate it with the captured selection.
                    if self.owned_container(incarnation):
                        self.stop_incarnation(incarnation, remove=True)
                    with self.sessions.begin() as session:
                        session.get(WorkloadIncarnation, incarnation.id).container_id = None
                    incarnation.container_id = None
                with self.sessions.begin() as session:
                    self.mark_stopped(session, session.get(Agent, agent.id))
            credentials, config = self.ensure_credentials(incarnation)
            self.ensure_network(operation.agent_id, native=agent.runtime_mode == "native")
            state, network = self.names(operation.agent_id)
            restart = not operation.role_application or operation.role_application["restart"]
            control_origin = ""
            if agent.runtime_mode == "native" and restart:
                proxy = self.ui_proxy(incarnation, ensure=True)
                binding = proxy.attrs["NetworkSettings"]["Ports"]["18789/tcp"][0]
                if binding["HostIp"] != "127.0.0.1":
                    raise OwnershipError("Native UI must be published on loopback only")
                host = (
                    f"{agent.id.hex}.localhost" if agent.runtime_kind == "hermes" else "127.0.0.1"
                )
                control_origin = f"http://{host}:" + binding["HostPort"]
            container = self.owned_container(incarnation)
            if container is None:
                prepare_volumes(
                    self.client,
                    state,
                    incarnation.config_volume,
                    config,
                    self.labels(operation.agent_id),
                    native=agent.runtime_mode == "native",
                    runtime_kind=agent.runtime_kind,
                )
                if has_setup:
                    apply_setup(
                        self.client,
                        state,
                        incarnation,
                        agent.runtime_kind,
                        application,
                        self.labels(agent.id),
                        prepared=prepared_setup,
                    )
                    verify_setup(
                        self.client,
                        state,
                        incarnation,
                        agent.runtime_kind,
                        application,
                        self.labels(agent.id),
                        network=network,
                    )
                if agent.runtime_mode == "native" and agent.inference_override:
                    apply_native_model(
                        self.client,
                        state,
                        incarnation,
                        agent.runtime_kind,
                        agent.inference_override,
                        credentials["agent_token"],
                        self.labels(agent.id),
                    )
                if not restart:
                    self.complete(operation, "stopped")
                    return
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
                        native_image=incarnation.image_digest
                        if agent.runtime_mode == "native"
                        else None,
                        control_token=credentials["control_token"],
                        control_origin=control_origin,
                        runtime_kind=agent.runtime_kind,
                    )
                )
            # A crash immediately above is recovered by looking up this exact
            # labeled name; setup receipts must also match before adoption.
            elif has_setup:
                verify_setup(
                    self.client,
                    state,
                    incarnation,
                    agent.runtime_kind,
                    application,
                    self.labels(agent.id),
                    network=network,
                )
            with self.sessions.begin() as session:
                session.get(WorkloadIncarnation, incarnation.id).container_id = container.id
            container.reload()
            if container.status != "running":
                container.start()
            asyncio.run(
                self.wait_ready(
                    container, credentials["control_token"], runtime_kind=agent.runtime_kind
                )
            )
            observed = "ready"
        else:
            for incarnation in self.revoke_all(operation.agent_id):
                self.stop_incarnation(incarnation, remove=operation.action == "delete")
            observed = "stopped"
            if operation.action == "delete":
                self.delete_resources(operation.agent_id)
                observed = "deleted"
        self.complete(operation, observed)

    def complete(self, operation, observed):
        with self.sessions.begin() as session:
            agent = session.get(Agent, operation.agent_id, with_for_update=True)
            if operation.role_application:
                agent.applied_role = operation.role_application["role"]
                agent.applied_application = {
                    k: v
                    for k, v in operation.role_application.items()
                    if k not in {"restart", "legacy_receipt"}
                }
                agent.selected_application = agent.applied_application
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
                if service not in {"worker", "gateway", "egress"}:
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
            if operation.step != "applying_setup":
                operation.step = operation.action
            agent = session.get(Agent, operation.agent_id)
            previous_observed_state = agent.observed_state
            agent.observed_state = {
                "create": "provisioning",
                "start": "starting",
                "apply_role": "applying",
                "stop": "stopping",
                "delete": "deleting",
            }.get(operation.action, agent.observed_state)
        try:
            self.execute(operation)
        except OperationalError:
            raise
        except Exception as error:
            stop_error = None
            preflight_failed = bool(
                operation.role_application and operation.step != "applying_setup"
            )
            if operation.role_application and not preflight_failed:
                with self.sessions() as session:
                    incarnations = session.scalars(
                        select(WorkloadIncarnation).where(
                            WorkloadIncarnation.agent_id == operation.agent_id
                        )
                    ).all()
                for incarnation in incarnations:
                    try:
                        self.stop_incarnation(incarnation)
                    except Exception as cause:
                        stop_error = cause
            message = (
                str(error)
                if isinstance(error, (NativeModelError, RuntimeReadinessError))
                else StorageFullError.message
                if isinstance(error, StorageFullError)
                else (
                    f"Role application failed ({type(error).__name__}). "
                    + ("Existing runtime preserved; " if preflight_failed else "Agent stopped; ")
                    + "check native configuration and retry Start or Apply."
                    if operation.role_application
                    else (
                        f"Model configuration failed ({type(error).__name__}). "
                        "Check native configuration and retry Save model."
                        if operation.action == "configure_model"
                        else f"{type(error).__name__}: lifecycle operation failed"
                    )
                )
            )
            if stop_error is not None:
                message = (
                    f"Role application failed ({type(error).__name__}); runtime stop could not "
                    f"be confirmed ({type(stop_error).__name__}). Inspect Docker runtime state "
                    "before retrying Start or Apply."
                )
            logging.error(
                "Lifecycle %s failed for operation %s: %s",
                operation.action,
                operation.id,
                message,
            )
            with self.sessions.begin() as session:
                current = session.get(Operation, operation.id)
                terminal = (
                    preflight_failed
                    or current.attempts >= MAX_ATTEMPTS
                    or isinstance(error, (OwnershipError, StorageFullError, NativeModelError))
                    or isinstance(stop_error, OwnershipError)
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
                if operation.action not in {"dashboard", "configure_model"}:
                    agent.observed_state, agent.last_error = (
                        previous_observed_state
                        if preflight_failed
                        else "stopped"
                        if operation.role_application and stop_error is None
                        else "error",
                        current.error,
                    )
                    if operation.role_application and not preflight_failed:
                        if stop_error is None:
                            self.mark_stopped(session, agent)
                        if terminal:
                            agent.desired_state = "stopped"
                if (
                    agent.current_incarnation_id
                    and terminal
                    and not preflight_failed
                    and operation.action not in {"dashboard", "configure_model"}
                ):
                    session.get(
                        WorkloadIncarnation, agent.current_incarnation_id
                    ).revoked_at = datetime.now(UTC)
        return True
