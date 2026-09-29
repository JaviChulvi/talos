"""Opt-in pinned native runtimes, real drivers, and local-only provider transports.

Run in the verification image with a Docker socket and a disposable PostgreSQL
database; no production provider keys or channel recipients are used.
"""

import asyncio
import ipaddress
import json
import os
import secrets
import threading
from unittest.mock import AsyncMock
from uuid import uuid4

import docker
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from backend.app.models import RUNTIME_RELEASES, Agent, Run
from connector.delivery import Delivery
from connector.slack import Slack
from connector.telegram import Telegram

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
from tests.integration.test_handoff import challenge, proof, receive
from tests.runtime_proof import ready
from tests.unit.test_slack import response, web_client
from worker.diagnostics import DiagnosticManager
from worker.hermes import HermesClient
from worker.openclaw import DeviceIdentity
from worker.runtime import (
    NATIVE_IMAGES,
    launch_options,
    prepare_volumes,
    runtime_config,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_NATIVE_CHANNEL_PROOF") != "1",
        reason="Opt-in disposable Docker native channel proof",
    ),
]


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_both_channels_use_native_scoped_history_and_receipts(
    client, session_maker, ready_accesses, runtime_kind, tmp_path
):
    agent_id, pairs = ready_accesses
    docker_client = docker.from_env()
    runner = docker_client.containers.get(os.environ["HOSTNAME"])
    name = "talos-channel-proof-" + uuid4().hex[:10]
    labels = {"io.talos.channel-proof": name}
    records = []
    token, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def provider(request: Request):
        assert request.headers["authorization"] == "Bearer " + token
        body = await request.json()
        records.append(body)
        users = [
            message.get("content", "") for message in body["messages"] if message["role"] == "user"
        ]
        text = "Native proof history: " + json.dumps(users)
        base = {"id": "chatcmpl-proof", "created": 1, "model": "fixture"}
        if body.get("stream"):

            async def chunks():
                for delta, finish in [({"role": "assistant", "content": text}, None), ({}, "stop")]:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                **base,
                                "object": "chat.completion.chunk",
                                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                            }
                        )
                        + "\n\n"
                    )
                yield "data: [DONE]\n\n"

            return StreamingResponse(chunks(), media_type="text/event-stream")
        return {
            **base,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8001, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    network, runtime = None, None
    volumes = [name + "-state", name + "-config"]
    try:
        occupied = [
            ipaddress.ip_network(config["Subnet"])
            for net in docker_client.networks.list()
            for config in (net.attrs.get("IPAM", {}).get("Config") or [])
            if config.get("Subnet")
        ]
        subnet = next(
            ipaddress.ip_network(f"10.253.{n}.0/24")
            for n in range(200, 250)
            if not any(ipaddress.ip_network(f"10.253.{n}.0/24").overlaps(net) for net in occupied)
        )
        network = docker_client.networks.create(
            name,
            internal=True,
            labels=labels,
            ipam=docker.types.IPAMConfig(pool_configs=[docker.types.IPAMPool(subnet=str(subnet))]),
        )
        network.connect(runner, aliases=["talos-gateway"])
        config = (
            runtime_config(control, token, "http://talos-gateway:8001")
            if runtime_kind == "openclaw"
            else {
                "model": {
                    "provider": "custom",
                    "default": "fixture",
                    "base_url": "http://talos-gateway:8001/v1",
                    "api_key": token,
                    "api_mode": "chat_completions",
                },
                "toolsets": [],
                "terminal": {"backend": "local", "cwd": "/opt/data/workspace"},
            }
        )
        prepare_volumes(
            docker_client, *volumes, config, labels, native=True, runtime_kind=runtime_kind
        )
        runtime = docker_client.containers.create(
            **launch_options(
                name,
                volumes[0],
                network.name,
                volumes[1],
                labels,
                native_image=NATIVE_IMAGES[runtime_kind],
                runtime_kind=runtime_kind,
                control_token=control,
                control_origin="http://localhost",
            )
        )
        runtime.start()
        identity = DeviceIdentity.load_or_create(tmp_path / "device.pem")
        with session_maker.begin() as session:
            agent = session.get(Agent, agent_id)
            agent.runtime_kind, agent.runtime_release = runtime_kind, RUNTIME_RELEASES[runtime_kind]

        async def connect(_sessions, _id):
            if runtime_kind == "hermes":
                return HermesClient(docker.from_env(), runtime.id)
            return await ready(f"ws://{name}:18789", control, identity, runtime)

        async def exercise():
            while not server.started:
                await asyncio.sleep(0.1)
            transports = []
            telegram_web = AsyncMock()
            # httpx Response.json is synchronous, unlike an arbitrary AsyncMock.
            import httpx

            telegram_web.post.return_value = httpx.Response(
                200, json={"ok": True, "result": {"message_id": 123}}
            )
            transports.append(Telegram("synthetic", client=telegram_web))
            slack_web = web_client()
            slack_web.chat_postMessage.return_value = response(
                {"ok": True, "channel": "D12345", "ts": "1.234"}
            )
            transports.append(Slack("bot", "app", web_client=slack_web))
            delivery = Delivery(session_maker)
            manager = DiagnosticManager(session_maker, connect, timeout=120)
            for index, pair in enumerate(pairs):
                request = challenge(client, pair)
                receive(
                    session_maker,
                    pair,
                    ("/verify " if index == 0 else "verify ") + request["token"],
                )
                assert await delivery.send_one(pair[1]["id"], transports[index])
                marker = "telegram-secret-CEDAR" if index == 0 else "slack-secret-MAPLE"
                inbox = receive(session_maker, pair, "Remember " + marker, 2)
                await manager._execute(inbox.run_id)
                with session_maker() as session:
                    run = session.get(Run, inbox.run_id)
                    assert run.status == "completed", run.error
                    assert marker in run.output
                    other = "slack-secret-MAPLE" if index == 0 else "telegram-secret-CEDAR"
                    assert other not in run.output
                await delivery.send_one(pair[1]["id"], transports[index])
                assert proof(client, pair)["verified_at"]
                print(
                    f"PASS {runtime_kind}/{pair[1]['provider']}: "
                    "native completed reply and provider-accepted receipt",
                    flush=True,
                )
            await asyncio.to_thread(runtime.stop, timeout=15)
            await asyncio.to_thread(runtime.start)
            for index, pair in enumerate(pairs):
                inbox = receive(session_maker, pair, "What did I ask you to remember?", 3)
                await manager._execute(inbox.run_id)
                with session_maker() as session:
                    run = session.get(Run, inbox.run_id)
                    assert run.status == "completed", run.error
                    marker = "telegram-secret-CEDAR" if index == 0 else "slack-secret-MAPLE"
                    other = "slack-secret-MAPLE" if index == 0 else "telegram-secret-CEDAR"
                    assert marker in run.output and other not in run.output
            print(
                f"PASS {runtime_kind}: private histories persist across native stop/start",
                flush=True,
            )

        asyncio.run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if runtime:
            runtime.remove(force=True)
        if network:
            network.disconnect(runner, force=True)
            network.remove()
        for volume in volumes:
            try:
                docker_client.volumes.get(volume).remove()
            except docker.errors.NotFound:
                pass
        docker_client.close()
