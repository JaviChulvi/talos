import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from backend.management.release import (
    IMAGE_ROLES,
    PLATFORMS,
    validate_manifest,
    verify_bundle,
)
from deploy import build_release


def release_manifest():
    images = {
        platform: {
            role: f"ghcr.io/example/talos-{role}@sha256:"
            + hashlib.sha256(f"{platform}-{role}".encode()).hexdigest()
            for role in IMAGE_ROLES
        }
        for platform in PLATFORMS
    }
    return {
        "schema_version": 1,
        "version": "0.1.0-beta.1",
        "source_revision": "a" * 40,
        "database_revision": "0025",
        "images": images,
        "runtime_versions": {
            platform: {
                "openclaw": {"2026.9.6": images[platform]["openclaw"]},
                "hermes": {"0.21.5": images[platform]["hermes"]},
            }
            for platform in PLATFORMS
        },
        "compatible_from": [],
    }


def license_evidence(directory, manifest, platform):
    arch = platform.split("/")[1]
    report = {
        "images": manifest["images"][platform],
        "platform": platform,
        "source_revision": manifest["source_revision"],
        "scope": "all-layers",
    }
    path = directory / f"licenses-{arch}.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        files = {"inventory.json": json.dumps(report)}
        files.update(
            {
                f"{role}.spdx.json": '{"spdxVersion":"SPDX-2.3"}'
                for role in manifest["images"][platform]
            }
        )
        for name, content in files.items():
            data = content.encode()
            member = tarfile.TarInfo(f"licenses-{arch}/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("version", "latest"),
        ("source_revision", "HEAD"),
        ("database_revision", "head;exit"),
        ("compatible_from", [{"version": "0.1.0-beta.1", "database_revision": "0025"}]),
    ],
)
def test_unsupported_release_rejected_before_installation(field, value):
    manifest = release_manifest()
    manifest[field] = value
    with pytest.raises(ValueError):
        validate_manifest(manifest)


def test_manifest_requires_complete_architecture_and_pullable_image_inventory():
    manifest = release_manifest()
    assert validate_manifest(manifest) is manifest
    for mutate in (
        lambda m: m["images"].pop("linux/arm64"),
        lambda m: m["images"]["linux/amd64"].pop("openclaw_base"),
        lambda m: m["images"]["linux/amd64"].update(management="ghcr.io/example/manager:latest"),
        lambda m: m["images"]["linux/amd64"].update(management="sha256:" + "a" * 64),
        lambda m: m["runtime_versions"]["linux/amd64"]["hermes"].update(
            {"0.21.5": "ghcr.io/example/other@sha256:" + "a" * 64}
        ),
    ):
        changed = deepcopy(manifest)
        mutate(changed)
        with pytest.raises(ValueError):
            validate_manifest(changed)


def test_bundle_preserves_architecture_and_checksums_every_download(tmp_path, monkeypatch):
    manifest = release_manifest()
    root = tmp_path / "source"
    (root / "deploy").mkdir(parents=True)
    (root / "licenses").mkdir()
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        (root / name).write_text("Test license notice")
    (root / "deploy/talos").write_text("#!/bin/bash\nexit 0\n")
    (root / "deploy/talos").chmod(0o755)
    (root / "deploy/compose.release.yaml").write_text("services: {}\n")
    (root / "deploy/Caddyfile").write_text("example.test { reverse_proxy api:8000 }\n")
    monkeypatch.setattr(build_release, "ROOT", root)
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        (tmp_path / f"images-{arch}.json").write_text(
            json.dumps(
                {
                    "platform": platform,
                    "source_revision": manifest["source_revision"],
                    "images": manifest["images"][platform],
                    "license_inventory": license_evidence(tmp_path, manifest, platform),
                }
            )
        )
    output = tmp_path / "bundle"
    build_release.bundle(
        manifest["version"], manifest["source_revision"], "0025", tmp_path, output, []
    )
    assert verify_bundle(output) == manifest
    assert (output / "talos").stat().st_mode & 0o111
    assert "Caddyfile" in (output / "checksums.txt").read_text()
    assert "licenses-arm64.tar.gz" in (output / "checksums.txt").read_text()
    assert (output / "NOTICE").read_text() == "Test license notice"
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        refs = set((output / f"images-{arch}.txt").read_text().splitlines())
        assert refs == set(manifest["images"][platform].values())
        assert manifest["images"][platform]["management"] in (output / "release.env").read_text()
    (output / "compose.release.yaml").write_text("services: {wrong: {image: wrong}}\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_bundle(output)
    with pytest.raises(FileExistsError):
        build_release.bundle(
            manifest["version"], manifest["source_revision"], "0025", tmp_path, output, []
        )


def test_bundle_rejects_images_from_different_source_before_writing(tmp_path):
    manifest = release_manifest()
    (tmp_path / "images-amd64.json").write_text(
        json.dumps(
            {
                "platform": "linux/amd64",
                "source_revision": "b" * 40,
                "images": manifest["images"]["linux/amd64"],
            }
        )
    )
    output = tmp_path / "bundle"
    with pytest.raises(ValueError, match="different source"):
        build_release.bundle(
            manifest["version"], manifest["source_revision"], "0025", tmp_path, output, []
        )
    assert not output.exists()


def test_release_compose_requires_pins_and_does_not_publish_private_services():
    path = Path(__file__).resolve().parents[2] / "deploy/compose.release.yaml"
    compose = yaml.safe_load(path.read_text())
    for name, service in compose["services"].items():
        assert "build" not in service
        assert service["image"].startswith("${TALOS_")
        assert service["labels"]["io.talos.installation"]
        if name != "api":
            assert "ports" not in service
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:${TALOS_PORT:-8000}:8000"]
    assert compose["services"]["worker"]["environment"]["TALOS_RUNTIME_VERSIONS"].startswith(
        "${TALOS_RUNTIME_VERSIONS:?"
    )


def test_upstream_inventory_includes_the_actual_helper_image():
    from worker.runtime import IMAGE

    assert build_release.UPSTREAM["openclaw_base"] == IMAGE


def test_unknown_and_reciprocal_licenses_are_never_automatically_cleared():
    from deploy.image_inventory import component_rows

    packages = [
        {"name": name, "version": "1", "type": "python", "licenses": licenses}
        for name, licenses in (
            ("unknown", []),
            ("reciprocal", [{"spdxExpression": "LGPL-3.0-only"}]),
            ("permissive", [{"spdxExpression": "MIT"}]),
        )
    ]
    rows = component_rows({"artifacts": packages}, "platform", "repo@sha256:test")
    assert [row["disposition"] for row in rows] == [
        "license_review_required",
        "source_or_reciprocity_review_required",
        "retain_notices",
    ]


@pytest.mark.parametrize(
    "visibility,allowed",
    [
        ("private", True),
        ("missing", True),
        ("public", False),
        ("internal", False),
        ("forbidden", False),
        ("unknown", False),
    ],
)
@pytest.mark.parametrize("owner_type,owner_path", [("User", "users"), ("Organization", "orgs")])
def test_candidate_workflow_refuses_nonprivate_destinations_before_push(
    tmp_path, visibility, allowed, owner_type, owner_path
):
    workflow = yaml.safe_load(
        (build_release.ROOT / ".github/workflows/release-candidate.yaml").read_text()
    )
    job = workflow["jobs"]["build"]
    destination = job["env"]["CANDIDATE_REGISTRY"]
    assert "${{ github.run_id }}" in destination and "${{ github.run_attempt }}" in destination
    steps = job["steps"]
    guard = next(
        step
        for step in steps
        if step.get("name") == "Require private candidate package destinations"
    )
    builder = next(step for step in steps if step.get("name") == "Build immutable candidate images")
    assert steps.index(guard) < steps.index(builder)
    assert '--registry "$CANDIDATE_REGISTRY"' in builder["run"]
    helper = tmp_path / "gh"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "with Path('endpoints').open('a') as log: log.write(sys.argv[2] + '\\n')\n"
        "state = os.environ['TEST_VISIBILITY']\n"
        "if state in ('missing', 'forbidden'):\n"
        "    print('HTTP ' + ('404' if state == 'missing' else '403'), file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "assert sys.argv[3:] == ['--jq', '.visibility']\n"
        "print(state)\n"
    )
    helper.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", guard["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "TEST_VISIBILITY": visibility,
            "OWNER_TYPE": owner_type,
            "GITHUB_REPOSITORY_OWNER": "owner",
            "CANDIDATE_REGISTRY": "ghcr.io/Owner/Talos-candidate-123-1",
            "GITHUB_ENV": str(tmp_path / "github-env"),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == allowed, result.stderr
    endpoints = (tmp_path / "endpoints").read_text().splitlines()
    expected = [
        f"/{owner_path}/owner/packages/container/talos-candidate-123-1-{role}"
        for role in build_release.TARGETS
    ]
    assert endpoints == (expected if allowed else expected[:1])

    if allowed:
        assert (tmp_path / "github-env").read_text().strip() == (
            "CANDIDATE_REGISTRY=ghcr.io/owner/talos-candidate-123-1"
        )
    else:
        assert not (tmp_path / "github-env").exists()
