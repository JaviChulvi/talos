"""Read-only reporting of provider calls observed by the Talos gateway."""

import base64
import binascii
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, tuple_

from backend.app.agents import Database
from backend.app.models import Agent, Employee, InferenceCall, InferenceConfig

router = APIRouter(prefix="/api/v1/usage")
Month = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
COVERAGE = (
    "Only provider requests routed through Talos are tracked. Direct-provider traffic, "
    "external tool charges, and infrastructure costs are excluded."
)


def month_bounds(month: str | None = None) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month or datetime.now(UTC).strftime("%Y-%m"), "%Y-%m").replace(
            tzinfo=UTC
        )
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, end
    except ValueError:
        raise HTTPException(422, "Invalid UTC calendar month") from None


def call_filters(start, end, employee_id, agent_id):
    return [
        InferenceCall.admitted_at >= start,
        InferenceCall.admitted_at < end,
        *([InferenceCall.employee_id == employee_id] if employee_id else []),
        *([InferenceCall.agent_id == agent_id] if agent_id else []),
    ]


def aggregate_columns():
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(InferenceCall.cost_usd), 0).label("known_spend_usd"),
        func.count().filter(InferenceCall.cost_usd.is_not(None)).label("reported_cost_calls"),
        func.count().filter(InferenceCall.completed_at.is_(None)).label("unresolved_calls"),
        func.count()
        .filter(InferenceCall.completed_at.is_not(None), InferenceCall.cost_usd.is_(None))
        .label("missing_cost_calls"),
        *[
            func.sum(getattr(InferenceCall, key)).label(key)
            for key in ("input_tokens", "output_tokens", "reasoning_tokens")
        ],
    ]


def totals(row):
    result = dict(row)
    result["known_spend_usd"] = str(result["known_spend_usd"])
    return result


@router.get("")
def usage(
    session: Database,
    month: Month = None,
    employee_id: UUID | None = None,
    agent_id: UUID | None = None,
):
    start, end = month_bounds(month)
    filters = call_filters(start, end, employee_id, agent_id)
    columns = aggregate_columns()
    total = totals(session.execute(select(*columns).where(*filters)).mappings().one())
    groups = {}
    for name, column in (
        ("employees", InferenceCall.employee_id),
        ("agents", InferenceCall.agent_id),
    ):
        rows = session.execute(
            select(column.label("id"), *columns).where(*filters).group_by(column)
        )
        groups[name] = [totals(row) for row in rows.mappings()]
    config = session.get(InferenceConfig, 1)
    tracking = config.tracking_started_at
    return {
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "tracking_started_at": tracking,
        "history_status": "before_tracking"
        if end <= tracking
        else "partial"
        if start < tracking
        else "tracked",
        "coverage": COVERAGE,
        "total": total,
        "budgets": [
            employee_budget(session, employee)
            for employee in session.scalars(
                select(Employee)
                .where(
                    Employee.id == employee_id if employee_id else True,
                    Employee.id.in_(select(Agent.employee_id).where(Agent.id == agent_id))
                    if agent_id and not employee_id
                    else True,
                )
                .order_by(Employee.name, Employee.id)
            )
        ]
        if start == month_bounds()[0]
        else [],
        **groups,
        "options": {
            "employees": [
                {"id": e.id, "name": e.name}
                for e in session.scalars(select(Employee).order_by(Employee.name, Employee.id))
            ],
            "agents": [
                {
                    "id": a.id,
                    "name": a.display_name,
                    "deleted": a.observed_state == "deleted",
                    "coverage": "Provider handled by agent: usage unavailable"
                    if a.runtime_mode == "native" and not a.inference_override
                    else "Requests through Talos are tracked",
                }
                for a in session.scalars(select(Agent).order_by(Agent.display_name, Agent.id))
            ],
        },
    }


@router.get("/calls")
def calls(
    session: Database,
    month: Month = None,
    employee_id: UUID | None = None,
    agent_id: UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    start, end = month_bounds(month)
    query = select(InferenceCall).where(*call_filters(start, end, employee_id, agent_id))
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError
            stamp, call_id = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            stamp = datetime.fromisoformat(stamp)
            if stamp.tzinfo is None:
                raise ValueError
            query = query.where(
                tuple_(InferenceCall.admitted_at, InferenceCall.id) < (stamp, UUID(call_id))
            )
        except (ValueError, TypeError, binascii.Error):
            raise HTTPException(400, "Invalid usage cursor") from None
    rows = list(
        session.scalars(
            query.order_by(InferenceCall.admitted_at.desc(), InferenceCall.id.desc()).limit(
                limit + 1
            )
        )
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.admitted_at.isoformat(), str(last.id)]).encode()
        ).decode()
    fields = (
        "id",
        "agent_id",
        "employee_id",
        "run_id",
        "model",
        "generation_id",
        "admitted_at",
        "completed_at",
        "outcome",
        "finish_reason",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_usd",
    )
    return {
        "items": [
            {
                key: str(value) if isinstance(value := getattr(row, key), Decimal) else value
                for key in fields
            }
            for row in page
        ],
        "next_cursor": next_cursor,
    }


def employee_budget(session, employee: Employee, now: datetime | None = None) -> dict:
    start, end = month_bounds((now or datetime.now(UTC)).strftime("%Y-%m"))
    total = totals(
        session.execute(
            select(*aggregate_columns()).where(*call_filters(start, end, employee.id, None))
        )
        .mappings()
        .one()
    )
    allowance = employee.monthly_allowance_usd
    spend = Decimal(total["known_spend_usd"])
    status = (
        "unlimited"
        if allowance is None
        else "exhausted"
        if spend >= allowance
        else "warning"
        if spend >= allowance * Decimal("0.8")
        else "available"
    )
    return {
        "employee_id": employee.id,
        "employee_name": employee.name,
        "monthly_allowance_usd": str(allowance) if allowance is not None else None,
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "status": status,
        **total,
    }
