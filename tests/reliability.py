"""Run the OpenClaw reliability suite in a disposable Docker installation.

Usage: uv run python -m tests.reliability
Builds current source, uses synthetic credentials, and cleans only owned resources.
"""

import ipaddress
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import docker

ROOT = Path(__file__).resolve().parents[1]
POSTGRES = (
    "postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652"
)
TESTS = [
    *[
        f"tests/unit/test_{name}.py"
        for name in (
            "openclaw",
            "openrouter",
            "setup_runtime",
            "setup_bundles",
            "setup_capture",
            "setup_application_status",
            "connection_secrets",
            "reliability",
            "slack",
            "telegram",
        )
    ],
    *[
        f"tests/integration/test_{name}.py"
        for name in (
            "agents",
            "lifecycle",
            "diagnostics",
            "channel_runs",
            "employee_channels",
            "telegram_delivery",
            "slack_delivery",
            "budgets",
            "connections",
            "setups",
            "setup_applications",
            "setup_snapshot_migration",
            "setup_lifecycle_recovery",
            "runtime_reliability",
            "access_revocation",
            "runtime_compatibility",
        )
    ],
    "tests/integration/test_setup_runtime_docker.py::"
    "test_setup_reproduces_native_skills_and_mcp_and_preserves_unmanaged_state[openclaw]",
    "tests/integration/test_native_channel_acceptance.py::"
    "test_both_channels_use_native_scoped_history_and_receipts[openclaw]",
]


def main():
    client = docker.from_env(timeout=180)
    client.ping()
    name = "talos-reliability-" + uuid4().hex[:12]
    labels = {"io.talos.reliability-run": name}
    report = ROOT / ".data" / "reliability" / name
    report.mkdir(parents=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    images = {}
    for target, kind in (("verification", "runner"), ("native-runtime", "openclaw")):
        image_file = report / f"{kind}.image-id"
        print(f"Building {target} from current source", flush=True)
        subprocess.run(
            [
                "docker",
                "build",
                "--target",
                target,
                "-f",
                "deploy/Dockerfile",
                "--iidfile",
                str(image_file),
                ".",
            ],
            cwd=ROOT,
            check=True,
        )
        images[kind] = image_file.read_text().strip()
    code = 1
    try:
        occupied = [
            ipaddress.ip_network(config["Subnet"])
            for network in client.networks.list()
            for config in (network.attrs.get("IPAM", {}).get("Config") or [])
            if config.get("Subnet")
        ]
        for number in range(1, 255):
            subnet = ipaddress.ip_network(f"10.252.{number}.0/24")
            if any(subnet.overlaps(net) for net in occupied):
                continue
            try:
                network = client.networks.create(
                    name,
                    internal=True,
                    labels=labels,
                    ipam=docker.types.IPAMConfig(
                        pool_configs=[docker.types.IPAMPool(subnet=str(subnet))]
                    ),
                )
            except docker.errors.APIError as error:
                if "pool overlaps" not in str(error.explanation).lower():
                    raise
            else:
                break
        else:
            raise RuntimeError("No available reliability test subnet in 10.252.0.0/16")
        try:
            client.images.get(POSTGRES)
        except docker.errors.ImageNotFound:
            client.images.pull(POSTGRES)
        database = client.containers.run(
            POSTGRES,
            name=name + "-db",
            labels=labels,
            network=network.name,
            environment={"POSTGRES_PASSWORD": "synthetic-reliability-password"},
            tmpfs={"/var/lib/postgresql/data": "rw,nosuid,nodev,size=512m"},
            detach=True,
        )
        deadline = time.monotonic() + 60
        while database.exec_run(["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]).exit_code:
            if time.monotonic() >= deadline:
                raise TimeoutError("Disposable PostgreSQL did not become ready")
            time.sleep(0.2)
        runner = client.containers.run(
            images["runner"],
            ["python", "-m", "pytest", *TESTS, "-q", "--tb=short", "--junitxml=/proof/results.xml"],
            name=name + "-runner",
            labels=labels,
            network=network.name,
            environment={
                "TALOS_TEST_DATABASE_URL": "postgresql+psycopg://postgres:"
                f"synthetic-reliability-password@{database.name}:5432/postgres",
                "TALOS_TEST_DOCKER": "1",
                "TALOS_NATIVE_CHANNEL_PROOF": "1",
                "TALOS_TEST_TMPFS_VOLUMES": "1",
                "TALOS_RELIABILITY_RUN_ID": name,
                "TALOS_TEST_OPENCLAW_IMAGE": images["openclaw"],
            },
            tmpfs={"/tmp": "rw,exec,nosuid,nodev,size=512m,mode=1777"},
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                str(report): {"bind": "/proof", "mode": "rw"},
            },
            detach=True,
        )
        with (report / "pytest.log").open("wb") as log:
            for chunk in runner.logs(stream=True, follow=True):
                log.write(chunk)
                print(chunk.decode(errors="replace"), end="", flush=True)
        code = runner.wait()["StatusCode"]
    finally:
        owned = {"label": "io.talos.reliability-run=" + name}
        for container in client.containers.list(all=True, filters=owned):
            container.remove(force=True, v=True)
        for resource in client.networks.list(filters=owned):
            resource.remove()
        for volume in client.volumes.list(filters=owned):
            volume.remove()
        (report / "environment.json").write_text(
            json.dumps(
                {
                    "finished_at": datetime.now(UTC).isoformat(),
                    "revision": revision,
                    "dirty": dirty,
                    "images": images,
                    "postgres": POSTGRES,
                    "exit_code": code,
                    "tests": TESTS,
                },
                indent=2,
            )
            + "\n"
        )
        client.close()
    print(f"Evidence: {report}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
