import json


def test_hermes_tool_events_preserve_text_without_tool_contents(monkeypatch):
    import asyncio

    from worker.hermes import HermesClient

    events = [
        {"type": "tool_use", "name": "terminal", "tool_call_id": "a", "input": "secret"},
        {"type": "text", "text": "Working"},
        {"type": "tool_result", "name": "terminal", "tool_call_id": "a", "output": "secret"},
        {"type": "tool_result", "name": "browser", "is_error": True},
        {"type": "result", "text": "Done", "exit_code": 0},
        {"type": "talos_exit", "cancelled": False},
    ]
    wire = "".join(json.dumps(e) + "\n" for e in events).encode()
    monkeypatch.setattr(
        "worker.hermes.frames_iter",
        lambda *_args, **_kwargs: iter([(1, wire[:30]), (1, wire[30:])]),
    )

    async def scenario():
        client = HermesClient(None, "fixture")
        client.run_id = "one"
        await client._read()
        received = [client.events.get_nowait()["payload"] for _ in range(5)]
        assert [e["state"] for e in received] == ["tool", "delta", "tool", "tool", "final"]
        assert [e["phase"] for e in received if e["state"] == "tool"] == [
            "started",
            "completed",
            "failed",
        ]
        assert received[0] == {
            "runId": "one",
            "state": "tool",
            "name": "terminal",
            "callId": "a",
            "phase": "started",
        }
        assert received[-1]["message"] == {"content": "Done"}
        assert client.output == "Working"
        assert "secret" not in json.dumps(received)

    asyncio.run(scenario())
