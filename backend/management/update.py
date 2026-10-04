"""Manual platform update with a snapshot and a durable admission boundary."""

import hashlib
from pathlib import Path

from backend.management.backup import backup, finish_resume, read_backup, restore, resume
from backend.management.installation import checked_bundle


def compatible(source: dict, target: dict) -> None:
    pair = {"version": source["version"], "database_revision": source["database_revision"]}
    if pair not in target["compatible_from"]:
        raise ValueError("Target release does not declare compatibility with this release/database")


def database_revision(installation) -> str:
    return installation.db_command(
        "from sqlalchemy import text; from backend.app.db import session_factory; "
        "s=session_factory()(); "
        "print(s.execute(text('SELECT version_num FROM alembic_version')).scalar_one())",
        capture=True,
    ).stdout.strip()


def update(installation, bundle: Path, archive: Path, identity: Path) -> None:
    target = checked_bundle(bundle)
    fingerprint = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    prior = installation.read("operation.json", {})
    if prior.get("kind") == "update" and prior.get("phase") not in ("complete", "cancelled"):
        if (
            prior.get("target_manifest") != fingerprint
            or prior.get("archive") != str(archive)
            or prior.get("identity") != str(identity)
        ):
            raise ValueError(
                "Resume the interrupted update with the identical bundle and backup paths"
            )
        if prior.get("phase") == "reopening":
            # The commit that clears maintenance may have succeeded before the process died.
            # Work could have been admitted: never restore a snapshot at this boundary.
            from backend.management.backup import _control

            status = _control(installation, "status", prior["operation_id"], "update")
            if status["maintenance"]["active"]:
                resume(installation, prior["operation_id"])
            else:
                installation.ready()
                finish_resume(installation, prior["operation_id"])
            installation.journal("update", "complete")
            return
        if prior.get("phase") in ("apply", "migrate", "verify", "rollback"):
            installation.journal("update", "rollback")
            restore(
                installation, archive, identity, rollback=True, operation_id=prior["operation_id"]
            )
            installation.journal("update", "reopening", result="rolled_back")
            resume(installation, prior["operation_id"])
            installation.journal("update", "complete", result="rolled_back")
            raise ValueError(
                "Interrupted update rolled back; the previous release is ready. Retry explicitly."
            )
    else:
        if installation.manifest["version"] == target["version"]:
            raise ValueError("This release is already installed")
        compatible(installation.manifest, target)
        if database_revision(installation) != installation.manifest["database_revision"]:
            raise ValueError("Installed database revision does not match its release manifest")
        prior = installation.journal(
            "update",
            "backup",
            target_manifest=fingerprint,
            archive=str(archive),
            identity=str(identity),
            previous_release=installation.manifest["version"],
        )
    operation_id = prior["operation_id"]
    if not archive.exists():
        backup(
            installation,
            archive,
            identity,
            operation_id=operation_id,
            keep_maintenance=True,
            kind="update",
        )
    # A file that exists is not evidence of a usable, matching backup.
    with read_backup(archive, identity) as (snapshot, _, _configs):
        if (
            snapshot["operation_id"] != operation_id
            or snapshot["installation_id"] != installation.state["installation_id"]
            or snapshot["release"]["version"] != prior["previous_release"]
        ):
            raise ValueError("Update requires its own verified snapshot of the installed release")
    from backend.management.backup import _control

    _control(installation, "assert", operation_id, "update")
    installation.journal("update", "apply")
    try:
        installation.write_config(bundle, preserve_runtime_catalog=True)
        installation.journal("update", "migrate")
        installation.compose("run", "--rm", "-T", "--pull", "never", "migrate")
        if database_revision(installation) != target["database_revision"]:
            raise ValueError("Migration did not reach the target database revision")
        installation.record_release()
        installation.journal("update", "verify")
        installation.compose("up", "-d", "--pull", "never", "--no-build")
        installation.ready()
        if installation.state["access"]["mode"] == "domain":
            from backend.management.access import verify_https

            verify_https(installation.state["access"]["domain"])
    except Exception:
        # Persist intent before removing any resources; a killed rollback stays resumable.
        installation.journal("update", "rollback")
        restore(installation, archive, identity, rollback=True, operation_id=operation_id)
        installation.journal("update", "reopening", result="rolled_back")
        resume(installation, operation_id)
        installation.journal("update", "complete", result="rolled_back")
        raise ValueError(
            "Update failed before reopening traffic. Previous release and data restored."
        ) from None
    # Once this intent is durable, even an unknown leave result cannot trigger rollback.
    installation.journal("update", "reopening")
    resume(installation, operation_id)
    installation.journal("update", "complete")
    print(f"Talos updated to {target['version']}; user runtime versions are preserved.")
