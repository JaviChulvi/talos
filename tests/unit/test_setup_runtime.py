import hashlib
from types import SimpleNamespace

import pytest

from backend.app.capabilities import compile_permissions
from worker.runtime import RuntimeReadinessError
from worker.setup_runtime import (
    _HELPER,
    _request,
    application_fingerprint,
    connector_tool_name,
    setup_permissions,
)


def application(kind, *, skills=True):
    return {
        "role": {"id": "role"},
        "permissions": compile_permissions([], kind),
        "connector_grants": ["fixture"],
        "setup": {
            "manifest": {
                "skills": [{"id": "guide", "name": "guide", "enabled": skills}],
                "connectors": [
                    {"id": "fixture", "tools": ["lookup"], "enabled": True},
                    {"id": "denied", "tools": ["write"], "enabled": True},
                ],
            }
        },
    }


def test_application_fingerprint_ignores_operation_restart_and_stored_fingerprint():
    value = application("hermes")
    expected = application_fingerprint(value)
    assert application_fingerprint({**value, "restart": True, "fingerprint": "cached"}) == expected
    assert application_fingerprint({**value, "legacy_receipt": True}) == expected
    value["connections"] = {"crm": {"version_id": "next"}}
    assert application_fingerprint(value) != expected


@pytest.mark.parametrize("kind", ["openclaw", "hermes"])
def test_permissions_grant_only_selected_connector_and_required_skill_access(kind):
    app = application(kind)
    policy = setup_permissions(app, kind)
    assert app["permissions"] == compile_permissions([], kind)
    if kind == "openclaw":
        assert policy == {"allow": ["read", "talos-fixture__lookup"], "deny": []}
    else:
        assert policy["enabled"] == ["mcp-talos-fixture", "skills"]
        assert "skills" not in policy["disabled"]
        assert "terminal" in policy["disabled"]


def test_hermes_uses_native_tool_naming_and_clamping():
    assert (
        connector_tool_name("hermes", "crm-main", "lookup-contact")
        == "mcp__talos_crm_main__lookup_contact"
    )
    raw = "mcp__talos_" + "x" * 48 + "__" + "y" * 100
    assert (
        connector_tool_name("hermes", "x" * 48, "y" * 100)
        == raw[:55] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:8]
    )


def test_native_name_collisions_are_rejected_before_application():
    app = application("openclaw")
    app["setup"]["manifest"]["connectors"][0]["tools"] = ["find.item", "find-item"]
    with pytest.raises(RuntimeReadinessError, match="collide"):
        _request(app, "openclaw", SimpleNamespace(attrs={}))


def test_unknown_grant_is_rejected_before_application():
    app = application("hermes")
    app["connector_grants"].append("unknown")
    with pytest.raises(RuntimeReadinessError, match="not enabled"):
        _request(app, "hermes", SimpleNamespace(attrs={}))


def test_embedded_native_helper_compiles():
    compile(_HELPER, "native setup helper", "exec")


def test_reserved_instruction_markers_are_rejected_before_mutation():
    app = application("hermes")
    app["setup"]["manifest"]["instructions"] = "<!-- TALOS SETUP BEGIN -->"
    with pytest.raises(RuntimeReadinessError, match="reserved"):
        _request(app, "hermes", SimpleNamespace(attrs={}))
