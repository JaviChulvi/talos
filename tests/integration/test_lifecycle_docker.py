"""Focused real-Docker provisioning/cleanup check; no browser or application journey."""

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import docker
import pytest
import test_agents
from docker.errors import NotFound

from worker.lifecycle import Worker
from worker.runtime import IMAGE

database_engine = test_agents.database_engine
session_maker = test_agents.session_maker
client = test_agents.client
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]


def test_real_private_network_and_owned_cleanup(client, session_maker, monkeypatch, tmp_path):
    project = "f4check-" + uuid4().hex[:12]
    docker_client = docker.from_env(timeout=30)
    settings = SimpleNamespace(
        worker_state_dir=tmp_path,
        compose_project=project,
        installation_id="test",
        worker_container=project + "-worker",
        readiness_timeout_seconds=10,
    )
    monkeypatch.setattr("worker.lifecycle.get_settings", lambda: settings)
    worker = Worker(sessions=session_maker, client=docker_client)
    platform = []
    state = network = None
    control = None
    try:
        docker_client.images.get(IMAGE)
        control = docker_client.networks.create(project + "-control", internal=True)
        for service in ("worker", "gateway"):
            container = docker_client.containers.run(
                IMAGE,
                name=project + "-" + service,
                entrypoint=["node", "-e"],
                command=["setInterval(()=>{},1000)"],
                network=control.name,
                labels=worker.service_labels(service),
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="128m",
                pids_limit=32,
                detach=True,
            )
            platform.append(container)
        created = test_agents.create(client).json()
        agent_id = UUID(created["agent_id"])
        state, network = worker.names(agent_id)
        if os.environ.get("TALOS_TEST_TMPFS_VOLUMES") == "1":
            docker_client.volumes.create(
                name=state,
                labels=worker.labels(agent_id),
                driver_opts={"type": "tmpfs", "device": "tmpfs", "o": "size=16m"},
            )
        assert worker.process_one()
        assert client.get(f"/api/v1/agents/{agent_id}").json()["observed_state"] == "stopped"
        resource = docker_client.networks.get(network)
        assert resource.attrs["Internal"] is True
        assert set(resource.attrs["Containers"]) == {container.id for container in platform}
        assert docker_client.volumes.get(state).attrs["Labels"] == worker.labels(agent_id)
        assert (
            client.delete(
                f"/api/v1/agents/{agent_id}", headers={"Idempotency-Key": "delete-real"}
            ).status_code
            == 202
        )
        assert worker.process_one()
        assert client.get(f"/api/v1/agents/{agent_id}").status_code == 404
        with pytest.raises(NotFound):
            docker_client.networks.get(network)
        with pytest.raises(NotFound):
            docker_client.volumes.get(state)
        for container in platform:
            container.reload()
            assert container.status == "running"
    finally:
        for container in platform:
            container.remove(force=True)
        if network:
            try:
                docker_client.networks.get(network).remove()
            except NotFound:
                pass
        if state:
            try:
                docker_client.volumes.get(state).remove()
            except NotFound:
                pass
        if control:
            control.remove()
        docker_client.close()


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_native_configuration_is_seeded_once_and_remains_private(runtime_kind, monkeypatch):
    from worker.runtime import STATE_PATH, native_config, prepare_volumes

    uid = "10000:10000" if runtime_kind == "hermes" else "1000:1000"
    mount = "/opt/data" if runtime_kind == "hermes" else STATE_PATH
    filename = "config.yaml" if runtime_kind == "hermes" else "openclaw.json"
    payload = native_config(runtime_kind, "synthetic-password-hash")
    client = docker.from_env(timeout=30)
    prefix = "talos-native-volume-test-" + uuid4().hex
    state, config = prefix + "-state", prefix + "-config"
    labels = {"io.talos.test": prefix}
    create_container = client.containers.create

    def interrupted_initializer(_collection, *args, **kwargs):
        kwargs["command"][0] = (
            "const chown=require('fs').chownSync;"
            "require('fs').chownSync=(path,...args)=>{"
            "if(path!=='/state')throw Error('interrupted after copy');return chown(path,...args);};"
            + kwargs["command"][0]
        )
        return create_container(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(type(client.containers), "create", interrupted_initializer)
            with pytest.raises(RuntimeError, match="private volume initialization failed"):
                prepare_volumes(
                    client, state, config, payload, labels, native=True, runtime_kind=runtime_kind
                )
        # Retry must replace the incomplete staged file, then retain subsequent user edits.
        prepare_volumes(
            client, state, config, payload, labels, native=True, runtime_kind=runtime_kind
        )
        script = (
            f"const fs=require('fs'); const p='{mount}/{filename}';"
            "const c=JSON.parse(fs.readFileSync(p));"
            "c.talosTestMarker='persistent';fs.writeFileSync(p,JSON.stringify(c));"
        )
        options = dict(
            entrypoint=["node", "-e"],
            user=uid,
            read_only=True,
            cap_drop=["ALL"],
            network_mode="none",
            remove=True,
            volumes={state: {"bind": mount, "mode": "rw"}},
        )
        client.containers.run(IMAGE, command=[script], **options)
        prepare_volumes(
            client, state, config, payload, labels, native=True, runtime_kind=runtime_kind
        )
        check = (
            f"const fs=require('fs');const p='{mount}/{filename}';"
            "const c=JSON.parse(fs.readFileSync(p));"
            "if(c.talosTestMarker!=='persistent')process.exit(1);"
            "if((fs.statSync(p).mode&511)!==384)process.exit(2);"
        )
        client.containers.run(IMAGE, command=[check], **options)
    finally:
        for name in (state, config):
            try:
                client.volumes.get(name).remove()
            except NotFound:
                pass
        client.close()


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_role_config_validates_in_pinned_native_image_and_preserves_state(runtime_kind):
    import json

    from backend.app.capabilities import compile_permissions
    from worker.runtime import (
        NATIVE_IMAGES,
        STATE_PATH,
        apply_native_permissions,
        native_config,
        prepare_volumes,
    )

    client = docker.from_env(timeout=120)
    prefix = "talos-role-config-" + uuid4().hex
    labels = {"io.talos.test": prefix}
    incarnation = SimpleNamespace(
        config_volume=prefix + "-config",
        image_digest=client.images.get(NATIVE_IMAGES[runtime_kind]).id,
    )
    payload = native_config(runtime_kind, "synthetic-password-hash")
    if runtime_kind == "hermes":
        payload["model"] = {
            "provider": "custom",
            "base_url": "http://fixture/v1",
            "default": "fixture",
        }
        payload["mcp_servers"] = {"deferred-test": {"command": "false"}}
    else:
        payload["models"] = {
            "providers": {
                "fixture": {
                    "baseUrl": "http://fixture/v1",
                    "apiKey": "synthetic-key",
                    "api": "openai-completions",
                    "models": [{"id": "fixture", "name": "Fixture"}],
                }
            }
        }
    try:
        prepare_volumes(
            client,
            prefix,
            incarnation.config_volume,
            payload,
            labels,
            native=True,
            runtime_kind=runtime_kind,
        )
        # A killed worker leaves this named initializer behind. Docker adds the
        # pinned image's own labels, which are not an ownership conflict.
        orphan = client.containers.create(
            incarnation.image_digest,
            name=incarnation.config_volume + "-permissions",
            entrypoint=["true"],
            labels=labels,
            network_mode="none",
        )
        assert orphan.labels != labels
        for capabilities in (
            [],
            ["web_research"],
            ["workspace_files"],
            ["terminal_execution"],
            ["web_research", "workspace_files", "terminal_execution"],
        ):
            apply_native_permissions(
                client,
                prefix,
                incarnation,
                runtime_kind,
                compile_permissions(capabilities, runtime_kind),
                labels,
            )
        mount = "/opt/data" if runtime_kind == "hermes" else STATE_PATH
        if runtime_kind == "hermes":
            command = [
                "python",
                "-c",
                "import yaml,json;print(json.dumps(yaml.safe_load(open('/opt/data/config.yaml'))))",
            ]
        else:
            command = [
                "node",
                "-e",
                f"console.log(require('fs').readFileSync('{mount}/openclaw.json','utf8'))",
            ]
        raw = client.containers.run(
            incarnation.image_digest,
            entrypoint=command,
            network_mode="none",
            volumes={prefix: {"bind": mount, "mode": "ro"}},
            remove=True,
        )
        current = json.loads(raw)
        for key, value in payload.items():
            if key not in {"tools", "agents"}:
                assert current[key] == value
    finally:
        for name in (prefix, incarnation.config_volume):
            client.volumes.get(name).remove()
        client.close()
