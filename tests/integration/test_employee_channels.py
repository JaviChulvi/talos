"""Identity claims are not approval and channel secrets never become setup bindings."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.app.channels import claim_invitation
from backend.app.connections import Connection
from backend.app.models import AccessInvitation, EmployeeAccess, EmployeeChannel

# ruff: noqa: F401, F811
from tests.integration.test_lifecycle import (
    client,
    database_engine,
    role_agent,
    worker,
)
from tests.integration.test_lifecycle import (
    session_maker as lifecycle_sessions,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def session_maker(lifecycle_sessions):
    with lifecycle_sessions.kw["bind"].begin() as connection:
        connection.execute(
            text("TRUNCATE employee_channels, connections, connection_versions CASCADE")
        )
    return lifecycle_sessions


@pytest.fixture
def channel_setup(client, role_agent, tmp_path, monkeypatch):
    from backend.app.config import Settings

    monkeypatch.setattr(
        "backend.app.connections.get_settings",
        lambda: Settings(connection_secrets_dir=tmp_path / "secrets"),
    )
    channel = client.post("/api/v1/channels", json={"provider": "telegram", "name": "Staff"}).json()
    agent_id, _, _ = role_agent
    employee = client.get("/api/v1/employees").json()[0]
    return channel, agent_id, employee


def access_body(channel_setup, user_id=None):
    channel, agent_id, employee = channel_setup
    return {
        "channel_id": channel["id"],
        "agent_id": str(agent_id),
        "employee_id": employee["id"],
        "external_user_id": user_id,
    }


def test_channel_secrets_are_write_only_and_cannot_be_assigned(client, channel_setup):
    channel, _, employee = channel_setup
    secret = "123456:test-secret-channel-only"
    response = client.put(
        f"/api/v1/channels/{channel['id']}/credentials", json={"values": {"bot_token": secret}}
    )
    assert response.status_code == 200 and response.json()["credentials_configured"]
    assert not response.json()["verified"] and secret not in response.text
    assert secret not in client.get("/api/v1/channels").text
    assert client.delete(f"/api/v1/connections/{channel['connection_id']}").status_code == 409
    result = client.put(
        f"/api/v1/employees/{employee['id']}",
        json={
            "name": employee["name"],
            "role_id": employee["role_id"],
            "connection_overrides": {"messaging": channel["connection_id"]},
        },
    )
    assert result.status_code == 400
    assert "cannot be given to agents" in result.json()["detail"]


def test_invitation_requires_explicit_approval_and_is_single_use(
    client, session_maker, channel_setup
):
    channel, _, _ = channel_setup
    access = client.post("/api/v1/employee-accesses", json=access_body(channel_setup)).json()
    path = f"/api/v1/employee-accesses/{access['id']}"
    assert client.post(path + "/approve").status_code == 409
    invitation = client.post(path + "/invitation")
    assert invitation.status_code == 201
    assert invitation.headers["cache-control"] == "no-store"
    token = invitation.json()["token"]
    with session_maker.begin() as session:
        claim = claim_invitation(session, UUID(channel["id"]), token, "123456789", "")
        assert claim.state == "pending"
    assert client.get("/api/v1/employee-accesses").json()[0]["state"] == "pending"
    with session_maker.begin() as session:
        with pytest.raises(HTTPException) as error:
            claim_invitation(session, UUID(channel["id"]), token, "123456789", "")
        assert error.value.status_code == 403
    approved = client.post(path + "/approve")
    assert approved.status_code == 200 and approved.json()["state"] == "active"
    disabled = client.post(path + "/disable").json()
    assert disabled["state"] == "disabled" and disabled["revision"] > approved.json()["revision"]


def test_expired_and_replaced_invitations_cannot_claim(client, session_maker, channel_setup):
    channel, _, _ = channel_setup
    access = client.post("/api/v1/employee-accesses", json=access_body(channel_setup)).json()
    path = f"/api/v1/employee-accesses/{access['id']}/invitation"
    old = client.post(path).json()["token"]
    current = client.post(path).json()["token"]
    with session_maker.begin() as session:
        with pytest.raises(HTTPException):
            claim_invitation(session, UUID(channel["id"]), old, "123456789", "")
        for invitation in session.scalars(select(AccessInvitation)):
            invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with session_maker.begin() as session:
        with pytest.raises(HTTPException):
            claim_invitation(session, UUID(channel["id"]), current, "123456789", "")


def test_slack_workspace_and_user_ids_are_not_interchangeable(client, channel_setup):
    channel = client.post(
        "/api/v1/channels",
        json={
            "provider": "slack",
            "name": "Company",
            "workspace_id": "T12345",
        },
    ).json()
    body = {**access_body(channel_setup), "channel_id": channel["id"], "external_user_id": "U12345"}
    assert client.post("/api/v1/employee-accesses", json=body).status_code == 422
    body["external_scope"] = "T99999"
    assert client.post("/api/v1/employee-accesses", json=body).status_code == 422
    body["external_scope"] = "T12345"
    result = client.post("/api/v1/employee-accesses", json=body)
    assert result.status_code == 201
    assert result.json()["state"] == "pending"
    assert client.post("/api/v1/employee-accesses", json=body).status_code == 409


def test_known_identity_cannot_be_replaced_by_an_invitation(client, session_maker, channel_setup):
    channel, _, _ = channel_setup
    access = client.post(
        "/api/v1/employee-accesses", json=access_body(channel_setup, "123456789")
    ).json()
    token = client.post(f"/api/v1/employee-accesses/{access['id']}/invitation").json()["token"]
    with session_maker.begin() as session:
        with pytest.raises(HTTPException) as error:
            claim_invitation(session, UUID(channel["id"]), token, "987654321", "")
        assert error.value.status_code == 403
        assert session.get(EmployeeAccess, UUID(access["id"])).external_user_id == "123456789"


def test_duplicate_channel_is_conflict(client, channel_setup):
    response = client.post("/api/v1/channels", json={"provider": "telegram", "name": "Again"})
    assert response.status_code == 409


def test_rotation_invalidates_channel_identity(client, session_maker, channel_setup):
    channel, _, _ = channel_setup
    path = f"/api/v1/channels/{channel['id']}"
    first = client.put(path + "/credentials", json={"values": {"bot_token": "original"}}).json()
    with session_maker.begin() as session:
        row = session.get(EmployeeChannel, UUID(channel["id"]))
        row.verified_version_id = session.get(Connection, row.connection_id).current_version_id
        row.verified_at = datetime.now(UTC)
        row.identity = {"bot_id": "123456789"}
    assert client.get("/api/v1/channels").json()[0]["verified"]
    second = client.put(path + "/credentials", json={"values": {"bot_token": "replacement"}}).json()
    assert second["revision"] > first["revision"]
    assert not second["verified"] and second["identity"] == {}
    assert second["credential_version_id"] != first["credential_version_id"]


def test_access_requires_the_employees_own_native_agent(client, channel_setup):
    _, _, employee = channel_setup
    other = client.post(
        "/api/v1/agents",
        json={
            "display_name": "Unassigned",
            "employee_label": "Someone else",
        },
        headers={"Idempotency-Key": "other-native"},
    ).json()
    body = {**access_body(channel_setup, "123456789"), "agent_id": other["id"]}
    assert body["employee_id"] == employee["id"]
    assert client.post("/api/v1/employee-accesses", json=body).status_code == 409
