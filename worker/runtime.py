"""Server-owned OpenClaw launch contract; callers cannot supply Docker settings."""

import io
import json
import tarfile

from docker.errors import NotFound

from .openclaw import SCOPES, DeviceIdentity

IMAGE = (
    "ghcr.io/openclaw/openclaw@sha256:"
    "0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15"
)
STATE_PATH = "/home/node/.openclaw"
CONFIG_PATH = "/etc/talos/openclaw.json"


def runtime_config(control_token: str, agent_token: str, gateway_url: str) -> dict:
    return {
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "port": 18789,
            "auth": {"mode": "token", "token": control_token},
            "controlUi": {"enabled": False},
        },
        "agents": {
            "defaults": {
                "workspace": STATE_PATH + "/workspace",
                "model": {"primary": "foundation/fixture"},
                "models": {"foundation/fixture": {}},
                "heartbeat": {"every": "0m"},
            }
        },
        "models": {
            "providers": {
                "foundation": {
                    "baseUrl": gateway_url + "/v1",
                    "apiKey": agent_token,
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": "fixture",
                            "name": "Talos diagnostic fixture",
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": 32000,
                            "maxTokens": 1024,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            }
        },
        "tools": {"profile": "minimal", "deny": ["*"]},
        "plugins": {"enabled": False},
        "browser": {"enabled": False},
    }


def launch_options(name: str, volume: str, network: str, config_volume: str, labels: dict) -> dict:
    return {
        "image": IMAGE,
        "name": name,
        "command": ["node", "openclaw.mjs", "gateway", "--bind", "lan", "--port", "18789"],
        "environment": {
            "OPENCLAW_CONFIG_PATH": CONFIG_PATH,
            "OPENCLAW_STATE_DIR": STATE_PATH,
            "HOME": "/home/node",
        },
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "mem_limit": "2g",
        "nano_cpus": 2_000_000_000,
        "pids_limit": 256,
        "tmpfs": {
            "/tmp": "rw,nosuid,nodev,size=256m,mode=1777",
            "/home/node/.cache": "rw,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=700",
        },
        "volumes": {
            volume: {"bind": STATE_PATH, "mode": "rw"},
            config_volume: {"bind": "/etc/talos", "mode": "ro"},
        },
        "network": network,
        "labels": labels,
        "detach": True,
    }


def prepare_volumes(client, state_name: str, config_name: str, config: dict, labels: dict):
    """Seed approved configuration and state ownership without host bind mounts."""
    for name in (state_name, config_name):
        try:
            volume = client.volumes.get(name)
            if volume.attrs.get("Labels") != labels:
                raise RuntimeError("Volume ownership conflict")
        except NotFound:
            client.volumes.create(name=name, labels=labels)
    try:
        previous = client.containers.get(config_name + "-init")
    except NotFound:
        pass
    else:
        if previous.labels != labels:
            raise RuntimeError("Initializer ownership conflict")
        # The single worker may have died during this fixed, idempotent bootstrap.
        previous.remove(force=True)
    initializer = client.containers.create(
        IMAGE,
        name=config_name + "-init",
        user="0:0",
        network_mode="none",
        entrypoint=["node", "-e"],
        command=[
            "const fs=require('fs');fs.chownSync('/state',1000,1000);fs.chmodSync('/state',448);"
        ],
        volumes={
            state_name: {"bind": "/state", "mode": "rw"},
            config_name: {"bind": "/config", "mode": "rw"},
        },
        read_only=True,
        cap_drop=["ALL"],
        cap_add=["CHOWN", "FOWNER"],
        security_opt=["no-new-privileges:true"],
        labels=labels,
        mem_limit="128m",
        pids_limit=32,
    )
    try:
        data = io.BytesIO()
        payload = json.dumps(config).encode()
        with tarfile.open(fileobj=data, mode="w") as archive:
            entry = tarfile.TarInfo("openclaw.json")
            entry.size, entry.mode, entry.uid, entry.gid = len(payload), 0o400, 1000, 1000
            archive.addfile(entry, io.BytesIO(payload))
        initializer.put_archive("/config", data.getvalue())
        initializer.start()
        if initializer.wait(timeout=30)["StatusCode"]:
            raise RuntimeError("OpenClaw private volume initialization failed")
    finally:
        initializer.remove(force=True)


def approve_device(container, identity: DeviceIdentity):
    """Approve exactly our key's pending request through the bundled local CLI."""

    def cli(*args):
        result = container.exec_run(["node", "openclaw.mjs", "devices", *args, "--json"])
        if result.exit_code:
            raise RuntimeError("OpenClaw device CLI failed")
        output = result.output.decode()
        # The CLI may print operational log lines before its JSON result.
        for index, char in enumerate(output):
            if char == "{":
                try:
                    return json.loads(output[index:])
                except json.JSONDecodeError:
                    pass
        raise RuntimeError("OpenClaw device CLI returned no JSON object")

    data = cli("list")
    matching = [
        item
        for item in data.get("pending", [])
        if item.get("deviceId") == identity.device_id
        and item.get("publicKey") == identity.public_key
        and item.get("role") == "operator"
        and set(item.get("scopes", [])) == set(SCOPES)
    ]
    if len(matching) != 1:
        raise RuntimeError("Expected exactly one matching worker pairing request")
    cli("approve", matching[0]["requestId"])
