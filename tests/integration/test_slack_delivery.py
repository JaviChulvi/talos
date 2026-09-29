import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.app.models import ChannelInbox, ChannelOutbox, EmployeeChannel, Run
from connector.delivery import Delivery
from connector.slack import Slack

# ruff: noqa: F401, F811
from tests.integration.test_channel_runs import (
    channel_setup,
    client,
    database_engine,
    lifecycle_sessions,
    ready_accesses,
    role_agent,
    session_maker,
    worker,
)
from tests.integration.test_diagnostics import FakeDriver
from tests.integration.test_telegram_delivery import receive as receive_telegram
from tests.unit.test_slack import response, web_client
from worker.diagnostics import DiagnosticManager

pytestmark = pytest.mark.integration


def payload(event_id="Ev12345", **changes):
    return {
        "team_id": "T12345",
        "api_app_id": "A12345",
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": "D12345",
            "user": "U12345",
            "text": "Hello",
            **changes,
        },
    }


def receive(sessions, pair, data=None):
    _, channel = pair
    transport = Slack("bot", "app", web_client=web_client())
    transport.identity = {"app_id": "A12345", "user_id": "U54321"}
    with sessions.begin() as session:
        return transport.receive(
            session,
            session.get(EmployeeChannel, UUID(channel["id"])),
            channel["revision"],
            data or payload(),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"subtype": "message_changed"},
        {"bot_id": "B12345"},
        {"channel_type": "channel", "channel": "C12345"},
        {"user": "U54321"},
        {"team": "T99999"},
        {"is_ext_shared_channel": True},
        {"user": "U99999"},
    ],
)
def test_private_workspace_filtering(session_maker, ready_accesses, changes):
    _, pairs = ready_accesses
    receive(session_maker, pairs[1], payload(**changes))
    with session_maker() as session:
        assert session.scalars(select(Run)).all() == []
        assert session.scalars(select(ChannelOutbox)).all() == []


def test_wrong_app_or_workspace_never_dispatches(session_maker, ready_accesses):
    _, pairs = ready_accesses
    for changes in (
        {"api_app_id": "A99999"},
        {"team_id": "T99999"},
        {"is_ext_shared_channel": True},
    ):
        receive(session_maker, pairs[1], {**payload(), **changes})
    with session_maker() as session:
        assert session.scalars(select(ChannelInbox)).all() == []


def test_event_retry_deduplicates_independent_of_envelope(session_maker, ready_accesses):
    _, pairs = ready_accesses
    one = receive(session_maker, pairs[1])
    two = receive(session_maker, pairs[1])
    assert one.id == two.id and one.run_id == two.run_id
    with session_maker() as session:
        assert len(session.scalars(select(Run)).all()) == 1


def test_slack_and_telegram_share_busy_admission(session_maker, ready_accesses):
    _, pairs = ready_accesses
    telegram = receive_telegram(session_maker, pairs[0])
    slack = receive(session_maker, pairs[1])
    assert telegram.code == "admitted" and slack.code == "unavailable"
    with session_maker() as session:
        assert len(session.scalars(select(Run)).all()) == 1


def test_native_result_returns_in_original_slack_dm(session_maker, ready_accesses):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs[1])
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            inbox.run_id
        )
    )
    web = web_client()
    web.chat_postMessage.return_value = response({"ok": True, "channel": "D12345", "ts": "1.234"})
    transport = Slack("bot", "app", web_client=web)
    asyncio.run(Delivery(session_maker).send_one(UUID(pairs[1][1]["id"]), transport))
    assert web.chat_postMessage.call_args.kwargs["channel"] == "D12345"
    assert web.chat_postMessage.call_args.kwargs["text"] == "Final answer"
    with session_maker() as session:
        assert session.scalar(select(ChannelOutbox)).state == "sent"


def test_envelope_ack_happens_after_commit_and_not_on_storage_failure(
    session_maker, ready_accesses
):
    _, pairs = ready_accesses
    with session_maker() as session:
        channel = session.get(EmployeeChannel, UUID(pairs[1][1]["id"]))

    class Socket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []
            self.observe = kwargs["on_message_listeners"][0]
            self.send_socket_mode_response = AsyncMock()

        async def connect(self):
            import json

            await self.observe(
                SimpleNamespace(
                    data=json.dumps(
                        {
                            "type": "hello",
                            "connection_info": {"app_id": "A12345"},
                            "num_connections": 1,
                        }
                    )
                )
            )

        async def close(self):
            pass

    async def check():
        web = web_client()
        web.apps_connections_open.return_value = response({"ok": True, "url": "wss://test.invalid"})
        transport = Slack("bot", "app", web_client=web, socket_factory=Socket)
        await transport.verify("T12345")
        await transport.connect(session_maker, channel)
        listener = transport.socket.socket_mode_request_listeners[0]

        async def ack(result):
            with session_maker() as session:
                assert session.scalar(select(ChannelInbox)).run_id is not None

        transport.socket.send_socket_mode_response.side_effect = ack
        for envelope in ("envelope-one", "envelope-two"):
            await listener(
                transport.socket,
                SimpleNamespace(type="events_api", payload=payload(), envelope_id=envelope),
            )
        assert transport.socket.send_socket_mode_response.await_count == 2
        with session_maker() as session:
            assert len(session.scalars(select(Run)).all()) == 1
        from sqlalchemy.exc import OperationalError

        class BrokenSessions:
            def begin(self):
                raise OperationalError("database down", None, None)

        await transport.close()
        transport = Slack("bot", "app", web_client=web, socket_factory=Socket)
        await transport.verify("T12345")
        await transport.connect(BrokenSessions(), channel)
        await transport.socket.socket_mode_request_listeners[0](
            transport.socket,
            SimpleNamespace(type="events_api", payload=payload("EvNext"), envelope_id="no-ack"),
        )
        transport.socket.send_socket_mode_response.assert_not_awaited()
        assert transport.failure == "database_unavailable"
        await transport.close()

    asyncio.run(check())
