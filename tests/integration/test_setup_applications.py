"""Exercise explicit application selection through the real API and operation worker."""

import pytest

# Imported fixtures intentionally share the existing lifecycle test harness.
# ruff: noqa: F401, F811
from tests.integration.test_lifecycle import (  # noqa: F401
    client,
    database_engine,
    role_agent,
    session_maker,
    worker,
)

pytestmark = pytest.mark.integration


def test_restart_retains_selection_until_explicit_apply(client, worker, role_agent):
    agent_id, role, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    assert client.post(path + "/start", headers={"Idempotency-Key": "initial"}).status_code == 202
    worker.process_one()
    first = client.get(path).json()["applied_application"]
    client.put(
        f"/api/v1/roles/{role['id']}", json={"name": "Sales", "capabilities": ["web_research"]}
    )
    assert client.post(path + "/stop", headers={"Idempotency-Key": "stop"}).status_code == 202
    worker.process_one()
    assert client.post(path + "/start", headers={"Idempotency-Key": "restart"}).status_code == 202
    worker.process_one()
    restarted = client.get(path).json()
    assert restarted["applied_application"] == first
    assert restarted["setup_pending"] and restarted["permissions_pending"]
    preview = client.post(path + "/setup-preview").json()
    assert preview["changes"] and not preview["blockers"]
    assert (
        client.post(path + "/apply-role", headers={"Idempotency-Key": "apply"}).status_code == 202
    )
    worker.process_one()
    applied = client.get(path).json()
    assert applied["applied_application"]["role"]["capabilities"] == ["web_research"]
    assert not applied["setup_pending"]


def test_reassignment_requires_explicit_apply(client, worker, role_agent):
    agent_id, role, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    client.post(path + "/apply-role", headers={"Idempotency-Key": "initial"})
    worker.process_one()
    employee = client.post(
        "/api/v1/employees", json={"name": "Other employee", "role_id": role["id"]}
    ).json()
    assert client.put(path + "/employee", json={"employee_id": employee["id"]}).status_code == 200
    response = client.post(path + "/start", headers={"Idempotency-Key": "start"})
    assert response.status_code == 409
    assert "Employee changed" in response.json()["detail"]
    assert (
        client.post(path + "/apply-role", headers={"Idempotency-Key": "reassign"}).status_code
        == 202
    )
    worker.process_one()
    assert client.get(path).json()["applied_application"]["employee_id"] == employee["id"]
