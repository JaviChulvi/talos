"""Installer contract tests use temporary files and a fake local Docker host."""

import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.management import installation as host

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/example/talos@sha256:" + "a" * 64
ASSETS = {
    "manifest.json": "{}\n",
    "compose.release.yaml": "services: {}\n",
    "release.env": f"TALOS_MANAGEMENT_AMD64={IMAGE}\nTALOS_MANAGEMENT_ARM64={IMAGE}\n",
    "talos": "#!/bin/bash\n",
    "images-amd64.txt": f"{IMAGE}\n",
    "images-arm64.txt": f"{IMAGE}\n",
}


def checksums(bundle):
    (bundle / "checksums.txt").write_text("".join(
        f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
        for name in ASSETS if (bundle / name).exists()
    ))


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "release"
    directory.mkdir()
    for name, content in ASSETS.items():
        (directory / name).write_text(content)
    checksums(directory)
    return directory


@pytest.fixture
def manifest():
    return {
        "version": "v0.1.0",
        "images": {"linux/arm64": {"platform": IMAGE, "management": IMAGE}},
        "runtime_versions": {"linux/arm64": {"openclaw": {"2026.9.6": IMAGE}}},
    }


@pytest.fixture
def installer(tmp_path, monkeypatch, manifest):
    directory = tmp_path / "install"
    directory.mkdir()
    monkeypatch.setattr(host, "checked_bundle", lambda _: manifest)
    monkeypatch.setattr(host.Installation, "preflight_resources", lambda _: None)
    monkeypatch.setattr(host.Installation, "preflight_ports", lambda _: None)
    monkeypatch.setattr(host.Installation, "record_release", lambda _: None, raising=False)
    monkeypatch.setattr(host.Installation, "ready", lambda _: None)
    monkeypatch.setattr(
        host.Installation, "maintenance_status", lambda _: {"maintenance": {"active": False}},
        raising=False,
    )
    return host.Installation(directory)


def install(instance, bundle):
    instance.install(
        bundle, platform="linux/arm64", host_os="macos", uid=os.getuid(), gid=os.getgid(),
        docker_socket="/var/run/docker.sock.raw", port=8000, domain=None, skip_admin=True,
    )


def test_interrupted_install_and_repeat_preserve_identity_secrets_and_resume(
    installer, bundle, monkeypatch,
):
    commands = []
    fail_migration = True

    def compose(_, *args, **kwargs):
        commands.append(args)
        if args[-1] == "migrate" and fail_migration:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="exists\n")

    monkeypatch.setattr(host.Installation, "compose", compose)
    with pytest.raises(subprocess.CalledProcessError):
        install(installer, bundle)
    identity = installer.state.copy()
    secret = (installer.directory / ".env").read_bytes()
    operation = installer.read("operation.json")
    assert operation["phase"] == "migrate"
    fail_migration = False
    resumed = host.Installation(installer.directory)
    install(resumed, bundle)
    assert resumed.read("operation.json")["operation_id"] == operation["operation_id"]
    assert resumed.read("operation.json")["phase"] == "complete"
    install(host.Installation(installer.directory), bundle)
    for key in ("installation_id", "compose_project", "release", "platform"):
        assert resumed.read("installation.json")[key] == identity[key]
    assert (installer.directory / ".env").read_bytes() == secret
    assert (installer.directory / ".env").stat().st_mode & 0o777 == 0o600
    assert not any(args[0] in {"build", "pull"} for args in commands)
    assert all("never" in args for args in commands if args[0] in {"up", "run"})
    assert all("--no-build" in args for args in commands if args[0] == "up")


@pytest.mark.parametrize("change", ["release", "architecture"])
def test_retry_refuses_release_or_architecture_changes_before_writing(
    installer, bundle, monkeypatch, manifest, change,
):
    monkeypatch.setattr(
        host.Installation, "compose", lambda *a, **kw: SimpleNamespace(stdout="exists")
    )
    install(installer, bundle)
    before = {p.name: p.read_bytes() for p in installer.directory.iterdir() if p.is_file()}
    if change == "release":
        manifest["version"] = "v0.2.0"
    else:
        installer.state["platform"] = "linux/amd64"
        installer.save()
        before["installation.json"] = (installer.directory / "installation.json").read_bytes()
    with pytest.raises(ValueError, match="preserves the selected release"):
        install(host.Installation(installer.directory), bundle)
    assert {p.name: p.read_bytes() for p in installer.directory.iterdir() if p.is_file()} == before


def test_retry_refuses_access_change_before_writing(installer, bundle, monkeypatch):
    monkeypatch.setattr(
        host.Installation, "compose", lambda *a, **kw: SimpleNamespace(stdout="exists")
    )
    install(installer, bundle)
    previous = (installer.directory / "installation.json").read_bytes()
    with pytest.raises(ValueError, match="saved domain and port"):
        installer.install(
            bundle, platform="linux/arm64", host_os="macos", uid=os.getuid(), gid=os.getgid(),
            docker_socket="/var/run/docker.sock.raw", port=9000, domain=None, skip_admin=True,
        )
    assert (installer.directory / "installation.json").read_bytes() == previous


def test_retry_refuses_changed_image_under_same_release_version(
    installer, bundle, monkeypatch, manifest,
):
    monkeypatch.setattr(
        host.Installation, "compose", lambda *a, **kw: SimpleNamespace(stdout="exists")
    )
    install(installer, bundle)
    original = (installer.directory / ".env").read_bytes()
    manifest["images"]["linux/arm64"]["platform"] = IMAGE.replace("a" * 64, "b" * 64)
    with pytest.raises(ValueError, match="manifest changed"):
        install(host.Installation(installer.directory), bundle)
    assert (installer.directory / ".env").read_bytes() == original



def test_developer_directory_is_never_adopted(installer, bundle):
    (installer.directory / "compose.yaml").write_text("services: {}")
    with pytest.raises(ValueError, match="not adopted"):
        install(installer, bundle)
    assert not (installer.directory / "installation.json").exists()


def test_failed_configuration_write_preserves_original_secret(installer, bundle, monkeypatch):
    monkeypatch.setattr(
        host.Installation, "compose", lambda *a, **kw: SimpleNamespace(stdout="exists")
    )
    install(installer, bundle)
    original = (installer.directory / ".env").read_bytes()
    path_open = Path.open

    class InterruptedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, content):
            self.stream.write(content[:20])
            self.stream.flush()
            raise OSError("Simulated disk write failure")

    @contextmanager
    def interrupted(path, mode="r", *args, **kwargs):
        with path_open(path, mode, *args, **kwargs) as stream:
            yield InterruptedWrite(stream) if (
                path.parent == installer.directory and path.name.startswith(".env") and "w" in mode
            ) else stream

    monkeypatch.setattr(Path, "open", interrupted)
    with pytest.raises(OSError, match="Simulated disk write failure"):
        installer.write_config(bundle)
    assert (installer.directory / ".env").read_bytes() == original


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_unknown_installation_format_makes_no_changes(tmp_path, version):
    path = tmp_path / "installation.json"
    original = json.dumps({"schema_version": version, "installation_id": "existing"})
    path.write_text(original)
    with pytest.raises(ValueError, match="Unsupported installation format"):
        host.Installation(tmp_path)
    assert path.read_text() == original


def test_checked_bundle_rejects_tampering_before_reading_manifest(bundle):
    (bundle / "manifest.json").write_text('{"images":"tampered"}')
    with pytest.raises(ValueError, match="(?i)checksum.*manifest.json"):
        host.checked_bundle(bundle)


def test_checked_bundle_requires_manifest_even_when_other_checksums_pass(bundle):
    (bundle / "manifest.json").unlink()
    checksums(bundle)
    with pytest.raises(ValueError, match="Release bundle is incomplete"):
        host.checked_bundle(bundle)


@pytest.mark.parametrize("entry", ["../manifest.json", "manifest.json\n", "manifest.json"])
def test_checked_bundle_rejects_invalid_or_duplicate_entries(bundle, entry):
    path = bundle / "checksums.txt"
    with path.open("a") as stream:
        stream.write("a" * 64 + "  " + entry + "\n")
    with pytest.raises(ValueError, match="Invalid release checksums"):
        host.checked_bundle(bundle)


@pytest.mark.parametrize("resource", ["container", "volume", "network"])
def test_project_resources_with_foreign_installation_labels_are_refused(
    tmp_path, monkeypatch, resource,
):
    instance = host.Installation(tmp_path)
    instance.state = {"installation_id": "owned", "compose_project": "talos-test"}

    def run(*args, **kwargs):
        current = "container" if args[1] == "ps" else args[1]
        if "inspect" in args:
            item = {"Config": {"Labels": {}}} if resource == "container" else {"Labels": {}}
            return SimpleNamespace(stdout=json.dumps([item]))
        return SimpleNamespace(stdout="foreign" if current == resource else "")

    monkeypatch.setattr(host, "run", run)
    with pytest.raises(ValueError, match=f"Foreign {resource}"):
        instance.preflight_resources()


def test_explicit_volume_name_cannot_adopt_unlabelled_foreign_data(tmp_path, monkeypatch):
    instance = host.Installation(tmp_path)
    instance.state = {"installation_id": "owned", "compose_project": "talos-test"}
    monkeypatch.setattr(host, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='[{"Labels": {}}]'))
    with pytest.raises(ValueError, match="Foreign volume talos-test_postgres-data"):
        instance.preflight_resources()


def test_occupied_port_removes_probe_and_closes_docker_client(tmp_path, monkeypatch, manifest):
    instance = host.Installation(tmp_path)
    instance.state = {
        "installation_id": "owned", "compose_project": "talos-test", "platform": "linux/arm64",
        "access": {"mode": "local", "port": 8000},
    }
    instance.manifest = manifest
    calls = []

    def start():
        raise host.docker.errors.APIError("Port already allocated")

    def create(image, **kwargs):
        calls.append(("create", image, kwargs))
        return SimpleNamespace(start=start, remove=lambda **kw: calls.append(("remove", kw)))

    client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda **kw: [], create=create),
        close=lambda: calls.append(("close",)),
    )
    monkeypatch.setattr(host.docker, "from_env", lambda: client)
    with pytest.raises(ValueError, match="Host port 8000 is unavailable"):
        instance.preflight_ports()
    assert calls[0][1] == IMAGE
    assert calls[0][2]["ports"] == {"9999/tcp": ("127.0.0.1", 8000)}
    assert calls[-2:] == [("remove", {"force": True}), ("close",)]


def test_owned_port_is_reusable_on_install_retry(tmp_path, monkeypatch, manifest):
    instance = host.Installation(tmp_path)
    instance.state = {
        "installation_id": "owned", "compose_project": "talos-test", "platform": "linux/arm64",
        "access": {"mode": "local", "port": 8000},
    }
    instance.manifest = manifest
    owned = SimpleNamespace(attrs={
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}},
    })

    def list_owned(**kwargs):
        assert "io.talos.installation=owned" in kwargs["filters"]["label"]
        return [owned]

    client = SimpleNamespace(containers=SimpleNamespace(
        list=list_owned, create=lambda *a, **kw: pytest.fail("Probed an owned port"),
    ), close=lambda: None)
    monkeypatch.setattr(host.docker, "from_env", lambda: client)
    instance.preflight_ports()


@pytest.fixture
def launcher(tmp_path, bundle):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "docker": r'''#!/bin/bash
printf '%s\n' "$*" >> "$MOCK_LOG"
case "$*" in
  'context show') echo default ;;
  'context inspect '*) echo "${MOCK_ENDPOINT:-unix:///var/run/docker.sock}" ;;
  'info') exit "${MOCK_DOCKER_EXIT:-0}" ;;
  'info --format {{json .SecurityOptions}}') echo "${MOCK_SECURITY:-[]}" ;;
  'info --format {{.OperatingSystem}}') echo 'Docker Desktop' ;;
  'info --format {{.Architecture}}') echo "${MOCK_SERVER_ARCH:-aarch64}" ;;
  'info --format {{.MemTotal}}') echo 8589934592 ;;
  'info --format {{.NCPU}}') echo 4 ;;
  'pull '*) exit 0 ;;
  'run '*) exit 0 ;;
  *) exit 9 ;;
esac
''',
        "uname": '#!/bin/bash\nif [ "$1" = -m ]; then echo "${MOCK_ARCH:-arm64}"; '
                 'else echo "${MOCK_OS:-Darwin}"; fi\n',
        "df": '#!/bin/bash\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\n'
              'fixture 90000000 1 80000000 1%% /fixture\\n"\n',
    }
    for name, content in scripts.items():
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)
    log = tmp_path / "docker.log"
    directory = tmp_path / "managed"
    env = {key: value for key, value in os.environ.items()
           if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "TALOS_DIRECTORY"}}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", MOCK_LOG=str(log))

    def invoke(**changes):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "deploy/talos"), "install", "--directory", str(directory),
             "--bundle", str(bundle), "--skip-admin"],
            env={**env, **changes}, capture_output=True, text=True, timeout=10,
        )
        return result, log.read_text() if log.exists() else "", directory

    return invoke


@pytest.mark.parametrize(("overrides", "message"), [
    ({"MOCK_ENDPOINT": "ssh://remote"}, "Only a local Docker context"),
    ({"MOCK_ENDPOINT": "tcp://127.0.0.1:2375"}, "Only a local Docker context"),
    ({"DOCKER_HOST": "unix:///other.sock"}, "Unset DOCKER_HOST/DOCKER_CONTEXT"),
    ({"DOCKER_CONTEXT": "remote"}, "Unset DOCKER_HOST/DOCKER_CONTEXT"),
    ({"MOCK_SECURITY": '["name=rootless"]'}, "Rootless Docker"),
    ({"MOCK_ARCH": "x86_64"}, "Supported hosts"),
    ({"MOCK_SERVER_ARCH": "x86_64"}, "Docker architecture does not match"),
    ({"MOCK_DOCKER_EXIT": "1"}, "Docker is unavailable"),
])
def test_launcher_rejects_unsupported_hosts_before_installation(launcher, overrides, message):
    result, commands, directory = launcher(**overrides)
    assert result.returncode != 0
    assert message in result.stderr
    assert not directory.exists()
    assert not any(line.startswith(("pull ", "run ")) for line in commands.splitlines())


def test_launcher_uses_host_digest_pulls_and_never_builds(launcher):
    result, commands, directory = launcher()
    assert result.returncode == 0, result.stderr
    assert f"pull --platform linux/arm64 {IMAGE}" in commands
    assert f"{IMAGE} install --directory {directory}" in commands
    assert "--pull never" in commands
    assert "build" not in commands
    assert not (directory / ".talos-lock").exists()


def test_launcher_rejects_unpinned_pull_list_before_docker_pull(launcher, bundle):
    (bundle / "images-arm64.txt").write_text("ghcr.io/example/talos:latest\n")
    checksums(bundle)
    result, commands, _ = launcher()
    assert result.returncode != 0
    assert "Invalid image in release pull list" in result.stderr
    assert not any(line.startswith(("pull ", "run ")) for line in commands.splitlines())
