"""Offline capture of portable skill and MCP candidates from a stopped native agent.

The helper reads raw files using parsers only. It never imports native configuration,
plugin discovery, credential hydration, or executes a captured command.
"""

import base64
import inspect
import json
import re

from docker.errors import NotFound

from worker.runtime import OwnershipError, require_labels


class SetupCaptureError(RuntimeError):
    """A bounded operator-safe capture failure, never native output or configuration."""


def inspect_state(root, runtime_kind, runtime_release, architecture):
    """Standalone helper body, also directly testable with a temporary state directory."""
    import base64
    import hashlib
    import json
    import os
    import re
    import stat
    import subprocess
    import sys
    from pathlib import Path, PurePosixPath
    from urllib.parse import urlsplit, urlunsplit

    root = Path(root)
    state_prefix = "/opt/data" if runtime_kind == "hermes" else "/home/node/.openclaw"
    max_file = 16 * 1024 * 1024
    max_total = 64 * 1024 * 1024
    max_files = 5000
    files = {}
    total = 0
    candidates = []
    blockers = []
    used_ids = set()
    private_names = {
        ".env",
        ".git",
        ".ssh",
        ".aws",
        ".azure",
        ".config",
        ".talos",
        "credentials",
        "credentials.json",
        "auth.json",
        "auth-profiles.json",
        "oauth",
        "oauth.json",
        "tokens.json",
        "history",
        "sessions",
        "memory",
        "__pycache__",
        ".venv",
        "venv",
    }

    def block(kind, message, item_id=None):
        item = {"kind": kind, "message": message}
        if item_id:
            item["item_id"] = item_id
        if item not in blockers:
            blockers.append(item)

    def slug(value, fallback):
        value = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")[:48]
        return value if value and value[0].isalpha() else (fallback + "-" + value)[:48]

    def unique_id(value, source):
        identifier = slug(value, "candidate")
        if identifier in used_ids:
            identifier = identifier[:37] + "-" + hashlib.sha256(source.encode()).hexdigest()[:10]
        used_ids.add(identifier)
        return identifier

    def private(path):
        return any(
            p.lower() in private_names
            or p.lower().startswith(".env.")
            or p.lower().endswith((".pem", ".key", ".sqlite", ".sqlite3", ".db"))
            for p in path.parts
        )

    def safe_path(path):
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ValueError("Path outside agent state") from None
        for index in range(len(relative.parts)):
            current = root.joinpath(*relative.parts[: index + 1])
            if current.is_symlink():
                raise ValueError("Symbolic links are not captured")
        return relative

    def read_file(path):
        safe_path(path)
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Only regular non-linked files are captured")
        if metadata.st_size > max_file:
            raise ValueError("File exceeds capture limit")
        with path.open("rb") as handle:
            data = handle.read(max_file + 1)
        if len(data) > max_file:
            raise ValueError("File exceeds capture limit")
        return data

    def add_file(path, data):
        nonlocal total
        if path in files:
            return
        total += len(data)
        if total > max_total or len(files) >= max_files:
            raise ValueError("Capture exceeds size limit")
        files[path] = data

    def map_path(raw):
        if not isinstance(raw, str) or not raw or "$" in raw or "\x00" in raw:
            return None
        if raw.startswith("~/"):
            home = "/opt/data" if runtime_kind == "hermes" else "/home/node"
            raw = home + raw[1:]
        path = PurePosixPath(raw)
        if ".." in path.parts:
            return None
        if path.is_absolute():
            try:
                relative = path.relative_to(state_prefix)
            except ValueError:
                return None
        else:
            relative = path
        if not relative.parts:
            return None
        result = root.joinpath(*relative.parts)
        if private(relative) and result not in managed_skill_roots:
            return None
        try:
            safe_path(result)
        except ValueError:
            return None
        return result

    def parse_config():
        filename = "config.yaml" if runtime_kind == "hermes" else "openclaw.json"
        path = root / filename
        raw = read_file(path).decode("utf-8")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            if runtime_kind == "hermes":
                import yaml

                config = yaml.safe_load(raw)
            else:
                # JSON5 is the image's parser only, not a native config loader.
                output = subprocess.run(
                    [
                        "node",
                        "-e",
                        "let s='';process.stdin.on('data',c=>s+=c);"
                        "process.stdin.on('end',()=>process.stdout.write("
                        "JSON.stringify(require('json5').parse(s))));",
                    ],
                    input=raw.encode(),
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                config = json.loads(output.stdout)
        if not isinstance(config, dict):
            raise ValueError("Native configuration must be an object")
        return config

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def strings(value):
        return (
            [value]
            if isinstance(value, str)
            else ([v for v in value if isinstance(v, str)] if isinstance(value, list) else [])
        )

    config = parse_config()
    skills_config = mapping(config.get("skills"))
    native_servers = (
        mapping(config.get("mcp_servers"))
        if runtime_kind == "hermes"
        else mapping(mapping(config.get("mcp")).get("servers"))
    )
    node_version = (
        subprocess.run(
            ["node", "--version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
        .removeprefix("v")
    )
    manifest = {
        "schema_version": 1,
        "instructions": "",
        "targets": [
            {
                "runtime_kind": runtime_kind,
                "runtime_release": runtime_release,
                "architecture": architecture,
                "node_major": int(node_version.split(".")[0]),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            }
        ],
        "skills": [],
        "connectors": [],
        "connection_slots": [],
        "assets": {},
        "unresolved": blockers,
    }
    metadata = {"candidates": candidates, "instructions_review_required": False}
    managed_servers = set()
    managed_skill_roots = set()
    receipt_path = root / ".talos/setup-receipt.json"
    if receipt_path.exists():
        try:
            receipt = json.loads(read_file(receipt_path))
            artifact_hash = receipt["artifact_hash"]
            if artifact_hash is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                    raise ValueError("Invalid managed artifact")
                original = receipt["manifest"]
                artifact_root = root / ".talos/setups" / artifact_hash
                asset_data = {}
                for relative, digest in original["assets"].items():
                    path = PurePosixPath(relative)
                    if path.is_absolute() or ".." in path.parts or private(path):
                        raise ValueError("Invalid managed asset path")
                    content = read_file(artifact_root.joinpath(*path.parts))
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("Managed assets were changed")
                    asset_data[relative] = content
                for relative, digest in mapping(receipt.get("file_hashes")).items():
                    path = PurePosixPath(relative)
                    if path.is_absolute() or ".." in path.parts or private(path):
                        raise ValueError("Invalid managed runtime asset path")
                    content = read_file(artifact_root.joinpath(*path.parts))
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("Managed runtime assets were changed")
                managed_skill_roots.add(artifact_root / "enabled-skills")
                # Secrets are absent from the portable manifest; the native server map
                # must still match the receipt before it is treated as reproducible.
                for connector in original.get("connectors", []):
                    name = "talos-" + connector["id"]
                    if name not in native_servers:
                        continue
                    if native_servers[name] != mapping(receipt.get("servers")).get(name):
                        block(
                            "managed-drift",
                            "A managed connector changed; review its configuration.",
                        )
                        continue
                    copied = json.loads(json.dumps(connector))
                    copied["source"] = "talos-managed"
                    manifest["connectors"].append(copied)
                    used_ids.add(copied["id"])
                    managed_servers.add(name)
                    candidates.append(
                        {
                            "id": copied["id"],
                            "kind": "connector",
                            "source": "talos-managed",
                            "enabled": copied.get("enabled", True),
                            "selected": copied.get("enabled", True),
                        }
                    )
                    prefix = f"connectors/{copied['id']}/"
                    for path, content in asset_data.items():
                        if path.startswith(prefix):
                            add_file(path, content)
                for skill in original.get("skills", []):
                    if not skill.get("enabled", True):
                        continue
                    copied = json.loads(json.dumps(skill))
                    copied["source"] = "talos-managed"
                    manifest["skills"].append(copied)
                    used_ids.add(copied["id"])
                    managed_skill_roots.add(artifact_root / copied["path"])
                    candidates.append(
                        {
                            "id": copied["id"],
                            "kind": "skill",
                            "source": "talos-managed",
                            "name": copied["name"],
                            "enabled": True,
                            "selected": True,
                            "shadowed": False,
                        }
                    )
                    prefix = copied["path"].rstrip("/") + "/"
                    for path, content in asset_data.items():
                        if path.startswith(prefix):
                            add_file(path, content)
                manifest["connection_slots"] = json.loads(
                    json.dumps(original.get("connection_slots", []))
                )
                manifest["instructions"] = original.get("instructions", "")
        except (ValueError, KeyError, TypeError, OSError):
            block(
                "managed-drift",
                "Managed setup metadata or assets changed; review before publishing.",
            )

    workspace_raw = (
        mapping(config.get("terminal")).get("cwd", state_prefix + "/workspace")
        if runtime_kind == "hermes"
        else mapping(mapping(config.get("agents")).get("defaults")).get(
            "workspace", state_prefix + "/workspace"
        )
    )
    workspace = map_path(workspace_raw)
    roots = []
    if runtime_kind == "hermes":
        trusted = {map_path(p) for p in strings(skills_config.get("trusted_project_dirs"))}
        if workspace:
            project = workspace
            while project != root and not (project / ".git").exists():
                project = project.parent
            # Keep untrusted project candidates visible for explicit selection.
            project = project if project != root else workspace
            active = project in trusted and (project / ".git").exists()
            for suffix in (".hermes/skills", ".agents/skills"):
                roots.append((project / suffix, active))
        roots.append((root / "skills", True))
        extra = strings(skills_config.get("create_dir")) + strings(
            skills_config.get("external_dirs")
        )
    else:
        # OpenClaw resolves later sources over earlier ones; enumerate high priority first.
        if workspace:
            roots.extend([(workspace / "skills", True), (workspace / ".agents/skills", True)])
        roots.append((root / "skills", True))
        extra = list(reversed(strings(mapping(skills_config.get("load")).get("extraDirs"))))
    for value in extra:
        mapped = map_path(value)
        if mapped:
            roots.append((mapped, True))
        else:
            block(
                "external-path",
                "A configured skill root is outside captured state or uses variables.",
            )
    if not workspace:
        block(
            "external-workspace", "The configured workspace is outside the captured state volume."
        )
    elif (workspace / "AGENTS.md").exists():
        # Personal identity text is not silently made into a reusable role.
        metadata["instructions_review_required"] = True

    seen_roots = set()
    seen_names = set()
    disabled_skills = set(strings(skills_config.get("disabled")))
    skill_entries = mapping(skills_config.get("entries"))
    scanned = 0
    for directory, trusted in roots:
        if directory in seen_roots or directory in managed_skill_roots or not directory.exists():
            continue
        seen_roots.add(directory)
        try:
            safe_path(directory)
        except ValueError:
            block("symlink", "A configured skill root is a symbolic link and was excluded.")
            continue
        queue = [(directory, 0)]
        for current, depth in queue:
            scanned += 1
            if scanned > max_files:
                raise ValueError("Skill discovery exceeds capture limit")
            if current in managed_skill_roots:
                continue
            if current.is_symlink():
                block("symlink", "A linked skill directory was excluded; supply a regular bundle.")
                continue
            skill_file = current / "SKILL.md"
            if not skill_file.exists():
                if depth < 6:
                    queue.extend(
                        (child, depth + 1)
                        for child in sorted(current.iterdir())
                        if child.is_dir()
                        and not private(Path(child.name))
                        and child.name != "node_modules"
                    )
                continue
            source = current.relative_to(root).as_posix()
            identifier = unique_id(current.name, source)
            try:
                markdown = read_file(skill_file).decode("utf-8")
            except (ValueError, UnicodeDecodeError, OSError):
                block(
                    "skill-file",
                    "A skill document is unreadable, linked, or too large.",
                    identifier,
                )
                continue
            name = current.name
            lines = markdown.removeprefix("\ufeff").splitlines()
            if lines and lines[0].strip() == "---":
                try:
                    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
                    frontmatter = "\n".join(lines[1:end])
                    if len(frontmatter) > 16_384:
                        raise ValueError("Skill metadata exceeds capture limit")
                    try:
                        import yaml

                        info = yaml.safe_load(frontmatter)
                    except ImportError:
                        # The OpenClaw image ships the safe YAML parser with Node.
                        parsed = subprocess.run(
                            [
                                "node",
                                "-e",
                                "let s='';process.stdin.on('data',c=>s+=c);"
                                "process.stdin.on('end',()=>process.stdout.write("
                                "JSON.stringify(require('yaml').parse(s))));",
                            ],
                            input=frontmatter.encode(),
                            capture_output=True,
                            check=True,
                            timeout=10,
                        )
                        info = json.loads(parsed.stdout)
                    native_name = mapping(info).get("name")
                    if isinstance(native_name, str) and native_name and len(native_name) <= 160:
                        name = native_name
                    else:
                        block(
                            "skill-metadata",
                            "Review the skill's native frontmatter name.",
                            identifier,
                        )
                except Exception:
                    block(
                        "skill-metadata",
                        "Review invalid or oversized skill frontmatter.",
                        identifier,
                    )
            shadowed = name in seen_names
            enabled = (
                name not in disabled_skills
                and mapping(skill_entries.get(name)).get("enabled") is not False
            )
            if not shadowed and enabled and trusted:
                seen_names.add(name)
            selected = enabled and trusted and not shadowed
            destination = f"skills/{identifier}"
            item = {
                "id": identifier,
                "name": name,
                "path": destination,
                "enabled": selected,
                "source": source,
            }
            manifest["skills"].append(item)
            candidates.append(
                {
                    "id": identifier,
                    "kind": "skill",
                    "name": name,
                    "source": source,
                    "enabled": enabled,
                    "selected": selected,
                    "shadowed": shadowed,
                    "trusted": trusted,
                }
            )
            for parent, dirs, names in os.walk(current, followlinks=False):
                parent = Path(parent)
                for child in list(dirs):
                    child_path = parent / child
                    relative = child_path.relative_to(current)
                    if private(relative) or child_path.is_symlink():
                        dirs.remove(child)
                        block(
                            "excluded-file",
                            "A skill contains private or linked files; review it.",
                            identifier,
                        )
                for filename in sorted(names):
                    path = parent / filename
                    relative = path.relative_to(current)
                    if private(relative):
                        block(
                            "excluded-file",
                            "Private files were excluded from a skill; review it.",
                            identifier,
                        )
                        continue
                    try:
                        data = read_file(path)
                    except (ValueError, OSError):
                        block(
                            "excluded-file",
                            "A skill asset is linked, unreadable, or too large.",
                            identifier,
                        )
                        continue
                    add_file(f"{destination}/{relative.as_posix()}", data)

    for candidate in candidates:
        if candidate["kind"] == "skill" and candidate["source"] == "talos-managed":
            if candidate["name"] in seen_names:
                candidate.update(selected=False, shadowed=True)
                for skill in manifest["skills"]:
                    if skill["id"] == candidate["id"]:
                        skill["enabled"] = False
            else:
                seen_names.add(candidate["name"])

    slot_ids = {s["id"] for s in manifest["connection_slots"]}
    for name, raw in sorted(native_servers.items()):
        if name in managed_servers:
            continue
        identifier = unique_id(name, "connector:" + name)
        if not isinstance(raw, dict):
            block("connector-config", "A connector configuration is not an object.", identifier)
            continue
        enabled = raw.get("enabled") is not False
        transport = raw.get("transport", raw.get("type"))
        if transport in ("http", "streamable_http"):
            transport = "streamable-http"
        if not transport:
            transport = (
                "stdio"
                if raw.get("command")
                else ("sse" if runtime_kind == "openclaw" else "streamable-http")
            )
        connector = {
            "id": identifier,
            "name": name,
            "enabled": enabled,
            "source": "native-config",
            "transport": transport,
            "tools": [],
            "env": {},
            "headers": {},
            "args": [],
        }
        manifest["connectors"].append(connector)
        candidates.append(
            {
                "id": identifier,
                "kind": "connector",
                "source": "native-config",
                "enabled": enabled,
                "selected": enabled,
            }
        )
        tool_filter = mapping(raw.get("toolFilter" if runtime_kind == "openclaw" else "tools"))
        names = strings(tool_filter.get("include"))
        connector["tools"] = sorted(
            {
                n
                for n in names
                if re.fullmatch(r"[A-Za-z0-9_.-]+", n)
                and n not in strings(tool_filter.get("exclude"))
            }
        )
        if enabled and (not names or len(connector["tools"]) != len(set(names))):
            block(
                "tool-inventory",
                "Declare the connector's exact exposed tool names before publishing.",
                identifier,
            )
        if transport == "stdio":
            # Commands, arguments, cwd and arbitrary config may contain tokens and
            # cannot prove dependency closure. They never enter the portable draft.
            block(
                "local-payload",
                "Supply a prepared connector payload with pinned dependencies and provenance.",
                identifier,
            )
        elif transport in ("streamable-http", "sse"):
            url = raw.get("url")
            try:
                parsed = urlsplit(url if isinstance(url, str) else "")
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or "$" in (url or "")
                ):
                    raise ValueError("Invalid portable URL")
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    block(
                        "url-credentials",
                        "URL credentials were removed; supply a portable endpoint.",
                        identifier,
                    )
                host = parsed.hostname
                if ":" in host:
                    host = f"[{host}]"
                netloc = host + (f":{parsed.port}" if parsed.port else "")
                connector["url"] = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
            except (ValueError, TypeError):
                block(
                    "connector-url",
                    "Supply a portable HTTP endpoint without embedded credentials.",
                    identifier,
                )
        else:
            block(
                "transport",
                "The native connector transport is not supported by setup bundles.",
                identifier,
            )
        fields = []
        slot_id = identifier
        if slot_id in slot_ids:
            slot_id = identifier[:37] + "-connection"
        for group in ("env", "headers"):
            for key in sorted(mapping(raw.get(group))):
                if not isinstance(key, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_-]{0,70}", key
                ):
                    block(
                        "credential-field", "A credential field name requires review.", identifier
                    )
                    continue
                field = re.sub(r"[^A-Za-z0-9_]", "_", group + "_" + key)
                if field in fields:
                    field = field[:70] + "_" + hashlib.sha256(key.encode()).hexdigest()[:8]
                fields.append(field)
                connector[group][key] = {"slot": slot_id, "field": field}
        if fields:
            slot_ids.add(slot_id)
            manifest["connection_slots"].append({"id": slot_id, "label": name, "fields": fields})
        if raw.get("oauth") or raw.get("auth") == "oauth":
            block(
                "oauth",
                "OAuth credentials were excluded; configure supported token-based authentication.",
                identifier,
            )
        if any(
            raw.get(key)
            for key in (
                "clientCert",
                "clientKey",
                "client_cert",
                "client_key",
                "cwd",
                "workingDirectory",
            )
        ):
            block(
                "external-path",
                "Connector file or working-directory references require a prepared payload.",
                identifier,
            )

    plugins = mapping(config.get("plugins"))
    plugin_entries = mapping(plugins.get("entries"))
    if plugins.get("enabled") is not False:
        for name, entry in plugin_entries.items():
            if name == "parallel" or mapping(entry).get("enabled") is False:
                continue
            identifier = unique_id(name, "plugin:" + name)
            candidates.append(
                {
                    "id": identifier,
                    "kind": "plugin",
                    "source": "native-config",
                    "enabled": True,
                    "selected": False,
                }
            )
            block(
                "unsupported-plugin",
                "A native plugin cannot be reproduced as a setup connector.",
                identifier,
            )
        if plugins and not plugin_entries:
            block("unsupported-plugin", "Native plugin configuration requires manual review.")
    plugin_paths = strings(mapping(plugins.get("load")).get("paths"))
    if any(
        path != "/opt/talos-plugins/node_modules/@openclaw/parallel-plugin" for path in plugin_paths
    ):
        block("unsupported-plugin", "Additional native plugin paths require manual review.")
    if runtime_kind == "hermes":
        plugin_roots = [root / "plugins"]
        if workspace:
            plugin_roots.append(workspace / ".hermes/plugins")
        for plugin_root in plugin_roots:
            if not plugin_root.exists():
                continue
            try:
                safe_path(plugin_root)
                installed = sorted(plugin_root.iterdir())
            except (OSError, ValueError):
                block("unsupported-plugin", "A native plugin directory requires manual review.")
                continue
            for directory in installed:
                if directory.name.startswith("."):
                    continue
                identifier = unique_id(directory.name, "plugin:" + directory.name)
                candidates.append(
                    {
                        "id": identifier,
                        "kind": "plugin",
                        "source": "native-plugin-directory",
                        "enabled": None,
                        "selected": False,
                    }
                )
                block(
                    "unsupported-plugin",
                    "An installed native plugin needs a portable replacement.",
                    identifier,
                )
    # These are non-secret, finite native permission fields, not arbitrary config.
    if runtime_kind == "hermes":
        metadata["native_permissions"] = {
            "disabled_toolsets": strings(mapping(config.get("agent")).get("disabled_toolsets")),
            "platform_toolsets": {
                key: strings(value)
                for key, value in mapping(config.get("platform_toolsets")).items()
            },
        }
    else:
        tools = mapping(config.get("tools"))
        metadata["native_permissions"] = {
            "profile": tools.get("profile")
            if tools.get("profile") in ("minimal", "coding", "messaging", "full")
            else None,
            "allow": strings(tools.get("allow")),
            "deny": strings(tools.get("deny")),
        }
    manifest["assets"] = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    return {
        "manifest": manifest,
        "files": {path: base64.b64encode(data).decode("ascii") for path, data in files.items()},
        "metadata": metadata,
    }


def capture_setup(client, state, incarnation, runtime_kind, labels):
    """Read a stopped owned incarnation with a network-isolated, read-only helper."""
    if runtime_kind not in {"openclaw", "hermes"} or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", incarnation.image_digest or ""
    ):
        raise SetupCaptureError("Capture requires a pinned native runtime image")
    require_labels(client.volumes.get(state).attrs.get("Labels") or {}, labels)
    reference = incarnation.container_id or incarnation.container_name
    if reference:
        try:
            container = client.containers.get(reference)
        except NotFound:
            pass
        else:
            require_labels(container.labels, labels)
            container.reload()
            if container.attrs.get("State", {}).get("Running") or container.status in {
                "running",
                "restarting",
                "paused",
            }:
                raise SetupCaptureError("Stop the agent before capturing a setup")
    image = client.images.get(incarnation.image_digest)
    if image.id != incarnation.image_digest:
        raise OwnershipError("Capture runtime image does not match its pinned incarnation")
    architecture = image.attrs.get("Architecture")
    if architecture not in {"amd64", "arm64"}:
        raise SetupCaptureError("The runtime architecture is not supported by setup bundles")
    name = incarnation.config_volume + "-capture"
    try:
        previous = client.containers.get(name)
    except NotFound:
        pass
    else:
        require_labels(previous.labels, labels)
        previous.remove(force=True)
    payload = ["/state", runtime_kind, incarnation.runtime_release, architecture]
    script = inspect.getsource(inspect_state) + (
        "\nimport json, sys\n"
        "try:\n"
        "    print(json.dumps(inspect_state(*json.loads(sys.argv[1]))))\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )
    helper = client.containers.create(
        incarnation.image_digest,
        name=name,
        entrypoint=["python3", "-c"],
        command=[script, json.dumps(payload)],
        user="10000:10000" if runtime_kind == "hermes" else "1000:1000",
        volumes={state: {"bind": "/state", "mode": "ro"}},
        network_mode="none",
        read_only=True,
        environment={"PYTHONDONTWRITEBYTECODE": "1"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        mem_limit="1g",
        pids_limit=32,
        labels=labels,
        # Candidate bytes leave through the attached stream, never persistent logs.
        log_config={"type": "none"},
    )
    try:
        stream = helper.attach(stream=True, stdout=True, stderr=False)
        helper.start()
        output = bytearray()
        for chunk in stream:
            output.extend(chunk)
            if len(output) > 96 * 1024 * 1024:
                raise SetupCaptureError("Capture exceeds the supported size limit")
        if helper.wait(timeout=90)["StatusCode"]:
            raise SetupCaptureError(
                "Native setup capture failed; check skill paths and configuration"
            )
        try:
            result = json.loads(output)
            result["files"] = {
                path: base64.b64decode(content, validate=True)
                for path, content in result["files"].items()
            }
        except (ValueError, KeyError, TypeError) as from_error:
            raise SetupCaptureError("Native setup capture returned invalid data") from from_error
        return result
    finally:
        helper.remove(force=True)
