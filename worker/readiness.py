"""Probes share durable run admission but never invoke tool-enabled native chat."""

import asyncio

import httpx

from backend.app.availability import agent_fingerprint, record_check
from backend.app.diagnostics import append_event
from backend.app.models import Agent, Run, WorkloadIncarnation
from worker.lifecycle import read_credentials
from worker.runtime import RuntimeReadinessError
from worker.setup_runtime import verify_setup


class Readiness:
    def __init__(self, worker):
        self.worker, self.sessions = worker, worker.sessions

    async def execute(self, run):
        kind = run.inference["probe"]
        with self.sessions() as session:
            agent = session.get(Agent, run.agent_id)
            incarnation = session.get(WorkloadIncarnation, run.incarnation_id)
            if agent_fingerprint(session, agent) != run.availability_fingerprint:
                self.finish(run, "unknown", "configuration_changed", stale=True)
                return
        try:
            if kind == "model":
                state, code, uncertain = await self.model(agent, incarnation)
            elif kind == "runtime":
                container = await asyncio.to_thread(self.worker.owned_container, incarnation)
                if container is None:
                    state, code = "blocked", "runtime_missing"
                else:
                    credentials = read_credentials(incarnation)
                    await self.worker.wait_ready(
                        container,
                        credentials["control_token"],
                        runtime_kind=agent.runtime_kind,
                        retry=False,
                    )
                    state, code = "ok", "runtime_connected"
                uncertain = False
            else:
                if not agent.applied_application or agent.applied_application.get("legacy_receipt"):
                    state, code = "not_applicable", "no_managed_setup"
                else:
                    state_name, network = self.worker.names(agent.id)
                    await asyncio.to_thread(
                        verify_setup,
                        self.worker.client,
                        state_name,
                        incarnation,
                        agent.runtime_kind,
                        agent.applied_application,
                        self.worker.labels(agent.id),
                        network=network,
                        discover=True,
                    )
                    state, code = "ok", "native_discovery_verified"
                uncertain = False
        except RuntimeReadinessError as error:
            state, code, uncertain = "blocked", f"{kind}_{error.code}", False
        except (RuntimeError, httpx.HTTPError, OSError, TimeoutError):
            state, code, uncertain = "blocked", f"{kind}_check_failed", False
        self.finish(run, state, code, uncertain=uncertain)

    async def model(self, agent, incarnation):
        if agent.runtime_mode == "native" and not agent.inference_override:
            # Pinned chat entry points do not establish a portable tools-off
            # contract for arbitrary native providers. Do not run ordinary chat.
            return "unknown", "native_safe_probe_unavailable", False
        credentials = read_credentials(incarnation)
        route = (
            "/native/v1/chat/completions"
            if agent.runtime_mode == "native"
            else "/v1/chat/completions"
        )
        model = (
            agent.inference_override["model_id"]
            if agent.runtime_mode == "native" and agent.inference_override
            else "default"
        )
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.post(
                    "http://talos-gateway:8001" + route,
                    headers={"Authorization": "Bearer " + credentials["agent_token"]},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply OK."}],
                        "max_tokens": 16,
                        "stream": False,
                    },
                )
            if response.status_code != 200:
                code = {
                    401: "provider_credentials_invalid",
                    402: "allowance_or_credit_exhausted",
                    403: "provider_permission_denied",
                    404: "model_unavailable",
                    429: "provider_rate_limited",
                    503: "gateway_or_provider_unavailable",
                }.get(response.status_code, "model_request_failed")
                return "blocked", code, False
            message = response.json().get("choices", [{}])[0].get("message", {})
            if (
                not isinstance(message.get("content"), str)
                or not message["content"].strip()
                or message.get("tool_calls")
            ):
                return "unknown", "model_response_unconfirmed", False
            return "ok", "model_responded_without_tools", False
        except (httpx.HTTPError, ValueError, IndexError, AttributeError, TypeError):
            return "unknown", "model_send_uncertain", True

    def finish(self, run, state, code, *, stale=False, uncertain=False):
        with self.sessions.begin() as session:
            agent = session.get(Agent, run.agent_id, with_for_update=True, populate_existing=True)
            current = session.get(Run, run.id, with_for_update=True)
            if current.status not in ("dispatching", "running", "cancel_requested"):
                return
            if current.cancel_requested and not uncertain:
                current.status, current.output = "cancelled", "check_cancelled"
                append_event(session, current, "cancelled", {})
                return
            stale = stale or agent_fingerprint(session, agent) != run.availability_fingerprint
            current.status = "unknown" if uncertain else "interrupted" if stale else "completed"
            current.output = "Configuration changed" if stale else code
            append_event(session, current, current.status, {"code": code})
            if not stale:
                record_check(
                    session,
                    agent,
                    run.inference["probe"],
                    state,
                    code,
                    fingerprint=run.availability_fingerprint,
                )
                if run.inference["probe"] == "connections" and state == "ok":
                    record_check(
                        session,
                        agent,
                        "setup",
                        "ok",
                        "setup_integrity_verified",
                        fingerprint=run.availability_fingerprint,
                    )
