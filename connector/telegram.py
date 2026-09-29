"""Private Telegram text DMs over long polling; bot tokens stay in the connector."""

import re

import httpx

from connector.delivery import TransportError, ingest


class Telegram:
    def __init__(self, bot_token: str, client=None):
        self.client = client or httpx.AsyncClient(timeout=20)
        self.base = f"https://api.telegram.org/bot{bot_token}/"

    async def request(self, method: str, body=None, *, sending=False):
        try:
            response = await self.client.post(self.base + method, json=body or {})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise TransportError("network_unavailable", uncertain=sending) from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            code = data.get("error_code", response.status_code) if isinstance(data, dict) else 0
            if code == 429:
                delay = (data.get("parameters") or {}).get("retry_after", 30)
                delay = delay if isinstance(delay, int) and not isinstance(delay, bool) else 30
                raise TransportError("rate_limited", retry_after=max(1, min(delay, 3600)))
            raise TransportError(
                {
                    401: "invalid_credentials",
                    403: "permission_denied",
                    409: "consumer_conflict",
                    400: "request_rejected",
                }.get(code, "provider_unavailable"),
                uncertain=sending and code not in (400, 401, 403, 409),
            )
        return data.get("result")

    async def verify(self, workspace_id=""):
        identity = await self.request("getMe")
        if (
            not isinstance(identity, dict)
            or identity.get("is_bot") is not True
            or not isinstance(identity.get("id"), int)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", identity.get("username", ""))
        ):
            raise TransportError("invalid_bot_identity")
        webhook = await self.request("getWebhookInfo")
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise TransportError("webhook_conflict")
        return {"bot_id": str(identity["id"]), "username": identity["username"]}

    async def poll(self, offset):
        result = await self.request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 10,
                "limit": 100,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(result, list):
            raise TransportError("invalid_updates")
        return result

    def receive(self, session, channel, revision, bot_id, update):
        """Discard edits, groups, forwarded/service/bot messages before shared admission."""
        message = update.get("message")
        if not isinstance(message, dict):
            return
        user, chat = message.get("from") or {}, message.get("chat") or {}
        if (
            user.get("is_bot") is not False
            or chat.get("type") != "private"
            or type(user.get("id")) is not int
            or user["id"] <= 0
            or chat.get("id") != user["id"]
            or "forward_origin" in message
            or "sender_chat" in message
            or not isinstance(message.get("text"), str)
        ):
            return
        ingest(
            session,
            channel.id,
            revision,
            f"{bot_id}:{update['update_id']}",
            str(user["id"]),
            "",
            str(chat["id"]),
            message["text"],
        )

    async def send(self, destination: str, text: str):
        result = await self.request(
            "sendMessage",
            {
                "chat_id": destination,
                "text": text,
                "protect_content": True,
                "link_preview_options": {"is_disabled": True},
            },
            sending=True,
        )
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TransportError("missing_send_identity", uncertain=True)
        return str(result["message_id"])

    async def close(self):
        await self.client.aclose()
