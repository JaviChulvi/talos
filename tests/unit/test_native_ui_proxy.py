"""Exercise the deployed Node relay against real HTTP/WebSocket sockets."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from aiohttp import ClientSession, WSServerHandshakeError, web
from docker.errors import APIError, NotFound

from backend.app.auth import COOKIE
from backend.app.models import HERMES_RELEASE
from worker.lifecycle import Worker
from worker.runtime import IMAGE

PROXY = Path(__file__).resolve().parents[2] / "worker/ui_proxy.cjs"


@asynccontextmanager
async def relay(handler):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the native UI relay")
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    listener = await asyncio.get_running_loop().create_server(runner.server, "127.0.0.1", 0)
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            node,
            "-e",
            PROXY.read_text(),
            "127.0.0.1",
            str(listener.sockets[0].getsockname()[1]),
            COOKIE,
            "0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        port = int(await asyncio.wait_for(process.stdout.readline(), 5))
        yield f"http://127.0.0.1:{port}"
    finally:
        if process is not None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
        listener.close()
        await listener.wait_closed()
        await runner.cleanup()


def test_http_relay_removes_only_admin_cookies_and_preserves_uploads():
    async def scenario():
        async def upstream(request):
            assert request.headers.get("Cookie") == "native_session=keep; other=also-keep"
            assert request.headers["Origin"] == "http://127.0.0.1:32123"
            assert request.path_qs == "/api/upload?name=fixture"
            assert await request.read() == b"synthetic upload"
            response = web.Response(body=b"native response")
            response.set_cookie("native_session", "renewed", httponly=True)
            response.set_cookie(COOKIE, "must-not-overwrite")
            return response

        async with relay(upstream) as url, ClientSession() as client:
            result = await client.post(
                url + "/api/upload?name=fixture",
                data=b"synthetic upload",
                headers={
                    "Cookie": (
                        f"{COOKIE}=secret; native_session=keep; "
                        f"{COOKIE} = duplicate; other=also-keep"
                    ),
                    "Origin": "http://127.0.0.1:32123",
                },
            )
            assert result.status == 200 and await result.read() == b"native response"
            assert [value.split("=", 1)[0] for value in result.headers.getall("Set-Cookie")] == [
                "native_session"
            ]

    asyncio.run(scenario())


def test_websocket_relay_preserves_native_auth_and_bidirectional_frames():
    async def scenario():
        async def upstream(request):
            assert request.headers.get("Cookie") == "native_session=keep"
            if request.path == "/denied":
                return web.Response(status=401, text="Native sign-in required")
            socket = web.WebSocketResponse()
            socket.set_cookie("native_session", "renewed")
            socket.set_cookie(COOKIE, "must-not-overwrite")
            await socket.prepare(request)
            await socket.send_str("native hello")
            async for message in socket:
                await socket.send_bytes(message.data)
            return socket

        async with relay(upstream) as url, ClientSession() as client:
            headers = {"Cookie": f"native_session=keep; {COOKIE}=secret"}
            async with client.ws_connect(url + "/ws", headers=headers) as socket:
                assert (await socket.receive()).data == "native hello"
                await socket.send_bytes(b"binary native frame")
                assert (await socket.receive()).data == b"binary native frame"
                assert COOKIE not in socket._response.cookies
                assert "native_session" in socket._response.cookies
            with pytest.raises(WSServerHandshakeError) as error:
                await client.ws_connect(url + "/denied", headers=headers)
            assert error.value.status == 401

    asyncio.run(scenario())


@pytest.fixture
def legacy_relay(monkeypatch):
    old = SimpleNamespace(
        name="native-ui",
        status="running",
        attrs={
            "Config": {"Image": IMAGE, "Cmd": ["legacy TCP relay"]},
            "HostConfig": {
                "PortBindings": {"18789/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32123"}]}
            },
        },
        remove=Mock(),
        rename=Mock(),
        stop=Mock(),
    )
    resources = {old.name: old}
    new = SimpleNamespace(id="new", status="created", start=Mock(), reload=Mock(), remove=Mock())

    def get(name):
        if name not in resources:
            raise NotFound("Missing fixture resource")
        return resources[name]

    def rename(name):
        del resources[old.name]
        old.name = name
        resources[name] = old

    def create(*args, **options):
        new.labels = options["labels"]
        new.attrs = {"Config": {"Image": IMAGE, "Cmd": options["command"]}}
        resources[options["name"]] = new
        return new

    old.rename.side_effect = rename
    old.stop.side_effect = lambda **kwargs: setattr(old, "status", "exited")
    old.remove.side_effect = lambda: resources.pop(old.name)
    new.start.side_effect = lambda: setattr(new, "status", "running")
    network = SimpleNamespace(attrs={"Containers": {}}, reload=Mock(), connect=Mock())
    docker = SimpleNamespace(
        containers=SimpleNamespace(get=Mock(side_effect=get), create=Mock(side_effect=create)),
        networks=SimpleNamespace(
            list=Mock(return_value=[SimpleNamespace(name="ingress")]),
            get=Mock(return_value=network),
        ),
    )
    monkeypatch.setattr(
        "worker.lifecycle.get_settings",
        lambda: SimpleNamespace(compose_project="fixture", installation_id="local"),
    )
    worker = Worker(sessions=Mock(), client=docker)
    incarnation = SimpleNamespace(
        container_name="native",
        agent_id=uuid4(),
        id=uuid4(),
        runtime_release=HERMES_RELEASE,
        model_route="native",
    )
    old.labels = {
        **worker.labels(incarnation.agent_id),
        "io.talos.incarnation": str(incarnation.id),
        "io.talos.role": "ui-proxy",
    }
    return worker, incarnation, old, new, create


@pytest.mark.parametrize("failure", [None, "create", "start"])
def test_worker_upgrades_legacy_relay_on_its_existing_port(legacy_relay, failure):
    worker, incarnation, old, new, create = legacy_relay
    if failure == "create":
        worker.client.containers.create.side_effect = APIError("Creation interrupted")
    elif failure == "start":
        new.start.side_effect = APIError("Start interrupted")
    if failure:
        with pytest.raises(APIError):
            worker.ui_proxy(incarnation, upgrade=True)
        assert old.status == "exited"
        worker.client.containers.create.side_effect = create
        new.start.side_effect = lambda: setattr(new, "status", "running")
    assert worker.ui_proxy(incarnation, upgrade=True) is new
    old.stop.assert_called_once_with(timeout=5)
    old.remove.assert_called_once_with()
    options = worker.client.containers.create.call_args.kwargs
    assert options["ports"] == {"18789/tcp": ("127.0.0.1", 32123)}
    assert options["command"] == [PROXY.read_text(), "native", "9119", COOKIE]
    assert new.status == "running"


def test_stopping_native_agent_never_creates_or_starts_a_relay(legacy_relay, monkeypatch):
    worker, incarnation, old, new, _ = legacy_relay
    runtime = SimpleNamespace(status="running", stop=Mock(), reload=Mock())
    runtime.stop.side_effect = lambda **kwargs: setattr(runtime, "status", "exited")
    monkeypatch.setattr(worker, "owned_container", lambda _: runtime)
    worker.client.containers.create.side_effect = AssertionError("Stop must not create a relay")
    worker.stop_incarnation(incarnation)
    old.stop.assert_called_once_with(timeout=5)
    runtime.stop.assert_called_once_with(timeout=15)
    old.rename.assert_not_called()
    new.start.assert_not_called()
