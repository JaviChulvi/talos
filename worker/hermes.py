"""Native Hermes chat over its structured CLI, inside the owned agent container."""

import asyncio
import json
import socket

from docker.utils.socket import frames_iter

from backend.app.config import get_settings

# The supervisor owns the child for its entire lifetime. Cancellation and a lost
# worker connection both reap it; the CLI never reads the control channel.
BRIDGE = r"""
import json, os, select, signal, subprocess, sys, uuid
from contextlib import suppress
from pathlib import Path
control = sys.stdin.buffer.raw
request = json.loads(control.readline())
path = Path('/opt/data/talos-chat-session')
pending = path.with_suffix('.pending')
from hermes_state import SessionDB
with SessionDB() as db:
    if pending.exists():
        checkpoint = json.loads(pending.read_text())
        session_id = checkpoint['session_id']
        if session_id:
            # Rewind is native, soft-archives the partial turn, and refuses a
            # live turn lease. Never replay unfinished tool intent on resume.
            if db.get_compression_tip(session_id) not in (None, session_id):
                raise RuntimeError('Interrupted Hermes session compacted; review native history')
            tail = db.get_messages(session_id, after_id=checkpoint['watermark'])
            target = next((m for m in tail if m['role'] == 'user'), None)
            if target:
                db.rewind_to_message(session_id, target['id'],
                                     expected_active_ids=db.get_active_message_ids(session_id))
                db.append_message(session_id, 'assistant',
                    'The previous turn was interrupted and withdrawn. Some tool effects may '
                    'already exist; inspect state before making changes. Do not resume that task.')
        pending.unlink()
    session_id = path.read_text().strip() if path.exists() else None
    checkpoint = {'session_id': session_id, 'watermark':
                  db.get_active_message_watermark(session_id) if session_id else 0}

def save_pending():
    temporary = pending.with_suffix('.next')
    temporary.write_text(json.dumps(checkpoint))
    temporary.chmod(0o600)
    temporary.replace(pending)

save_pending()
command = ['hermes', 'chat', '--oneshot', '--format', 'stream-json',
           '--query', request['message']]
if not request.get('model_id') and Path('/opt/data/config.yaml').exists():
    from hermes_cli.config import load_config
    model = load_config().get('model') or {}
    if isinstance(model, dict) and model.get('default'):
        command += ['--model', model['default']]
        if model.get('provider'):
            command += ['--provider', model['provider']]
if request.get('model_id'):
    command += ['--model', request['model_id'], '--provider', 'custom']
# Older running containers predate this internal endpoint's proxy exclusion.
for key in ('NO_PROXY', 'no_proxy'):
    os.environ[key] = os.environ.get(key, '') + ',talos-gateway'
if path.exists():
    command += ['--resume', path.read_text().strip()]
else:
    # If the first process died before publishing its ID, do not find that
    # abandoned conversation again by title.
    command += ['--continue', request['session'] + ':' + uuid.uuid4().hex, '--create-if-missing']
child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, start_new_session=True)
cancelled = False
completed = False
buffer = b''
try:
    while True:
        ready, _, _ = select.select([control, child.stdout], [], [], 0.25)
        if control in ready:
            # Only cancel can follow the initial message; EOF also cancels.
            control.readline()
            cancelled = True
            break
        if child.stdout in ready:
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                # Reap normal EOF before cleanup can signal an already-exiting group.
                child.wait(timeout=3)
                break
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if len(line) > request['max_frame']:
                    raise ValueError('Hermes frame too large')
                # Hermes 0.21.5 prints its optional scanner warning on stdout.
                # Only JSON records belong to the chat protocol.
                if not line.startswith(b'{'):
                    continue
                event = json.loads(line)
                if event.get('session_id'):
                    if checkpoint['session_id'] is None:
                        checkpoint['session_id'] = event['session_id']
                        save_pending()
                    # Keep the actual native identity even if its title changes.
                    temp = path.with_suffix('.next')
                    temp.write_text(event['session_id'])
                    temp.chmod(0o600)
                    temp.replace(path)
                if event.get('type') == 'result':
                    completed = event.get('exit_code') == 0
                print(json.dumps(event), flush=True)
            if len(buffer) > request['max_frame']:
                raise ValueError('Hermes frame too large')
finally:
    if cancelled or child.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    child.wait()
if completed and not cancelled and child.returncode == 0:
    pending.unlink()
print(json.dumps({'type': 'talos_exit', 'cancelled': cancelled,
                  'exit_code': child.returncode}), flush=True)
"""


class HermesClient:
    def __init__(self, docker_client, container_id):
        self.docker = docker_client
        self.container_id = container_id
        self.connection = None
        self.reader = None
        self.events = asyncio.Queue()
        self.run_id = None
        self.model_id = None
        self.output = ""
        self.max_frame = get_settings().inference_max_output_chars * 6 + 65536

    async def send(self, session: str, message: str, run_id: str) -> dict:
        self.run_id = run_id
        api = self.docker.api
        execution = await asyncio.to_thread(
            api.exec_create,
            self.container_id,
            ["python", "-u", "-c", BRIDGE],
            stdin=True,
            stdout=True,
            stderr=False,
            user="10000:10000",
            workdir="/opt/data/workspace",
        )
        self.connection = await asyncio.to_thread(api.exec_start, execution["Id"], socket=True)
        self.reader = asyncio.create_task(self._read())
        await asyncio.to_thread(
            self.connection._sock.sendall,
            (
                json.dumps(
                    {
                        "session": session,
                        "message": message,
                        "max_frame": self.max_frame,
                        "model_id": self.model_id,
                    }
                )
                + "\n"
            ).encode(),
        )
        return {"runId": run_id}

    async def _read(self):
        buffer = b""
        result = None
        try:
            frames = frames_iter(self.connection, tty=False)
            while (frame := await asyncio.to_thread(next, frames, None)) is not None:
                stream, data = frame
                if stream != 1:
                    continue
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > self.max_frame:
                        raise ValueError("Hermes frame too large")
                    event = json.loads(line)
                    if event["type"] == "text":
                        self.output += event["text"]
                        if len(self.output) > get_settings().inference_max_output_chars:
                            raise ValueError("Hermes output too large")
                        await self._event("delta", deltaText=event["text"])
                    elif event["type"] == "result":
                        result = event
                    elif event["type"] == "talos_exit":
                        if event["cancelled"]:
                            await self._event("aborted", message={"content": self.output})
                        elif result is not None:
                            await self._event(
                                "error" if result["exit_code"] else "final",
                                message={"content": result["text"]},
                            )
                        else:
                            # A process exit without a result cannot prove delivery.
                            await self.events.put({"type": "disconnect"})
                        return
                if len(buffer) > self.max_frame:
                    raise ValueError("Hermes frame too large")
            await self.events.put({"type": "disconnect"})
        except (OSError, ValueError, KeyError):
            await self.events.put({"type": "disconnect"})

    async def _event(self, state, **payload):
        await self.events.put(
            {
                "event": "chat",
                "payload": {
                    "runId": self.run_id,
                    "state": state,
                    **payload,
                },
            }
        )

    async def next_event(self, timeout=0.25):
        return await asyncio.wait_for(self.events.get(), timeout)

    async def abort(self, session, run_id):
        await asyncio.to_thread(self.connection._sock.sendall, b'{"cancel":true}\n')

    async def close(self):
        try:
            if self.connection is not None:
                try:
                    self.connection._sock.shutdown(socket.SHUT_WR)
                    if self.reader:
                        await asyncio.wait_for(asyncio.shield(self.reader), timeout=5)
                finally:
                    self.connection.close()
                    if self.reader and not self.reader.done():
                        self.reader.cancel()
        finally:
            self.docker.close()
