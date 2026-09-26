"""Focused OpenClaw protocol test, run inside the disposable proof container."""

import asyncio
import json
import os
import secrets
import threading
import uuid
from pathlib import Path

import docker
import httpx
import uvicorn
from fastapi import FastAPI

from gateway import openrouter
from gateway.fake_model import model_router
from worker.openclaw import DeviceIdentity, GatewayError, OpenClawClient
from worker.runtime import (
    IMAGE,
    approve_device,
    launch_options,
    prepare_volumes,
    runtime_config,
)

PROOF_ID = uuid.uuid4().hex[:10]
LABELS = {"io.talos.proof": PROOF_ID}


async def ready(url, token, identity, runtime):
    last = None
    for _ in range(45):
        runtime.reload()
        if runtime.status == "exited":
            raise RuntimeError("OpenClaw exited during startup")
        client = OpenClawClient(url, token, identity, timeout=10)
        try:
            await client.connect()
            return client
        except GatewayError as exc:
            print("Waiting for Gateway:", exc.code, flush=True)
            if exc.code == "NOT_PAIRED" or exc.details.get("code") == "PAIRING_REQUIRED":
                await asyncio.to_thread(approve_device, runtime, identity)
            elif exc.code != "UNAVAILABLE":
                raise
        except (OSError, TimeoutError, EOFError) as exc:
            last = exc
        await asyncio.sleep(2)
    raise TimeoutError(f"Runtime unavailable: {last}")


async def terminal(client, run_id):
    async with asyncio.timeout(60):
        while True:
            event = await client.next_event()
            if event.get("type") == "disconnect":
                raise RuntimeError(event)
            payload = event.get("payload", {})
            if event.get("event") == "chat" and payload.get("runId") == run_id:
                if payload.get("state") in ("final", "error", "aborted"):
                    return payload


async def main():
    docker_client = docker.from_env()
    runner = docker_client.containers.get(os.environ["HOSTNAME"])
    tokens = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    selected = {"model": "fixture"}
    slow_started, slow_closed = threading.Event(), threading.Event()
    key_file = Path("/tmp") / (PROOF_ID + "-openrouter-key")
    key_file.write_text("synthetic-provider-key")
    os.environ["TALOS_OPENROUTER_KEY_FILE"] = str(key_file)

    class ProviderStream(httpx.AsyncByteStream):
        def __init__(self, message, model):
            self.message, self.model = message, model

        async def __aiter__(self):
            if "[openrouter-slow]" in self.message:
                slow_started.set()
                await asyncio.sleep(60)
            if "[openrouter-error]" in self.message:
                yield b'data: {"error":{"message":"synthetic upstream failure"}}\n\n'
                return
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": "OpenRouter transport reply from " + self.model,
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
            yield ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()

        async def aclose(self):
            if "[openrouter-slow]" in self.message:
                slow_closed.set()

    async def provider(request):
        assert request.headers["authorization"] == "Bearer synthetic-provider-key"
        body = json.loads(request.content)
        message = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ProviderStream(message, body["model"]),
        )

    real_client = httpx.AsyncClient
    openrouter.httpx.AsyncClient = lambda **kw: real_client(
        transport=httpx.MockTransport(provider), **kw
    )
    app = FastAPI()
    app.include_router(model_router(lambda t: t in tokens, lambda _: selected["model"]))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.1)
    resources = []
    volumes = []
    clients = []
    try:
        for number in range(2):
            prefix = f"talos-f2-{PROOF_ID}-{number}"
            network = docker_client.networks.create(prefix, internal=True, labels=LABELS)
            resources.append(network)
            network.connect(runner, aliases=["fake-model"])
            control = secrets.token_urlsafe(32)
            config = runtime_config(
                control,
                tokens[number],
                "http://fake-model:8000",
                model_route="default" if number == 1 else "fixture",
            )
            if os.environ.get("TALOS_PROOF_RAM_VOLUMES") == "1":
                for kind in ("state", "config"):
                    docker_client.volumes.create(
                        prefix + "-" + kind,
                        labels=LABELS,
                        driver_opts={
                            "type": "tmpfs",
                            "device": "tmpfs",
                            "o": "size=256m,uid=1000,gid=1000",
                        },
                    )
                keeper = docker_client.containers.run(
                    os.environ.get("TALOS_PROOF_RUNNER_IMAGE", "talos-verification:local"),
                    ["sleep", "600"],
                    name=prefix + "-volume-holder",
                    network_mode="none",
                    labels=LABELS,
                    detach=True,
                    volumes={
                        prefix + "-state": {"bind": "/state", "mode": "rw"},
                        prefix + "-config": {"bind": "/config", "mode": "rw"},
                    },
                )
                resources.append(keeper)
            volumes.extend([prefix + "-state", prefix + "-config"])
            prepare_volumes(docker_client, prefix + "-state", prefix + "-config", config, LABELS)
            runtime = docker_client.containers.create(
                **launch_options(
                    prefix, prefix + "-state", network.name, prefix + "-config", LABELS
                )
            )
            resources.append(runtime)
            runtime.start()
            try:
                identity = DeviceIdentity.load_or_create(Path("/tmp") / (prefix + ".pem"))
                client = await ready(f"ws://{prefix}:18789", control, identity, runtime)
            except BaseException:
                print(runtime.logs().decode()[-18000:], flush=True)
                raise
            clients.append(client)
            run_id = str(uuid.uuid4())
            session = "agent:main:talos-diagnostic"
            reply = await client.send(session, f"agent-{number}-unique-message", run_id)
            print("ACK", number, reply, flush=True)
            result = await terminal(client, run_id)
            print("RESULT", number, result, flush=True)
            assert result["state"] == "final"
            history = await client.history(session)
            serialized = json.dumps(history)
            assert f"agent-{number}-unique-message" in serialized
            assert "Talos diagnostic:" in serialized
            assert f"agent-{1 - number}-unique-message" not in serialized
            slow_id = str(uuid.uuid4())
            await client.send(session, "[slow] cancel this response", slow_id)
            await asyncio.sleep(1)
            abort = await client.abort(session, slow_id)
            print("ABORT", abort, flush=True)
            assert abort.get("aborted")
            aborted = await terminal(client, slow_id)
            assert aborted["state"] == "aborted"
            await client.close()
            runtime.stop(timeout=15)
            runtime.start()
            client = await ready(f"ws://{prefix}:18789", control, identity, runtime)
            clients.append(client)
            assert f"agent-{number}-unique-message" in json.dumps(await client.history(session))
            print(
                "PASS",
                number,
                "connect/send/events/history/abort/restart/private-state",
                flush=True,
            )
        # The same pinned runtime must understand real gateway streaming and failures.
        client = clients[-1]
        for model in ("deepseek/deepseek-v4-flash-0731", "other/approved-model"):
            selected["model"] = model
            run_id = str(uuid.uuid4())
            await client.send(session, "Check the selected model", run_id)
            result = await terminal(client, run_id)
            assert result["state"] == "final", result
            assert model in json.dumps(result), result
        run_id = str(uuid.uuid4())
        await client.send(session, "[openrouter-error]", run_id)
        result = await terminal(client, run_id)
        assert result["state"] == "error", result
        run_id = str(uuid.uuid4())
        await client.send(session, "[openrouter-slow]", run_id)
        assert await asyncio.to_thread(slow_started.wait, 10)
        await client.abort(session, run_id)
        assert (await terminal(client, run_id))["state"] == "aborted"
        assert await asyncio.to_thread(slow_closed.wait, 5), "Upstream connection remained open"
        print(
            "PASS OpenRouter transport / hot model switch / stream error / upstream cancellation",
            flush=True,
        )
        print("ALL RUNTIME CHECKS PASSED", IMAGE, flush=True)
    finally:
        for client in clients:
            await client.close()
        for resource in reversed(resources):
            if isinstance(resource, docker.models.containers.Container):
                resource.remove(force=True)
            else:
                resource.disconnect(runner, force=True)
                resource.remove()
        for volume_name in volumes:
            docker_client.volumes.get(volume_name).remove(force=True)
        server.should_exit = True
        openrouter.httpx.AsyncClient = real_client
        key_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
