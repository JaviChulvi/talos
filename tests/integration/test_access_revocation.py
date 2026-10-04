"""Revocation during user work cancels the runtime and blocks channel delivery."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.app.models import Agent, ChannelOutbox, Run, UserChannel
from connector.delivery import Delivery

# ruff: noqa: F401, F811
from tests.integration.test_channel_runs import (
    channel_setup,
    client,
    database_engine,
    lifecycle_sessions,
    profile_agent,
    ready_accesses,
    session_maker,
    worker,
)
from tests.integration.test_diagnostics import FakeDriver
from tests.integration.test_runtime_reliability import receive, wait_for
from worker.diagnostics import DiagnosticManager
from worker.openclaw import GatewayError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("provider", ["telegram", "slack"])
@pytest.mark.parametrize(
    "change", ["access", "credential", "channel", "assignment", "verification"]
)
def test_active_user_run_is_cancelled_when_authorization_changes(
    client, session_maker, ready_accesses, provider, change
):
    agent_id, pairs = ready_accesses
    access, channel = pairs[0 if provider == "telegram" else 1]
    inbox = receive(session_maker, pairs, provider)
    driver = FakeDriver(streaming=True)
    abort_seen = asyncio.Event()
    original_abort = driver.abort

    async def abort(*args):
        result = await original_abort(*args)
        abort_seen.set()
        return result

    driver.abort = abort

    async def scenario():
        manager = DiagnosticManager(session_maker, AsyncMock(return_value=driver), timeout=5)
        task = asyncio.create_task(manager._execute(inbox.run_id))
        try:

            def running():
                with session_maker() as session:
                    return session.get(Run, inbox.run_id).status == "running"

            await wait_for(running)
            if change == "access":
                assert (
                    client.post(f"/api/v1/user-accesses/{access['id']}/disable").status_code == 200
                )
            elif change == "credential":
                fields = ("bot_token",) if provider == "telegram" else ("bot_token", "app_token")
                assert (
                    client.put(
                        f"/api/v1/channels/{channel['id']}/credentials",
                        json={"values": dict.fromkeys(fields, "synthetic-replacement")},
                    ).status_code
                    == 200
                )
            elif change == "channel":
                assert (
                    client.put(
                        f"/api/v1/channels/{channel['id']}",
                        json={"name": channel["name"], "enabled": False},
                    ).status_code
                    == 200
                )
            else:
                with session_maker.begin() as session:
                    if change == "assignment":
                        session.get(Agent, agent_id).user_id = None
                    else:
                        session.get(UserChannel, UUID(channel["id"])).verified_version_id = None
            # This is cancellation of user work, independent of an admin UI connection.
            await asyncio.wait_for(abort_seen.wait(), 1.5)
            await asyncio.wait_for(task, 1.5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert driver.sent == driver.aborted == 1
    with session_maker() as session:
        run = session.get(Run, inbox.run_id)
        assert run.cancel_requested and run.status == "cancelled"
    transport = AsyncMock()
    assert not asyncio.run(Delivery(session_maker).send_one(inbox.channel_id, transport))
    transport.send.assert_not_called()
    with session_maker() as session:
        assert session.scalar(select(ChannelOutbox)).state == "blocked"


def test_rejected_revocation_abort_keeps_run_unresolved(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs, "telegram")
    driver = FakeDriver(streaming=True)
    driver.abort = AsyncMock(side_effect=GatewayError({"code": "UNAVAILABLE"}))

    async def send(*args):
        driver.sent += 1
        client.post(f"/api/v1/user-accesses/{pairs[0][0]['id']}/disable")
        return {"runId": str(inbox.run_id)}

    driver.send = send
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=driver))._execute(inbox.run_id)
    )
    driver.abort.assert_awaited_once()
    with session_maker() as session:
        assert session.get(Run, inbox.run_id).status == "unknown"
