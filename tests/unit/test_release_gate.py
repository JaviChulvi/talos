import hashlib
import json
import shutil
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import installation_acceptance, release_gate
from tests.unit.test_release import release_manifest


def trusted_run():
    return {
        "path": release_gate.CANDIDATE_WORKFLOW,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": "success",
        "repository": {"full_name": "owner/talos"},
        "head_repository": {"full_name": "owner/talos"},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_sha", "b" * 40),
        ("conclusion", "failure"),
        ("path", ".github/workflows/arbitrary.yml"),
        ("head_repository", {"full_name": "attacker/talos"}),
    ],
)
def test_gate_does_not_accept_self_asserted_success_from_other_workflows(monkeypatch, field, value):
    run = trusted_run()
    run[field] = value
    monkeypatch.setattr(release_gate, "api", lambda *args: run)
    with pytest.raises(ValueError, match="successful main-branch workflow"):
        release_gate.trusted_run("owner/talos", "123", release_gate.CANDIDATE_WORKFLOW, "a" * 40)


def test_artifact_must_be_owned_by_the_successful_workflow_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_gate,
        "api",
        lambda *args: {
            "artifacts": [
                {
                    "name": "release-candidate",
                    "expired": False,
                    "workflow_run": {"id": 321},
                }
            ]
        },
    )
    download = Mock()
    monkeypatch.setattr(release_gate, "gh", download)
    with pytest.raises(ValueError, match="workflow-bound"):
        release_gate.download("owner/talos", "123", "release-candidate", tmp_path / "bundle")
    download.assert_not_called()


@pytest.mark.parametrize("defect", ["corruption", "schema"])
def test_trusted_candidate_still_requires_valid_bundle_checksums_and_schema(
    monkeypatch, tmp_path, defect
):
    source = tmp_path / "source"
    source.mkdir()
    manifest = release_manifest()
    if defect == "schema":
        manifest["schema_version"] = 2
    (source / "manifest.json").write_text(json.dumps(manifest))
    for name in (
        "talos",
        "compose.release.yaml",
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
        "license-texts.tar.gz",
        "licenses-amd64.tar.gz",
        "licenses-arm64.tar.gz",
    ):
        (source / name).write_text("fixture")
    env = []
    for platform, images in manifest["images"].items():
        arch = platform.split("/")[1]
        (source / f"images-{arch}.txt").write_text("\n".join(images.values()) + "\n")
        env.append(f"TALOS_MANAGEMENT_{arch.upper()}={images['management']}")
    (source / "release.env").write_text("\n".join(env) + "\n")
    (source / "checksums.txt").write_text(
        "".join(
            hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
            for path in sorted(source.iterdir())
            if path.is_file() and path.name != "checksums.txt"
        )
    )
    if defect == "corruption":
        (source / "talos").write_text("tampered")
    monkeypatch.setattr(
        release_gate, "api", lambda _repo, path: {"private": True} if not path else trusted_run()
    )
    monkeypatch.setattr(
        release_gate,
        "download",
        lambda _repo, _run, _name, destination: shutil.copytree(source, destination),
    )
    with pytest.raises(ValueError, match="checksum mismatch|Unsupported release"):
        release_gate.candidate("owner/talos", "123", "a" * 40, tmp_path / "candidate")


@pytest.fixture
def proof(tmp_path):
    manifest = release_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    report = tmp_path / "report"
    (report / "reliability").mkdir(parents=True)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    acceptance = {
        "schema_version": 1,
        "manifest_sha256": digest,
        "source_revision": manifest["source_revision"],
        "candidate_run": "123",
        "platform": "linux/arm64",
        "result": "passed",
        "scenarios": sorted(release_gate.COMMON_SCENARIOS),
    }
    (report / "acceptance.json").write_text(json.dumps(acceptance))
    (report / "reliability/environment.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "dirty": False,
                "revision": manifest["source_revision"],
                "manifest_sha256": digest,
                "image_references": manifest["images"]["linux/arm64"],
            }
        )
    )
    (report / "reliability/results.xml").write_text(
        "<testsuites><testsuite>"
        + "".join(
            '<testcase name="test_both_channels_use_native_scoped_history_and_receipts['
            + runtime
            + ']"/>'
            for runtime in ("openclaw", "hermes")
        )
        + "".join(
            f'<testcase name="{name}[{case}]"/>'
            for name, count in release_gate.REQUIRED_INSTALLATION_TESTS.items()
            for case in range(count)
        )
        + "</testsuite></testsuites>"
    )
    return SimpleNamespace(report=report, manifest=manifest_path, acceptance=acceptance)


def test_gate_accepts_reports_only_for_the_exact_candidate(proof):
    release_gate.validate_evidence(proof.report, proof.manifest, "123", "linux/arm64")
    manifest = json.loads(proof.manifest.read_text())
    manifest["source_revision"] = "b" * 40
    proof.manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="match the candidate"):
        release_gate.validate_evidence(proof.report, proof.manifest, "123", "linux/arm64")


@pytest.mark.parametrize(
    "mutation",
    [
        "skipped",
        "wrong-images",
        "missing-scenario",
        "other-run",
        "missing-installation-tests",
    ],
)
def test_gate_rejects_incomplete_or_mismatched_execution(proof, mutation):
    if mutation == "skipped":
        (proof.report / "reliability/results.xml").write_text(
            '<testsuites><testsuite><testcase name="native"><skipped/></testcase>'
            "</testsuite></testsuites>"
        )
    elif mutation == "wrong-images":
        path = proof.report / "reliability/environment.json"
        report = json.loads(path.read_text())
        report["image_references"] = {}
        path.write_text(json.dumps(report))
    elif mutation == "missing-installation-tests":
        path = proof.report / "reliability/results.xml"
        path.write_text(
            path.read_text().replace(
                '<testcase name="test_backup_inventory_rejects_missing_required_resource[0]"/>', ""
            )
        )
    else:
        report = deepcopy(proof.acceptance)
        if mutation == "missing-scenario":
            report["scenarios"].remove("failed_update_recovery")
        else:
            report["candidate_run"] = "321"
        (proof.report / "acceptance.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        release_gate.validate_evidence(proof.report, proof.manifest, "123", "linux/arm64")


def test_missing_acceptance_hosts_fails_instead_of_skipping(monkeypatch):
    for key in ("FIRST_HOST", "SECOND_HOST", "HOST_ID"):
        monkeypatch.delenv("TALOS_ACCEPTANCE_" + key, raising=False)
    with pytest.raises(ValueError, match="two distinct disposable hosts"):
        installation_acceptance.require_environment()


def test_host_address_cannot_be_a_shell_or_ssh_option(tmp_path):
    for address in ("", "host;curl attacker", "-oProxyCommand=evil", "--evil", "user@host:/path"):
        with pytest.raises(ValueError):
            installation_acceptance.Host(address, "/tmp/acceptance", tmp_path / "log")
