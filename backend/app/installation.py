"""One durable admission fence shared by the API, workers, and host maintenance."""

from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.db import Database
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    ACTIVE_RUN_STATUSES,
    Agent,
    ChannelOutbox,
    ChannelProbe,
    InferenceCall,
    InstallationState,
    Operation,
    Run,
)


def state_row(session, *, exclusive=False):
    # Every migrated database contains this row. A missing row is not permission
    # to admit work: the maintenance lock would otherwise protect nothing.
    state = session.scalar(
        select(InstallationState)
        .where(InstallationState.id == 1)
        .with_for_update(read=not exclusive)
        .execution_options(populate_existing=True)
    )
    if state is None:
        raise HTTPException(503, "Installation state is unavailable")
    return state


def writable(session) -> bool:
    """Hold shared admission until this transaction ends, including when paused."""
    return state_row(session).maintenance_operation_id is None


def ensure_writable(session):
    if not writable(session):
        raise HTTPException(503, "Installation maintenance is active")


def management_admission(request: Request, session: Database):
    # Route handlers own their transaction boundaries. A separate connection
    # retains admission through file writes and commits for the whole request.
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        yield
        return
    with Session(session.get_bind()) as admission:
        ensure_writable(admission)
        yield


def installation_status(session):
    state = session.get(InstallationState, 1, populate_existing=True)
    if state is None:
        raise HTTPException(503, "Installation state is unavailable")
    return {
        "release": state.release,
        "maintenance": {
            "active": state.maintenance_operation_id is not None,
            "operation_id": state.maintenance_operation_id,
            "kind": state.maintenance_kind,
            "started_at": state.maintenance_started_at,
        },
    }


def enter_maintenance(session, operation_id: str, kind: str):
    """Caller commits the fence before pausing services; failures retain no fence."""
    if (
        not operation_id
        or len(operation_id) > 128
        or kind
        not in {
            "install",
            "backup",
            "restore",
            "update",
            "reconcile",
        }
    ):
        raise ValueError("Invalid maintenance operation")
    state = state_row(session, exclusive=True)
    if state.maintenance_operation_id is not None:
        if (state.maintenance_operation_id, state.maintenance_kind) == (operation_id, kind):
            return
        raise HTTPException(409, "Another maintenance operation is active")
    blockers = (
        (
            select(Agent.id).where(
                (Agent.desired_state == "running")
                | Agent.observed_state.not_in(("stopped", "deleted")),
            ),
            "Stop every agent and wait for its operation before maintenance",
        ),
        (
            select(Operation.id).where(Operation.status.in_(ACTIVE_OPERATION_STATUSES)),
            "Resolve active lifecycle operations before maintenance",
        ),
        (
            select(Run.id).where(Run.status.in_(ACTIVE_RUN_STATUSES)),
            "Resolve active or uncertain work before maintenance",
        ),
        (
            select(ChannelOutbox.id).where(
                ChannelOutbox.state.in_(
                    ("waiting", "pending")
                    if kind == "reconcile"
                    else ("waiting", "pending", "sending", "uncertain"),
                )
            ),
            "Resolve pending or uncertain channel delivery before maintenance",
        ),
        (
            select(ChannelProbe.id).where(ChannelProbe.status.in_(("queued", "running"))),
            "Wait for channel checks before maintenance",
        ),
        (
            select(InferenceCall.id).where(
                InferenceCall.completed_at.is_(None), kind != "reconcile"
            ),
            "Resolve outstanding inference accounting before maintenance",
        ),
    )
    for query, message in blockers:
        if session.scalar(query.limit(1)) is not None:
            raise HTTPException(409, message)
    state.maintenance_operation_id = operation_id
    state.maintenance_kind = kind
    state.maintenance_started_at = datetime.now(UTC)
    session.flush()


def reconcile_uncertain(session, operation_id: str):
    """Host management calls only after confirming every platform writer stopped."""
    state = state_row(session, exclusive=True)
    if (state.maintenance_operation_id, state.maintenance_kind) != (operation_id, "reconcile"):
        raise HTTPException(409, "Uncertainty reconciliation requires its own maintenance fence")
    session.execute(
        update(InferenceCall)
        .where(InferenceCall.completed_at.is_(None))
        .values(completed_at=datetime.now(UTC), outcome="interrupted")
    )
    # Retain parts, provider IDs and partial-send progress. Never resend or claim success.
    session.execute(
        update(ChannelOutbox)
        .where(ChannelOutbox.state.in_(("sending", "uncertain")))
        .values(state="blocked", code="uncertain_reconciled", retry_at=None)
    )


def leave_maintenance(session, operation_id: str):
    state = state_row(session, exclusive=True)
    if state.maintenance_operation_id is None:
        return
    if state.maintenance_operation_id != operation_id:
        raise HTTPException(409, "Maintenance operation identity does not match")
    state.maintenance_operation_id = None
    state.maintenance_kind = None
    state.maintenance_started_at = None
    session.flush()
