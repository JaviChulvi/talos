"""Exercise failure boundaries through the real launcher and installation owners."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.management import backup, installation
from tests.unit.test_installation import bundle as bundle
from tests.unit.test_installation import install
from tests.unit.test_installation import installer as installer
from tests.unit.test_installation import launcher as launcher
from tests.unit.test_installation import manifest as manifest


@pytest.mark.parametrize(
    "resources",
    [
        {"MOCK_MEMORY": "1073741824"},
        {"MOCK_CPUS": "1"},
        {"MOCK_FREE_KB": "1"},
    ],
)
def test_launcher_rejects_insufficient_resources(launcher, resources):
    result, commands, _ = launcher(**resources)
    assert result.returncode != 0
    assert "RAM and 4 CPUs" in result.stderr or "free installation disk" in result.stderr
    assert not any(line.startswith(("pull ", "run ")) for line in commands.splitlines())


def test_launcher_registry_credentials_failure_prevents_installation(launcher):
    result, commands, directory = launcher(MOCK_PULL_EXIT="1")
    assert result.returncode != 0
    assert "Check registry availability/access" in result.stderr
    assert not any(line.startswith("run ") for line in commands.splitlines())
    assert not (directory / "installation.json").exists()
    assert not (directory / ".talos-lock").exists()


def test_interrupted_digest_pull_does_not_start_services_and_retry_preserves_data(launcher):
    result, commands, directory = launcher(MOCK_PULL_EXIT="137")
    assert result.returncode != 0
    assert not any(line.startswith("run ") for line in commands.splitlines())
    # The host-side failure cannot erase any preexisting persisted installation bytes.
    identity = directory / "installation.json"
    identity.write_text('{"installation_id":"saved"}')
    result, commands, _ = launcher()
    assert result.returncode == 0, result.stderr
    assert identity.read_text() == '{"installation_id":"saved"}'
    assert any(line.startswith("run ") for line in commands.splitlines())
    assert not (directory / ".talos-lock").exists()


def test_interrupted_private_download_is_retryable_without_changing_data(
    launcher, tmp_path, bundle
):
    gh = tmp_path / "bin" / "gh"
    gh.write_text(r"""#!/bin/bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = --dir ]; then destination=$2; shift 2; else shift; fi
done
if [ "${MOCK_DOWNLOAD_INTERRUPT:-0}" = 1 ]; then
  printf partial > "$destination/manifest.json"
  exit 74
fi
cp "$MOCK_DOWNLOAD_SOURCE/"* "$destination/"
""")
    gh.chmod(0o755)
    result, commands, directory = launcher(
        "--version",
        "0.1.0-beta.1",
        MOCK_DOWNLOAD_INTERRUPT="1",
        MOCK_DOWNLOAD_SOURCE=str(bundle),
    )
    assert result.returncode != 0
    assert "Release download failed" in result.stderr
    assert not any(line.startswith(("pull ", "run ")) for line in commands.splitlines())
    identity = directory / "installation.json"
    identity.write_text('{"installation_id":"saved"}')
    result, commands, _ = launcher("--version", "0.1.0-beta.1", MOCK_DOWNLOAD_SOURCE=str(bundle))
    assert result.returncode == 0, result.stderr
    assert identity.read_text() == '{"installation_id":"saved"}'
    assert len(list((directory / ".downloads").glob("release.*"))) == 2
    assert not (directory / ".talos-lock").exists()


def test_interrupted_bootstrap_resumes_same_installation_without_rotating_secrets(
    installer, bundle, monkeypatch
):
    interrupt = True
    bootstrapped = False

    def compose(_instance, *args, **kwargs):
        nonlocal bootstrapped
        if args[-1] == "bootstrap":
            if interrupt:
                raise subprocess.CalledProcessError(130, args)
            bootstrapped = True
        return SimpleNamespace(stdout="exists" if bootstrapped else "missing")

    monkeypatch.setattr(installation.Installation, "compose", compose)
    with pytest.raises(subprocess.CalledProcessError):
        install(installer, bundle, skip_admin=False)
    before = installer.read("installation.json")
    secret = (installer.directory / ".env").read_bytes()
    operation = installer.read("operation.json")
    assert operation["phase"] == "bootstrap"
    interrupt = False
    resumed = installation.Installation(installer.directory)
    install(resumed, bundle, skip_admin=False)
    assert resumed.read("operation.json")["operation_id"] == operation["operation_id"]
    assert resumed.read("operation.json")["phase"] == "complete"
    assert resumed.state["installation_id"] == before["installation_id"]
    assert (resumed.directory / ".env").read_bytes() == secret


def test_backup_inventory_rejects_missing_required_resource():
    labels = {"com.docker.compose.project": "installation", "io.talos.installation": "owner"}
    database = SimpleNamespace(
        id="db",
        labels=labels,
        attrs={
            "Mounts": [
                {"Type": "volume", "Name": "installation_postgres-data"},
            ]
        },
    )
    client = Mock()
    client.containers.list.return_value = [database]
    client.volumes.list.return_value = [
        SimpleNamespace(
            name="installation_postgres-data",
            attrs={"Labels": labels, "Driver": "local"},
        )
    ]
    host = SimpleNamespace(
        state={"compose_project": "installation", "installation_id": "owner"},
        compose=lambda *a, **kw: SimpleNamespace(
            stdout=json.dumps(
                {
                    "volumes": {
                        "postgres-data": {},
                        "provider-secrets": {},
                    }
                }
            )
        ),
    )
    with pytest.raises(backup.BackupError, match="required installation volume is missing"):
        backup._inventory(client, host, "db")
    client.volumes.create.assert_not_called()
    client.containers.run.assert_not_called()
