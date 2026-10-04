"""Run native runtime reliability checks in a disposable Docker installation.

Usage: uv run python -m tests.reliability
Builds current source, uses synthetic credentials, and cleans only owned resources.
"""

import argparse
import hashlib
import ipaddress
import json
import subprocess
import sys
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
            "runtime_versions",
            "slack",
            "telegram",
            "release",
            "installation",
            "installation_access",
            "installation_backup",
            "installation_update",
            "installation_failures",
            "release_gate",
        )
    ],
    "tests/unit/test_native_ui_proxy.py::test_worker_upgrades_legacy_relay_on_its_existing_port",
    *[
        f"tests/integration/test_{name}.py"
        for name in (
            "agents",
            "lifecycle",
            "diagnostics",
            "channel_runs",
            "user_channels",
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
            "installation_maintenance",
        )
    ],
    *[
        f"tests/integration/{module}.py::{test}[{kind}]"
        for kind in ("openclaw", "hermes")
        for module, test in (
            (
                "test_setup_runtime_docker",
                "test_setup_reproduces_native_skills_and_mcp_and_preserves_unmanaged_state",
            ),
            (
                "test_native_channel_acceptance",
                "test_both_channels_use_native_scoped_history_and_receipts",
            ),
        )
    ],
]


def release_images(client, manifest_path):
    """Use the exact published artifacts; never silently rebuild a candidate."""
    from backend.management.release import load_manifest

    manifest = load_manifest(manifest_path)
    architecture = client.info()["Architecture"]
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    platform = "linux/" + architecture
    refs = manifest["images"][platform]
    images = {}
    for kind, role in (("runner", "verification"), ("openclaw", "openclaw"), ("hermes", "hermes")):
        # Host CLI honors Docker Desktop's configured credential helper.
        subprocess.run(["docker", "pull", "--platform", platform, refs[role]], check=True)
        image = client.images.get(refs[role])
        if image.attrs["Architecture"] != architecture:
            raise ValueError("Release acceptance requires native images, not emulation")
        if image.labels.get("org.opencontainers.image.revision") != manifest["source_revision"]:
            raise ValueError("Release image revision does not match its manifest")
        images[kind] = image.id
    return (
        images,
        refs["postgres"],
        {
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "revision": manifest["source_revision"],
            "platform": platform,
            "image_references": refs,
        },
    )


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="Test a prebuilt release without rebuilding")
    parser.add_argument("--report", type=Path, help="Evidence output directory")
    args = parser.parse_args(argv)
    client = docker.from_env(timeout=180)
    client.ping()
    name = "talos-reliability-" + uuid4().hex[:12]
    labels = {"io.talos.reliability-run": name}
    report = args.report or ROOT / ".data" / "reliability" / name
    report.mkdir(parents=True)
    revision = None
    dirty = None
    images = {}
    metadata = {}
    postgres = POSTGRES
    if args.manifest:
        images, postgres, metadata = release_images(client, args.manifest)
        revision = metadata["revision"]
        dirty = False
    else:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    for target, kind in (
        ()
        if args.manifest
        else (
            ("verification", "runner"),
            ("native-runtime", "openclaw"),
            ("hermes-runtime", "hermes"),
        )
    ):
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
            client.images.get(postgres)
        except docker.errors.ImageNotFound:
            subprocess.run(["docker", "pull", postgres], check=True)
        database = client.containers.run(
            postgres,
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
                "TALOS_TEST_HERMES_IMAGE": images["hermes"],
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
                    "postgres": postgres,
                    "exit_code": code,
                    "tests": TESTS,
                    **metadata,
                },
                indent=2,
            )
            + "\n"
        )
        client.close()
    print(f"Evidence: {report}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
