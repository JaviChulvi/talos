import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from connector.delivery import TransportError
from connector.slack import Slack


def response(data, headers=None, status=200):
    return AsyncSlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.invalid/api",
        req_args={},
        data=data,
        headers=headers or {},
        status_code=status,
    )


def web_client(scopes="chat:write,im:history,users:read", team="T12345", app="A12345"):
    web = AsyncMock()
    web.auth_test.return_value = response(
        {"ok": True, "team_id": team, "bot_id": "B12345", "user_id": "U54321"},
        {"X-OAuth-Scopes": scopes},
    )
    web.bots_info.return_value = response(
        {
            "ok": True,
            "bot": {
                "id": "B12345",
                "user_id": "U54321",
                "app_id": app,
                "deleted": False,
            },
        }
    )
    return web


@pytest.mark.parametrize(
    "team, scopes, error",
    [
        ("T99999", "chat:write,im:history,users:read", "workspace_mismatch"),
        ("T12345", "chat:write,users:read", "missing_scope"),
    ],
)
def test_workspace_and_scopes_are_verified_before_socket(team, scopes, error):
    async def check():
        transport = Slack("bot", "app", web_client=web_client(scopes=scopes, team=team))
        with pytest.raises(TransportError, match=error):
            await transport.verify("T12345")
        assert transport.socket is None

    asyncio.run(check())


def test_hello_requires_same_app_and_single_consumer():
    async def check():
        transport = Slack("bot", "app", web_client=web_client())
        identity = await transport.verify("T12345")
        assert identity["app_id"] == "A12345"
        await transport.observe(
            SimpleNamespace(
                data=json.dumps(
                    {
                        "type": "hello",
                        "connection_info": {"app_id": "A99999"},
                        "num_connections": 1,
                    }
                )
            )
        )
        assert transport.failure == "app_token_mismatch" and not transport.ready.is_set()
        transport.failure = None
        await transport.observe(
            SimpleNamespace(
                data=json.dumps(
                    {
                        "type": "hello",
                        "connection_info": {"app_id": "A12345"},
                        "num_connections": 2,
                    }
                )
            )
        )
        assert transport.failure == "consumer_conflict" and not transport.ready.is_set()

    asyncio.run(check())


@pytest.mark.parametrize(
    "code, category, uncertain",
    [
        ("invalid_auth", "invalid_credentials", False),
        ("missing_scope", "missing_scope", False),
        ("account_inactive", "app_uninstalled", False),
        ("internal_error", "provider_unavailable", True),
    ],
)
def test_send_errors_never_persist_sdk_exception_body(code, category, uncertain):
    async def check():
        web = web_client()
        web.chat_postMessage.side_effect = SlackApiError(
            "private-token-and-url",
            response({"ok": False, "error": code, "private": "never-show-this"}),
        )
        transport = Slack("bot", "app", web_client=web)
        with pytest.raises(TransportError) as caught:
            await transport.send("D12345", "Hello")
        assert str(caught.value) == category and caught.value.uncertain == uncertain

    asyncio.run(check())


def test_sdk_send_retries_are_disabled_and_plain_text_is_used():
    async def check():
        actual = Slack("xoxb-test", "xapp-test")
        assert actual.web.retry_handlers == []
        web = web_client()
        web.chat_postMessage.return_value = response(
            {"ok": True, "channel": "D12345", "ts": "1.234"}
        )
        transport = Slack("bot", "app", web_client=web)
        assert await transport.send("D12345", "<literal>") == "1.234"
        web.chat_postMessage.assert_awaited_once_with(
            channel="D12345",
            text="<literal>",
            mrkdwn=False,
            parse="none",
            unfurl_links=False,
            unfurl_media=False,
        )

    asyncio.run(check())
