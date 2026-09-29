"""Apply immutable setup artifacts to the two pinned native runtimes.

Helpers run inside the selected image. Their argv contains only static code; all
application data travels through Docker's archive upload into the private config
volume. The lifecycle worker owns stop/start and calls preflight before stopping.
"""

import hashlib
import io
import json
import re
import tarfile
from copy import deepcopy
from threading import RLock

from docker.errors import NotFound

from .runtime import STATE_PATH, OwnershipError, RuntimeReadinessError, require_labels

BEGIN = "<!-- TALOS SETUP BEGIN -->"
END = "<!-- TALOS SETUP END -->"
_HELPER_LOCK = RLock()


def application_fingerprint(application):
    payload = {
        key: value
        for key, value in application.items()
        if key not in {"fingerprint", "restart", "legacy_receipt"}
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def connector_name(identifier):
    return "talos-" + identifier


def connector_tool_name(runtime_kind, identifier, tool):
    server = connector_name(identifier)
    if runtime_kind == "hermes":
        name = (
            "mcp__"
            + re.sub(r"[^A-Za-z0-9_]", "_", server)
            + "__"
            + re.sub(r"[^A-Za-z0-9_]", "_", tool)
        )
        if len(name) > 64:
            name = name[:55] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
        return name
    server = server[:30]
    name = re.sub(r"[^A-Za-z0-9_-]", "-", tool.strip())
    if not name or not name[0].isalpha():
        name = "tool-" + name
    return server + "__" + name[: 64 - len(server) - 2]


def setup_permissions(application, runtime_kind):
    """Expand base role permissions with only the selected setup's grants."""
    policy = deepcopy(application["permissions"])
    manifest = (application.get("setup") or {}).get("manifest") or {}
    grants = set(application.get("connector_grants", []))
    connectors = [
        c for c in manifest.get("connectors", []) if c["id"] in grants and c.get("enabled", True)
    ]
    skills = any(s.get("enabled", True) for s in manifest.get("skills", []))
    if runtime_kind == "openclaw":
        allowed = set(policy["allow"])
        if skills:
            allowed.add("read")
        allowed.update(
            connector_tool_name(runtime_kind, c["id"], tool)
            for c in connectors
            for tool in c["tools"]
        )
        policy.update(allow=sorted(allowed), deny=[] if allowed else ["*"])
    else:
        enabled = set(policy["enabled"])
        if skills:
            enabled.add("skills")
        enabled.update("mcp-" + connector_name(c["id"]) for c in connectors)
        policy.update(enabled=sorted(enabled), disabled=sorted(set(policy["disabled"]) - enabled))
    return policy


def _request(application, runtime_kind, image):
    setup = application.get("setup") or {}
    manifest = setup.get("manifest") or {
        "instructions": "",
        "skills": [],
        "connectors": [],
        "targets": [],
        "assets": {},
    }
    if BEGIN in manifest.get("instructions", "") or END in manifest.get("instructions", ""):
        raise RuntimeReadinessError("Setup instructions contain reserved managed markers.")
    grants = set(application.get("connector_grants", []))
    if grants - {c["id"] for c in manifest.get("connectors", []) if c.get("enabled", True)}:
        raise RuntimeReadinessError("A granted connector is not enabled in this setup revision.")
    # Native naming can normalize/truncate. A collision must never silently grant another tool.
    names = [
        connector_tool_name(runtime_kind, c["id"], t)
        for c in manifest.get("connectors", [])
        if c["id"] in grants
        for t in c["tools"]
    ]
    servers = [
        connector_name(c["id"])[:30] if runtime_kind == "openclaw" else connector_name(c["id"])
        for c in manifest.get("connectors", [])
        if c["id"] in grants
    ]
    if len(set(names)) != len(names) or len(set(servers)) != len(servers):
        raise RuntimeReadinessError("Setup connector names collide after native normalization.")
    return {
        "runtime_kind": runtime_kind,
        "fingerprint": application_fingerprint(application),
        "manifest": manifest,
        "artifact_hash": setup.get("artifact_hash"),
        "grants": sorted(grants),
        "policy": setup_permissions(application, runtime_kind),
        "connections": application.get("connections", {}),
        "image": {
            "architecture": image.attrs.get("Architecture"),
            "labels": image.attrs.get("Config", {}).get("Labels") or {},
        },
    }


def prepare_setup(client, state, incarnation, runtime_kind, application, labels, *, secrets=None):
    """Read-only state preflight; validates an archive before the runtime is stopped."""
    request = _request(application, runtime_kind, client.images.get(incarnation.image_digest))
    files = {}
    if request["artifact_hash"]:
        from backend.app.setups import load_bundle

        manifest, files = load_bundle(request["artifact_hash"])
        if manifest != request["manifest"]:
            raise RuntimeReadinessError("The setup artifact does not match the selected revision.")
    request["secrets"] = secrets or {}
    _run(client, state, incarnation, labels, request, "prepare")
    return {"request": request, "files": files}


def apply_setup(
    client, state, incarnation, runtime_kind, application, labels, *, prepared=None, secrets=None
):
    """Publish owned files/configuration, writing a durable receipt last. Agent must be stopped."""
    prepared = prepared or prepare_setup(
        client, state, incarnation, runtime_kind, application, labels, secrets=secrets
    )
    request = deepcopy(prepared["request"])
    if request["fingerprint"] != application_fingerprint(application):
        raise OwnershipError("Prepared setup belongs to a different application")
    request["secrets"] = secrets or {}
    return _run(client, state, incarnation, labels, request, "apply", prepared["files"])


def verify_setup(
    client, state, incarnation, runtime_kind, application, labels, *, network=None, discover=True
):
    """Check receipt/integrity and discover actual native skills and granted MCP tools."""
    request = _request(application, runtime_kind, client.images.get(incarnation.image_digest))
    return _run(
        client,
        state,
        incarnation,
        labels,
        request,
        "verify" if discover else "inspect",
        network=network if discover else None,
    )


def _run(client, state, incarnation, labels, request, mode, files=None, network=None):
    # Recovery, lifecycle and explicit probes share the same deterministic helper
    # name and input volume. Never remove an in-process verification helper.
    with _HELPER_LOCK:
        return _run_locked(client, state, incarnation, labels, request, mode, files, network)


def _run_locked(client, state, incarnation, labels, request, mode, files=None, network=None):
    require_labels(client.volumes.get(state).attrs.get("Labels") or {}, labels)
    require_labels(client.volumes.get(incarnation.config_volume).attrs.get("Labels") or {}, labels)
    name = incarnation.config_volume + "-setup"
    try:
        old = client.containers.get(name)
    except NotFound:
        pass
    else:
        require_labels(old.labels, labels)
        old.remove(force=True)
    hermes = request["runtime_kind"] == "hermes"
    mount, uid = ("/opt/data", 10000) if hermes else (STATE_PATH, 1000)
    env = {
        "HOME": mount if hermes else "/home/node",
        "HERMES_HOME": mount,
        "OPENCLAW_STATE_DIR": mount,
        "OPENCLAW_CONFIG_PATH": mount + "/openclaw.json",
        "OPENCLAW_GATEWAY_TOKEN": "offline-validation-only",
        "TALOS_CONTROL_ORIGIN": "http://127.0.0.1",
        "HTTP_PROXY": "http://talos-egress:3128",
        "HTTPS_PROXY": "http://talos-egress:3128",
        "http_proxy": "http://talos-egress:3128",
        "https_proxy": "http://talos-egress:3128",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "NODE_USE_ENV_PROXY": "1",
    }
    helper = client.containers.create(
        incarnation.image_digest,
        name=name,
        entrypoint=["python3", "-c"],
        command=[_HELPER],
        user=f"{uid}:{uid}",
        working_dir="/app" if not hermes else "/opt/hermes",
        environment=env,
        network_mode=network or "none",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        labels=labels,
        volumes={
            state: {"bind": mount, "mode": "ro" if mode in {"prepare", "inspect"} else "rw"},
            incarnation.config_volume: {"bind": "/input", "mode": "rw"},
        },
        tmpfs={
            "/tmp": "rw,nosuid,nodev,size=128m,mode=1777",
            **(
                {"/home/node/.cache": "rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=700"}
                if not hermes
                else {}
            ),
        },
        mem_limit="1g",
        pids_limit=128,
    )
    try:
        payload = {"setup-request.json": json.dumps({**request, "mode": mode}).encode()}
        payload.update({"assets/" + path: data for path, data in (files or {}).items()})
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for path, content in payload.items():
                info = tarfile.TarInfo(path)
                info.size, info.mode, info.uid, info.gid = len(content), 0o600, uid, uid
                archive.addfile(info, io.BytesIO(content))
        helper.put_archive("/input", data.getvalue())
        helper.start()
        status = helper.wait(timeout=180)
        output = helper.logs(stdout=True, stderr=False)
        # The helper emits only these bounded outcomes, never config or exceptions.
        result = json.loads(output.decode().split("TALOS_SETUP_RESULT=")[-1])
        if status["StatusCode"] or not result.get("ok"):
            raise RuntimeReadinessError(
                _ERRORS.get(
                    result.get("error"), "Setup validation failed in the selected native runtime."
                ),
                code=result.get("error") if result.get("error") in _ERRORS else "native",
            )
        return result["receipt"]
    except RuntimeReadinessError:
        raise
    except Exception:
        raise RuntimeReadinessError(
            "Setup helper failed; the agent must remain stopped until Apply succeeds."
        ) from None
    finally:
        helper.remove(force=True)


_ERRORS = {
    "compatibility": "Unsupported runtime release, architecture, or interpreter for this setup.",
    "shadow": "A native skill shadows a setup skill. Resolve the conflicting skill before Apply.",
    "edited": "Talos-managed setup files or configuration were edited. Restore them before Apply.",
    "path": "A setup-owned path is a symlink or leaves the agent state volume.",
    "collision": "A native connector name conflicts with a setup connector namespace.",
    "receipt": "The selected setup has no matching complete application receipt. Apply it again.",
    "connection": "A required setup connection credential is missing.",
    "tools_changed": (
        "Expected MCP tools are unavailable. "
        "Check the endpoint and credential, then run discovery again."
    ),
    "encoding": "This runtime cannot represent a credential exactly. Use a compatible API token.",
    "discovery": (
        "A granted connector or skill is unavailable in native discovery. "
        "Check its configuration and retry Apply."
    ),
    "native": "The setup produced invalid native configuration.",
}

# Executed inside the immutable native image. It deliberately never prints native
# logs, configuration, environment values, or connector error details.
_HELPER = r"""
import hashlib, json, os, pathlib, platform, re, shutil, subprocess, sys

P = pathlib.Path
safe_output = os.fdopen(os.dup(1), "w")
null_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(null_fd, 1)
os.dup2(null_fd, 2)
os.close(null_fd)
request_path = P("/input/setup-request.json")
r = json.loads(request_path.read_text())
mode, kind = r["mode"], r["runtime_kind"]
root = P("/opt/data" if kind == "hermes" else "/home/node/.openclaw")
managed = root / ".talos"
receipt_path, pending_path = managed / "setup-receipt.json", managed / "setup-pending.json"
manifest = r["manifest"]


class Failure(Exception):
    pass


def fail(code):
    raise Failure(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def guarded(path):
    path = P(path)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail("path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            fail("path")
    return path


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path, data, permissions=0o600):
    path = guarded(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nxt = guarded(str(path) + ".talos-next")
    fd = os.open(nxt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, permissions)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(nxt, path)
    fd = os.open(path.parent, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)


def run(args, *, error_code="native", **kwargs):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90, **kwargs
    )
    if result.returncode:
        fail(error_code)
    return result.stdout


def load_config():
    path = root / ("config.yaml" if kind == "hermes" else "openclaw.json")
    guarded(path)
    if kind == "hermes":
        import yaml

        value = yaml.safe_load(path.read_text())
    else:
        value = json.loads(
            run(
                [
                    "node",
                    "-e",
                    "console.log(JSON.stringify(require('json5').parse(require('fs').readFileSync(process.env.OPENCLAW_CONFIG_PATH,'utf8'))))",
                ]
            )
        )
    if not isinstance(value, dict):
        fail("native")
    return path, value


def instructions(existing, previous, target):
    begin, end = "<!-- TALOS SETUP BEGIN -->", "<!-- TALOS SETUP END -->"
    if existing.count(begin) != existing.count(end) or existing.count(begin) > 1:
        fail("edited")
    before, after, current = existing, "", ""
    if begin in existing:
        before, rest = existing.split(begin)
        current, after = rest.split(end)
        if not current.startswith("\n") or not current.endswith("\n"):
            fail("edited")
        current = current[1:-1]
    allowed = {previous.get("instructions", ""), target}
    if current not in allowed or (
        previous.get("instructions") and target and begin not in existing
    ):
        fail("edited")
    if target:
        return (
            before
            + ("" if before.endswith("\n\n") or not before else "\n\n")
            + begin
            + "\n"
            + target
            + "\n"
            + end
            + after
        )
    return before + after


def ensure_compatibility():
    if not r["artifact_hash"]:
        return
    labels = r["image"]["labels"]
    if kind == "hermes":
        import hermes_cli

        release = "hermes-" + hermes_cli.__version__
    else:
        release = "openclaw-" + labels.get("org.opencontainers.image.version", "")
    targets = [
        t
        for t in manifest["targets"]
        if t["runtime_kind"] == kind
        and t["runtime_release"] == release
        and t["architecture"] == r["image"]["architecture"]
    ]
    for target in targets:
        if target.get("python_version") and platform.python_version_tuple()[:2] != tuple(
            target["python_version"].split(".")[:2]
        ):
            continue
        if target.get("node_major"):
            try:
                major = int(run(["node", "-p", 'process.versions.node.split(".")[0]']).strip())
            except Exception:
                continue
            if major != target["node_major"]:
                continue
        return
    fail("compatibility")


def make_servers(asset_root):
    servers = {}
    for c in manifest.get("connectors", []):
        if c["id"] not in r["grants"] or not c.get("enabled", True):
            continue
        server = {"enabled": True}
        if c["transport"] == "stdio":
            server.update(
                command=c["runner"], args=[str(asset_root / c["entrypoint"])] + c.get("args", [])
            )
            if kind == "openclaw":
                server["cwd"] = str(asset_root / "connectors" / c["id"])
        else:
            server.update(url=c["url"], transport=c["transport"])
        for key in ("env", "headers"):
            if c.get(key):
                server[key] = {}
                for field, value in c[key].items():
                    if isinstance(value, dict):
                        env = (
                            "TALOS_SETUP_"
                            + hashlib.sha256((value["slot"] + ":" + value["field"]).encode())
                            .hexdigest()[:24]
                            .upper()
                        )
                        server[key][field] = "${" + env + "}"
                    else:
                        server[key][field] = value
        if kind == "hermes":
            server["tools"] = {"include": c["tools"], "resources": False, "prompts": False}
        else:
            server["toolFilter"] = {"include": c["tools"]}
        servers["talos-" + c["id"]] = server
    return servers


def check_assets(previous):
    if not previous.get("artifact_hash"):
        return
    base = guarded(managed / "setups" / previous["artifact_hash"])
    for name, expected in previous.get(
        "file_hashes", previous.get("manifest", {}).get("assets", {})
    ).items():
        path = guarded(base / name)
        if (
            not path.is_file()
            or digest(path.read_bytes()) != expected
            or path.stat().st_mode & 0o111
            != (0o100 if name in previous.get("executables", []) else 0)
        ):
            fail("edited")


def native_server_key(name):
    if kind == "hermes":
        return re.sub(r"[^A-Za-z0-9_]", "_", name)
    value = re.sub(r"[^A-Za-z0-9_-]", "-", name.strip()) or "mcp"
    if not value[0].isalpha():
        value = "mcp-" + value
    return value[:30].lower()


def patch(config, previous, target):
    config = json.loads(json.dumps(config))
    if kind == "hermes":
        servers = config.setdefault("mcp_servers", {})
        dirs = config.setdefault("skills", {}).setdefault("external_dirs", [])
    else:
        servers = config.setdefault("mcp", {}).setdefault("servers", {})
        dirs = config.setdefault("skills", {}).setdefault("load", {}).setdefault("extraDirs", [])
    for name in target["servers"]:
        if any(
            native_server_key(other) == native_server_key(name)
            for other in servers
            if other != name and other not in previous.get("servers", {})
        ):
            fail("collision")
    for name, expected in previous.get("servers", {}).items():
        if servers.get(name) not in (expected, target["servers"].get(name)):
            fail("edited")
    for name, expected in target["servers"].items():
        if (
            name in servers
            and name not in previous.get("servers", {})
            and servers[name] != expected
        ):
            fail("edited")
    for name in previous.get("servers", {}):
        servers.pop(name, None)
    servers.update(target["servers"])
    dirs[:] = [
        d for d in dirs if d not in previous.get("skill_dirs", []) and d not in target["skill_dirs"]
    ] + target["skill_dirs"]
    policy = r["policy"]
    if kind == "openclaw":
        tools = config.setdefault("tools", {})
        tools.update(profile="full", allow=policy["allow"], deny=policy["deny"])
        tools.pop("alsoAllow", None)
        config.setdefault("browser", {})["enabled"] = False
        config.setdefault("cron", {})["enabled"] = False
        config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("heartbeat", {})[
            "every"
        ] = "0m"
    else:
        from hermes_cli.platforms import PLATFORMS
        from hermes_cli.tools_config import _get_plugin_toolset_keys

        disabled = (
            set(policy["disabled"])
            | _get_plugin_toolset_keys()
            | {"mcp-" + name for name in servers if name not in target["servers"]}
        )
        disabled -= set(policy["enabled"])
        config.setdefault("agent", {})["disabled_toolsets"] = sorted(disabled)
        config["platform_toolsets"] = {
            k: policy["enabled"] + (["no_mcp"] if not target["servers"] else []) for k in PLATFORMS
        }
    return config


def check_shadow(config, workspace, previous):
    wanted = {s["name"] for s in manifest.get("skills", []) if s.get("enabled", True)}
    if not wanted:
        return
    paths = [
        root / "skills",
        workspace / "skills",
        workspace / ".agents/skills",
        root / ".agents/skills",
    ]
    if kind == "hermes":
        paths += [P(p) for p in config.get("skills", {}).get("external_dirs", [])]
    else:
        paths += [P("/app/skills")] + [
            P(p) for p in config.get("skills", {}).get("load", {}).get("extraDirs", [])
        ]
    for path in paths:
        if str(path) in previous.get("skill_dirs", []) or str(path).startswith(
            str(managed / "setups") + "/"
        ):
            continue
        if not path.is_dir():
            continue
        for item in path.rglob("SKILL.md"):
            text = item.read_text(errors="replace")[:8192]
            match = re.search(r"^name:\s*(.+)$", text, re.M)
            name = match.group(1).strip().strip("\"'") if match else item.parent.name
            if name in wanted:
                fail("shadow")


def save_config(path, config):
    if kind == "hermes":
        import yaml
        from hermes_cli.config import validate_config_structure

        if any(i.severity == "error" for i in validate_config_structure(config)):
            fail("native")
        data = yaml.safe_dump(config, sort_keys=False).encode()
    else:
        data = json.dumps(config, indent=2).encode()
        temp = guarded(str(path) + ".setup-validate")
        atomic(temp, data)
        try:
            run(
                ["node", "openclaw.mjs", "config", "validate"],
                env={**os.environ, "OPENCLAW_CONFIG_PATH": str(temp)},
            )
        finally:
            temp.unlink(missing_ok=True)
    atomic(path, data)


def env_section(text):
    begin, end = "# TALOS SETUP BEGIN", "# TALOS SETUP END"
    if text.count(begin) != text.count(end) or text.count(begin) > 1:
        fail("edited")
    if begin not in text:
        return text, "", ""
    before, rest = text.split(begin)
    current, after = rest.split(end)
    return before, current.strip("\n"), after


def encoded_secret(key, value):
    if any(c in value for c in ("\0", "\r", "\n")):
        fail("encoding")
    if kind == "hermes":
        import io
        from dotenv import dotenv_values
        from agent.secret_scope import load_env_file

        if "${" in value:
            fail("encoding")
        encoded = json.dumps(value, ensure_ascii=False)
        line = key + "=" + encoded
        if dotenv_values(stream=io.StringIO(line)).get(key) != value:
            fail("encoding")
        # The scoped Hermes secret reader is independent of python-dotenv.
        path = P("/tmp/talos-env-roundtrip")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(line)
        try:
            if load_env_file(path).get(key) != value:
                fail("encoding")
        finally:
            path.unlink(missing_ok=True)
        return line
    candidates = [q + value + q for q in ("'", "`", '"') if q not in value] + [value]
    script = (
        "const fs=require('fs');"
        "console.log(JSON.stringify(require('dotenv').parse(fs.readFileSync(0))))"
    )
    for encoded in candidates:
        line = key + "=" + encoded
        parsed = json.loads(run(["node", "-e", script], input=line.encode()))
        if parsed.get(key) == value:
            return line
    fail("encoding")


def credential_env(previous):
    env = {}
    for c in manifest.get("connectors", []):
        if c["id"] not in r["grants"]:
            continue
        for section in ("env", "headers"):
            for value in c.get(section, {}).values():
                if not isinstance(value, dict):
                    continue
                key = (
                    "TALOS_SETUP_"
                    + hashlib.sha256((value["slot"] + ":" + value["field"]).encode())
                    .hexdigest()[:24]
                    .upper()
                )
                secret = r.get("secrets", {}).get(value["slot"], {}).get(value["field"])
                if not isinstance(secret, str):
                    fail("connection")
                env[key] = secret
    path = guarded(root / ".env")
    text = path.read_text() if path.exists() else ""
    before, current, after = env_section(text)
    next_section = "\n".join(encoded_secret(key, value) for key, value in sorted(env.items()))
    allowed = {previous.get("env_hash", digest(b"")), digest(next_section.encode())}
    if digest(current.encode()) not in allowed:
        fail("edited")
    for key in env:
        if re.search(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=", before + after, re.M):
            fail("edited")
    result = before + after
    if next_section:
        result = (
            before
            + ("" if not before or before.endswith("\n") else "\n")
            + "# TALOS SETUP BEGIN\n"
            + next_section
            + "\n# TALOS SETUP END"
            + after
        )
    return result.encode(), sorted(env), digest(next_section.encode())


def verify_env(previous):
    path = guarded(root / ".env")
    _, current, _ = env_section(path.read_text() if path.exists() else "")
    if digest(current.encode()) != previous.get("env_hash", digest(b"")):
        fail("edited")


def native_verify(target, config):
    skills = {s["name"] for s in manifest.get("skills", []) if s.get("enabled", True)}
    expected = set()
    if kind == "hermes":
        from tools.skills_tool import _find_all_skills, _locate_skill, _skill_search_dirs

        project_dirs, all_dirs, _ = _skill_search_dirs()
        for skill in manifest.get("skills", []):
            if not skill.get("enabled", True):
                continue
            error, _, resolved = _locate_skill(skill["name"], None, project_dirs, all_dirs)
            expected_path = (
                managed
                / "setups"
                / r["artifact_hash"]
                / "enabled-skills"
                / skill["id"]
                / "SKILL.md"
            )
            if error or resolved.resolve() != expected_path.resolve():
                fail("shadow")
        if not skills <= {s["name"] for s in _find_all_skills()}:
            fail("discovery")
        from tools.mcp_tool import discover_mcp_tools, shutdown_mcp_servers
        from tools.mcp_tool_schema import mcp_prefixed_tool_name
        from model_tools import _select_tool_names
        from toolsets import resolve_toolset

        try:
            actual = set(discover_mcp_tools(list(target["servers"])))
            for c in manifest.get("connectors", []):
                if "talos-" + c["id"] in target["servers"]:
                    expected.update(
                        mcp_prefixed_tool_name("talos-" + c["id"], t) for t in c["tools"]
                    )
            if actual != expected:
                fail("tools_changed")
            allowed = set().union(*(set(resolve_toolset(k)) for k in r["policy"]["enabled"]))
            disabled = config["agent"]["disabled_toolsets"]
            if _select_tool_names(None, disabled, True) != allowed:
                fail("discovery")
            if not expected <= _select_tool_names(r["policy"]["enabled"], disabled, True):
                fail("discovery")
        finally:
            shutdown_mcp_servers()
    else:
        data = json.loads(
            run(["node", "openclaw.mjs", "skills", "list", "--json"], error_code="discovery")
        )
        available = {s["name"] for s in data.get("skills", []) if s.get("eligible")}
        for skill in manifest.get("skills", []):
            if not skill.get("enabled", True):
                continue
            entry = json.loads(
                run(
                    ["node", "openclaw.mjs", "skills", "info", skill["name"], "--json"],
                    error_code="discovery",
                )
            )
            expected_path = (
                managed
                / "setups"
                / r["artifact_hash"]
                / "enabled-skills"
                / skill["id"]
                / "SKILL.md"
            )
            if entry and P(entry["filePath"]).resolve() != expected_path.resolve():
                fail("shadow")
        if not skills <= available:
            fail("discovery")
        for name in target["servers"]:
            probe = json.loads(
                run(
                    ["node", "openclaw.mjs", "mcp", "probe", name, "--json"], error_code="discovery"
                )
            )
            c = next(c for c in manifest["connectors"] if "talos-" + c["id"] == name)
            if name not in probe.get("servers", {}) or probe["servers"][name]["tools"] != len(
                c["tools"]
            ):
                fail("tools_changed")
            expected.update(probe["tools"])
        if not expected <= set(config["tools"]["allow"]):
            fail("discovery")


def recover_previous(previous, pending, config, original):
    # Adopt only known intermediate writes before superseding a failed target.
    failed = pending.get("target")
    if not failed:
        fail("receipt")
    servers = (
        config.get("mcp_servers", {})
        if kind == "hermes"
        else config.get("mcp", {}).get("servers", {})
    )
    owned = set(previous.get("servers", {})) | set(failed.get("servers", {}))
    recovered = dict(previous)
    recovered["servers"] = {}
    for name in owned:
        actual = servers.get(name)
        allowed = (previous.get("servers", {}).get(name), failed.get("servers", {}).get(name))
        if actual not in allowed:
            fail("edited")
        if actual is not None:
            recovered["servers"][name] = actual
    recovered["skill_dirs"] = sorted(
        set(previous.get("skill_dirs", [])) | set(failed.get("skill_dirs", []))
    )
    instructions(original, previous, failed.get("instructions", ""))
    begin, end = "<!-- TALOS SETUP BEGIN -->", "<!-- TALOS SETUP END -->"
    recovered["instructions"] = (
        original.split(begin)[1].split(end)[0][1:-1] if begin in original else ""
    )
    env_path = guarded(root / ".env")
    _, current, _ = env_section(env_path.read_text() if env_path.exists() else "")
    current_hash = digest(current.encode())
    if current_hash not in (
        previous.get("env_hash", digest(b"")),
        failed.get("env_hash", digest(b"")),
    ):
        fail("edited")
    recovered["env_hash"] = current_hash
    recovered["env_keys"] = sorted(
        set(previous.get("env_keys", [])) | set(failed.get("env_keys", []))
    )
    return recovered


def main():
    guarded(managed)
    ensure_compatibility()
    previous = read_json(guarded(receipt_path), {})
    pending = read_json(guarded(pending_path), {})
    if pending:
        previous = pending["previous"]
    path, config = load_config()
    workspace = guarded(
        config.get("terminal", {}).get("cwd", str(root / "workspace"))
        if kind == "hermes"
        else config.get("agents", {}).get("defaults", {}).get("workspace", str(root / "workspace"))
    )
    artifact = guarded(managed / "setups" / (r["artifact_hash"] or "empty"))
    enabled_skills = [s for s in manifest.get("skills", []) if s.get("enabled", True)]
    skill_dirs = [str(artifact / "enabled-skills")] if enabled_skills else []
    file_hashes = dict(manifest.get("assets", {}))
    executables = set(manifest.get("executables", []))
    for skill in enabled_skills:
        for name, expected in manifest.get("assets", {}).items():
            if name.startswith(skill["path"] + "/"):
                relative = name[len(skill["path"]) + 1 :]
                destination = "enabled-skills/" + skill["id"] + "/" + relative
                file_hashes[destination] = expected
                if name in executables:
                    executables.add(destination)
    target = {
        "fingerprint": r["fingerprint"],
        "artifact_hash": r["artifact_hash"],
        "manifest": manifest,
        "servers": make_servers(artifact),
        "skill_dirs": skill_dirs,
        "instructions": manifest.get("instructions", ""),
        "connections": r.get("connections", {}),
        "file_hashes": file_hashes,
        "executables": sorted(executables),
    }
    check_assets(previous)
    doc = guarded(workspace / "AGENTS.md")
    original = doc.read_text() if doc.exists() else ""
    if pending and mode not in {"verify", "inspect"}:
        previous = recover_previous(previous, pending, config, original)
    check_shadow(config, workspace, previous)
    updated = patch(config, previous, target)
    rewritten = instructions(original, previous, target["instructions"])
    if mode == "prepare":
        credential_env(previous)
        return target
    if mode in {"verify", "inspect"}:
        if pending or previous.get("fingerprint") != r["fingerprint"]:
            fail("receipt")
        if updated != config or rewritten != original:
            fail("edited")
        verify_env(previous)
        if mode == "verify":
            native_verify(target, config)
        return previous
    env, keys, env_hash = credential_env(previous)
    target["env_keys"] = keys
    target["env_hash"] = env_hash
    atomic(
        pending_path,
        json.dumps(
            {"fingerprint": r["fingerprint"], "previous": previous, "target": target}
        ).encode(),
    )
    for name, expected in manifest.get("assets", {}).items():
        data = (P("/input/assets") / name).read_bytes()
        if digest(data) != expected:
            fail("edited")
        atomic(artifact / name, data, 0o700 if name in executables else 0o600)
    for skill in enabled_skills:
        for name in manifest.get("assets", {}):
            if name.startswith(skill["path"] + "/"):
                relative = name[len(skill["path"]) + 1 :]
                atomic(
                    artifact / "enabled-skills" / skill["id"] / relative,
                    (artifact / name).read_bytes(),
                    0o700 if name in executables else 0o600,
                )
    atomic(root / ".env", env)
    save_config(path, updated)
    if rewritten != original:
        atomic(doc, rewritten.encode())
    atomic(receipt_path, json.dumps(target, sort_keys=True).encode())
    pending_path.unlink(missing_ok=True)
    fd = os.open(managed, os.O_RDONLY)
    os.fsync(fd)
    os.close(fd)
    # Old immutable assets are retained until a subsequent explicit application.
    # Their native search paths and connector entries have already been removed.
    if previous.get("artifact_hash") and previous["artifact_hash"] != r["artifact_hash"]:
        shutil.rmtree(guarded(managed / "setups" / previous["artifact_hash"]))
    return target


try:
    receipt = main()
    print(
        "TALOS_SETUP_RESULT=" + json.dumps({"ok": True, "receipt": receipt}),
        file=safe_output,
        flush=True,
    )
except Failure as error:
    print(
        "TALOS_SETUP_RESULT=" + json.dumps({"ok": False, "error": str(error)}),
        file=safe_output,
        flush=True,
    )
    sys.exit(1)
except Exception:
    print(
        "TALOS_SETUP_RESULT=" + json.dumps({"ok": False, "error": "native"}),
        file=safe_output,
        flush=True,
    )
    sys.exit(1)
finally:
    request_path.write_text("{}")
"""
