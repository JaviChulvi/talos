"""Identity claims are not approval and channel secrets never become setup bindings."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.app.channels import claim_invitation
from backend.app.connections import Connection
from backend.app.models import AccessInvitation, Employee, EmployeeAccess, EmployeeChannel

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
    assert channel["connection_id"] not in {
        row["id"] for row in client.get("/api/v1/connections").json()
    }
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


def test_duplicate_invitation_claim_keeps_outer_transaction_usable(
    client, session_maker, channel_setup
):
    channel, agent_id, employee = channel_setup
    access = client.post("/api/v1/employee-accesses", json=access_body(channel_setup)).json()
    token = client.post(f"/api/v1/employee-accesses/{access['id']}/invitation").json()["token"]
    with session_maker.begin() as session:
        other = Employee(name="Another employee", role_id=UUID(employee["role_id"]))
        session.add(other)
        session.flush()
        session.add(
            EmployeeAccess(
                channel_id=UUID(channel["id"]),
                employee_id=other.id,
                agent_id=agent_id,
                external_user_id="123456789",
                state="disabled",
            )
        )
    with session_maker.begin() as session:
        revision = session.get(EmployeeAccess, UUID(access["id"])).revision
        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                claim_invitation(session, UUID(channel["id"]), token, "123456789", "")
            assert error.value.status_code == 403
            row = session.get(EmployeeAccess, UUID(access["id"]))
            assert row.external_user_id is None and row.revision == revision
        # The caller can persist progress after rejecting this event.
        session.get(EmployeeChannel, UUID(channel["id"])).name = "Progress committed"
        assert session.scalar(select(AccessInvitation)).consumed_at is None
    assert client.get("/api/v1/channels").json()[0]["name"] == "Progress committed"


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


@pytest.mark.parametrize("approve", [False, True])
def test_workspace_correction_invalidates_verification_access_and_invitations(
    client, session_maker, channel_setup, approve
):
    channel = client.post(
        "/api/v1/channels",
        json={"provider": "slack", "name": "Slack", "workspace_id": "TWRONG"},
    ).json()
    path = f"/api/v1/channels/{channel['id']}"
    configured = client.put(
        path + "/credentials", json={"values": {"app_token": "xapp-test", "bot_token": "xoxb-test"}}
    ).json()
    body = {
        **access_body(channel_setup),
        "channel_id": channel["id"],
        "external_scope": "TWRONG",
        "external_user_id": "U12345",
    }
    access = client.post("/api/v1/employee-accesses", json=body).json()
    access_path = f"/api/v1/employee-accesses/{access['id']}"
    invitation = client.post(access_path + "/invitation").json()["token"]
    if approve:
        client.post(access_path + "/approve")
    before = client.get("/api/v1/employee-accesses").json()[0]
    with session_maker.begin() as session:
        row = session.get(EmployeeChannel, UUID(channel["id"]))
        row.enabled = True
        row.verified_version_id = UUID(configured["credential_version_id"])
        row.verified_at = datetime.now(UTC)
        row.identity = {"team_id": "TWRONG"}
    corrected = client.put(
        path, json={"name": "Slack", "enabled": True, "workspace_id": "TCORRECT"}
    )
    assert corrected.status_code == 200
    result = corrected.json()
    assert result["workspace_id"] == "TCORRECT"
    assert not result["enabled"] and not result["verified"]
    assert result["verified_at"] is None and result["identity"] == {}
    assert result["revision"] > configured["revision"]
    assert result["credential_version_id"] == configured["credential_version_id"]
    current = client.get("/api/v1/employee-accesses").json()[0]
    assert current["state"] == "pending" and current["revision"] > before["revision"]
    assert client.post(access_path + "/approve").status_code == 422
    with session_maker.begin() as session:
        with pytest.raises(HTTPException):
            claim_invitation(session, UUID(channel["id"]), invitation, "U12345", "TCORRECT")
    body["external_scope"] = "TCORRECT"
    assert client.put(access_path, json=body).status_code == 200
    assert client.post(access_path + "/approve").json()["state"] == "active"


def test_workspace_edits_validate_provider_and_preserve_existing_update_requests(
    client, channel_setup
):
    telegram, _, _ = channel_setup
    path = f"/api/v1/channels/{telegram['id']}"
    assert client.put(
        path, json={"name": "Staff", "enabled": False, "workspace_id": "T12345"}
    ).status_code == 422
    slack = client.post(
        "/api/v1/channels", json={"provider": "slack", "name": "Slack", "workspace_id": "T12345"}
    ).json()
    path = f"/api/v1/channels/{slack['id']}"
    for workspace in ("", "invalid", "T1"):
        assert client.put(
            path, json={"name": "Slack", "enabled": False, "workspace_id": workspace}
        ).status_code == 422
    updated = client.put(path, json={"name": "Renamed", "enabled": True}).json()
    assert updated["workspace_id"] == "T12345" and updated["enabled"]
    assert updated["revision"] == slack["revision"] + 1
