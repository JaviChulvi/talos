import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import docker
import pytest

from tests import reliability


@pytest.fixture
def harness(monkeypatch, tmp_path):
    client = Mock()
    client.networks.list.return_value = []
    client.networks.create.return_value = SimpleNamespace(name="proof-network")
    client.containers.list.return_value = []
    client.volumes.list.return_value = []
    database, runner = Mock(), Mock()
    database.name = "proof-db"
    database.exec_run.return_value = SimpleNamespace(exit_code=0)
    runner.logs.return_value = [b"passed\n"]
    runner.wait.return_value = {"StatusCode": 0}
    client.containers.run.side_effect = [database, runner]
    # Simulate another checkout publishing the shared tags after our build.
    client.images.get.return_value = SimpleNamespace(id="sha256:other-checkout")

    def build(command, **kwargs):
        target = command[command.index("--target") + 1]
        if "--iidfile" in command:
            image_file = Path(command[command.index("--iidfile") + 1])
            image_file.write_text("sha256:our-" + target)

    monkeypatch.setattr(reliability, "ROOT", tmp_path)
    monkeypatch.setattr(reliability.docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(
        reliability.subprocess, "check_output", Mock(side_effect=["our-revision\n", b""])
    )
    monkeypatch.setattr(reliability.subprocess, "run", build)
    return SimpleNamespace(client=client, root=tmp_path)


def environment(harness):
    return json.loads(next(harness.root.glob(".data/reliability/*/environment.json")).read_text())


def test_runner_uses_its_build_ids_even_if_another_checkout_overwrites_shared_tags(harness):
    assert reliability.main() == 0
    call = harness.client.containers.run.call_args_list[1]
    assert call.args[0] == "sha256:our-verification"
    assert call.kwargs["environment"]["TALOS_TEST_OPENCLAW_IMAGE"] == "sha256:our-native-runtime"
    assert call.kwargs["environment"]["TALOS_TEST_HERMES_IMAGE"] == "sha256:our-hermes-runtime"
    report = environment(harness)
    assert report["revision"] == "our-revision"
    assert report["images"] == {
        "runner": "sha256:our-verification",
        "openclaw": "sha256:our-native-runtime",
        "hermes": "sha256:our-hermes-runtime",
    }


def test_network_allocation_retries_when_another_run_claims_the_selected_subnet(harness):
    harness.client.networks.create.side_effect = [
        docker.errors.APIError("bad request", explanation="Pool overlaps with other one"),
        SimpleNamespace(name="proof-network"),
    ]
    assert reliability.main() == 0
    assert [
        call.kwargs["ipam"]["Config"][0]["Subnet"]
        for call in harness.client.networks.create.call_args_list
    ] == ["10.252.1.0/24", "10.252.2.0/24"]
    assert environment(harness)["exit_code"] == 0


def test_unrelated_network_errors_are_reported_without_retrying(harness):
    harness.client.networks.create.side_effect = docker.errors.APIError(
        "forbidden", explanation="network creation is forbidden"
    )
    with pytest.raises(docker.errors.APIError, match="forbidden"):
        reliability.main()
    harness.client.networks.create.assert_called_once()
    harness.client.containers.run.assert_not_called()
    assert environment(harness)["exit_code"] == 1
    harness.client.close.assert_called_once()


def test_prebuilt_release_uses_exact_native_artifacts_without_building(harness, monkeypatch):
    from tests.unit.test_release import release_manifest

    manifest = release_manifest()
    path = harness.root / "manifest.json"
    path.write_text(json.dumps(manifest))
    harness.client.info.return_value = {"Architecture": "aarch64"}
    harness.client.images.get.side_effect = lambda ref: SimpleNamespace(
        id="id:" + ref,
        attrs={"Architecture": "arm64"},
        labels={"org.opencontainers.image.revision": manifest["source_revision"]},
    )
    commands = Mock()
    monkeypatch.setattr(reliability.subprocess, "run", commands)
    assert reliability.main(["--manifest", str(path)]) == 0
    refs = manifest["images"]["linux/arm64"]
    assert all(call.args[0][:2] == ["docker", "pull"] for call in commands.call_args_list)
    assert harness.client.containers.run.call_args_list[0].args[0] == refs["postgres"]
    assert harness.client.containers.run.call_args_list[1].args[0] == "id:" + refs["verification"]
    report = environment(harness)
    assert report["image_references"] == refs
    assert report["revision"] == manifest["source_revision"]
    assert report["dirty"] is False
    assert report["platform"] == "linux/arm64"


@pytest.mark.parametrize("architecture,revision", [("amd64", "a" * 40), ("arm64", "b" * 40)])
def test_prebuilt_release_rejects_emulation_and_wrong_source(
    harness, monkeypatch, architecture, revision
):
    from tests.unit.test_release import release_manifest

    path = harness.root / "manifest.json"
    path.write_text(json.dumps(release_manifest()))
    harness.client.info.return_value = {"Architecture": "arm64"}
    harness.client.images.get.return_value = SimpleNamespace(
        id="image",
        attrs={"Architecture": architecture},
        labels={"org.opencontainers.image.revision": revision},
    )
    monkeypatch.setattr(reliability.subprocess, "run", Mock())
    with pytest.raises(ValueError, match="native images|revision"):
        reliability.main(["--manifest", str(path)])
    harness.client.containers.run.assert_not_called()
