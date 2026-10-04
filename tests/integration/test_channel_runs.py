"""Channel turns share admission while retaining independent native histories."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from backend.app.connections import Connection
from backend.app.diagnostics import admit_user_run
from backend.app.models import Agent, Run, UserChannel
from tests.integration.test_diagnostics import FakeDriver

# ruff: noqa: F401, F811
from tests.integration.test_user_channels import (
    channel_setup,
    client,
    database_engine,
    lifecycle_sessions,
    profile_agent,
    session_maker,
    worker,
)
from worker.diagnostics import DiagnosticManager
from worker.lifecycle import configure_inference

pytestmark = pytest.mark.integration


@pytest.fixture
def ready_accesses(client, session_maker, channel_setup, worker):
    telegram, agent_id, user = channel_setup
    slack = client.post(
        "/api/v1/channels",
        json={"provider": "slack", "name": "Company", "workspace_id": "T12345"},
    ).json()
    pairs = []
    for channel, external_user, scope in ((telegram, "123456789", ""), (slack, "U12345", "T12345")):
        fields = ("bot_token",) if channel["provider"] == "telegram" else ("bot_token", "app_token")
        assert (
            client.put(
                f"/api/v1/channels/{channel['id']}/credentials",
                json={"values": dict.fromkeys(fields, "synthetic-token")},
            ).status_code
            == 200
        )
        channel = client.put(
            f"/api/v1/channels/{channel['id']}", json={"name": channel["name"], "enabled": True}
        ).json()
        with session_maker.begin() as session:
            row = session.get(UserChannel, UUID(channel["id"]))
            row.verified_version_id = session.get(Connection, row.connection_id).current_version_id
            row.verified_at = datetime.now(UTC)
        access = client.post(
            "/api/v1/user-accesses",
            json={
                "channel_id": channel["id"],
                "agent_id": str(agent_id),
                "user_id": user["id"],
                "external_user_id": external_user,
                "external_scope": scope,
            },
        ).json()
        access = client.post(f"/api/v1/user-accesses/{access['id']}/approve").json()
        pairs.append((access, channel))
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/start", headers={"Idempotency-Key": "channel-start"}
        ).status_code
        == 202
    )
    worker.process_one()
    return agent_id, pairs


def admit(sessions, pair, key, message="Hello"):
    access, channel = pair
    with sessions.begin() as session:
        return admit_user_run(
            session, UUID(access["id"]), message, key, channel_revision=channel["revision"]
        )


def test_shared_admission_and_separate_sessions(client, session_maker, ready_accesses):
    agent_id, pairs = ready_accesses
    telegram = admit(session_maker, pairs[0], "telegram-one")
    assert admit(session_maker, pairs[0], "telegram-one").id == telegram.id
    with pytest.raises(HTTPException) as error:
        admit(session_maker, pairs[1], "slack-one")
    assert error.value.status_code == 409
    with session_maker.begin() as session:
        session.get(Run, telegram.id).status = "completed"
    slack = admit(session_maker, pairs[1], "slack-one")
    assert slack.session_key != telegram.session_key
    assert ":employee:slack:" in slack.session_key
    assert ":employee:telegram:" in telegram.session_key
    assert slack.user_id == telegram.user_id
    assert client.get(f"/api/v1/agents/{agent_id}/runs").json() == []
    assert len(client.get(f"/api/v1/agents/{agent_id}/runs?source=user").json()) == 2


def test_revoked_queued_run_never_connects(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    run = admit(session_maker, pairs[0], "revoked")
    client.post(f"/api/v1/user-accesses/{pairs[0][0]['id']}/disable")
    connector = AsyncMock()
    asyncio.run(DiagnosticManager(session_maker, connector)._execute(run.id))
    connector.assert_not_called()
    with session_maker() as session:
        assert session.get(Run, run.id).status == "interrupted"


def test_revocation_while_connecting_prevents_send(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    run = admit(session_maker, pairs[0], "connecting")
    driver = FakeDriver()

    async def connect(_sessions, _agent_id):
        client.post(f"/api/v1/user-accesses/{pairs[0][0]['id']}/disable")
        return driver

    asyncio.run(DiagnosticManager(session_maker, connect)._execute(run.id))
    assert driver.sent == 0
    with session_maker() as session:
        assert session.get(Run, run.id).status == "interrupted"


def test_model_configuration_and_dispatch_use_user_session(session_maker, ready_accesses):
    agent_id, pairs = ready_accesses
    run = admit(session_maker, pairs[0], "scoped")
    with session_maker.begin() as session:
        session.get(Agent, agent_id).inference_override = {"model_id": "lab/model"}
    runtime = AsyncMock()
    runtime.request.return_value = {"models": [{"provider": "talos-openrouter", "id": "lab/model"}]}
    asyncio.run(configure_inference(session_maker, run, runtime))
    runtime.request.assert_any_call(
        "sessions.patch", {"key": run.session_key, "model": "talos-openrouter/lab/model"}
    )
    driver = FakeDriver()
    keys = []
    original = driver.send

    async def send(session_key, *args, **kwargs):
        keys.append(session_key)
        return await original(session_key, *args, **kwargs)

    driver.send = send
    asyncio.run(DiagnosticManager(session_maker, AsyncMock(return_value=driver))._execute(run.id))
    assert keys == [run.session_key]
    with session_maker() as session:
        assert session.get(Run, run.id).status == "completed"


def test_rotation_invalidates_queued_run(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    run = admit(session_maker, pairs[0], "rotated")
    client.put(
        f"/api/v1/channels/{pairs[0][1]['id']}/credentials",
        json={"values": {"bot_token": "replacement"}},
    )
    assert DiagnosticManager(session_maker, AsyncMock())._claim(run.id) is None
    with session_maker() as session:
        assert session.get(Run, run.id).status == "interrupted"


def test_replacing_identity_does_not_inherit_prior_conversation(
    client, session_maker, ready_accesses
):
    _, pairs = ready_accesses
    old = admit(session_maker, pairs[0], "old-identity")
    with session_maker.begin() as session:
        session.get(Run, old.id).status = "completed"
    access, channel = pairs[0]
    changed = client.put(
        f"/api/v1/user-accesses/{access['id']}",
        json={
            "channel_id": channel["id"],
            "user_id": access["user_id"],
            "agent_id": access["agent_id"],
            "external_user_id": "987654321",
        },
    )
    assert changed.status_code == 200
    approved = client.post(f"/api/v1/user-accesses/{access['id']}/approve").json()
    new = admit(session_maker, (approved, channel), "new-identity")
    assert new.session_key != old.session_key
