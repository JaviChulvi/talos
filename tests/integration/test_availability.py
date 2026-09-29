"""Freshness and revision binding are verified against the real API/database."""

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.availability import CHECKS, availability, heartbeat, record_check
from backend.app.models import Agent

# Reuse the lifecycle harness rather than another database/worker fixture.
# ruff: noqa: F401, F811
from tests.integration.test_lifecycle import (
    client,
    database_engine,
    role_agent,
    session_maker,
    worker,
)

pytestmark = pytest.mark.integration


def test_missing_evidence_and_stopped_agent(client, role_agent):
    agent_id, _, _ = role_agent
    response = client.get(f"/api/v1/agents/{agent_id}/availability")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "stopped"
    assert next(c for c in result["checks"] if c["kind"] == "model")["state"] == "unknown"


def test_worker_expiry_and_revision_invalidation(session_maker, role_agent):
    agent_id, _, _ = role_agent
    now = datetime.now(UTC)
    with session_maker.begin() as session:
        agent = session.get(Agent, agent_id)
        agent.desired_state, agent.observed_state = "running", "ready"
        session.flush()
        heartbeat(session, "worker", now)
        for kind in CHECKS:
            record_check(session, agent, kind, "ok", "verified", now=now)
        assert availability(session, agent, now)["status"] == "available"
        expired = availability(session, agent, now + timedelta(seconds=31))
        assert expired["status"] == "unverified"
        assert expired["checks"][0]["state"] == "stale"
        agent.revision += 1
        session.flush()
        changed = availability(session, agent, now)
        assert changed["status"] == "unverified"
        assert next(c for c in changed["checks"] if c["kind"] == "model")["state"] == "stale"


def test_pending_role_edit_does_not_invalidate_applied_evidence(
    client, session_maker, worker, role_agent
):
    agent_id, role, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    client.post(path + "/start", headers={"Idempotency-Key": "availability-start"})
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, agent_id)
        heartbeat(session, "worker")
        record_check(session, agent, "model", "ok", "model_responded")
    client.put(f"/api/v1/roles/{role['id']}", json={"name": "Changed", "capabilities": []})
    result = client.get(path + "/availability").json()
    assert result["status"] == "available"
    assert result["update_available"]


def test_lifecycle_records_runtime_evidence(client, worker, role_agent):
    agent_id, _, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    client.post(path + "/start", headers={"Idempotency-Key": "runtime-evidence"})
    worker.process_one()
    result = client.get(path + "/availability").json()
    runtime = next(c for c in result["checks"] if c["kind"] == "runtime")
    assert runtime["state"] == "ok" and runtime["code"] == "runtime_connected"
    assert result["status"] == "unverified"


def test_get_does_not_probe_network(client, role_agent, monkeypatch):
    import socket

    def forbidden(*_, **__):
        raise AssertionError("GET made an external probe")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert client.get(f"/api/v1/agents/{role_agent[0]}/availability").status_code == 200


def test_platform_status_uses_recent_heartbeats(client, session_maker, monkeypatch):
    monkeypatch.setattr("backend.app.main.database_ready", lambda: True)
    with session_maker.begin() as session:
        heartbeat(session, "worker")
        heartbeat(session, "gateway")
    assert client.get("/api/v1/status").json()["status"] == "ok"
    with session_maker.begin() as session:
        heartbeat(session, "worker", datetime.now(UTC) - timedelta(minutes=1))
    result = client.get("/api/v1/status").json()
    assert result["worker"] == "stale" and result["status"] == "degraded"


def test_expired_runtime_identity_is_blocked(client, session_maker, worker, role_agent):
    agent_id, _, _ = role_agent
    path = f"/api/v1/agents/{agent_id}"
    client.post(path + "/start", headers={"Idempotency-Key": "expire-runtime"})
    worker.process_one()
    with session_maker.begin() as session:
        agent = session.get(Agent, agent_id)
        agent.current_incarnation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    runtime = next(
        c for c in client.get(path + "/availability").json()["checks"] if c["kind"] == "runtime"
    )
    assert runtime["state"] == "blocked" and runtime["code"] == "runtime_identity_inactive"
