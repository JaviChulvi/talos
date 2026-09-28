import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from worker.hermes import BRIDGE


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
