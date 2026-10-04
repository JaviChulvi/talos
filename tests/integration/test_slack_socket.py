"""Exercise the official SDK against a local TLS HTTP/WebSocket provider."""

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select

from backend.app.connections import Connection
from backend.app.models import ChannelInbox, Run, UserChannel
from connector.delivery import Delivery
from connector.main import Connector
from connector.slack import PRIVATE_LOGGER, Slack

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
from tests.integration.test_slack_delivery import payload

pytestmark = pytest.mark.integration


def test_real_sdk_private_dm_commit_ack_and_http_reply(
    session_maker, ready_accesses, tmp_path, monkeypatch
):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    _, pairs = ready_accesses
    channel_id = UUID(pairs[1][1]["id"])
    with session_maker() as session:
        channel = session.get(UserChannel, channel_id)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(cert_path, key_path)
    client_ssl = ssl.create_default_context(cafile=str(cert_path))

    async def check():
        acks, sends = [], []
        base = None

        async def api(request):
            method = request.match_info["method"]
            if method == "auth.test":
                assert request.headers["Authorization"] == "Bearer xoxb-synthetic"
                return web.json_response(
                    {"ok": True, "team_id": "T12345", "bot_id": "B12345", "user_id": "U54321"},
                    headers={"x-oauth-scopes": "chat:write,im:history,users:read"},
                )
            if method == "bots.info":
                return web.json_response(
                    {
                        "ok": True,
                        "bot": {
                            "id": "B12345",
                            "user_id": "U54321",
                            "app_id": "A12345",
                            "deleted": False,
                        },
                    }
                )
            if method == "apps.connections.open":
                assert request.headers["Authorization"] == "Bearer xapp-synthetic"
                return web.json_response(
                    {"ok": True, "url": base.replace("https:", "wss:") + "/socket"}
                )
            if method == "chat.postMessage":
                sends.append(await request.json())
                return web.json_response({"ok": True, "channel": "D12345", "ts": "1.234"})
            raise AssertionError(method)

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json(
                {"type": "hello", "connection_info": {"app_id": "A12345"}, "num_connections": 1}
            )
            await asyncio.sleep(0.05)
            for envelope in ("one", "two"):
                await ws.send_json(
                    {
                        "type": "events_api",
                        "envelope_id": envelope,
                        "accepts_response_payload": False,
                        "payload": payload(),
                    }
                )
            async for message in ws:
                if message.type == web.WSMsgType.TEXT:
                    acks.append(message.json()["envelope_id"])
                    with session_maker() as session:
                        assert session.scalar(select(ChannelInbox)).run_id
            return ws

        app = web.Application()
        app.router.add_route("*", "/api/{method}", api)
        app.router.add_get("/socket", socket)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"https://127.0.0.1:{port}"
        tunnels = []

        async def tunnel(reader, writer):
            upstream_writer = None
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                assert header.startswith(f"CONNECT 127.0.0.1:{port} ".encode())
                tunnels.append(header.split(b"\r\n", 1)[0])
                upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                async def relay(source, destination):
                    while data := await source.read(65536):
                        destination.write(data)
                        await destination.drain()
                    destination.close()

                await asyncio.gather(relay(reader, upstream_writer), relay(upstream_reader, writer))
            finally:
                writer.close()
                if upstream_writer:
                    upstream_writer.close()

        proxy = await asyncio.start_server(tunnel, "127.0.0.1", 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
        client = AsyncWebClient(
            token="xoxb-synthetic",
            base_url=base + "/api/",
            ssl=client_ssl,
            retry_handlers=[],
            logger=PRIVATE_LOGGER,
        )
        transport = Slack("xoxb-synthetic", "xapp-synthetic", web_client=client)
        try:
            await transport.verify("T12345")
            with session_maker.begin() as session:
                row = session.get(UserChannel, channel.id)
                version = session.get(Connection, row.connection_id).current_version_id
                row.verified_version_id = None
            owner = Connector(session_maker)
            await transport.connect(
                session_maker,
                channel,
                verified=lambda: owner.report(
                    channel.id, channel.revision, version, "ok", "socket_active", transport.identity
                ),
            )
            async with asyncio.timeout(5):
                while len(acks) < 2:
                    await asyncio.sleep(0.05)
            assert sorted(acks) == ["one", "two"]
            assert await transport.connected()
            with session_maker.begin() as session:
                runs = session.scalars(select(Run)).all()
                assert len(runs) == 1
                runs[0].status, runs[0].output = "completed", "SDK round trip"
            await Delivery(session_maker).send_one(channel_id, transport)
            assert len(sends) == 1 and sends[0]["channel"] == "D12345"
            assert sends[0]["text"] == "SDK round trip" and sends[0]["mrkdwn"] is False
            assert len(tunnels) >= 4  # Auth, bot info, app connection, WSS and send use CONNECT.
        finally:
            await transport.close()
            proxy.close()
            await proxy.wait_closed()
            await runner.cleanup()

    asyncio.run(check())
