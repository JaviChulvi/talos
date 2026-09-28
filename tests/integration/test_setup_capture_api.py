"""Capture admission and durable draft completion through the existing worker queue."""

from unittest.mock import Mock

import pytest

from tests.integration.test_lifecycle import (  # noqa: F401
    client,
    database_engine,
    role_agent,
    session_maker,
    worker,
)
from worker.setup_capture import SetupCaptureError

# Imported fixtures share the lifecycle harness; each test uses an isolated DB schema.
# ruff: noqa: F401, F811
pytestmark = pytest.mark.integration


def stop_initialized(client, worker, agent_id):
    path = f"/api/v1/agents/{agent_id}"
    client.post(path + "/start", headers={"Idempotency-Key": "initialize"})
    worker.process_one()
    client.post(path + "/stop", headers={"Idempotency-Key": "stop-source"})
    worker.process_one()
    return path


def test_capture_creates_one_reviewable_draft_and_preserves_source(
    client, worker, role_agent, monkeypatch, tmp_path
):
    from backend.app.config import get_settings

    monkeypatch.setattr(get_settings(), "setup_artifacts_dir", tmp_path)
    agent_id, _, _ = role_agent
    path = stop_initialized(client, worker, agent_id)
    before = client.get(path).json()
    capture = Mock(
        return_value={
            "manifest": {
                "schema_version": 1,
                "instructions": "",
                "targets": [],
                "skills": [],
                "connectors": [],
                "connection_slots": [],
                "unresolved": [],
                "assets": {},
            },
            "files": {},
            "metadata": {"candidates": [], "instructions_review_required": True},
        }
    )
    monkeypatch.setattr("worker.lifecycle.capture_setup", capture)
    response = client.post(path + "/capture-setup", headers={"Idempotency-Key": "capture"})
    assert response.status_code == 202
    # An admitted capture serializes mutations while the source is read.
    assert client.post(path + "/start", headers={"Idempotency-Key": "conflict"}).status_code == 409
    worker.process_one()
    operation = client.get("/api/v1/operations/" + response.json()["id"]).json()
    assert operation["status"] == "succeeded"
    draft = client.get("/api/v1/setups/" + operation["result"]["setup_id"]).json()
    assert draft["capture_metadata"]["instructions_review_required"]
    assert not draft["revisions"]
    replay = client.post(path + "/capture-setup", headers={"Idempotency-Key": "capture"}).json()
    assert replay["result"] == operation["result"]
    assert capture.call_count == 1
    after = client.get(path).json()
    for key in (
        "desired_state",
        "observed_state",
        "revision",
        "applied_application",
        "current_incarnation_id",
    ):
        assert before[key] == after[key]


def test_capture_failure_leaves_stopped_source_unchanged(client, worker, role_agent, monkeypatch):
    agent_id, _, _ = role_agent
    path = stop_initialized(client, worker, agent_id)
    before = client.get(path).json()
    monkeypatch.setattr(
        "worker.lifecycle.capture_setup",
        Mock(side_effect=SetupCaptureError("Capture requires review")),
    )
    response = client.post(path + "/capture-setup", headers={"Idempotency-Key": "failed"}).json()
    worker.process_one()
    operation = client.get("/api/v1/operations/" + response["id"]).json()
    assert operation["status"] == "failed" and operation["result"] is None
    after = client.get(path).json()
    assert after["observed_state"] == "stopped"
    assert after["last_error"] == before["last_error"]
    assert after["applied_application"] == before["applied_application"]


def test_capture_rejects_uninitialized_and_running_agents(client, worker, role_agent):
    agent_id, _, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    assert (
        client.post(path + "/capture-setup", headers={"Idempotency-Key": "empty"}).status_code
        == 409
    )
    client.post(path + "/start", headers={"Idempotency-Key": "start"})
    worker.process_one()
    assert (
        client.post(path + "/capture-setup", headers={"Idempotency-Key": "running"}).status_code
        == 409
    )
