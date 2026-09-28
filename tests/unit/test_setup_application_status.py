from types import SimpleNamespace

import pytest

from backend.app.applications import annotate_agent, normalize_application


@pytest.mark.parametrize("error, expected", [(None, "pending"), ("Unsupported setup", "blocked")])
def test_selection_does_not_claim_successful_installation(monkeypatch, error, expected):
    selected = {"setup": {"revision_id": "chosen-but-not-applied"}}
    agent = SimpleNamespace(
        runtime_mode="native",
        employee_id="employee",
        selected_application=selected,
        applied_application=None,
        observed_state="stopped",
        last_error=error,
    )
    monkeypatch.setattr(
        "backend.app.applications.application_preview",
        lambda *_: {"application": {"fingerprint": "desired"}, "blockers": [], "changes": []},
    )
    annotate_agent(None, agent)
    assert agent.setup_status == expected
    assert agent.setup_pending


def test_legacy_receipt_marker_does_not_change_selected_configuration():
    application = {
        "role": {"id": "sales", "name": "Sales", "revision": 1, "capabilities": []},
        "employee_id": "employee",
    }
    current = normalize_application(application, "openclaw")
    legacy = normalize_application({**application, "legacy_receipt": True}, "openclaw")
    assert legacy["legacy_receipt"] is True
    assert legacy["fingerprint"] == current["fingerprint"]
