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


def test_native_configuration_is_seeded_once_and_remains_private(monkeypatch):
    from worker.runtime import STATE_PATH, native_config, prepare_volumes

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
                prepare_volumes(client, state, config, native_config(), labels, native=True)
        # Retry must replace the incomplete staged file, then retain subsequent user edits.
        prepare_volumes(client, state, config, native_config(), labels, native=True)
        script = (
            "const fs=require('fs'); const p=process.env.HOME+'/.openclaw/openclaw.json';"
            "const c=JSON.parse(fs.readFileSync(p));"
            "if(c.tools.web.search.provider!=='parallel-free')process.exit(1);"
            "c.agents.defaults.heartbeat={every:'0m'};fs.writeFileSync(p,JSON.stringify(c));"
        )
        options = dict(
            entrypoint=["node", "-e"],
            user="1000:1000",
            read_only=True,
            cap_drop=["ALL"],
            network_mode="none",
            remove=True,
            volumes={state: {"bind": STATE_PATH, "mode": "rw"}},
        )
        client.containers.run(IMAGE, command=[script], **options)
        prepare_volumes(client, state, config, native_config(), labels, native=True)
        check = (
            "const fs=require('fs');const p=process.env.HOME+'/.openclaw/openclaw.json';"
            "const c=JSON.parse(fs.readFileSync(p));"
            "if(c.agents.defaults.heartbeat.every!=='0m')process.exit(1);"
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
