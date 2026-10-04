"""Durability, private identity filtering and uncertain response delivery."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.app.connections import Connection
from backend.app.models import ChannelCursor, ChannelInbox, ChannelOutbox, Run, UserChannel
from connector.delivery import Delivery, TransportError
from connector.main import Connector
from connector.telegram import Telegram

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
from worker.diagnostics import DiagnosticManager

pytestmark = pytest.mark.integration


def receive(sessions, pair, event=1, message="Hello", **changes):
    access, channel = pair
    update = {
        "update_id": event,
        "message": {
            "from": {"id": int(access["external_user_id"]), "is_bot": False},
            "chat": {"id": int(access["external_user_id"]), "type": "private"},
            "text": message,
            **changes,
        },
    }
    with sessions.begin() as session:
        Telegram("synthetic", client=AsyncMock()).receive(
            session,
            session.get(UserChannel, UUID(channel["id"])),
            channel["revision"],
            "test-bot",
            update,
        )
    with sessions() as session:
        return session.scalar(
            select(ChannelInbox).where(ChannelInbox.event_id == f"test-bot:{event}")
        )


def test_duplicate_update_has_one_run_and_outbox(session_maker, ready_accesses):
    _, pairs = ready_accesses
    first = receive(session_maker, pairs[0])
    second = receive(session_maker, pairs[0])
    assert first.id == second.id and first.run_id == second.run_id
    with session_maker() as session:
        assert len(session.scalars(select(Run)).all()) == 1
        assert len(session.scalars(select(ChannelOutbox)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"chat": {"id": 123456789, "type": "group"}},
        {"from": {"id": 123456789, "is_bot": True}},
        {"chat": {"id": 987654321, "type": "private"}},
        {"from": {"id": 987654321, "is_bot": False}, "chat": {"id": 987654321, "type": "private"}},
        {"forward_origin": {"type": "user"}},
    ],
)
def test_unapproved_and_non_private_events_do_not_run(session_maker, ready_accesses, change):
    _, pairs = ready_accesses
    receive(session_maker, pairs[0], **change)
    with session_maker() as session:
        assert session.scalars(select(Run)).all() == []
        assert session.scalars(select(ChannelOutbox)).all() == []


def test_native_commands_stay_at_connector(session_maker, ready_accesses):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs[0], message="/reset")
    assert inbox.code == "command" and inbox.run_id is None
    with session_maker() as session:
        assert "Comando no disponible" in session.scalar(select(ChannelOutbox)).parts[0]


def test_busy_response_is_not_blocked_by_older_running_turn(session_maker, ready_accesses):
    _, pairs = ready_accesses
    receive(session_maker, pairs[0], event=1)
    busy = receive(session_maker, pairs[0], event=2)
    assert busy.code == "unavailable" and busy.run_id is None
    delivery = Delivery(session_maker)
    claimed = delivery.claim(UUID(pairs[0][1]["id"]))
    assert claimed[1].id == busy.id


def test_completed_native_reply_goes_to_original_private_chat(session_maker, ready_accesses):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs[0])
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            inbox.run_id
        )
    )
    transport = AsyncMock()
    transport.send.return_value = "telegram-message-123"
    assert asyncio.run(Delivery(session_maker).send_one(UUID(pairs[0][1]["id"]), transport))
    transport.send.assert_awaited_once_with("123456789", "Final answer")
    with session_maker() as session:
        row = session.scalar(select(ChannelOutbox))
        assert row.state == "sent" and row.provider_ids == ["telegram-message-123"]


@pytest.mark.parametrize(
    "failure, expected",
    [
        (TransportError("network_unavailable", uncertain=True), "uncertain"),
        (TransportError("permission_denied"), "failed"),
        (TransportError("rate_limited", retry_after=30), "pending"),
    ],
)
def test_send_failure_is_classified_without_blind_retry(
    session_maker, ready_accesses, failure, expected
):
    _, pairs = ready_accesses
    receive(session_maker, pairs[0], message="/help")
    transport = AsyncMock()
    transport.send.side_effect = failure
    delivery = Delivery(session_maker)
    asyncio.run(delivery.send_one(UUID(pairs[0][1]["id"]), transport))
    assert not asyncio.run(delivery.send_one(UUID(pairs[0][1]["id"]), transport))
    assert transport.send.await_count == 1
    with session_maker() as session:
        assert session.scalar(select(ChannelOutbox)).state == expected


def test_connector_recovery_never_retries_unowned_intent(session_maker, ready_accesses):
    _, pairs = ready_accesses
    receive(session_maker, pairs[0], message="/help")
    delivery = Delivery(session_maker)
    assert delivery.claim(UUID(pairs[0][1]["id"]))
    delivery.recover()
    assert delivery.claim(UUID(pairs[0][1]["id"])) is None
    with session_maker() as session:
        assert session.scalar(select(ChannelOutbox)).state == "uncertain"


def test_revocation_between_response_parts_blocks_remaining(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs[0])
    with session_maker.begin() as session:
        run = session.get(Run, inbox.run_id)
        run.status, run.output = "completed", "A" * 5000
    transport = AsyncMock()
    transport.send.return_value = "part1"
    delivery = Delivery(session_maker)
    asyncio.run(delivery.send_one(UUID(pairs[0][1]["id"]), transport))
    client.post(f"/api/v1/user-accesses/{pairs[0][0]['id']}/disable")
    with session_maker.begin() as session:
        session.scalar(select(ChannelOutbox)).retry_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not asyncio.run(delivery.send_one(UUID(pairs[0][1]["id"]), transport))
    assert transport.send.await_count == 1
    with session_maker() as session:
        assert session.scalar(select(ChannelOutbox)).state == "blocked"


def test_invitation_is_pending_and_never_persisted_as_message(
    client, session_maker, ready_accesses
):
    _, pairs = ready_accesses
    access, channel = pairs[0]
    client.post(f"/api/v1/user-accesses/{access['id']}/disable")
    token = client.post(f"/api/v1/user-accesses/{access['id']}/invitation").json()["token"]
    inbox = receive(session_maker, pairs[0], message=f"/start {token}")
    assert inbox.code == "identity_pending" and inbox.run_id is None
    with session_maker() as session:
        assert session.scalars(select(Run)).all() == []
        assert token not in str(session.scalar(select(ChannelOutbox)).parts)
        assert token not in str(inbox.__dict__)
    actual = next(
        row for row in client.get("/api/v1/user-accesses").json() if row["id"] == access["id"]
    )
    assert actual["state"] == "pending"


def test_cursor_retains_offset_for_same_bot_but_resets_for_new_bot(session_maker, ready_accesses):
    _, pairs = ready_accesses
    _, channel = pairs[0]
    identifier = UUID(channel["id"])
    with session_maker() as session:
        version = session.get(Connection, UUID(channel["connection_id"])).current_version_id
    connector = Connector(session_maker)
    assert connector.report(
        identifier, channel["revision"], version, "ok", "polling_active", {"bot_id": "42"}
    )
    with session_maker.begin() as session:
        session.get(ChannelCursor, identifier).offset = 100
    assert connector.report(
        identifier, channel["revision"], version, "ok", "polling_active", {"bot_id": "42"}
    )
    with session_maker() as session:
        assert session.get(ChannelCursor, identifier).offset == 100
    assert connector.report(
        identifier, channel["revision"], version, "ok", "polling_active", {"bot_id": "99"}
    )
    with session_maker() as session:
        assert session.get(ChannelCursor, identifier).offset == 0
    assert not connector.report(
        identifier, channel["revision"] - 1, version, "ok", "polling_active"
    )


def test_database_lease_allows_one_consumer(database_engine):
    from sqlalchemy import text

    with database_engine.connect() as owner, database_engine.connect() as other:
        try:
            assert owner.scalar(text("SELECT pg_try_advisory_lock(1413565519, 1)"))
            assert not other.scalar(text("SELECT pg_try_advisory_lock(1413565519, 1)"))
        finally:
            owner.execute(text("SELECT pg_advisory_unlock(1413565519, 1)"))


def test_restored_telegram_discards_backlog_before_resuming(
    session_maker,
    ready_accesses,
    monkeypatch,
):
    _, pairs = ready_accesses
    _, channel_data = pairs[0]
    identifier = UUID(channel_data["id"])
    with session_maker.begin() as session:
        channel = session.get(UserChannel, identifier)
        version = session.get(Connection, channel.connection_id).current_version_id
        session.add(ChannelCursor(channel_id=identifier, reconnect_required=True))
    transport = AsyncMock()
    transport.verify.return_value = {"bot_id": "restored-bot"}
    transport.poll.side_effect = [
        [{"update_id": 100, "message": {"text": "old unsent message"}}],
        asyncio.CancelledError(),
    ]
    monkeypatch.setattr("connector.main.Telegram", lambda _: transport)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Connector(session_maker).consume(channel, version))
    assert [call.args for call in transport.poll.await_args_list] == [(-1,), (101,)]
    transport.receive.assert_not_called()
    with session_maker() as session:
        cursor = session.get(ChannelCursor, identifier)
        assert not cursor.reconnect_required and cursor.accept_after is not None
        assert cursor.offset == 101
        assert not session.scalar(select(ChannelInbox.id))
