"""Server-owned runtime launch contract; callers cannot supply Docker settings."""

import hashlib
import io
import json
import tarfile

from docker.errors import NotFound

from .openclaw import SCOPES, DeviceIdentity


class OwnershipError(RuntimeError):
    pass


class RuntimeReadinessError(RuntimeError):
    """An operator-safe readiness diagnosis, never a raw native log line."""

    def __init__(self, message, *, code="native"):
        super().__init__(message)
        self.code = code


def runtime_error_message(detail) -> str | None:
    """Classify bounded native diagnostics; no part of the input is returned."""
    if not isinstance(detail, str):
        return None
    detail = detail[:8192].lower()
    for markers, message in (
        (
            ("another gateway owner lease is still active",),
            "The previous OpenClaw gateway still holds its startup lease. Stop the agent, "
            "then retry Start after its owner has stopped or the lease expires.",
        ),
        (
            (
                "invalid api key",
                "invalid_api_key",
                "401 unauthorized",
                "authentication failed",
                "credentials or agent init failed",
            ),
            "The runtime could not authenticate or initialize its provider. Check the selected "
            "provider and API key in Settings or the native workspace.",
        ),
        (
            ("insufficient credits", "insufficient_quota", "credit balance", "payment required"),
            "The provider reports insufficient credits. Check the provider account balance.",
        ),
        (
            ("rate limit", "rate_limit", "too many requests"),
            "The provider rate limit was reached. Wait before sending a new message.",
        ),
        (
            ("model not found", "model_not_found", "no endpoints found"),
            "The selected model is unavailable. Choose an available model in agent Settings.",
        ),
        (
            ("eai_again", "enotfound", "err_name_not_resolved"),
            "The runtime could not resolve a destination. Check the Talos egress service and "
            "native browser proxy configuration.",
        ),
        (
            ("chrome not found", "chromium not found", "no usable browser"),
            "The browser executable is missing. "
            "Rebuild the native runtime image and restart the agent.",
        ),
        (
            ("requires approval", "approval required", "blocked by policy"),
            "The tool needs approval or is blocked by its native policy. "
            "Review it in the native workspace.",
        ),
    ):
        if any(marker in detail for marker in markers):
            return message
    return None


def require_labels(actual: dict, expected: dict):
    if any(actual.get(key) != value for key, value in expected.items()):
        raise OwnershipError("Docker resource does not belong to this agent installation")


IMAGE = (
    "ghcr.io/openclaw/openclaw@sha256:"
    "0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15"
)
NATIVE_IMAGE = "talos-openclaw-native:local"
NATIVE_IMAGES = {"openclaw": NATIVE_IMAGE, "hermes": "talos-hermes-native:local"}
STATE_PATH = "/home/node/.openclaw"
CONFIG_PATH = "/etc/talos/openclaw.json"


def release_stopped_gateway_lease(client, volume: str, hostname: str, labels: dict, *, image=IMAGE):
    """Release only the pinned OpenClaw lease belonging to a confirmed stopped container."""
    if not hostname:
        raise OwnershipError("Stopped gateway hostname is missing")
    require_labels(client.volumes.get(volume).attrs.get("Labels") or {}, labels)
    client.containers.run(
        image,
        entrypoint=["node", "-e"],
        command=[
            "const fs=require('node:fs');const p='/state/state/openclaw.sqlite';"
            "if(fs.existsSync(p)){const {DatabaseSync}=require('node:sqlite');"
            "const db=new DatabaseSync(p);db.exec('PRAGMA busy_timeout=5000');"
            "if(db.prepare(\"SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='state_leases'\").get())"
            "db.prepare(\"DELETE FROM state_leases WHERE scope='gateway-owner' "
            "AND lease_key='global' "
            "AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,"
            "'$.owner.host')=?\").run(process.argv[1]);db.close();}",
            hostname,
        ],
        user="1000:1000",
        network_mode="none",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        volumes={volume: {"bind": "/state", "mode": "rw"}},
        labels=labels,
        mem_limit="128m",
        pids_limit=32,
        remove=True,
    )


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
        "browser": {
            "headless": True,
            "noSandbox": True,
            "executablePath": "/usr/bin/chromium",
            "extraArgs": [
                "--proxy-server=http://talos-egress:3128",
                "--proxy-bypass-list=<-loopback>",
            ],
            # Proxy DNS is authoritative; Squid rejects private destinations.
            # OpenClaw's direct-DNS preflight cannot run on an internal network.
            "ssrfPolicy": {"dangerouslyAllowPrivateNetwork": True},
        },
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
        # Chromium renderers plus the native CLI can exhaust 256 Linux tasks.
        options["pids_limit"] = 512
        options["environment"].update(
            {
                "OPENCLAW_CONFIG_PATH": STATE_PATH + "/openclaw.json",
                "OPENCLAW_GATEWAY_TOKEN": control_token,
                "TALOS_CONTROL_ORIGIN": control_origin,
                "HTTP_PROXY": "http://talos-egress:3128",
                "HTTPS_PROXY": "http://talos-egress:3128",
                "http_proxy": "http://talos-egress:3128",
                "https_proxy": "http://talos-egress:3128",
                "NO_PROXY": "localhost,127.0.0.1,::1,talos-gateway",
                "no_proxy": "localhost,127.0.0.1,::1,talos-gateway",
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
            "AGENT_BROWSER_PROXY": "http://talos-egress:3128",
            "AGENT_BROWSER_ARGS": (
                "--no-sandbox,--disable-dev-shm-usage,--proxy-bypass-list=<-loopback>"
            ),
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
        require_labels(previous.labels, labels)
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
                f"if(!fs.existsSync('/state/{filename}')){{fs.rmSync('/state/.talos-config.next',{{force:true}});fs.copyFileSync('/config/openclaw.json.next','/state/.talos-config.next');fs.chownSync('/state/.talos-config.next',{uid},{uid});fs.chmodSync('/state/.talos-config.next',384);fs.renameSync('/state/.talos-config.next','/state/{filename}');}}fs.unlinkSync('/config/openclaw.json.next');"
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


def apply_native_permissions(client, state, incarnation, runtime_kind, permissions, labels):
    """Patch only native permission fields in an offline, pinned-image initializer."""
    if runtime_kind == "hermes":
        entrypoint = ["python", "-c"]
        script = """
import json, os, pathlib, sys, yaml
from hermes_cli.config import validate_config_structure
from hermes_cli.platforms import PLATFORMS
from hermes_cli.tools_config import _get_platform_tools, _get_plugin_toolset_keys
from model_tools import _select_tool_names
from toolsets import resolve_toolset
policy = json.loads(sys.argv[1])
path = pathlib.Path('/opt/data/config.yaml')
config = yaml.safe_load(path.read_text())
assert isinstance(config, dict)
allowed = set().union(*(set(resolve_toolset(k)) for k in policy['enabled']))
# Disable installed third-party toolsets too, retaining their credentials/config.
disabled = sorted(set(policy['disabled']) | _get_plugin_toolset_keys() |
                  {'mcp-' + str(k) for k in (config.get('mcp_servers') or {})})
config.setdefault('agent', {})['disabled_toolsets'] = disabled
config['platform_toolsets'] = {k: policy['enabled'] + ['no_mcp'] for k in PLATFORMS}
issues = validate_config_structure(config)
assert not any(i.severity == 'error' for i in issues), 'Invalid native configuration'
for platform in PLATFORMS:
    selected = _get_platform_tools(config, platform)
    actual = _select_tool_names(list(selected), disabled, True)
    assert actual == allowed, 'Native tool selection differs from captured permissions'
# Also test session posture overrides and deferred discovery's scoped selection.
assert _select_tool_names(None, disabled, True) == allowed, 'Global exclusions are incomplete'
next_path = path.with_suffix('.talos-next')
next_path.write_text(yaml.safe_dump(config, sort_keys=False))
next_path.chmod(0o600)
os.replace(next_path, path)
"""
    else:
        entrypoint = ["node", "-e"]
        script = """
const fs = require('fs'), JSON5 = require('json5'), cp = require('child_process');
const policy = JSON.parse(process.argv[1]);
const path = process.env.OPENCLAW_CONFIG_PATH;
const config = JSON5.parse(fs.readFileSync(path, 'utf8'));
config.tools ??= {};
Object.assign(config.tools, {profile: 'full', allow: policy.allow, deny: policy.deny});
delete config.tools.alsoAllow;
config.browser = {...config.browser, enabled: false};
config.cron = {...config.cron, enabled: false};
config.agents ??= {}; config.agents.defaults ??= {};
config.agents.defaults.heartbeat = {...config.agents.defaults.heartbeat, every: '0m'};
const next = path + '.talos-next';
fs.writeFileSync(next, JSON.stringify(config, null, 2), {mode: 0o600});
cp.execFileSync('node', ['openclaw.mjs', 'config', 'validate'], {
  env: {...process.env, OPENCLAW_CONFIG_PATH: next}, stdio: 'ignore', timeout: 60000
});
fs.renameSync(next, path);
"""
    write_native_config(
        client,
        state,
        incarnation,
        runtime_kind,
        labels,
        "permissions",
        entrypoint,
        script,
        permissions,
    )


def apply_native_model(client, state, incarnation, runtime_kind, selection, token, labels):
    """Change model defaults, retaining the prior native choice for handing control back."""
    payload = {"selection": selection or None, "token": token}
    if runtime_kind == "hermes":
        entrypoint = ["python", "-c"]
        script = """
import json, os, pathlib, sys, yaml
from hermes_cli.config import validate_config_structure
payload = json.loads(sys.argv[1])
selection = payload['selection']
path = pathlib.Path('/opt/data/config.yaml')
backup = path.with_name('talos-model-backup.json')
config = yaml.safe_load(path.read_text())
if selection:
    if not backup.exists():
        backup.write_text(json.dumps({k: config.get(k) for k in ('model', 'fallback_model')}))
        backup.chmod(0o600)
    config['model'] = {'provider': 'custom', 'default': selection['model_id'],
                       'base_url': 'http://talos-gateway:8001/native/v1',
                       'api_key': payload['token'], 'api_mode': 'chat_completions'}
    config.pop('fallback_model', None)
elif backup.exists():
    for key, value in json.loads(backup.read_text()).items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
issues = validate_config_structure(config)
assert not any(i.severity == 'error' for i in issues), 'Invalid native model configuration'
next_path = path.with_suffix('.talos-model-next')
next_path.write_text(yaml.safe_dump(config, sort_keys=False))
next_path.chmod(0o600)
os.replace(next_path, path)
if not selection:
    backup.unlink(missing_ok=True)
"""
    else:
        entrypoint = ["node", "-e"]
        script = """
const fs = require('fs'), JSON5 = require('json5'), cp = require('child_process');
const {selection, token} = JSON.parse(process.argv[1]);
const path = process.env.OPENCLAW_CONFIG_PATH, backup = path + '.talos-model-backup';
const config = JSON5.parse(fs.readFileSync(path, 'utf8'));
config.agents ??= {}; config.agents.defaults ??= {};
const defaults = config.agents.defaults;
config.models ??= {}; config.models.providers ??= {};
if (selection) {
  if (!fs.existsSync(backup)) fs.writeFileSync(backup, JSON.stringify({
    model: defaults.model ?? null,
    models: defaults.models ?? null,
    provider: config.models.providers['talos-openrouter'] ?? null,
  }), {mode: 0o600});
  const caps = selection.capabilities;
  const model = 'talos-openrouter/' + selection.model_id;
  defaults.model = {primary: model};
  defaults.models = {...defaults.models, [model]: {}};
  config.models.providers['talos-openrouter'] = {
    baseUrl: 'http://talos-gateway:8001/native/v1', apiKey: token,
    api: 'openai-completions', models: [{id: selection.model_id, name: caps.name,
      reasoning: !!caps.reasoning?.supported_efforts, input: ['text'],
      contextWindow: caps.context_length,
      maxTokens: caps.max_completion_tokens ?? Math.min(8192, caps.context_length)}],
  };
} else if (fs.existsSync(backup)) {
  const prior = JSON.parse(fs.readFileSync(backup, 'utf8'));
  for (const key of ['model', 'models']) {
    if (prior[key] === null) delete defaults[key]; else defaults[key] = prior[key];
  }
  if (prior.provider === null) delete config.models.providers['talos-openrouter'];
  else config.models.providers['talos-openrouter'] = prior.provider;
}
const next = path + '.talos-model-next';
fs.writeFileSync(next, JSON.stringify(config, null, 2), {mode: 0o600});
cp.execFileSync('node', ['openclaw.mjs', 'config', 'validate'], {
  env: {...process.env, OPENCLAW_CONFIG_PATH: next}, stdio: 'ignore', timeout: 60000
});
fs.renameSync(next, path);
if (!selection) fs.rmSync(backup, {force: true});
"""
    write_native_config(
        client, state, incarnation, runtime_kind, labels, "model", entrypoint, script, payload
    )


def write_native_config(
    client, state, incarnation, runtime_kind, labels, kind, entrypoint, script, payload
):
    name = incarnation.config_volume + "-" + kind
    try:
        previous = client.containers.get(name)
    except NotFound:
        pass
    else:
        require_labels(previous.labels, labels)
        previous.remove(force=True)
    if runtime_kind == "hermes":
        volumes = {state: {"bind": "/opt/data", "mode": "rw"}}
        environment = {"HOME": "/opt/data", "HERMES_HOME": "/opt/data"}
        user = "10000:10000"
    else:
        volumes = {state: {"bind": STATE_PATH, "mode": "rw"}}
        environment = {
            "HOME": "/home/node",
            "OPENCLAW_STATE_DIR": STATE_PATH,
            "OPENCLAW_CONFIG_PATH": STATE_PATH + "/openclaw.json",
            "OPENCLAW_GATEWAY_TOKEN": "offline-validation-only",
            "TALOS_CONTROL_ORIGIN": "http://127.0.0.1",
        }
        user = "1000:1000"
    initializer = client.containers.create(
        incarnation.image_digest,
        name=name,
        entrypoint=entrypoint,
        command=[script, json.dumps(payload)],
        user=user,
        environment=environment,
        volumes=volumes,
        network_mode="none",
        read_only=True,
        tmpfs={
            "/tmp": "rw,nosuid,nodev,size=256m,mode=1777",
            **(
                {"/home/node/.cache": "rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=700"}
                if runtime_kind == "openclaw"
                else {}
            ),
        },
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        labels=labels,
        mem_limit="2g",
        pids_limit=128,
    )
    try:
        initializer.start()
        if initializer.wait(timeout=90)["StatusCode"]:
            raise RuntimeError("Native configuration validation failed")
    finally:
        initializer.remove(force=True)
