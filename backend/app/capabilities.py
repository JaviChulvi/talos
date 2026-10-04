"""The capability contract for Talos's pinned native runtimes."""

CAPABILITIES = {
    "web_research": {
        "name": "Web research",
        "description": "Search the web and retrieve page content.",
        "openclaw": ["web_search", "web_fetch"],
        "hermes": ["web"],
        "hermes_tools": ["web_search", "web_extract"],
    },
    "workspace_files": {
        "name": "Workspace files",
        "description": "Read, search, create and edit files accessible to the runtime.",
        "openclaw": ["read", "write", "edit", "apply_patch"],
        "hermes": ["file"],
        "hermes_tools": ["read_file", "write_file", "patch", "search_files"],
    },
    "terminal_execution": {
        "name": "Terminal execution",
        "description": "Run commands and code. This also permits file and network operations, "
        "even when their dedicated tools are disabled.",
        "openclaw": ["exec", "process", "code_execution"],
        "hermes": ["terminal", "code_execution"],
        "hermes_tools": ["terminal", "process_manage", "execute_code"],
    },
}


# Pinned Hermes 0.21.5 basic groups. Platform/posture composites overlap these
# groups and must not be disabled: Hermes subtracts tools, not group names.
HERMES_GROUPS = """web search x_search vision video image_gen video_gen computer_use
terminal skills browser cronjob file tts todo memory context_engine session_search
connections project bot_room desktop_ui setup clarify code_execution delegation
homeassistant kanban discord discord_admin yuanbao feishu_doc feishu_drive spotify""".split()


def compile_permissions(capabilities: list[str], runtime_kind: str) -> dict:
    if set(capabilities) - CAPABILITIES.keys():
        raise ValueError("Unknown profile capability")
    if runtime_kind == "openclaw":
        allowed = sorted({tool for key in capabilities for tool in CAPABILITIES[key]["openclaw"]})
        return {"allow": allowed, "deny": [] if allowed else ["*"]}
    enabled = {tool for key in capabilities for tool in CAPABILITIES[key]["hermes"]}
    # search is a subset of web; disabling it would remove granted web_search.
    covered = enabled | ({"search"} if "web" in enabled else set())
    return {"enabled": sorted(enabled), "disabled": sorted(set(HERMES_GROUPS) - covered)}
