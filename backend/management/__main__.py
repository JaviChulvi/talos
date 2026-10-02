"""The six public administrator commands; called by the pinned host launcher."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import docker

from backend.management.installation import Installation, checked_bundle


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command", choices=("install", "status", "doctor", "backup", "restore")
    )
    result.add_argument("--directory", type=Path, required=True)
    result.add_argument("--bundle", type=Path, required=True)
    result.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    result.add_argument("--host-os", choices=("ubuntu", "macos"), required=True)
    result.add_argument("--owner-uid", type=int, required=True)
    result.add_argument("--owner-gid", type=int, required=True)
    result.add_argument("--docker-socket", required=True)
    result.add_argument("--management-image", required=True)
    result.add_argument("--port", type=int, default=8000)
    result.add_argument("--domain")
    result.add_argument("--skip-admin", action="store_true")
    result.add_argument("--source-fenced", action="store_true")
    result.add_argument("--archive", type=Path)
    result.add_argument("--identity", type=Path)
    result.add_argument("--cancel-maintenance", action="store_true")
    result.add_argument("--acme-directory")
    result.add_argument("--acme-root", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        manifest = checked_bundle(args.bundle)
        if manifest["images"][args.platform]["management"] != args.management_image:
            raise ValueError("Management image does not match the selected release manifest")
        installation = Installation(args.directory)
        if args.command in ("status", "doctor") and not args.cancel_maintenance:
            print(json.dumps(installation.status(), indent=2))
            if args.command == "doctor":
                installation.preflight_resources()
                installation.ready(timeout=15)
                print(
                    "Platform services are running; "
                    "this check does not prove employee channel delivery."
                )
            return 0
        if args.command in ("install", "update") and shutil.disk_usage("/").free < 30 * 1024**3:
            raise ValueError(
                "Docker storage needs at least 30 GiB free; increase its disk allocation "
                "or remove unneeded resources explicitly"
            )
        with installation.lock():
            if args.acme_directory:
                # Acceptance fixture metadata never changes a normal HTTPS installation.
                installation.write(
                    "acceptance-acme.json",
                    {"directory": args.acme_directory, "root": str(args.acme_root)},
                )
            if args.command == "install":
                installation.install(
                    args.bundle,
                    platform=args.platform,
                    host_os=args.host_os,
                    uid=args.owner_uid,
                    gid=args.owner_gid,
                    docker_socket=args.docker_socket,
                    port=args.port,
                    domain=args.domain,
                    skip_admin=args.skip_admin,
                )
            elif args.command == "status" and args.cancel_maintenance:
                from backend.management.backup import resume

                journal = installation.read("operation.json", {})
                if journal.get("phase") not in ("backup", "preflight", "snapshot"):
                    raise ValueError(
                        "Operation has written state; resume it instead of cancelling maintenance"
                    )
                status = installation.maintenance_status()
                if status["maintenance"]["active"]:
                    resume(installation, journal.get("operation_id"))
                else:
                    pending = installation.read(".backup-operation.json", {})
                    if pending.get("operation_id") not in (None, journal.get("operation_id")):
                        raise ValueError("Another backup operation owns this installation")
                    (installation.directory / ".backup-operation.json").unlink(missing_ok=True)
                installation.journal(journal["kind"], "cancelled")
            else:
                if not args.archive or not args.identity:
                    raise ValueError(f"{args.command} requires --archive and --identity")
                from backend.management.backup import backup, restore, resume

                if args.command == "backup":
                    op = installation.journal("backup", "snapshot")
                    backup(
                        installation, args.archive, args.identity, operation_id=op["operation_id"]
                    )
                    installation.journal("backup", "complete")
                    print(f"Encrypted backup saved: {args.archive}")
                    print(f"Retain the recovery identity separately: {args.identity}")
                elif args.command == "restore":
                    restore(
                        installation,
                        args.archive,
                        args.identity,
                        source_fenced=args.source_fenced,
                        destination_options={
                            "owner_uid": args.owner_uid,
                            "owner_gid": args.owner_gid,
                            "host_os": args.host_os,
                            "docker_socket": args.docker_socket,
                        },
                    )
                    resume(installation)
                    installation.journal("restore", "complete")
                    print(
                        "Restored with employee agents stopped, "
                        "channels disabled and sessions revoked."
                    )
                    print(
                        "Keep the old installation fenced; "
                        "explicitly reconnect each employee channel."
                    )
        return 0
    except (
        ValueError,
        RuntimeError,
        OSError,
        subprocess.CalledProcessError,
        docker.errors.DockerException,
    ) as error:
        # Never print subprocess argv/stdout: DB commands and diagnostics can contain credentials.
        message = (
            "A Docker operation failed; inspect talos status and retry the same command."
            if isinstance(error, (subprocess.CalledProcessError, docker.errors.DockerException))
            else str(error)
        )
        print(f"Talos: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
