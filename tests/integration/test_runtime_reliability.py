"""Kill real worker processes around a synthetic OpenClaw side effect.

PostgreSQL and the adapter's WebSocket transport are real. The runtime and
business action are local fixtures; no model, channel service or secret is used.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select, text
from websockets.asyncio.server import serve

from backend.app.models import ChannelOutbox, Run, RunEvent
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
from tests.integration.test_slack_delivery import receive as receive_slack
from tests.integration.test_telegram_delivery import receive as receive_telegram
from worker.diagnostics import DiagnosticManager

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

WORKER = """
import asyncio, os
from uuid import UUID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from worker.diagnostics import DiagnosticManager
from worker.openclaw import DeviceIdentity, OpenClawClient

sessions = sessionmaker(create_engine(os.environ['PROOF_DATABASE_URL']), expire_on_commit=False)
async def connect(*_):
    return await OpenClawClient(
        os.environ['PROOF_RUNTIME_URL'], 'synthetic',
        DeviceIdentity.load_or_create(__import__('pathlib').Path(os.environ['PROOF_IDENTITY'])),
        timeout=15,
    ).connect()
asyncio.run(DiagnosticManager(sessions, connect)._execute(UUID(os.environ['PROOF_RUN_ID'])))
"""


def child_environment(sessions, **values):
    engine = sessions.kw["bind"]
    with engine.connect() as connection:
        schema = connection.scalar(text("SHOW search_path"))
    url = engine.url.update_query_dict(
        {
            "options": f"-csearch_path={schema}",
            "application_name": "talos-proof-"
            + values.get("PROOF_RUN_ID", values.get("PROOF_CHANNEL", "")),
        }
    )
    return {
        **os.environ,
        "PROOF_DATABASE_URL": url.render_as_string(hide_password=False),
        **values,
    }


async def wait_for(predicate):
    async with asyncio.timeout(15):
        while not await asyncio.to_thread(predicate):
            await asyncio.sleep(0.02)


def receive(sessions, pairs, provider):
    return (
        receive_telegram(sessions, pairs[0])
        if provider == "telegram"
        else receive_slack(sessions, pairs[1])
    )


@pytest.mark.parametrize("provider", ["telegram", "slack"])
@pytest.mark.parametrize("boundary", ["before_ack", "during_tool", "before_result_commit"])
def test_killed_worker_never_repeats_external_action(
    session_maker, ready_accesses, tmp_path, provider, boundary
):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs, provider)
    journal = tmp_path / "external-actions.jsonl"

    async def scenario():
        action = asyncio.Event()
        publish_result = asyncio.Event()
        result_sent = asyncio.Event()

        async def gateway(ws):
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": "proof"},
                    }
                )
            )
            handshake = json.loads(await ws.recv())
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": handshake["id"],
                        "ok": True,
                        "payload": {"protocol": 4},
                    }
                )
            )
            request = json.loads(await ws.recv())
            assert request["method"] == "chat.send"
            assert request["params"]["idempotencyKey"] == str(inbox.run_id)
            # Independent evidence of an action outside the worker transaction.
            with journal.open("a") as output:
                output.write(
                    json.dumps({"action": "fixture-write", "run": str(inbox.run_id)}) + "\n"
                )
                output.flush()
                os.fsync(output.fileno())
            action.set()
            if boundary != "before_ack":
                await ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": request["id"],
                            "ok": True,
                            "payload": {"runId": str(inbox.run_id)},
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "agent",
                            "payload": {
                                "runId": str(inbox.run_id),
                                "stream": "tool",
                                "data": {
                                    "phase": "start",
                                    "name": "fixture-write",
                                    "toolCallId": "one",
                                },
                            },
                        }
                    )
                )
            if boundary == "before_result_commit":
                await publish_result.wait()
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "event": "chat",
                            "payload": {
                                "runId": str(inbox.run_id),
                                "state": "final",
                                "message": {"content": "Action completed"},
                            },
                        }
                    )
                )
                result_sent.set()
            await ws.wait_closed()

        async with serve(gateway, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            process = subprocess.Popen(
                [sys.executable, "-c", WORKER],
                cwd=ROOT,
                env=child_environment(
                    session_maker,
                    PROOF_RUNTIME_URL=f"ws://127.0.0.1:{port}",
                    PROOF_IDENTITY=str(tmp_path / "identity.pem"),
                    PROOF_RUN_ID=str(inbox.run_id),
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(action.wait(), 15)
                if boundary != "before_ack":

                    def tool_recorded():
                        with session_maker() as session:
                            return (
                                session.scalar(
                                    select(RunEvent.sequence).where(
                                        RunEvent.run_id == inbox.run_id,
                                        RunEvent.type == "tool",
                                    )
                                )
                                is not None
                            )

                    await wait_for(tool_recorded)
                if boundary == "before_result_commit":
                    # Hold the durable result row while the runtime completes.
                    with session_maker.begin() as session:
                        session.get(Run, inbox.run_id, with_for_update=True)
                        publish_result.set()
                        await asyncio.wait_for(result_sent.wait(), 5)

                        def result_blocked():
                            with session_maker() as observer:
                                return (
                                    observer.scalar(
                                        text(
                                            "SELECT count(*) FROM pg_stat_activity "
                                            "WHERE application_name = :name "
                                            "AND wait_event_type = 'Lock'"
                                        ),
                                        {"name": "talos-proof-" + str(inbox.run_id)},
                                    )
                                    == 1
                                )

                        await wait_for(result_blocked)
                        process.kill()
                        await asyncio.to_thread(process.wait, 5)
                else:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
                assert process.returncode < 0
            finally:
                if process.poll() is None:
                    process.kill()
                await asyncio.to_thread(process.wait, 5)
                process.stderr.close()

        restarted = DiagnosticManager(session_maker, AsyncMock())
        await restarted.tick()
        await restarted.close()
        restarted.connector.assert_not_called()

    asyncio.run(scenario())
    assert len(journal.read_text().splitlines()) == 1
    with session_maker() as session:
        run = session.get(Run, inbox.run_id)
        assert run.status == "unknown" and run.output == ""
        assert session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run.id,
                RunEvent.type == "unknown",
            )
        ).payload == {"reason": "worker_recovery"}
    # Provider redelivery returns the same admitted turn, without redispatch.
    assert receive(session_maker, pairs, provider).run_id == inbox.run_id


@pytest.mark.parametrize("provider", ["telegram", "slack"])
def test_killed_connector_never_resends_accepted_message(
    session_maker, ready_accesses, tmp_path, provider
):
    _, pairs = ready_accesses
    inbox = receive(session_maker, pairs, provider)
    with session_maker.begin() as session:
        run = session.get(Run, inbox.run_id)
        run.status, run.output = "completed", "Fixture reply"
    journal = tmp_path / "provider-accepted.txt"
    child = """
import asyncio, os
from uuid import UUID
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from connector.delivery import Delivery
class Provider:
    async def send(self, destination, text):
        with Path(os.environ['PROOF_JOURNAL']).open('a') as output:
            output.write(destination + '\\n')
            output.flush()
            os.fsync(output.fileno())
        await asyncio.Event().wait()  # Provider accepted; acknowledgment never arrives.
sessions = sessionmaker(create_engine(os.environ['PROOF_DATABASE_URL']), expire_on_commit=False)
asyncio.run(Delivery(sessions).send_one(UUID(os.environ['PROOF_CHANNEL']), Provider()))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child],
        cwd=ROOT,
        env=child_environment(
            session_maker,
            PROOF_CHANNEL=str(inbox.channel_id),
            PROOF_JOURNAL=str(journal),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        asyncio.run(wait_for(lambda: journal.exists() and journal.read_text().endswith("\n")))
        process.kill()
        process.wait(timeout=5)
        assert process.returncode < 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stderr.close()
    restarted = Delivery(session_maker)
    restarted.recover()
    provider_transport = AsyncMock()
    assert not asyncio.run(restarted.send_one(inbox.channel_id, provider_transport))
    provider_transport.send.assert_not_called()
    assert len(journal.read_text().splitlines()) == 1
    with session_maker() as session:
        outbox = session.scalar(select(ChannelOutbox).where(ChannelOutbox.inbox_id == inbox.id))
        assert outbox.state == "uncertain" and outbox.provider_ids == []
