import asyncio
import json

import httpx
import pytest

from connector.delivery import TransportError, split_response
from connector.telegram import Telegram


def test_transport_verifies_bot_and_refuses_webhook():
    def handle(request):
        if request.url.path.endswith("getMe"):
            result = {"id": 42, "is_bot": True, "username": "talos_test_bot"}
        else:
            result = {"url": "https://other-consumer.invalid"}
        return httpx.Response(200, json={"ok": True, "result": result})

    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        bot = Telegram("test-secret", client)
        try:
            with pytest.raises(TransportError, match="webhook_conflict"):
                await bot.verify()
        finally:
            await bot.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "code, category, uncertain",
    [
        (401, "invalid_credentials", False),
        (403, "permission_denied", False),
        (409, "consumer_conflict", False),
        (500, "provider_unavailable", True),
    ],
)
def test_provider_errors_are_sanitized(code, category, uncertain):
    async def check():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    code,
                    json={
                        "ok": False,
                        "error_code": code,
                        "description": "secret-token-and-private-url",
                    },
                )
            )
        )
        bot = Telegram("private-secret", client)
        try:
            with pytest.raises(TransportError) as caught:
                await bot.send("42", "Hello")
            assert str(caught.value) == category and caught.value.uncertain == uncertain
        finally:
            await bot.close()

    asyncio.run(check())


def test_send_is_plain_text_and_preserves_identity():
    def handle(request):
        body = json.loads(request.content)
        assert body["chat_id"] == "42" and body["text"] == "<b>literal</b>"
        assert "parse_mode" not in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 17}})

    async def check():
        bot = Telegram("test", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        try:
            assert await bot.send("42", "<b>literal</b>") == "17"
        finally:
            await bot.close()

    asyncio.run(check())


def test_unicode_response_parts_stay_under_limit():
    value = "🌍" * 5000
    parts = split_response(value)
    assert "".join(parts) == value
    assert all(len(part.encode("utf-16-le")) <= 6000 for part in parts)
