"""Immutable role/setup selections shared by previews and lifecycle admission."""

import copy
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.capabilities import compile_permissions
from backend.app.models import Agent, Employee, Role, SetupRevision


def application_fingerprint(application: dict) -> str:
    payload = {
        k: v
        for k, v in application.items()
        if k not in {"fingerprint", "restart", "legacy_receipt"}
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_application(application: dict, runtime_kind: str) -> dict:
    result = copy.deepcopy(application)
    result.pop("restart", None)
    result["role"].setdefault("setup_revision_id", None)
    result["role"].setdefault("connector_grants", [])
    result.setdefault("setup", None)
    result.setdefault("connector_grants", [])
    result.setdefault("connections", {})
    result.setdefault(
        "permissions", compile_permissions(result["role"]["capabilities"], runtime_kind)
    )
    result["fingerprint"] = application_fingerprint(result)
    return result


def desired_application(session: Session, agent: Agent, *, lock: bool = True) -> dict:
    if agent.runtime_mode != "native" or agent.employee_id is None:
        raise HTTPException(409, "Choose an employee for a native agent first")
    employee = session.get(
        Employee, agent.employee_id, with_for_update=lock, populate_existing=True
    )
    role = session.get(Role, employee.role_id, with_for_update=lock, populate_existing=True)
    setup = None
    grants = sorted(role.connector_grants or [])
    if role.setup_revision_id:
        revision = session.get(SetupRevision, role.setup_revision_id)
        if revision is None:
            raise HTTPException(409, "The selected setup revision is unavailable")
        manifest = revision.manifest
        if not any(
            t["runtime_kind"] == agent.runtime_kind
            and t["runtime_release"] == agent.runtime_release
            for t in manifest["targets"]
        ):
            raise HTTPException(409, "This setup does not support the agent's runtime release")
        known = {c["id"] for c in manifest["connectors"]}
        if set(grants) - known:
            raise HTTPException(409, "Role grants a connector outside its selected setup")
        setup = {
            "revision_id": str(revision.id),
            "version": revision.version,
            "artifact_hash": revision.artifact_hash,
            "manifest": manifest,
        }
        if manifest["connection_slots"]:
            raise HTTPException(
                409, "This setup needs account connections before it can be applied"
            )
    elif grants:
        raise HTTPException(409, "Choose a setup before granting its connectors")
    return normalize_application(
        {
            "employee_id": str(employee.id),
            "role": {
                "id": str(role.id),
                "name": role.name,
                "revision": role.revision,
                "capabilities": list(role.capabilities),
                "setup_revision_id": str(role.setup_revision_id)
                if role.setup_revision_id
                else None,
                "connector_grants": grants,
            },
            "setup": setup,
            "connector_grants": grants,
            "permissions": compile_permissions(role.capabilities, agent.runtime_kind),
        },
        agent.runtime_kind,
    )


def application_preview(session: Session, agent: Agent) -> dict:
    try:
        application = desired_application(session, agent, lock=False)
    except HTTPException as error:
        return {"application": None, "changes": [], "blockers": [str(error.detail)]}
    applied = agent.applied_application
    changes = []
    if not applied:
        changes.append("Install this role configuration for the first time")
    else:
        for key, message in (
            ("role", "Role permissions or setup selection changed"),
            ("setup", "Setup version changed"),
            ("connections", "Account connections changed"),
            ("employee_id", "Employee assignment changed"),
        ):
            if application.get(key) != applied.get(key):
                changes.append(message)
    if agent.desired_state == "running" and changes:
        changes.append("Running work will stop and the agent will restart")
    return {"application": application, "changes": changes, "blockers": []}


def annotate_agent(session: Session, agent: Agent) -> Agent:
    agent.setup_status = "not_configured"
    agent.setup_pending = False
    agent.setup_blockers = []
    if agent.runtime_mode != "native" or agent.employee_id is None:
        return agent
    preview = application_preview(session, agent)
    agent.setup_blockers = preview["blockers"]
    selected = agent.selected_application
    applied = agent.applied_application
    desired = preview["application"]
    agent.setup_pending = bool(
        preview["blockers"]
        or (
            desired
            and (
                not applied
                or desired["fingerprint"]
                != normalize_application(applied, agent.runtime_kind)["fingerprint"]
            )
        )
    )
    if preview["blockers"]:
        agent.setup_status = (
            "needs_connection"
            if any("connection" in b.lower() for b in preview["blockers"])
            else "blocked"
        )
    elif agent.setup_pending and applied:
        agent.setup_status = "update_available"
    elif applied and applied.get("setup"):
        agent.setup_status = "verified_ready" if agent.observed_state == "ready" else "installed"
    elif selected and selected.get("setup"):
        agent.setup_status = "blocked" if agent.last_error else "pending"
    return agent
