"""Persistent, resumable host installation operations (run only by the management image)."""

import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import docker

from backend.management.release import verify_bundle

STATE_VERSION = 1
SERVICES = ("db", "egress", "api", "worker", "gateway", "connector")


def run(*args: str, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=capture, **kwargs)


def atomic_json(path: Path, value: dict, uid: int | None = None, gid: int | None = None) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if uid is not None:
        os.chown(temporary, uid, gid)
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


checked_bundle = verify_bundle


def atomic_text(path: Path, text: str, uid: int, gid: int) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, uid, gid)
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Installation:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.state = self.read("installation.json", {})
        self.manifest = self.read("manifest.json", {})
        if self.state and self.state.get("schema_version") != STATE_VERSION:
            raise ValueError("Unsupported installation format; no changes made")
        if self.state:
            for key in ("installation_id", "compose_project"):
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", self.state.get(key, "")):
                    raise ValueError("Invalid installation identity")

    def read(self, name: str, default=None):
        path = self.directory / name
        return json.loads(path.read_text()) if path.exists() else default

    def write(self, name: str, value: dict) -> None:
        atomic_json(
            self.directory / name, value, self.state.get("owner_uid"), self.state.get("owner_gid")
        )

    def save(self) -> None:
        self.write("installation.json", self.state)

    def compose(self, *args: str, capture: bool = False, **kwargs):
        return run(
            "docker",
            "compose",
            "--project-directory",
            str(self.directory),
            "--env-file",
            str(self.directory / ".env"),
            "--project-name",
            self.state["compose_project"],
            "-f",
            str(self.directory / "compose.yaml"),
            *args,
            capture=capture,
            **kwargs,
        )

    @contextmanager
    def lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / "operation.lock").open("a+") as stream:
            os.chmod(stream.name, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another Talos operation holds this installation lock") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def journal(self, kind: str, phase: str, **values) -> dict:
        operation = self.read("operation.json", {})
        if operation.get("phase") not in (None, "complete", "cancelled"):
            if operation.get("kind") != kind:
                raise ValueError(f"Resume the interrupted {operation['kind']} operation first")
        else:
            operation = {"operation_id": str(uuid.uuid4()), "kind": kind}
        operation.update(phase=phase, **values)
        self.write("operation.json", operation)
        return operation

    def status(self) -> dict:
        result = {"installation": self.state, "operation": self.read("operation.json", {})}
        if (self.directory / "compose.yaml").exists():
            result["services"] = self.compose(
                "ps", "--all", "--format", "json", capture=True
            ).stdout
        return result

    def db_command(self, code: str, *args: str, capture=False):
        return self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--pull",
            "never",
            "api",
            "python",
            "-c",
            code,
            *args,
            capture=capture,
        )

    def preflight_ports(self) -> None:
        access = self.state["access"]
        bindings = (
            [("0.0.0.0", 80), ("0.0.0.0", 443)]
            if access["mode"] == "domain"
            else [("127.0.0.1", access["port"])]
        )
        client = docker.from_env()
        try:
            owned = client.containers.list(
                filters={
                    "label": [
                        f"com.docker.compose.project={self.state['compose_project']}",
                        f"io.talos.installation={self.state['installation_id']}",
                    ]
                }
            )
            used = {
                (item["HostIp"], int(item["HostPort"]))
                for row in owned
                for items in row.attrs["NetworkSettings"]["Ports"].values()
                for item in (items or [])
            }
            for address, port in bindings:
                if (address, port) in used:
                    continue
                probe = client.containers.create(
                    self.manifest["images"][self.state["platform"]]["management"],
                    entrypoint="python",
                    command=["-c", "import time; time.sleep(30)"],
                    ports={"9999/tcp": (address, port)},
                    labels={
                        "io.talos.installation": self.state["installation_id"],
                        "io.talos.purpose": "port-preflight",
                    },
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                )
                try:
                    probe.start()
                except docker.errors.APIError:
                    raise ValueError(
                        f"Host port {port} is unavailable; stop its owner or choose another port"
                    ) from None
                finally:
                    probe.remove(force=True)
        finally:
            client.close()

    def preflight_resources(self) -> None:
        """Check names before Compose can adopt resources from another installation."""
        project = self.state["compose_project"]
        expected = self.state["installation_id"]
        for resource, command in (("container", "ps"), ("volume", "ls"), ("network", "ls")):
            prefix = ("docker",) if resource == "container" else ("docker", resource)
            args = ("-a",) if resource == "container" else ()
            ids = run(
                *prefix,
                command,
                *args,
                "--filter",
                f"label=com.docker.compose.project={project}",
                "-q",
                capture=True,
            ).stdout.split()
            if not ids:
                continue
            inspected = json.loads(run("docker", resource, "inspect", *ids, capture=True).stdout)
            for item in inspected:
                labels = (
                    item.get("Config", {}).get("Labels")
                    if resource == "container"
                    else item.get("Labels")
                )
                if (labels or {}).get("io.talos.installation") != expected:
                    raise ValueError(
                        f"Foreign {resource} in project {project}; choose an empty directory"
                    )
        # Compose adopts explicitly named volumes/networks even with missing/wrong project labels.
        for resource, names in (
            (
                "volume",
                (
                    "postgres-data",
                    "worker-state",
                    "setup-artifacts",
                    "connection-secrets",
                    "provider-secrets",
                    "caddy-data",
                    "caddy-config",
                ),
            ),
            ("network", ("control", "ingress", "egress", "inference-egress")),
        ):
            for name in names:
                result = subprocess.run(
                    ["docker", resource, "inspect", f"{project}_{name}"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    labels = json.loads(result.stdout)[0].get("Labels") or {}
                    if labels.get("io.talos.installation") != expected:
                        raise ValueError(f"Foreign {resource} {project}_{name}; refusing adoption")

    def write_config(self, bundle: Path, *, preserve_runtime_catalog: bool = False) -> None:
        manifest = checked_bundle(bundle)
        platform = self.state["platform"]
        images = manifest["images"][platform]
        existing = {}
        env_path = self.directory / ".env"
        if env_path.exists():
            existing = dict(
                line.split("=", 1)
                for line in env_path.read_text().splitlines()
                if "=" in line and not line.startswith("#")
            )
        access = self.state["access"]
        port = access["port"]
        values = {
            "COMPOSE_PROJECT_NAME": self.state["compose_project"],
            "TALOS_INSTALLATION_ID": self.state["installation_id"],
            "POSTGRES_PASSWORD": existing.get("POSTGRES_PASSWORD", secrets.token_hex(32)),
            "TALOS_PORT": str(port),
            "TALOS_DOCKER_SOCKET": self.state["docker_socket"],
            "TALOS_ALLOWED_HOSTS": json.dumps(["127.0.0.1", "localhost"]),
            "TALOS_ALLOWED_ORIGINS": json.dumps(
                [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
            ),
            "TALOS_ADMIN_COOKIE_SECURE": "false",
            "TALOS_RUNTIME_VERSIONS": json.dumps(manifest["runtime_versions"][platform]),
        }
        if preserve_runtime_catalog and "TALOS_RUNTIME_VERSIONS" in existing:
            values["TALOS_RUNTIME_VERSIONS"] = existing["TALOS_RUNTIME_VERSIONS"]
        for role, reference in images.items():
            values[f"TALOS_{role.upper()}_IMAGE"] = reference
        content = (bundle / "compose.release.yaml").read_text()
        if access["mode"] == "domain":
            from backend.management.access import configure_https

            content, values = configure_https(self, content, values, bundle)
        atomic_text(
            env_path,
            "".join(f"{key}={value}\n" for key, value in values.items()),
            self.state["owner_uid"],
            self.state["owner_gid"],
        )
        atomic_text(
            self.directory / "compose.yaml",
            content,
            self.state["owner_uid"],
            self.state["owner_gid"],
        )
        self.manifest = manifest
        self.write("manifest.json", manifest)
        saved = self.directory / "bundle"
        if bundle.resolve() != saved.resolve():
            if saved.exists():
                shutil.rmtree(saved)
            shutil.copytree(bundle, saved)
        self.state["release"] = manifest["version"]
        self.save()

    def ready(self, timeout: int = 180) -> None:
        code = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.compose("exec", "-T", "api", "python", "-c", code, capture=True)
                break
            except subprocess.CalledProcessError:
                time.sleep(2)
        else:
            raise ValueError("API readiness timed out; rerun install after inspecting talos doctor")
        # Ready must include actual daemon processes, not only an API/database healthcheck.
        records = self.compose("ps", "--format", "json", capture=True).stdout
        services = (
            json.loads(records)
            if records.lstrip().startswith("[")
            else [json.loads(line) for line in records.splitlines() if line]
        )
        running = {item["Service"] for item in services if item["State"] == "running"}
        missing = set(SERVICES) - running
        if missing:
            raise ValueError("Services are not running: " + ", ".join(sorted(missing)))

    def install(
        self,
        bundle: Path,
        *,
        platform: str,
        host_os: str,
        uid: int,
        gid: int,
        docker_socket: str,
        port: int,
        domain: str | None,
        skip_admin: bool,
    ) -> None:
        manifest = checked_bundle(bundle)
        if platform not in manifest["images"]:
            raise ValueError("Release does not support this container architecture")
        if self.state:
            if self.state["platform"] != platform or self.state["release"] != manifest["version"]:
                raise ValueError(
                    "Install preserves the selected release; use talos update for changes"
                )
            if self.state["access"] != {
                "mode": "domain" if domain else "local",
                "domain": domain,
                "port": port,
            }:
                raise ValueError(
                    "Rerun using the saved domain and port; access mode cannot change on retry"
                )
        else:
            # The launcher creates only these entries prior to management preflight.
            allowed = {
                ".talos-lock",
                "operation.lock",
                ".downloads",
                "acceptance-acme.json",
                "acceptance-acme-root.pem",
            }
            if any(item.name not in allowed for item in self.directory.iterdir()):
                raise ValueError(
                    "Installation destination is not empty; developer installations are not adopted"
                )
            self.state = {
                "schema_version": STATE_VERSION,
                "installation_id": secrets.token_hex(12),
                "compose_project": "talos-" + secrets.token_hex(6),
                "release": manifest["version"],
                "platform": platform,
                "host_os": host_os,
                "owner_uid": uid,
                "owner_gid": gid,
                "docker_socket": docker_socket,
                "phase": "created",
                "access": {"mode": "domain" if domain else "local", "domain": domain, "port": port},
            }
            self.save()
        if self.manifest and manifest != self.manifest:
            raise ValueError(
                "Release manifest changed for the saved version; use its original bundle"
            )
        self.journal("install", "configure")
        self.preflight_resources()
        if domain:
            from backend.management.access import validate_domain

            validate_domain(domain, host_os, test_acme=bool(self.read("acceptance-acme.json", {})))
        self.write_config(bundle)
        self.preflight_ports()
        self.journal("install", "migrate")
        self.compose("up", "-d", "--pull", "never", "--no-build", "db")
        self.compose("run", "--rm", "-T", "--pull", "never", "migrate")
        self.journal("install", "start")
        self.compose("up", "-d", "--pull", "never", "--no-build")
        self.ready()
        if domain:
            from backend.management.access import verify_https

            verify_https(
                domain,
                root=(self.directory / "acceptance-acme-root.pem")
                if self.read("acceptance-acme.json", {})
                else None,
            )
        self.journal("install", "bootstrap")
        code = (
            "from backend.app.db import session_factory; "
            "from backend.app.models import Administrator; "
            "s=session_factory()(); print('exists' if s.get(Administrator,1) else 'missing')"
        )
        needs_admin = (
            self.compose("exec", "-T", "api", "python", "-c", code, capture=True).stdout.strip()
            == "missing"
        )
        if needs_admin and not skip_admin:
            self.compose("exec", "api", "python", "-m", "backend.app.auth", "bootstrap")
            needs_admin = False
        self.state["phase"] = "bootstrap" if needs_admin else "ready"
        self.save()
        self.journal("install", "bootstrap" if needs_admin else "complete")
        url = f"https://{domain}" if domain else f"http://127.0.0.1:{port}"
        print(f"Talos: {url}/#onboarding")
        if needs_admin:
            print(
                "Administrator bootstrap is still required. "
                "Rerun install in an interactive terminal."
            )
