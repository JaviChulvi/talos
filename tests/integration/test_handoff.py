"""A challenge is not a model run, and delivery verification is not liveness."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.app.models import Agent, ChannelInbox, ChannelOutbox, DeliveryChallenge, Run
from connector.delivery import Delivery, TransportError

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
from tests.integration.test_slack_delivery import payload
from tests.integration.test_slack_delivery import receive as slack_receive
from tests.integration.test_telegram_delivery import receive as telegram_receive
from worker.diagnostics import DiagnosticManager

pytestmark = pytest.mark.integration


def receive(sessions, pair, text, event=1):
    if pair[1]["provider"] == "telegram":
        return telegram_receive(sessions, pair, event=event, message=text)
    return slack_receive(sessions, pair, payload(event_id=f"EvTest{event}", text=text))


def challenge(client, pair):
    response = client.post(f"/api/v1/user-accesses/{pair[0]['id']}/challenge")
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    return response.json()


def proof(client, pair):
    response = client.get(f"/api/v1/user-accesses/{pair[0]['id']}/handoff")
    assert response.status_code == 200
    return response.json()["history"][0]


def send(sessions, pair, error=None):
    transport = AsyncMock()
    transport.send.return_value = "synthetic-provider-id"
    transport.send.side_effect = error
    asyncio.run(Delivery(sessions).send_one(UUID(pair[1]["id"]), transport))
    return transport


@pytest.mark.parametrize("provider", [0, 1])
def test_challenge_then_native_reply_verifies_delivery(
    client, session_maker, ready_accesses, provider
):
    _, pairs = ready_accesses
    pair = pairs[provider]
    request = challenge(client, pair)
    text = ("/verify " if provider == 0 else "verify ") + request["token"]
    inbox = receive(session_maker, pair, text)
    assert inbox.code == "transport_challenge" and inbox.run_id is None
    with session_maker() as session:
        assert session.scalars(select(Run)).all() == []
        assert request["token"] not in str(session.scalar(select(ChannelOutbox)).parts)
        assert session.scalar(select(DeliveryChallenge)).token_hash != request["token"]
    assert proof(client, pair)["transport_state"] == "pending"
    send(session_maker, pair)
    assert proof(client, pair)["transport_state"] == "accepted"
    assert proof(client, pair)["verified_at"] is None
    turn = receive(session_maker, pair, "Hello after transport test", 2)
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(turn.run_id)
    )
    assert proof(client, pair)["verified_at"] is None
    send(session_maker, pair)
    evidence = proof(client, pair)
    assert evidence["verified_at"] and evidence["current"]
    assert evidence["receipt"]["run_id"] == str(turn.run_id)
    with session_maker() as session:
        run = session.get(Run, turn.run_id)
        assert evidence["snapshot"]["incarnation_id"] == str(run.incarnation_id)
        assert evidence["snapshot"]["configuration_fingerprint"] == run.availability_fingerprint
    assert evidence["receipt"]["accepted_parts"] == evidence["receipt"]["total_parts"] == 1
    assert request["token"] not in str(evidence)
    client.post(f"/api/v1/user-accesses/{pair[0]['id']}/disable")
    old = proof(client, pair)
    assert old["verified_at"] == evidence["verified_at"] and not old["current"]


@pytest.mark.parametrize("invalidator", ["expired", "replaced", "credentials", "model", "revoked"])
def test_challenge_scope_and_config_are_rechecked(
    client, session_maker, ready_accesses, invalidator
):
    agent_id, pairs = ready_accesses
    pair = pairs[0]
    request = challenge(client, pair)
    if invalidator == "expired":
        with session_maker.begin() as session:
            session.get(DeliveryChallenge, UUID(request["id"])).expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
    elif invalidator == "replaced":
        challenge(client, pair)
    elif invalidator == "credentials":
        client.put(
            f"/api/v1/channels/{pair[1]['id']}/credentials",
            json={"values": {"bot_token": "rotated-synthetic"}},
        )
    elif invalidator == "model":
        with session_maker.begin() as session:
            session.get(Agent, agent_id).inference_override = {"model_id": "changed"}
    else:
        client.post(f"/api/v1/user-accesses/{pair[0]['id']}/disable")
    inbox = receive(session_maker, pair, "/verify " + request["token"])
    assert inbox.code == (
        "channel_changed" if invalidator == "credentials" else "challenge_invalid"
    )
    assert inbox.run_id is None
    with session_maker() as session:
        assert session.scalars(select(ChannelOutbox)).all() == []


def test_token_cannot_cross_platform_or_identity(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    request = challenge(client, pairs[0])
    wrong = receive(session_maker, pairs[1], "verify " + request["token"])
    assert wrong.code == "challenge_invalid"
    stranger = telegram_receive(
        session_maker,
        pairs[0],
        event=99,
        message="/verify " + request["token"],
        **{
            "from": {"id": 987654321, "is_bot": False},
            "chat": {"id": 987654321, "type": "private"},
        },
    )
    assert stranger.code == "challenge_invalid"
    with session_maker() as session:
        assert session.get(DeliveryChallenge, UUID(request["id"])).consumed_at is None
    first = receive(session_maker, pairs[0], "/verify " + request["token"])
    second = receive(session_maker, pairs[0], "/verify " + request["token"], 2)
    assert first.code == "transport_challenge" and second.code == "challenge_invalid"


@pytest.mark.parametrize("failure", ["timeout", "restart", "revoked_after_receive"])
def test_unconfirmed_transport_never_verifies(client, session_maker, ready_accesses, failure):
    _, pairs = ready_accesses
    pair = pairs[0]
    request = challenge(client, pair)
    receive(session_maker, pair, "/verify " + request["token"])
    if failure == "timeout":
        send(session_maker, pair, TransportError("send_timeout", uncertain=True))
    elif failure == "restart":
        delivery = Delivery(session_maker)
        assert delivery.claim(UUID(pair[1]["id"]))
        delivery.recover()
    else:
        client.post(f"/api/v1/user-accesses/{pair[0]['id']}/disable")
        send(session_maker, pair).send.assert_not_awaited()
    evidence = proof(client, pair)
    assert evidence["transport_accepted_at"] is None and evidence["verified_at"] is None


def test_partial_reply_waits_for_all_parts(client, session_maker, ready_accesses):
    _, pairs = ready_accesses
    pair = pairs[0]
    request = challenge(client, pair)
    receive(session_maker, pair, "/verify " + request["token"])
    send(session_maker, pair)
    inbox = receive(session_maker, pair, "Give a long reply", 2)
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            inbox.run_id
        )
    )
    with session_maker.begin() as session:
        session.get(Run, inbox.run_id).output = "a" * 5000
    send(session_maker, pair)
    assert proof(client, pair)["verified_at"] is None
    with session_maker.begin() as session:
        row = session.scalar(select(ChannelOutbox).where(ChannelOutbox.inbox_id == inbox.id))
        row.retry_at = datetime.now(UTC) - timedelta(seconds=1)
    send(session_maker, pair)
    assert proof(client, pair)["receipt"]["accepted_parts"] == 2


@pytest.mark.parametrize("provider", [0, 1])
@pytest.mark.parametrize("length", [24000, 24001])
def test_bounded_reply_receipt_requires_complete_output(
    client, session_maker, ready_accesses, provider, length
):
    _, pairs = ready_accesses
    pair = pairs[provider]
    request = challenge(client, pair)
    receive(session_maker, pair, ("/verify " if provider == 0 else "verify ") + request["token"])
    send(session_maker, pair)
    inbox = receive(session_maker, pair, "Give a long reply", 2)
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            inbox.run_id
        )
    )
    with session_maker.begin() as session:
        session.get(Run, inbox.run_id).output = "a" * length
    # A rate limit must not lose the fact that the eventual response is incomplete.
    send(session_maker, pair, TransportError("rate_limited", retry_after=1))
    delivered = []
    for _ in range(17):
        with session_maker.begin() as session:
            outbox = session.scalar(select(ChannelOutbox).where(ChannelOutbox.inbox_id == inbox.id))
            if outbox.state == "sent":
                break
            outbox.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        transport = send(session_maker, pair)
        delivered.append(transport.send.call_args.args[1])
    else:
        pytest.fail("Bounded response did not finish delivery")
    truncated = length > 24000
    assert outbox.code == ("response_truncated" if truncated else "provider_accepted")
    assert (proof(client, pair)["verified_at"] is None) is truncated
    assert ("Respuesta recortada" in "".join(delivered)) is truncated
    assert outbox.next_part == len(outbox.parts) == len(delivered)


@pytest.mark.parametrize("case", ["empty", "failed", "unknown", "admin", "uncertain_send"])
def test_only_confirmed_user_replies_complete_handoff(client, session_maker, ready_accesses, case):
    _, pairs = ready_accesses
    pair = pairs[0]
    request = challenge(client, pair)
    receive(session_maker, pair, "/verify " + request["token"])
    send(session_maker, pair)
    inbox = receive(session_maker, pair, "First user message", 2)
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            inbox.run_id
        )
    )
    with session_maker.begin() as session:
        run = session.get(Run, inbox.run_id)
        if case == "empty":
            run.output = ""
        elif case in ("failed", "unknown"):
            run.status = case
        elif case == "admin":
            run.source = "admin"
    send(
        session_maker,
        pair,
        TransportError("timeout", uncertain=True) if case == "uncertain_send" else None,
    )
    assert proof(client, pair)["verified_at"] is None


def test_agent_change_after_challenge_receive_blocks_confirmation(
    client, session_maker, ready_accesses
):
    agent_id, pairs = ready_accesses
    pair = pairs[0]
    request = challenge(client, pair)
    receive(session_maker, pair, "/verify " + request["token"])
    with session_maker.begin() as session:
        session.get(Agent, agent_id).revision += 1
    send(session_maker, pair).send.assert_not_awaited()
    assert proof(client, pair)["transport_accepted_at"] is None


def test_pending_access_cannot_create_delivery_challenge(client, ready_accesses):
    _, pairs = ready_accesses
    path = f"/api/v1/user-accesses/{pairs[0][0]['id']}"
    client.post(path + "/disable")
    assert client.post(path + "/challenge").status_code == 409


@pytest.mark.parametrize("provider", [0, 1])
def test_reply_before_confirmation_acknowledgment_verifies_delivery(
    client, session_maker, ready_accesses, provider
):
    _, pairs = ready_accesses
    pair = pairs[provider]
    request = challenge(client, pair)
    receive(session_maker, pair, "/verify " + request["token"])
    turns = []

    async def confirmation_send(*_):
        # The provider has delivered confirmation; its HTTP acknowledgment is pending.
        turn = receive(session_maker, pair, "Immediate user reply", 2)
        turns.append(turn)
        await DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(
            turn.run_id
        )
        assert proof(client, pair)["transport_accepted_at"] is None
        return "confirmation-provider-id"

    transport = AsyncMock()
    transport.send.side_effect = confirmation_send
    asyncio.run(Delivery(session_maker).send_one(UUID(pair[1]["id"]), transport))
    with session_maker() as session:
        row = session.get(DeliveryChallenge, UUID(request["id"]))
        turn = session.get(Run, turns[0].run_id)
        assert row.consumed_at <= turn.created_at < row.accepted_at
    assert proof(client, pair)["verified_at"] is None
    send(session_maker, pair)
    evidence = proof(client, pair)
    assert evidence["verified_at"] and evidence["current"]
    assert evidence["receipt"]["run_id"] == str(turns[0].run_id)


@pytest.mark.parametrize("provider", [0, 1])
def test_reply_admitted_before_challenge_claim_does_not_verify_delivery(
    client, session_maker, ready_accesses, provider
):
    _, pairs = ready_accesses
    pair = pairs[provider]
    request = challenge(client, pair)
    turn = receive(session_maker, pair, "Earlier conversation")
    receive(session_maker, pair, "/verify " + request["token"], 2)
    send(session_maker, pair)
    assert proof(client, pair)["transport_state"] == "accepted"
    asyncio.run(
        DiagnosticManager(session_maker, AsyncMock(return_value=FakeDriver()))._execute(turn.run_id)
    )
    send(session_maker, pair)
    assert proof(client, pair)["verified_at"] is None
