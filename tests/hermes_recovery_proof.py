import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path

from hermes_state import SessionDB

bridge_template = sys.stdin.read()
root = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(root)
path = root / "talos-chat-session"
path.write_text("test-session")
with SessionDB() as db:
    db.create_session("test-session", "cli")
    db.append_message("test-session", "user", "Remember marker CEDAR")
    db.append_message("test-session", "assistant", "CEDAR remembered")
cli = root / "hermes"
cli.write_text(
    """#!/opt/venv/bin/python
import json,sys,time
from hermes_state import SessionDB
query=sys.argv[sys.argv.index('--query')+1]
with SessionDB() as db:
 db.append_message('test-session','user',query)
 if query == 'interrupt':
  db.append_message('test-session','assistant',tool_calls=[{'id':'old-call','type':'function','function':{'name':'terminal','arguments':'{}'}}])
 else:
  assert not any(m.get('tool_calls') for m in db.get_messages('test-session'))
  assert any(m.get('content') == 'CEDAR remembered' for m in db.get_messages('test-session'))
  db.append_message('test-session','assistant','Only inspected; no work resumed')
print(json.dumps({'type':'system','session_id':'test-session'}),flush=True)
if query == 'interrupt': time.sleep(60)
print(json.dumps({'type':'result','text':'OK','exit_code':0}),flush=True)
""".replace("/opt/venv/bin/python", sys.executable)
)
cli.chmod(0o755)
os.environ["PATH"] = str(root) + ":" + os.environ["PATH"]
bridge = bridge_template.replace("/opt/data/talos-chat-session", str(path))


def start(message):
    p = subprocess.Popen(
        [sys.executable, "-u", "-c", bridge],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p.stdin.write(json.dumps({"session": "test", "message": message, "max_frame": 65536}) + "\n")
    p.stdin.flush()
    assert select.select([p.stdout], [], [], 30)[0], "no init"
    line = p.stdout.readline()
    if not line:
        raise Exception(p.stderr.read())
    assert json.loads(line)["type"] == "system", line
    return p


for end in ("cancel", "disconnect", "kill"):
    p = start("interrupt")
    if end == "cancel":
        p.stdin.write('{"cancel":true}\n')
        p.stdin.flush()
    elif end == "disconnect":
        p.stdin.close()
    else:
        # Simulate the container killing supervisor and its child. Find the known child only.
        children = subprocess.check_output(["pgrep", "-P", str(p.pid)], text=True).split()
        for pid in children:
            os.kill(int(pid), 9)
        p.kill()
    assert p.wait(timeout=10) is not None
    assert path.with_suffix(".pending").exists()
    follow = start("Inspect only; do not create files")
    assert follow.wait(timeout=30) == 0, follow.stderr.read()
    assert not path.with_suffix(".pending").exists()
    with SessionDB() as db:
        active = db.get_messages("test-session")
        archive = db.get_messages("test-session", include_inactive=True)
        assert not any(m.get("content") == "interrupt" or m.get("tool_calls") for m in active)
        assert any(m.get("tool_calls") for m in archive)
    print(end, "PASS: prior context retained, unfinished task archived, audit retained")


for case in ("compaction", "first-turn-compaction", "native-turn", "legacy-checkpoint"):
    os.environ["HERMES_HOME"] = str(root / case)
    path = root / case / "talos-chat-session"
    bridge = bridge_template.replace("/opt/data/talos-chat-session", str(path))
    with SessionDB() as db:
        db.create_session("test-session", "cli")
        if case != "first-turn-compaction":
            db.append_message("test-session", "user", "Remember marker CEDAR")
            db.append_message("test-session", "assistant", "CEDAR remembered")
    path.write_text("test-session")
    p = start("interrupt")
    p.stdin.write('{"cancel":true}\n')
    p.stdin.flush()
    assert p.wait(timeout=10) == 0, p.stderr.read()
    pending = path.with_suffix(".pending")
    with SessionDB() as db:
        if "compaction" in case:
            # Exercise the real default compaction API: same session, fresh IDs.
            tail = db.get_messages("test-session")[-2:]
            db.archive_and_compact(
                "test-session",
                [{"role": "user", "content": "Summary: CEDAR remembered"}, *tail],
                tail_count=len(tail),
            )
            assert db.get_compression_tip("test-session") == "test-session"
        elif case == "native-turn":
            # Even an identical later prompt is a separate completed native turn.
            db.append_message("test-session", "user", "interrupt")
            db.append_message("test-session", "assistant", "Completed in native UI")
        else:
            checkpoint = json.loads(pending.read_text())
            del checkpoint["active_ids"]
            pending.write_text(json.dumps(checkpoint))
        before = db.get_messages("test-session", include_inactive=True)
    pending_before = pending.read_bytes()
    refused = subprocess.run(
        [sys.executable, "-u", "-c", bridge],
        input=json.dumps({"session": "test", "message": "Inspect only", "max_frame": 65536})
        + "\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert refused.returncode != 0, refused.stdout
    assert "review native history" in refused.stderr, refused.stderr
    assert not refused.stdout, "recovery must refuse before launching the next native turn"
    assert pending.read_bytes() == pending_before
    with SessionDB() as db:
        assert db.get_messages("test-session", include_inactive=True) == before
    print(case, "PASS: recovery refused without changing native history or pending checkpoint")
