"""Server-owned runtime launch contract; callers cannot supply Docker settings."""

import hashlib
import io
import json
import tarfile

from docker.errors import NotFound

from .openclaw import SCOPES, DeviceIdentity

IMAGE = (
    "ghcr.io/openclaw/openclaw@sha256:"
    "0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15"
)
NATIVE_IMAGE = "talos-openclaw-native:local"
NATIVE_IMAGES = {"openclaw": NATIVE_IMAGE, "hermes": "talos-hermes-native:local"}
STATE_PATH = "/home/node/.openclaw"
CONFIG_PATH = "/etc/talos/openclaw.json"


def runtime_config(
    control_token: str, agent_token: str, gateway_url: str, model_route: str = "fixture"
) -> dict:
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
                "model": {"primary": f"foundation/{model_route}"},
                "models": {f"foundation/{model_route}": {}},
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
                            "id": model_route,
                            "name": "Talos selected model"
                            if model_route == "default"
                            else "Talos diagnostic fixture",
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


def native_config(runtime_kind="openclaw", password_hash=None) -> dict:
    """Seed once; subsequent configuration belongs to the native runtime user."""
    if runtime_kind == "hermes":
        if not password_hash:
            raise ValueError("Hermes requires a dashboard password hash")
        return {
            "terminal": {"backend": "local", "cwd": "/opt/data/workspace"},
            "web": {"backend": "parallel"},
            "dashboard": {"basic_auth": {"username": "talos", "password_hash": password_hash}},
        }
    return {
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "port": 18789,
            "auth": {"mode": "token", "token": "${OPENCLAW_GATEWAY_TOKEN}"},
            "controlUi": {"enabled": True, "allowedOrigins": ["${TALOS_CONTROL_ORIGIN}"]},
        },
        "agents": {"defaults": {"workspace": STATE_PATH + "/workspace"}},
        "tools": {
            "profile": "full",
            "web": {"search": {"provider": "parallel-free"}, "fetch": {"useTrustedEnvProxy": True}},
        },
        "plugins": {
            "load": {"paths": ["/opt/talos-plugins/node_modules/@openclaw/parallel-plugin"]},
            "entries": {"parallel": {"enabled": True}},
        },
    }


def model_profile(config: dict, model_id: str, capabilities: dict) -> tuple[dict, str]:
    """Only the trusted worker replaces inference fields in the launch contract."""
    if model_id == "fixture":
        return config, "default"
    if not capabilities.get("context_length"):
        raise ValueError("Apply the model selection to refresh its capabilities")
    identifier = (
        "talos-"
        + hashlib.sha256(
            json.dumps({"model": model_id, "capabilities": capabilities}, sort_keys=True).encode()
        ).hexdigest()[:20]
    )
    provider = config["models"]["providers"]["foundation"]
    provider["models"] = [
        {
            "id": identifier,
            "name": capabilities.get("name", model_id),
            "reasoning": bool(capabilities.get("reasoning"))
            or bool(
                {"reasoning", "reasoning_effort"}.intersection(
                    capabilities.get("supported_parameters", [])
                )
            ),
            "input": ["text"],
            "contextWindow": capabilities["context_length"],
            **(
                {"maxTokens": capabilities["max_completion_tokens"]}
                if capabilities.get("max_completion_tokens")
                else {}
            ),
        }
    ]
    config["agents"]["defaults"]["model"] = {"primary": f"foundation/{identifier}"}
    config["agents"]["defaults"]["models"] = {f"foundation/{identifier}": {}}
    return config, identifier


def launch_options(
    name: str,
    volume: str,
    network: str,
    config_volume: str,
    labels: dict,
    *,
    native_image: str | None = None,
    runtime_kind: str = "openclaw",
    control_token: str = "",
    control_origin: str = "",
) -> dict:
    options = {
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

    if native_image:
        options["image"] = native_image
        options["environment"].update(
            {
                "OPENCLAW_CONFIG_PATH": STATE_PATH + "/openclaw.json",
                "OPENCLAW_GATEWAY_TOKEN": control_token,
                "TALOS_CONTROL_ORIGIN": control_origin,
                "HTTP_PROXY": "http://talos-egress:3128",
                "HTTPS_PROXY": "http://talos-egress:3128",
                "http_proxy": "http://talos-egress:3128",
                "https_proxy": "http://talos-egress:3128",
                "NO_PROXY": "localhost,127.0.0.1,::1",
                "no_proxy": "localhost,127.0.0.1,::1",
                "NODE_USE_ENV_PROXY": "1",
            }
        )
    if runtime_kind == "hermes":
        options.update(
            user="10000:10000",
            command=["sleep", "infinity"],
            working_dir="/opt/data/workspace",
            volumes={volume: {"bind": "/opt/data", "mode": "rw"}},
            tmpfs={
                "/tmp": "rw,nosuid,nodev,size=256m,mode=1777",
                "/run": "rw,exec,nosuid,nodev,size=64m,uid=10000,gid=10000,mode=755",
            },
        )
        options["environment"] = {
            key: value
            for key, value in options["environment"].items()
            if key.lower() in {"http_proxy", "https_proxy", "no_proxy"}
        } | {
            "HOME": "/opt/data",
            "HERMES_HOME": "/opt/data",
            "S6_READ_ONLY_ROOT": "1",
            "HERMES_DASHBOARD": "1",
            "HERMES_DASHBOARD_HOST": "0.0.0.0",
            "HERMES_DASHBOARD_PORT": "9119",
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET": control_token,
            "HERMES_DASHBOARD_PUBLIC_URL": control_origin,
        }
    return options


def prepare_volumes(
    client,
    state_name: str,
    config_name: str,
    config: dict,
    labels: dict,
    *,
    native=False,
    runtime_kind="openclaw",
):
    """Seed approved configuration and state ownership without host bind mounts."""
    uid = 10000 if runtime_kind == "hermes" else 1000
    filename = "config.yaml" if runtime_kind == "hermes" else "openclaw.json"
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
            "const fs=require('fs');fs.chownSync('/state',0,0);fs.chmodSync('/state',448);"
            + (
                f"if(!fs.existsSync('/state/{filename}')){{fs.copyFileSync('/config/openclaw.json.next','/state/{filename}');fs.chownSync('/state/{filename}',{uid},{uid});fs.chmodSync('/state/{filename}',384);}}fs.unlinkSync('/config/openclaw.json.next');"
                if native
                else "fs.renameSync('/config/openclaw.json.next','/config/openclaw.json');"
            )
            + "fs.mkdirSync('/state/workspace',{recursive:true});"
            + f"fs.chownSync('/state/workspace',{uid},{uid});fs.chownSync('/state',{uid},{uid});"
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
            entry = tarfile.TarInfo("openclaw.json.next")
            entry.size, entry.mode, entry.uid, entry.gid = (
                len(payload),
                0o400,
                0 if native else 1000,
                0 if native else 1000,
            )
            archive.addfile(entry, io.BytesIO(payload))
        initializer.put_archive("/config", data.getvalue())
        initializer.start()
        if initializer.wait(timeout=30)["StatusCode"]:
            raise RuntimeError("Runtime private volume initialization failed")
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
