"""One asynchronous task per admitted diagnostic, with durable send intent."""

import asyncio
from contextlib import suppress
from time import monotonic
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.diagnostics import append_event
from backend.app.models import ACTIVE_RUN_STATUSES, Agent, Run
from worker.openclaw import GatewayError

MAX_OUTPUT = 16000
MAX_EVENTS = 500


def message_text(message: dict | None) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


class DiagnosticManager:
    def __init__(self, session_maker, connector, timeout: float = 180):
        self.sessions = session_maker
        self.connector = connector
        self.timeout = timeout
        self.tasks: dict[UUID, asyncio.Task] = {}

    def recover(self):
        # Unowned send intent is ambiguous after a restart or database failure.
        # Live tasks still own their delivery and must not be interrupted here.
        with self.sessions.begin() as session:
            runs = session.scalars(
                select(Run)
                .where(
                    Run.status.in_(("dispatching", "running", "cancel_requested")),
                    Run.id.not_in(list(self.tasks)),
                )
                .with_for_update()
            ).all()
            for run in runs:
                run.status = "unknown"
                run.error = "Worker lost delivery tracking; stop the agent before retrying"
                append_event(session, run, "unknown", {"reason": "worker_recovery"})

    async def tick(self):
        for run_id, task in list(self.tasks.items()):
            if task.done():
                del self.tasks[run_id]
                # Surface persistence failures to the worker, rather than
                # silently abandoning an in-progress database record.
                task.result()
        self.recover()
        with self.sessions() as session:
            queued = session.scalars(select(Run.id).where(Run.status == "queued")).all()
        for run_id in queued:
            if run_id not in self.tasks:
                self.tasks[run_id] = asyncio.create_task(self._execute(run_id))

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    def _finish(self, run_id: UUID, status: str, error: str | None = None):
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status not in ACTIVE_RUN_STATUSES:
                return
            run.status, run.error = status, error
            append_event(session, run, status, {"reason": error} if error else {})

    def _claim(self, run_id: UUID) -> Run | None:
        with self.sessions.begin() as session:
            agent_id = session.scalar(select(Run.agent_id).where(Run.id == run_id))
            agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status != "queued":
                return None
            if (
                agent.desired_state != "running"
                or agent.observed_state != "ready"
                or agent.current_incarnation_id != run.incarnation_id
            ):
                run.status = "interrupted"
                append_event(session, run, "interrupted", {"reason": "runtime_changed"})
                return None
            run.status = "dispatching"
            append_event(session, run, "dispatching", {})
            return run

    def _can_send(self, run_id: UUID) -> bool:
        with self.sessions.begin() as session:
            agent_id = session.scalar(select(Run.agent_id).where(Run.id == run_id))
            agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status not in ACTIVE_RUN_STATUSES:
                return False
            if run.cancel_requested:
                run.status = "cancelled"
                append_event(session, run, "cancelled", {})
                return False
            if (
                agent.desired_state != "running"
                or agent.observed_state != "ready"
                or agent.current_incarnation_id != run.incarnation_id
            ):
                run.status = "interrupted"
                append_event(session, run, "interrupted", {"reason": "runtime_changed"})
                return False
            return True

    def _ack(self, run_id: UUID, upstream_id: str) -> bool:
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status not in ACTIVE_RUN_STATUSES:
                return False
            run.upstream_run_id = upstream_id
            run.status = "cancel_requested" if run.cancel_requested else "running"
            append_event(session, run, "accepted", {})
            return True

    def _event(self, run_id: UUID, payload: dict) -> bool:
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run.status not in ACTIVE_RUN_STATUSES:
                return True
            state = payload.get("state")
            if state not in {"delta", "final", "error", "aborted"}:
                return False
            if run.event_count >= MAX_EVENTS:
                raise ValueError("Diagnostic event limit exceeded")
            if state == "delta":
                delta = payload.get("deltaText")
                if not isinstance(delta, str):
                    return False
                replace = payload.get("replace") is True
                output = delta if replace else run.output + delta
                event = {"text": delta, "replace": replace}
            else:
                output = message_text(payload.get("message"))
                # Terminal snapshots are authoritative, including a deliberately
                # empty snapshot. Aborted/error events can omit the message.
                if "message" not in payload:
                    output = run.output
                event = {"text": output}
            if len(output) > MAX_OUTPUT:
                raise ValueError("Diagnostic output limit exceeded")
            run.output = output
            if state in {"final", "error", "aborted"}:
                run.status = {"final": "completed", "error": "failed", "aborted": "cancelled"}[
                    state
                ]
                if state == "error":
                    run.error = "Runtime reported a diagnostic error"
            append_event(session, run, state, event)
            return state != "delta"

    async def _execute(self, run_id: UUID):
        run = self._claim(run_id)
        if run is None:
            return
        client = None
        sent = False
        try:
            client = await self.connector(self.sessions, run.agent_id)
            if not self._can_send(run_id):
                return
            # The dispatching state is committed before any possible send.
            # Even a timeout while sending is uncertain, never a retry signal.
            sent = True
            session_key = f"agent:main:talos:{run.agent_id}"
            acknowledgment = await client.send(session_key, run.message, str(run.id))
            upstream_id = acknowledgment.get("runId")
            if not isinstance(upstream_id, str) or not 1 <= len(upstream_id) <= 128:
                raise ValueError("Missing diagnostic acknowledgment identity")
            if not self._ack(run_id, upstream_id):
                return
            deadline = monotonic() + self.timeout
            abort_sent = False
            while monotonic() < deadline:
                with self.sessions() as session:
                    current = session.get(Run, run_id)
                    if current.status not in ACTIVE_RUN_STATUSES:
                        return
                    cancel = current.cancel_requested
                if cancel and not abort_sent:
                    await client.abort(session_key, upstream_id)
                    abort_sent = True
                    deadline = min(deadline, monotonic() + 10)
                try:
                    frame = await client.next_event(timeout=0.25)
                except TimeoutError:
                    continue
                if frame.get("type") == "disconnect":
                    raise ConnectionError("Runtime disconnected")
                payload = frame.get("payload", {})
                if (
                    frame.get("event") == "chat"
                    and isinstance(payload, dict)
                    and payload.get("runId") == upstream_id
                    and self._event(run_id, payload)
                ):
                    return
            self._finish(run_id, "unknown", "Result not confirmed; stop the agent before retrying")
        except GatewayError:
            # A negative send acknowledgment proves rejection. Failures of an
            # abort RPC cannot prove the already accepted run has ended.
            with self.sessions() as session:
                accepted = session.get(Run, run_id).upstream_run_id is not None
            self._finish(
                run_id, "unknown" if accepted else "failed", "Runtime rejected the request"
            )
        except asyncio.CancelledError:
            self._finish(
                run_id,
                "unknown" if sent else "interrupted",
                "Worker stopped; no automatic diagnostic retry",
            )
            raise
        except OperationalError:
            # Let tick recover durable intent after the database returns.
            raise
        except Exception:
            # Store no raw exception or protocol payload: either may contain
            # internal URLs or credentials.
            self._finish(
                run_id,
                "unknown" if sent else "failed",
                "Delivery or result uncertain; stop the agent before retrying"
                if sent
                else "Could not connect to the runtime",
            )
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()
