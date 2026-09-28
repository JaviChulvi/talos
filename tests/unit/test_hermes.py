import json
import os
import select
import subprocess
import sys

import pytest

from worker.hermes import BRIDGE


@pytest.mark.parametrize("end", ["result", "cancel", "disconnect"])
def test_hermes_bridge_preserves_session_and_reaps_child(tmp_path, end):
    executable = tmp_path / "hermes"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\n"
        "print('  scanner warning from upstream',flush=True)\n"
        "print(json.dumps({'type':'system','session_id':'native-session','pid':os.getpid(),"
        "'args':sys.argv[1:]}),flush=True)\n"
        "if '--resume' not in sys.argv: time.sleep(30)\n"
        "else: print(json.dumps({'type':'result','text':'42','exit_code':0}),flush=True)\n"
    )
    executable.chmod(0o755)
    path = tmp_path / "session"
    if end == "result":
        path.write_text("native-session")
    bridge = BRIDGE.replace("/opt/data/talos-chat-session", str(path))
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", bridge],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        text=True,
    )
    try:
        child.stdin.write(
            json.dumps({"session": "talos-unique-agent", "message": "hello", "max_frame": 4096})
            + "\n"
        )
        child.stdin.flush()
        assert select.select([child.stdout], [], [], 5)[0]
        init = json.loads(child.stdout.readline())
        assert path.read_text() == "native-session"
        assert path.stat().st_mode & 0o777 == 0o600
        if end == "result":
            assert "--resume" in init["args"]
            result = json.loads(child.stdout.readline())
            assert result["text"] == "42"
        elif end == "cancel":
            child.stdin.write('{"cancel":true}\n')
            child.stdin.flush()
        else:
            child.stdin.close()
        assert select.select([child.stdout], [], [], 5)[0]
        finished = json.loads(child.stdout.readline())
        assert finished["type"] == "talos_exit"
        assert finished["cancelled"] == (end != "result")
        assert child.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(init["pid"], 0)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


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
