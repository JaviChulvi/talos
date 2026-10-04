"""Consume evidence only from this repository's successful, trusted release workflows.

The JSON reports are diagnostics, not independently trusted attestations. Their
identity comes from GitHub's workflow-run and artifact APIs, never caller paths.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from backend.management.release import verify_bundle

CANDIDATE_WORKFLOW = ".github/workflows/release-candidate.yaml"
ACCEPTANCE_WORKFLOW = ".github/workflows/installation-acceptance.yml"
PLATFORMS = ("linux/amd64", "linux/arm64")
COMMON_SCENARIOS = {
    "clean_install",
    "repeat_install",
    "interrupted_install",
    "diagnostics",
    "backup_restore_second_host",
    "reboot_recovery",
    "employee_reboot_recovery",
    "failed_update_recovery",
    "native_channels",
    "installation_failure_contracts",
    "invalid_archive",
    "wrong_identity",
}
# Focused fault-injection tests must execute in the exact published verification
# image on both architectures. Host-level happy paths alone cannot authorize a release.
REQUIRED_INSTALLATION_TESTS = {
    "test_launcher_rejects_unsupported_hosts_before_installation": 8,
    "test_launcher_rejects_insufficient_resources": 3,
    "test_launcher_registry_credentials_failure_prevents_installation": 1,
    "test_interrupted_private_download_is_retryable_without_changing_data": 1,
    "test_interrupted_digest_pull_does_not_start_services_and_retry_preserves_data": 1,
    "test_interrupted_install_and_repeat_preserve_identity_secrets_and_resume": 1,
    "test_interrupted_bootstrap_resumes_same_installation_without_rotating_secrets": 1,
    "test_backup_inventory_rejects_missing_required_resource": 1,
    "test_project_resources_with_foreign_installation_labels_are_refused": 3,
    "test_occupied_port_removes_probe_and_closes_docker_client": 1,
    "test_bad_component_checksum_refuses_restore_before_any_mutation": 1,
    "test_gate_survives_sessions_blocks_mutations_and_preserves_reads": 1,
    "test_entry_waits_for_admitted_writer_then_rechecks_quiescence": 1,
    "test_failed_health_before_reopening_restores_matching_snapshot": 1,
    "test_reopening_never_rolls_back_after_possible_traffic": 2,
}


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True)


def api(repo, path):
    return json.loads(gh("api", f"repos/{repo}/{path}"))


def trusted_run(repo, run_id, workflow, revision):
    if not str(run_id).isdigit():
        raise ValueError("Workflow run ID must be numeric")
    run = api(repo, f"actions/runs/{run_id}")
    if (
        run.get("path") != workflow
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("head_sha") != revision
        or run.get("conclusion") != "success"
        or run.get("repository", {}).get("full_name") != repo
        or run.get("head_repository", {}).get("full_name") != repo
    ):
        raise ValueError(
            "Evidence must come from the successful main-branch workflow at this commit"
        )
    return run


def download(repo, run_id, name, destination):
    artifacts = api(repo, f"actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    matches = [a for a in artifacts if a["name"] == name and not a["expired"]]
    if len(matches) != 1 or matches[0].get("workflow_run", {}).get("id") != int(run_id):
        raise ValueError("Missing or ambiguous workflow-bound artifact")
    destination.mkdir(parents=True, exist_ok=False)
    gh("run", "download", str(run_id), "--repo", repo, "--name", name, "--dir", str(destination))


def candidate(repo, run_id, revision, destination):
    trusted_run(repo, run_id, CANDIDATE_WORKFLOW, revision)
    download(repo, run_id, "release-candidate", destination)
    manifest = verify_bundle(destination)
    if manifest.get("source_revision") != revision:
        raise ValueError("Candidate source revision differs from the trusted workflow")
    return manifest


def assert_passed_xml(path):
    root = ElementTree.parse(path).getroot()
    cases = root.findall(".//testcase")
    if not cases or any(
        case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")
    ):
        raise ValueError("Acceptance requires executed passing tests without skips")
    return {case.attrib.get("name", "") for case in cases}


def assert_installation_contracts(names):
    counts = Counter(name.split("[", 1)[0] for name in names)
    if any(counts[name] < count for name, count in REQUIRED_INSTALLATION_TESTS.items()):
        raise ValueError("Missing executed installation failure or maintenance acceptance tests")


def validate_evidence(directory, manifest_path, candidate_run, platform):
    manifest = json.loads(manifest_path.read_text())
    report = json.loads((directory / "acceptance.json").read_text())
    expected = {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_revision": manifest["source_revision"],
        "candidate_run": str(candidate_run),
        "platform": platform,
        "result": "passed",
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("Acceptance evidence does not match the candidate")
    scenarios = COMMON_SCENARIOS | (
        {"acme_issuance_renewal"} if platform == "linux/amd64" else set()
    )
    if set(report.get("scenarios", [])) != scenarios:
        raise ValueError("Required real-host acceptance scenarios have not all passed")
    names = assert_passed_xml(directory / "reliability" / "results.xml")
    assert_installation_contracts(names)
    for runtime in ("openclaw", "hermes"):
        if f"test_both_channels_use_native_scoped_history_and_receipts[{runtime}]" not in names:
            raise ValueError("Missing native Slack/Telegram evidence for " + runtime)
    proof = json.loads((directory / "reliability" / "environment.json").read_text())
    if (
        proof.get("exit_code") != 0
        or proof.get("dirty") is not False
        or proof.get("revision") != manifest["source_revision"]
        or proof.get("manifest_sha256") != expected["manifest_sha256"]
        or proof.get("image_references") != manifest["images"][platform]
    ):
        raise ValueError("Native reliability evidence is not bound to the candidate images")


def assert_anonymous_images(manifest):
    """Query the registry directly with an empty credential store, never local image cache."""
    with tempfile.TemporaryDirectory(prefix="talos-public-registry-") as config:
        for platform in PLATFORMS:
            refs = set(manifest["images"][platform].values())
            for versions in manifest["runtime_versions"][platform].values():
                refs.update(versions.values())
            for reference in sorted(refs):
                result = json.loads(
                    subprocess.check_output(
                        [
                            "docker",
                            "--config",
                            config,
                            "manifest",
                            "inspect",
                            "--verbose",
                            reference,
                        ],
                        text=True,
                        timeout=120,
                    )
                )
                entries = result if isinstance(result, list) else [result]
                platforms = {
                    f"{item.get('os')}/{item.get('architecture')}"
                    for entry in entries
                    if (item := entry.get("Descriptor", {}).get("platform", {}))
                }
                if platform not in platforms:
                    raise ValueError(f"Anonymous image does not provide {platform}: {reference}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("candidate", "promote"))
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--acceptance-run")
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    repo, revision = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    if (
        not re.fullmatch(r"[a-f0-9]{40}", revision)
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Release workflows must run from main")
    bundle = args.directory / "bundle"
    manifest = candidate(repo, args.candidate_run, revision, bundle)
    if args.command == "candidate":
        return
    trusted_run(repo, args.acceptance_run, ACCEPTANCE_WORKFLOW, revision)
    for platform in PLATFORMS:
        name = "installation-acceptance-" + platform.split("/")[1]
        directory = args.directory / name
        download(repo, args.acceptance_run, name, directory)
        validate_evidence(directory, bundle / "manifest.json", args.candidate_run, platform)
    private = api(repo, "").get("private")
    if type(private) is not bool:
        raise ValueError("Cannot establish repository visibility before promotion")
    if not private:
        assert_anonymous_images(manifest)
    # No --clobber: an existing immutable release is never silently replaced.
    gh(
        "release",
        "create",
        manifest["version"],
        "--repo",
        repo,
        "--target",
        revision,
        "--prerelease",
        "--title",
        "Talos " + manifest["version"],
        "--notes",
        f"Development preview. Acceptance workflow: {args.acceptance_run}.",
        *[str(path) for path in sorted(bundle.iterdir()) if path.is_file()],
    )


if __name__ == "__main__":
    main()
