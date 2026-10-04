"""Destructive acceptance on two explicitly provisioned disposable native hosts.

The controller never reboots itself. Hosts must have matching out-of-band sentinel
files, known SSH keys, Docker registry authentication, uv, and passwordless reboot.
Missing infrastructure is a failure, never a skipped or successful release proof.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from tests.release_gate import assert_installation_contracts, assert_passed_xml

ROOT = Path(__file__).resolve().parents[1]


class Host:
    def __init__(self, address, directory, log):
        if not address or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", address):
            raise ValueError("Configure an explicit SSH disposable host")
        self.address, self.directory, self.log = address, directory, log

    def run(self, *args, check=True, input=None, timeout=900, sensitive=False):
        command = shlex.join(args)
        result = subprocess.run(
            [
                "ssh",
                "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes",
                "-oConnectTimeout=10",
                self.address,
                command,
            ],
            input=input,
            capture_output=True,
            timeout=timeout,
        )
        # Do not print commands: some fixture commands read secret-bearing config.
        if not sensitive:
            with self.log.open("ab") as output:
                output.write(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"Acceptance command failed on {self.address}; inspect {self.log}")
        return result

    def phase(self, name, platform):
        self.run(
            "bash",
            "-lc",
            f"cd {shlex.quote(self.directory + '/source')} && "
            + shlex.join(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "tests.installation_host",
                    name,
                    "--root",
                    self.directory,
                    "--platform",
                    platform,
                ]
            ),
            timeout=1800,
        )

    def read(self, relative):
        return self.run("cat", self.directory + "/" + relative).stdout

    def copy_in(self, source, relative):
        subprocess.run(
            [
                "scp",
                "-q",
                "-r",
                "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes",
                str(source),
                self.address + ":" + self.directory + "/" + relative,
            ],
            check=True,
        )

    def copy_out(self, relative, destination):
        subprocess.run(
            [
                "scp",
                "-q",
                "-r",
                "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes",
                self.address + ":" + self.directory + "/" + relative,
                str(destination),
            ],
            check=True,
        )


def require_environment():
    names = ("FIRST_HOST", "SECOND_HOST", "HOST_ID")
    result = {key: os.environ.get("TALOS_ACCEPTANCE_" + key, "") for key in names}
    if not all(result.values()) or result["FIRST_HOST"] == result["SECOND_HOST"]:
        raise ValueError("Acceptance needs two distinct disposable hosts and their sentinel ID")
    return result


def wait_for_reboot(host, old_boot, platform):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            result = host.run(
                "bash",
                "-lc",
                "cat /proc/sys/kernel/random/boot_id"
                if platform.endswith("amd64")
                else "sysctl -n kern.boottime",
                check=False,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip() != old_boot.strip():
                docker = host.run("docker", "info", check=False, timeout=30)
                if docker.returncode == 0:
                    return
        except subprocess.TimeoutExpired:
            pass  # SSH/Docker can time out during boot before their services are ready.
        time.sleep(10)
    raise TimeoutError(
        "Host did not reboot and recover Docker (Mac requires Desktop after sign-in)"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--platform", required=True, choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=False)
    manifest_path = args.bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    report = {
        "schema_version": 1,
        "candidate_run": str(args.candidate_run),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_revision": manifest["source_revision"],
        "platform": args.platform,
        "result": "failed",
        "scenarios": [],
    }
    try:
        environment = require_environment()
        directory = "/tmp/talos-acceptance-" + uuid4().hex
        hosts = [
            Host(environment[key], directory, args.report / (key.lower() + ".log"))
            for key in ("FIRST_HOST", "SECOND_HOST")
        ]
        # Do this before any write/reboot. Operators provision the sentinel out of band.
        for host in hosts:
            actual = host.run(
                "bash", "-lc", 'cat "$HOME/.talos-acceptance-host"', sensitive=True
            ).stdout
            if actual.decode().strip() != environment["HOST_ID"]:
                raise ValueError("Disposable-host sentinel does not match; refusing to modify host")
            host.run("mkdir", "-p", directory + "/source")
            archive = subprocess.check_output(["git", "archive", "HEAD"], cwd=ROOT)
            host.run("tar", "-xf", "-", "-C", directory + "/source", input=archive)
            host.copy_in(args.bundle, "bundle")
            host.phase("preflight", args.platform)
        first, second = hosts
        first.phase("install", args.platform)
        report["scenarios"] += [
            "clean_install",
            "repeat_install",
            "diagnostics",
            "interrupted_install",
        ]
        first.phase("seed-runtime-recovery", args.platform)
        boot_cmd = (
            ["cat", "/proc/sys/kernel/random/boot_id"]
            if args.platform.endswith("amd64")
            else ["sysctl", "-n", "kern.boottime"]
        )
        old_boot = first.run(*boot_cmd).stdout
        reboot = first.run("sudo", "-n", "reboot", check=False, timeout=30)
        if reboot.returncode not in (0, 255):
            raise RuntimeError("Disposable host rejected the requested reboot")
        wait_for_reboot(first, old_boot, args.platform)
        first.phase("recovered", args.platform)
        report["scenarios"] += ["reboot_recovery", "user_reboot_recovery"]
        first.phase("backup", args.platform)
        # Stop source services before restoring the same installation identity elsewhere.
        first.phase("fence", args.platform)
        # Never stage the recovery key beside uploaded evidence, even on transfer failure.
        with tempfile.TemporaryDirectory(prefix="talos-acceptance-transfer-") as transfer:
            for name in ("backup.tar", "identity.age", "snapshot.json", "runtime.json"):
                local = Path(transfer) / name
                first.copy_out(name, local)
                second.copy_in(local, name)
        second.phase("restore", args.platform)
        report["scenarios"] += ["backup_restore_second_host", "invalid_archive", "wrong_identity"]
        second.phase("failed-update", args.platform)
        report["scenarios"].append("failed_update_recovery")
        second.phase("reliability", args.platform)
        second.copy_out("reliability", args.report / "reliability")
        names = assert_passed_xml(args.report / "reliability" / "results.xml")
        assert_installation_contracts(names)
        report["scenarios"] += ["native_channels", "installation_failure_contracts"]
        if args.platform == "linux/amd64":
            for key in ("ACME_DIRECTORY", "ACME_DOMAIN", "ACME_ROOT"):
                value = os.environ.get("TALOS_ACCEPTANCE_" + key)
                if not value:
                    raise ValueError(
                        "Configure the disposable short-lived ACME test issuer: " + key
                    )
                first.run("tee", directory + "/" + key.lower(), input=value.encode())
            first.phase("acme", args.platform)
            report["scenarios"].append("acme_issuance_renewal")
        for host in hosts:
            host.phase("fence", args.platform)
        report["result"] = "passed"
    except BaseException as error:
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        (args.report / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
