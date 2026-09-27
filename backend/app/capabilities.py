"""The capability contract for Talos's pinned native runtimes."""

CAPABILITIES = {
    "web_research": {
        "name": "Web research",
        "description": "Search the web and retrieve page content.",
        "openclaw": ["web_search", "web_fetch"],
        "hermes": ["web"],
    },
    "workspace_files": {
        "name": "Workspace files",
        "description": "Read, search, create and edit files accessible to the runtime.",
        "openclaw": ["read", "write", "edit", "apply_patch"],
        "hermes": ["file"],
    },
    "terminal_execution": {
        "name": "Terminal execution",
        "description": "Run commands and code. This also permits file and network operations, "
        "even when their dedicated tools are disabled.",
        "openclaw": ["exec", "process", "code_execution"],
        "hermes": ["terminal", "code_execution"],
    },
}
