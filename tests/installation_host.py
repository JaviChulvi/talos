"""Real host-side scenarios for the protected installation acceptance workflow.

Only the SSH controller calls these phases, after checking the disposable-host
sentinel. They operate inside its random directory and never prune shared Docker.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import yaml


def run(*command, check=True, **kwargs):
    return subprocess.run(command, check=check, text=True, capture_output=True, **kwargs)


def wait_ready(port=18765):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/ready", timeout=3
            ) as reply:
                if reply.status == 200:
                    return
        except OSError:
            time.sleep(2)
    raise TimeoutError("Installed application did not recover readiness")


class Scenarios:
    def __init__(self, root, native_platform):
        self.root, self.platform = root, native_platform
        self.directory = root / "installation"
        self.bundle = root / "bundle"
        self.launcher = self.bundle / "talos"
        self.manifest = json.loads((self.bundle / "manifest.json").read_text())

    def cli(self, command, *arguments, directory=None, check=True, **kwargs):
        return run(
            "bash",
            str(self.launcher),
            command,
            "--directory",
            str(directory or self.directory),
            *map(str, arguments),
            check=check,
            **kwargs,
        )

    def compose(self, *arguments, **kwargs):
        return run(
            "docker",
            "compose",
            "--project-directory",
            str(self.directory),
            "--file",
            str(self.directory / "compose.yaml"),
            *arguments,
            **kwargs,
        )

    def sql(self, statement):
        return self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "talos",
            "-d",
            "talos",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        ).stdout.strip()

    def snapshot(self):
        state = json.loads((self.directory / "installation.json").read_text())
        return {
            "installation_id": state["installation_id"],
            "release": state["release"],
            "env_sha256": hashlib.sha256((self.directory / ".env").read_bytes()).hexdigest(),
            "marker": self.sql("SELECT marker FROM installation_acceptance"),
        }

    def preflight(self):
        expected_system = "Linux" if self.platform.endswith("amd64") else "Darwin"
        if platform.system() != expected_system:
            raise ValueError(
                "Acceptance must execute on Ubuntu amd64 or native Apple Silicon macOS"
            )
        if expected_system == "Linux":
            values = dict(
                line.split("=", 1)
                for line in Path("/etc/os-release").read_text().splitlines()
                if "=" in line
            )
            if values.get("ID") != "ubuntu" or values.get("VERSION_ID", "").strip('"') != "24.04":
                raise ValueError("Acceptance requires Ubuntu 24.04")
        info = json.loads(run("docker", "info", "--format", "{{json .}}").stdout)
        architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            info["Architecture"], info["Architecture"]
        )
        if "linux/" + architecture != self.platform:
            raise ValueError("Docker architecture must match the native acceptance target")
        if self.directory.exists():
            raise ValueError("Acceptance installation destination must initially be empty")
        for port in (18765, 18766):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        if run("docker", "ps", "-aq", "--filter", "label=io.talos.installation").stdout.strip():
            raise ValueError("Acceptance hosts must have no existing Talos installations")

    def install(self):
        arguments = ("--bundle", self.bundle, "--port", "18765", "--skip-admin")
        self.cli("install", *arguments)
        wait_ready()
        self.sql("CREATE TABLE installation_acceptance (marker text NOT NULL)")
        self.sql("INSERT INTO installation_acceptance VALUES ('" + uuid4().hex + "')")
        before = self.snapshot()
        self.cli("install", *arguments)
        if self.snapshot() != before:
            raise AssertionError(
                "Rerunning installation changed identity, secrets, release, or data"
            )
        self.cli("status")
        self.cli("doctor")
        if (self.directory / ".env").stat().st_mode & 0o077:
            raise AssertionError("Generated secrets are accessible to other users")
        (self.root / "snapshot.json").write_text(json.dumps(before))
        # Interrupt the real management container after persistent state has been written.
        interrupted = self.root / "interrupted"
        command = [
            "bash",
            str(self.launcher),
            "install",
            "--directory",
            str(interrupted),
            "--bundle",
            str(self.bundle),
            "--port",
            "18766",
            "--skip-admin",
        ]
        with (self.root / "interrupted.log").open("w") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            deadline, killed = time.monotonic() + 180, False
            while process.poll() is None and time.monotonic() < deadline:
                ids = run(
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "ancestor=" + self.manifest["images"][self.platform]["management"],
                ).stdout.split()
                if (interrupted / ".env").exists() and len(ids) == 1:
                    run("docker", "kill", ids[0])
                    killed = True
                    break
                time.sleep(0.05)
            process.wait(timeout=180)
            if not killed or process.returncode == 0:
                raise AssertionError("Installer interruption was not actually exercised")
        initial = json.loads((interrupted / "installation.json").read_text())
        env_hash = hashlib.sha256((interrupted / ".env").read_bytes()).hexdigest()
        self.cli(
            "install",
            "--bundle",
            self.bundle,
            "--port",
            "18766",
            "--skip-admin",
            directory=interrupted,
        )
        wait_ready(18766)
        resumed = json.loads((interrupted / "installation.json").read_text())
        if (
            initial["installation_id"] != resumed["installation_id"]
            or env_hash != hashlib.sha256((interrupted / ".env").read_bytes()).hexdigest()
        ):
            raise AssertionError("Interrupted install changed its identity or generated secrets")
        run(
            "docker",
            "compose",
            "--project-directory",
            str(interrupted),
            "--file",
            str(interrupted / "compose.yaml"),
            "down",
            "--volumes",
        )

    def recovered(self):
        wait_ready()
        expected = json.loads((self.root / "snapshot.json").read_text())
        if self.snapshot() != expected:
            raise AssertionError("Reboot did not preserve installation state")
        self.cli("doctor")
        self.verify_runtime_recovery()

    def runtime_fixture(self, phase):
        source = Path(__file__).with_name("installation_runtime_fixture.py").read_text()
        return json.loads(
            self.compose("exec", "-T", "api", "python", "-", phase, input=source).stdout
        )

    def volume_marker(self, volume, *, write=False):
        script = (
            "from pathlib import Path; import os,json; p=Path('/proof/.talos-acceptance-marker'); "
        )
        if write:
            script += (
                "p.write_text('persisted-native-state');os.chmod(p,0o640);os.chown(p,1000,1000);"
            )
        script += (
            "s=p.stat(); print(json.dumps([p.read_text(),s.st_uid,s.st_gid,s.st_mode & 0o777]))"
        )
        return json.loads(
            run(
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "--entrypoint",
                "python",
                "--volume",
                volume + ":/proof",
                self.manifest["images"][self.platform]["platform"],
                "-c",
                script,
            ).stdout
        )

    def seed_runtime_recovery(self):
        self.runtime_fixture("bootstrap")
        # Finish the installer's persisted bootstrap phase before creating agents.
        self.cli("install", "--bundle", self.bundle, "--port", "18765", "--skip-admin")
        journal = json.loads((self.directory / "operation.json").read_text())
        if journal.get("phase") != "complete":
            raise AssertionError("Administrator bootstrap did not complete installation")
        rows = self.runtime_fixture("seed")
        if len(rows) != 6:
            raise AssertionError("Missing real native runtime reboot fixtures")
        for row in rows:
            row["marker"] = self.volume_marker(row["state_volume"], write=True)
        if self.sql("SELECT count(*) FROM user_channels WHERE enabled") != "2":
            raise AssertionError("Restore channel-disable fixture was not seeded")
        if self.sql("SELECT count(*) FROM administrator_sessions") != "1":
            raise AssertionError("Restore administrator-session fixture was not seeded")
        (self.root / "runtime.json").write_text(json.dumps(rows))

    def verify_runtime_recovery(self):
        expected = json.loads((self.root / "runtime.json").read_text())
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            rows = {row["id"]: row for row in self.runtime_fixture("inventory")}
            if all(
                rows[row["id"]]["observed_state"] == "ready"
                for row in expected
                if row["name"].endswith("-running")
            ):
                break
            time.sleep(2)
        else:
            raise AssertionError("Native running users did not recover after host reboot")
        for before in expected:
            after = rows[before["id"]]
            if any(before[key] != after[key] for key in ("incarnation", "image", "runs")):
                raise AssertionError(
                    "Host recovery replaced an identity or changed unresolved work"
                )
            inspected = run(
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                before["container"],
                check=False,
            )
            running = inspected.returncode == 0 and inspected.stdout.strip() == "true"
            if running != before["name"].endswith("-running"):
                raise AssertionError("Host recovery restarted a stopped or uncertain user")
            if self.volume_marker(before["state_volume"]) != before["marker"]:
                raise AssertionError("Runtime state or Unix permissions changed after reboot")
        # Administrator explicitly stops all remaining users before manual backup.
        self.runtime_fixture("stop")

    def backup(self):
        self.cli(
            "backup",
            "--archive",
            self.root / "backup.tar",
            "--identity",
            self.root / "identity.age",
        )
        if not (self.root / "backup.tar").stat().st_size:
            raise AssertionError("Backup did not produce a completed archive")

    def fence(self):
        if (self.directory / "compose.yaml").exists():
            self.compose("down")

    def restore(self):
        archive, identity = self.root / "backup.tar", self.root / "identity.age"
        identity.chmod(0o600)
        damaged = self.root / "damaged.tar"
        damaged.write_bytes(archive.read_bytes()[:1024])
        result = self.cli(
            "restore",
            "--source-fenced",
            "--archive",
            damaged,
            "--identity",
            identity,
            check=False,
            directory=self.root / "damaged-restore",
        )
        if result.returncode == 0:
            raise AssertionError("Damaged archive was accepted")
        wrong = self.root / "wrong.age"
        key = run(
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "age-keygen",
            self.manifest["images"][self.platform]["management"],
        ).stdout
        wrong.write_text(key)
        wrong.chmod(0o600)
        result = self.cli(
            "restore",
            "--source-fenced",
            "--archive",
            archive,
            "--identity",
            wrong,
            check=False,
            directory=self.root / "wrong-identity",
        )
        if result.returncode == 0:
            raise AssertionError("Wrong recovery identity was accepted")
        self.cli(
            "restore",
            "--source-fenced",
            "--bundle",
            self.bundle,
            "--archive",
            archive,
            "--identity",
            identity,
        )
        wait_ready()
        expected = json.loads((self.root / "snapshot.json").read_text())
        actual = self.snapshot()
        if actual != expected:
            raise AssertionError("Second-host restore lost persistent identity, secrets, or data")
        if self.sql("SELECT count(*) FROM agents WHERE desired_state <> 'stopped'") != "0":
            raise AssertionError("Restore enabled user agents")
        if self.sql("SELECT count(*) FROM user_channels WHERE enabled") != "0":
            raise AssertionError("Restore enabled user channels")
        if self.sql("SELECT count(*) FROM administrator_sessions") != "0":
            raise AssertionError("Restore retained administrator sessions")
        for row in json.loads((self.root / "runtime.json").read_text()):
            if self.volume_marker(row["state_volume"]) != row["marker"]:
                raise AssertionError("Restore lost native state bytes or ownership/permissions")

    def failed_update(self):
        # A local synthetic candidate exercises rollback without publishing a broken release.
        target = self.root / "failing-update"
        shutil.copytree(self.bundle, target)
        manifest = json.loads((target / "manifest.json").read_text())
        manifest["compatible_from"] = [
            {"version": manifest["version"], "database_revision": manifest["database_revision"]}
        ]
        manifest["version"] = "9999.0.0-acceptance"
        (target / "manifest.json").write_text(json.dumps(manifest))
        compose = yaml.safe_load((target / "compose.release.yaml").read_text())
        compose["services"]["api"]["command"] = ["python", "-c", "raise SystemExit(42)"]
        (target / "compose.release.yaml").write_text(yaml.safe_dump(compose))
        (target / "checksums.txt").write_text(
            "".join(
                hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
                for path in sorted(target.iterdir())
                if path.is_file() and path.name != "checksums.txt"
            )
        )
        before = self.snapshot()
        result = self.cli(
            "update",
            "--bundle",
            target,
            "--archive",
            self.root / "update-backup.tar",
            "--identity",
            self.root / "identity.age",
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("Intentionally broken release was accepted")
        wait_ready()
        if self.snapshot() != before:
            raise AssertionError("Failed update did not restore the previous database and release")
        journal = json.loads((self.directory / "operation.json").read_text())
        if journal.get("result") != "rolled_back":
            raise AssertionError("Failure was rejected before actually exercising rollback")

    def reliability(self):
        from tests.reliability import main

        # The SDK does not read the CLI's selected Docker Desktop context itself.
        context = run("docker", "context", "show").stdout.strip()
        endpoint = run(
            "docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"
        ).stdout.strip()
        if not endpoint.startswith("unix:///"):
            raise ValueError("Acceptance Docker SDK must use the local engine")
        os.environ["DOCKER_HOST"] = endpoint

        if main(
            [
                "--manifest",
                str(self.bundle / "manifest.json"),
                "--report",
                str(self.root / "reliability"),
            ]
        ):
            raise AssertionError("Native runtime acceptance failed")

    def acme(self):
        # Never reactivate the installation identity restored onto the other host.
        self.directory = self.root / "https"
        domain = (self.root / "acme_domain").read_text().strip()
        issuer = (self.root / "acme_directory").read_text().strip()
        root_cert = self.root / "acme_root"
        # The protected fixture supplies a real ACME issuer with <=60-second cert validity.
        env = {**os.environ, "TALOS_ACCEPTANCE_TEST": "1"}
        self.cli(
            "install",
            "--bundle",
            self.bundle,
            "--domain",
            domain,
            "--skip-admin",
            "--acme-directory",
            issuer,
            "--acme-root",
            root_cert,
            env=env,
        )
        context = ssl.create_default_context(cafile=str(root_cert))

        def certificate():
            with socket.create_connection((domain, 443), timeout=10) as connection:
                with context.wrap_socket(connection, server_hostname=domain) as wrapped:
                    cert = wrapped.getpeercert()
                    if ssl.cert_time_to_seconds(cert["notAfter"]) - time.time() > 120:
                        raise ValueError("Acceptance ACME issuer must use short-lived certificates")
                    return cert["serialNumber"]

        try:
            serial, deadline = certificate(), time.monotonic() + 240
            while time.monotonic() < deadline:
                time.sleep(5)
                if certificate() != serial:
                    return
            raise AssertionError("Caddy did not renew the test certificate")
        finally:
            self.fence()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "preflight",
            "install",
            "recovered",
            "backup",
            "fence",
            "restore",
            "failed-update",
            "reliability",
            "acme",
            "seed-runtime-recovery",
        ),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    getattr(Scenarios(args.root, args.platform), args.phase.replace("-", "_"))()


if __name__ == "__main__":
    main()
