"""Slack Socket Mode using the official async SDK; private workspace DMs only."""

import asyncio
import json
import logging
import re

import aiohttp
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

from connector.delivery import TransportError, ingest

REQUIRED_SCOPES = {"im:history", "chat:write", "users:read"}
# The SDK logs raw payloads and exception bodies, including invitation tokens.
PRIVATE_LOGGER = logging.getLogger("talos.slack.private")
PRIVATE_LOGGER.setLevel(logging.CRITICAL)


class Slack:
    def __init__(self, bot_token, app_token, *, web_client=None, socket_factory=SocketModeClient):
        self.web = web_client or AsyncWebClient(
            token=bot_token, timeout=20, retry_handlers=[], logger=PRIVATE_LOGGER
        )
        self.app_token, self.socket_factory = app_token, socket_factory
        self.socket = None
        self.identity = None
        self.ready = asyncio.Event()
        self.failure = None
        self.verified = None

    async def request(self, method, *, sending=False, **kwargs):
        try:
            return await getattr(self.web, method)(**kwargs)
        except SlackApiError as error:
            code = (
                error.response.data.get("error") if isinstance(error.response.data, dict) else None
            )
            if error.response.status_code == 429 or code == "ratelimited":
                try:
                    headers = {key.lower(): value for key, value in error.response.headers.items()}
                    delay = int(headers.get("retry-after", "30"))
                except (ValueError, TypeError):
                    delay = 30
                raise TransportError("rate_limited", retry_after=max(1, min(delay, 3600))) from None
            category = {
                "invalid_auth": "invalid_credentials",
                "not_authed": "invalid_credentials",
                "token_revoked": "invalid_credentials",
                "token_expired": "invalid_credentials",
                "account_inactive": "app_uninstalled",
                "missing_scope": "missing_scope",
                "not_allowed_token_type": "invalid_token_type",
                "no_permission": "permission_denied",
                "channel_not_found": "destination_unavailable",
                "not_in_channel": "destination_unavailable",
                "team_access_not_granted": "workspace_mismatch",
                "is_archived": "destination_unavailable",
            }.get(code, "provider_unavailable")
            raise TransportError(
                category, uncertain=sending and category == "provider_unavailable"
            ) from None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise TransportError("network_unavailable", uncertain=sending) from None

    async def verify(self, workspace_id):
        auth = await self.request("auth_test")
        if auth.get("team_id") != workspace_id:
            raise TransportError("workspace_mismatch")
        if not re.fullmatch(r"B[A-Z0-9]+", auth.get("bot_id", "")) or not re.fullmatch(
            r"[UW][A-Z0-9]+", auth.get("user_id", "")
        ):
            raise TransportError("invalid_bot_identity")
        scopes = {key.lower(): value for key, value in auth.headers.items()}.get(
            "x-oauth-scopes", ""
        )
        if isinstance(scopes, list):
            scopes = ",".join(scopes)
        if not REQUIRED_SCOPES.issubset({value.strip() for value in scopes.split(",")}):
            raise TransportError("missing_scope")
        info = await self.request("bots_info", bot=auth["bot_id"])
        bot = info.get("bot", {})
        if (
            bot.get("id") != auth["bot_id"]
            or bot.get("deleted") is not False
            or bot.get("user_id") != auth["user_id"]
            or not re.fullmatch(r"A[A-Z0-9]+", bot.get("app_id", ""))
        ):
            raise TransportError("invalid_bot_identity")
        self.identity = {
            "bot_id": auth["bot_id"],
            "user_id": auth["user_id"],
            "app_id": bot["app_id"],
            "team_id": workspace_id,
        }
        return self.identity

    async def observe(self, message):
        if not isinstance(message.data, str):
            return
        try:
            data = json.loads(message.data)
        except ValueError:
            return
        if data.get("type") == "hello":
            if (data.get("connection_info") or {}).get("app_id") != self.identity["app_id"]:
                self.failure = "app_token_mismatch"
                self.ready.clear()
            elif data.get("num_connections") != 1:
                self.failure = "consumer_conflict"
                self.ready.clear()
            else:
                if self.verified is not None and not self.verified():
                    self.failure = "channel_changed"
                    self.ready.clear()
                else:
                    self.ready.set()
        elif data.get("type") == "disconnect":
            self.ready.clear()
            self.failure = "socket_disconnected"

    def receive(self, session, channel, revision, payload):
        if (
            not isinstance(payload, dict)
            or payload.get("team_id") != channel.workspace_id
            or payload.get("api_app_id") != self.identity["app_id"]
            or payload.get("is_ext_shared_channel") is True
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", payload.get("event_id", ""))
        ):
            return
        event = payload.get("event") or {}
        if (
            event.get("type") != "message"
            or event.get("channel_type") != "im"
            or event.get("subtype") is not None
            or event.get("bot_id")
            or event.get("user") == self.identity["user_id"]
            or event.get("team", channel.workspace_id) != channel.workspace_id
            or event.get("is_ext_shared_channel") is True
            or not re.fullmatch(r"[UW][A-Z0-9]+", event.get("user", ""))
            or not re.fullmatch(r"D[A-Z0-9]+", event.get("channel", ""))
            or not isinstance(event.get("text"), str)
        ):
            return
        text = event["text"].strip()
        # Slack slash commands do not arrive through message.im. These narrow
        # private-message commands work with the internal Socket Mode manifest.
        registration = re.fullmatch(r"register ([A-Za-z0-9_-]{20,64})", text)
        if registration:
            text = f"/register {registration[1]}"
        elif text in ("agent help", "agent status"):
            text = "/help" if text == "agent help" else "/status"
        return ingest(
            session,
            channel.id,
            revision,
            f"{self.identity['app_id']}:{payload['event_id']}",
            event["user"],
            channel.workspace_id,
            event["channel"],
            text,
        )

    async def connect(self, sessions, channel, *, verified=None):
        self.verified = verified
        self.socket = self.socket_factory(
            app_token=self.app_token,
            web_client=self.web,
            logger=PRIVATE_LOGGER,
            auto_reconnect_enabled=False,
            on_message_listeners=[self.observe],
        )

        async def process(client, request):
            if not self.ready.is_set() or self.failure:
                return
            try:
                if request.type == "events_api":
                    with sessions.begin() as session:
                        self.receive(session, channel, channel.revision, request.payload)
                # Ignored envelopes need no admission; each duplicate envelope is acked.
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=request.envelope_id)
                )
            except SQLAlchemyError:
                self.ready.clear()
                self.failure = "database_unavailable"
                # No ack: Slack can redeliver this event after persistence returns.
            except Exception:
                self.ready.clear()
                self.failure = "socket_ack_failed"

        self.socket.socket_mode_request_listeners.append(process)
        # Retrieve explicitly: SDK connect otherwise swallows credential errors and
        # retries forever. Our owner records a sanitized failure and reconnects.
        opened = await self.request("apps_connections_open", app_token=self.app_token)
        self.socket.wss_uri = opened.get("url")
        if not isinstance(self.socket.wss_uri, str) or not self.socket.wss_uri.startswith("wss://"):
            raise TransportError("invalid_socket_url")
        try:
            await asyncio.wait_for(self.socket.connect(), timeout=20)
            async with asyncio.timeout(20):
                while not self.ready.is_set():
                    if self.failure:
                        raise TransportError(self.failure)
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise TransportError("socket_connect_timeout") from None

    async def connected(self):
        if self.failure:
            raise TransportError(self.failure)
        return self.ready.is_set() and await self.socket.is_connected()

    async def send(self, destination, text):
        response = await self.request(
            "chat_postMessage",
            sending=True,
            channel=destination,
            text=text,
            mrkdwn=False,
            parse="none",
            unfurl_links=False,
            unfurl_media=False,
        )
        if response.get("channel") != destination or not re.fullmatch(
            r"[0-9]+\.[0-9]+", response.get("ts", "")
        ):
            raise TransportError("missing_send_identity", uncertain=True)
        return response["ts"]

    async def close(self):
        self.ready.clear()
        if self.socket:
            await self.socket.close()
