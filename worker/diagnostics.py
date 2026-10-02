"""One asynchronous task per admitted diagnostic, with durable send intent."""

import asyncio
import re
from contextlib import suppress
from time import monotonic
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.availability import record_check
from backend.app.config import get_settings
from backend.app.diagnostics import append_event, authorized_run
from backend.app.models import ACTIVE_RUN_STATUSES, Agent, Run
from worker.openclaw import GatewayError
from worker.runtime import runtime_error_message

MAX_OUTPUT = get_settings().inference_max_output_chars


class OutputLimitError(Exception):
    pass


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
    def __init__(
        self, session_maker, connector, timeout: float | None = None, configure=None, probe=None
    ):
        self.sessions = session_maker
        self.connector = connector
        self.timeout = (
            timeout if timeout is not None else get_settings().inference_timeout_seconds + 60
        )
        self.configure = configure
        self.probe = probe
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
            if not authorized_run(session, run, agent):
                run.status = "interrupted"
                run.error = "Employee access changed before dispatch"
                append_event(session, run, "interrupted", {"reason": "access_changed"})
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
            if not authorized_run(session, run, agent):
                run.status = "interrupted"
                run.error = "Employee access changed before dispatch"
                append_event(session, run, "interrupted", {"reason": "access_changed"})
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
            if state == "tool":
                name, phase = payload.get("name"), payload.get("phase")
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name)
                    or phase not in ("started", "completed", "failed")
                ):
                    return False
                event = {"name": name, "phase": phase}
                call_id = payload.get("callId")
                if isinstance(call_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", call_id):
                    event["callId"] = call_id
                append_event(session, run, "tool", event)
                return False
            if state not in {"delta", "final", "error", "aborted"}:
                return False
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
                raise OutputLimitError("Diagnostic output limit exceeded")
            run.output = output
            if state in {"final", "error", "aborted"}:
                run.status = {"final": "completed", "error": "failed", "aborted": "cancelled"}[
                    state
                ]
                if state == "error":
                    agent = session.get(Agent, run.agent_id)
                    if agent.desired_state != "running":
                        run.status = "interrupted"
                        run.error = "The agent was stopped. This message will not be retried."
                    else:
                        run.error = runtime_error_message(payload.get("errorMessage")) or (
                            "The runtime could not finish this message. Open the native workspace "
                            "to inspect its provider and tool diagnostics."
                        )
                    event["reason"] = run.error
                elif state == "final" and run.availability_fingerprint and run.output.strip():
                    agent = session.get(Agent, run.agent_id)
                    record_check(
                        session,
                        agent,
                        "model",
                        "ok",
                        "native_conversation_completed",
                        fingerprint=run.availability_fingerprint,
                    )
            append_event(session, run, state, event)
            return state != "delta"

    async def _execute(self, run_id: UUID):
        run = self._claim(run_id)
        if run is None:
            return
        if run.source == "probe":
            if self.probe is None:
                self._finish(run.id, "failed", "Readiness probe handler is unavailable")
                return
            if self._can_send(run.id):
                try:
                    await self.probe(run)
                except asyncio.CancelledError:
                    self._finish(run.id, "unknown", "Worker stopped during readiness check")
                    raise
                except OperationalError:
                    raise
                except Exception:
                    self._finish(run.id, "unknown", "Readiness check could not finish")
            return
        client = None
        sent = False
        try:
            client = await self.connector(self.sessions, run.agent_id)
            if self.configure:
                await self.configure(self.sessions, run, client)
            if not self._can_send(run_id):
                return
            # The dispatching state is committed before any possible send.
            # Even a timeout while sending is uncertain, never a retry signal.
            sent = True
            session_key = run.session_key or f"agent:main:talos:{run.agent_id}"
            acknowledgment = await client.send(session_key, run.message, str(run.id))
            upstream_id = acknowledgment.get("runId")
            if not isinstance(upstream_id, str) or not 1 <= len(upstream_id) <= 128:
                raise ValueError("Missing diagnostic acknowledgment identity")
            if not self._ack(run_id, upstream_id):
                return
            deadline = monotonic() + self.timeout
            abort_sent = False
            while monotonic() < deadline:
                with self.sessions.begin() as session:
                    current = session.get(Run, run_id, with_for_update=True)
                    if current.status not in ACTIVE_RUN_STATUSES:
                        return
                    if not current.cancel_requested and not authorized_run(
                        session, current, session.get(Agent, current.agent_id)
                    ):
                        current.cancel_requested = True
                        current.status = "cancel_requested"
                        append_event(
                            session, current, "cancel_requested", {"reason": "access_changed"}
                        )
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
            with suppress(Exception):
                await client.abort(session_key, upstream_id)
            self._finish(
                run_id, "unknown", "Runtime deadline exceeded; stop the agent before retrying"
            )
        except OutputLimitError:
            with suppress(Exception):
                await client.abort(session_key, upstream_id)
            self._finish(
                run_id, "unknown", "Output character limit exceeded; stop the agent before retrying"
            )
        except GatewayError as error:
            # A negative send acknowledgment proves rejection. Failures of an
            # abort RPC cannot prove the already accepted run has ended.
            with self.sessions() as session:
                accepted = session.get(Run, run_id).upstream_run_id is not None
            self._finish(
                run_id,
                "unknown" if accepted else "failed",
                "Runtime rejected cancellation; stop the agent before retrying"
                if accepted
                else runtime_error_message(str(error))
                or "The runtime rejected this request. "
                "Check its model and permissions in the native workspace.",
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
