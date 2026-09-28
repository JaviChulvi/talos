"""Budget correctness requires real PostgreSQL locks and mocked provider transport."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.app.models import Agent, Employee, InferenceCall, Run, WorkloadIncarnation
from backend.app.usage import employee_budget, month_bounds
from gateway import identity, main, openrouter
from tests.integration import test_diagnostics as diagnostics

agent_id = diagnostics.agent_id
client = diagnostics.client
sessions = diagnostics.sessions
database_engine = diagnostics.database_engine
ledger_identity = diagnostics.ledger_identity

pytestmark = pytest.mark.integration


def set_limit(client, employee, limit):
    response = client.put(
        f"/api/v1/employees/{employee}/budget", json={"monthly_allowance_usd": limit}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_allowance_edits_unknown_cost_and_warning(client, sessions, agent_id, monkeypatch):
    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    assert set_limit(client, owner, None)["status"] == "unlimited"
    for value in (-1, "NaN", "Infinity", True, "1000000000000", "0.0000000000001"):
        assert (
            client.put(
                f"/api/v1/employees/{owner}/budget", json={"monthly_allowance_usd": value}
            ).status_code
            == 422
        )
    assert set_limit(client, owner, "0")["status"] == "exhausted"
    with pytest.raises(identity.AdmissionDenied) as error:
        identity.admit_inference(token)
    assert error.value.code == "employee_budget_exceeded"
    set_limit(client, owner, "1")
    missing = identity.admit_inference(token)
    identity.record_inference(missing, {"outcome": "cancelled"})
    first = identity.admit_inference(token)
    identity.record_inference(first, {"cost": Decimal("0.8"), "outcome": "completed"})
    status = client.get(f"/api/v1/employees/{owner}/budget").json()
    assert status["status"] == "warning" and status["missing_cost_calls"] == 1
    inflight = identity.admit_inference(token)
    assert set_limit(client, owner, "0.5")["status"] == "exhausted"
    # Lowering the allowance never revokes an already admitted call.
    assert identity.validate_token(token)
    identity.record_inference(inflight, {"cost": Decimal("0.4"), "outcome": "completed"})
    assert set_limit(client, owner, "2")["known_spend_usd"] == "1.200000000000"
    identity.admit_inference(token)
    assert set_limit(client, owner, None)["status"] == "unlimited"
    # Normal employee saves cannot erase or replace a budget.
    with sessions() as session:
        employee = session.get(Employee, owner)
        role = str(employee.role_id)
    set_limit(client, owner, "7")
    assert (
        client.put(
            f"/api/v1/employees/{owner}", json={"name": "Renamed", "role_id": role}
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/employees/{owner}/budget").json()["monthly_allowance_usd"]
        == "7.000000000000"
    )


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_rejected_requests_never_reach_provider(
    client, sessions, agent_id, monkeypatch, native, stream
):
    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        if not native:
            agent.runtime_mode = "managed"
            session.add(
                Run(
                    agent_id=agent.id,
                    incarnation_id=agent.current_incarnation_id,
                    message="test",
                    status="running",
                    model_id="test/model",
                    inference={},
                    idempotency_key="budget-test",
                    request_hash="hash",
                )
            )
    monkeypatch.setattr(main, "provider_key", lambda: "synthetic")
    monkeypatch.setattr(openrouter, "provider_key", lambda: "synthetic")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: pytest.fail("Provider contacted"))
    endpoint = "/native/v1/chat/completions" if native else "/v1/chat/completions"
    body = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }
    headers = {"Authorization": f"Bearer {token}"}
    set_limit(client, owner, "0")
    with TestClient(main.app) as gateway:
        response = gateway.post(endpoint, headers=headers, json=body)
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "employee_budget_exceeded"
        with sessions.begin() as session:
            session.get(Agent, agent_id).employee_id = None
        response = gateway.post(endpoint, headers=headers, json=body)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "employee_assignment_required"
        if not native:
            # Explicit simulator remains usable without an employee or budget.
            response = gateway.post(endpoint, headers=headers, json={**body, "model": "fixture"})
            assert response.status_code == 200
        with sessions() as session:
            assert list(session.scalars(select(InferenceCall))) == []

        def unavailable():
            raise OperationalError("unavailable", None, None)

        monkeypatch.setattr(identity, "session_factory", unavailable)
        response = gateway.post(endpoint, headers=headers, json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "accounting_unavailable"


def test_shared_allowance_and_simultaneous_overshoot(client, sessions, agent_id, monkeypatch):
    import hashlib

    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    second_token = "second-agent-token-at-least-twenty-chars"
    with sessions.begin() as session:
        second = Agent(
            display_name="Second",
            employee_label="",
            employee_id=owner,
            runtime_mode="native",
            inference_override={"model_id": "test/model"},
            desired_state="running",
            observed_state="ready",
        )
        session.add(second)
        session.flush()
        incarnation = WorkloadIncarnation(
            agent_id=second.id,
            generation=1,
            gateway_token_hash=hashlib.sha256(second_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(incarnation)
        session.flush()
        second.current_incarnation_id = incarnation.id
    set_limit(client, owner, "1")
    barrier = Barrier(2)

    def admit(value):
        barrier.wait(timeout=5)
        return identity.admit_inference(value)

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(admit, [token, second_token]))
        list(pool.map(lambda call: identity.record_inference(call, {"cost": Decimal("0.6")}), ids))
    assert (
        client.get(f"/api/v1/employees/{owner}/budget").json()["known_spend_usd"]
        == "1.200000000000"
    )
    for value in (token, second_token):
        with pytest.raises(identity.AdmissionDenied):
            identity.admit_inference(value)
    # Another employee's allowance is independent; past charges retain their owner.
    new_token, new_owner = ledger_identity(sessions, agent_id, monkeypatch)
    identity.admit_inference(new_token)
    assert (
        Decimal(client.get(f"/api/v1/employees/{new_owner}/budget").json()["known_spend_usd"]) == 0
    )


def test_allowance_edit_serializes_with_admission(client, sessions, agent_id, monkeypatch):
    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    set_limit(client, owner, "1")
    waiting = Event()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "employees" in statement and "FOR UPDATE" in statement:
            waiting.set()

    engine = sessions.kw["bind"]
    with ThreadPoolExecutor(1) as pool:
        with sessions.begin() as writer:
            employee = writer.get(Employee, owner, with_for_update=True)
            employee.monthly_allowance_usd = Decimal(0)
            writer.flush()
            event.listen(engine, "before_cursor_execute", before_execute)
            pending = pool.submit(identity.admit_inference, token)
            assert waiting.wait(5) and not pending.done()
        try:
            with pytest.raises(identity.AdmissionDenied):
                pending.result(timeout=5)
        finally:
            event.remove(engine, "before_cursor_execute", before_execute)


def test_month_boundary_and_duplicate_finalization(client, sessions, agent_id, monkeypatch):
    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    start, end = month_bounds()
    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        session.get(WorkloadIncarnation, agent.current_incarnation_id).expires_at = end + timedelta(
            days=40
        )
    set_limit(client, owner, "1")
    clock = {"now": end - timedelta(seconds=1)}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(identity, "datetime", Frozen)
    previous = identity.admit_inference(token)
    clock["now"] = end
    with ThreadPoolExecutor(2) as pool:
        list(
            pool.map(
                lambda _: identity.record_inference(previous, {"cost": Decimal("1.5")}), range(2)
            )
        )
    current = identity.admit_inference(token)
    with sessions() as session:
        before = session.get(InferenceCall, previous)
        after = session.get(InferenceCall, current)
        assert before.admitted_at < end and before.completed_at == end
        assert before.cost_usd == Decimal("1.5") and after.admitted_at == end
        status = employee_budget(session, session.get(Employee, owner), end)
        assert Decimal(status["known_spend_usd"]) == 0 and status["status"] == "available"


def test_failed_finalization_remains_visible(client, sessions, agent_id, monkeypatch):
    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    monkeypatch.setattr(main, "provider_key", lambda: "synthetic")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"usage": {"cost": 0.1}})
            ),
            **kw,
        ),
    )

    def failed_finalization(*_):
        raise OperationalError("unavailable", None, None)

    monkeypatch.setattr(main, "record_inference", failed_finalization)
    with TestClient(main.app) as gateway:
        result = gateway.post(
            "/native/v1/chat/completions",
            json={"messages": []},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert result.status_code == 200
    status = client.get(f"/api/v1/employees/{owner}/budget").json()
    assert status["unresolved_calls"] == 1 and Decimal(status["known_spend_usd"]) == 0
    with sessions() as session:
        call = session.scalar(select(InferenceCall))
        assert call.completed_at is None and call.cost_usd is None


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_gateway_records_allowed_calls_then_blocks(
    client, sessions, agent_id, monkeypatch, native, stream
):
    import json

    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        if not native:
            agent.runtime_mode = "managed"
            session.add(
                Run(
                    agent_id=agent.id,
                    incarnation_id=agent.current_incarnation_id,
                    message="test",
                    status="running",
                    model_id="test/model",
                    inference={},
                    idempotency_key="allow-test",
                    request_hash="hash",
                )
            )
    set_limit(client, owner, "1")
    monkeypatch.setattr(main, "provider_key", lambda: "synthetic")
    monkeypatch.setattr(openrouter, "provider_key", lambda: "synthetic")
    provider_calls = []

    async def provider(request):
        provider_calls.append(request)
        reply = {
            "id": f"generation-{len(provider_calls)}",
            "usage": {"cost": 0.8, "prompt_tokens": 10},
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": "stop"}],
        }
        return (
            httpx.Response(
                200,
                text="data: " + json.dumps(reply) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
            if (stream or not native)
            else httpx.Response(200, json=reply)
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(provider), **kw),
    )
    with TestClient(main.app) as gateway:
        endpoint = "/native/v1/chat/completions" if native else "/v1/chat/completions"
        body = {
            "model": "default",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        }
        for _ in range(2):
            assert (
                gateway.post(
                    endpoint, headers={"Authorization": f"Bearer {token}"}, json=body
                ).status_code
                == 200
            )
        assert (
            gateway.post(
                endpoint, headers={"Authorization": f"Bearer {token}"}, json=body
            ).status_code
            == 402
        )
    assert len(provider_calls) == 2
    report = client.get(f"/api/v1/employees/{owner}/budget").json()
    assert report["known_spend_usd"] == "1.600000000000" and report["unresolved_calls"] == 0


def test_expiry_is_rechecked_after_employee_lock(sessions, agent_id, monkeypatch):
    from fastapi import HTTPException

    token, _ = ledger_identity(sessions, agent_id, monkeypatch)
    with sessions() as session:
        agent = session.get(Agent, agent_id)
        expiry = session.get(WorkloadIncarnation, agent.current_incarnation_id).expires_at
    ticks = iter([expiry - timedelta(seconds=1), expiry + timedelta(seconds=1)])

    class Tick(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(identity, "datetime", Tick)
    with pytest.raises(HTTPException) as error:
        identity.admit_inference(token)
    assert error.value.status_code == 401
    with sessions() as session:
        assert list(session.scalars(select(InferenceCall))) == []


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_selection_outage_preserves_accounting_error(
    client, sessions, agent_id, monkeypatch, native, stream
):
    from uuid import UUID

    token, _ = ledger_identity(sessions, agent_id, monkeypatch)
    if not native:
        run_id = UUID(diagnostics.submit(client, agent_id).json()["id"])
        with sessions.begin() as session:
            session.get(Run, run_id).status = "running"
            session.get(Agent, agent_id).runtime_mode = "managed"
    lookups = 0

    def fail_after_validation():
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            raise OperationalError("lookup unavailable", None, None)
        return sessions

    monkeypatch.setattr(identity, "session_factory", fail_after_validation)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: pytest.fail("Provider contacted"))
    endpoint = "/native/v1/chat/completions" if native else "/v1/chat/completions"
    with TestClient(main.app) as gateway:
        response = gateway.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "default", "messages": [], "stream": stream},
        )
    assert lookups == 2
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "accounting_unavailable",
            "type": "admission_error",
            "message": "Inference accounting is unavailable",
        }
    }
    with sessions() as session:
        assert list(session.scalars(select(InferenceCall))) == []
