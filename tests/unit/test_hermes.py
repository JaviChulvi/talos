import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from worker.hermes import BRIDGE


def test_user_sessions_have_separate_private_checkpoints(tmp_path):
    (tmp_path / "hermes_state.py").write_text(
        "class SessionDB:\n"
        " def __enter__(self): return self\n"
        " def __exit__(self, *args): pass\n"
        " def get_active_message_ids(self, session_id): return []\n"
        " def get_messages(self, session_id, **kwargs): return []\n"
    )
    executable = tmp_path / "hermes"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,uuid\n"
        "session=(sys.argv[sys.argv.index('--resume')+1] "
        "if '--resume' in sys.argv else str(uuid.uuid4()))\n"
        "print(json.dumps({'type':'system','session_id':session,'args':sys.argv[1:]}),flush=True)\n"
        "print(json.dumps({'type':'result','text':session,'exit_code':0}),flush=True)\n"
    )
    executable.chmod(0o755)
    legacy = tmp_path / "talos-chat-session"
    legacy.write_text("existing-admin-session")
    bridge = BRIDGE.replace("/opt/data/talos-chat-session", str(legacy))

    def call(key):
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", bridge],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                "PYTHONPATH": str(tmp_path),
            },
            text=True,
        )
        try:
            child.stdin.write(
                json.dumps({"session": key, "message": "hello", "max_frame": 4096}) + "\n"
            )
            child.stdin.flush()
            events = []
            while True:
                assert select.select([child.stdout], [], [], 5)[0]
                line = child.stdout.readline()
                assert line, child.stderr.read()
                events.append(json.loads(line))
                if events[-1]["type"] == "talos_exit":
                    break
            assert child.wait(timeout=5) == 0
            return events
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    scope = "01234567-89ab-cdef-0123-456789abcdef:2:fedcba98-7654-3210-fedc-ba9876543210"
    telegram = "agent:main:employee:telegram:" + scope
    slack = "agent:main:employee:slack:" + scope
    first = call(telegram)
    second = call(slack)
    resumed = call(telegram)
    assert first[0]["session_id"] != second[0]["session_id"]
    assert resumed[0]["session_id"] == first[0]["session_id"]
    assert "--resume" in resumed[0]["args"]
    args = first[0]["args"]
    title = args[args.index("--continue") + 1]
    assert title.startswith("talos-") and len(title) <= 100
    assert legacy.read_text() == "existing-admin-session"
    directory = tmp_path / "talos-chat-sessions"
    files = list(directory.glob("*.session"))
    assert len(files) == 2
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


@pytest.mark.parametrize("end", ["result", "cancel", "disconnect"])
def test_hermes_bridge_preserves_session_and_reaps_child(tmp_path, end):
    (tmp_path / "hermes_state.py").write_text(
        "class SessionDB:\n"
        " def __enter__(self): return self\n"
        " def __exit__(self, *args): pass\n"
        " def get_active_message_ids(self, session_id): return []\n"
        " def get_messages(self, session_id, **kwargs): return []\n"
    )
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
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(tmp_path),
        },
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
        assert path.with_suffix(".pending").exists() == (end != "result")
        with pytest.raises(ProcessLookupError):
            os.kill(init["pid"], 0)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1")
def test_pinned_hermes_recovers_interrupted_native_history():
    proof = Path(__file__).parents[1] / "hermes_recovery_proof.py"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "talos-hermes-native:local",
            "-c",
            proof.read_text(),
        ],
        input=BRIDGE,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS") == 7
