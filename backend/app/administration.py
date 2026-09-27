"""Local administrator records; employee identities are not login accounts."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.agents import Database
from backend.app.capabilities import CAPABILITIES
from backend.app.models import Employee, Role

router = APIRouter(prefix="/api/v1")


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    description: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("capabilities")
    @classmethod
    def known_capabilities(cls, value):
        if set(value) - CAPABILITIES.keys():
            raise ValueError("Unknown capability")
        return sorted(set(value))


class RoleResponse(RoleInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    revision: int


class EmployeeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]*$")
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role_id: UUID


class EmployeeResponse(EmployeeInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


@router.get("/capabilities")
def capabilities():
    return [{"id": key, **value} for key, value in CAPABILITIES.items()]


@router.get("/roles", response_model=list[RoleResponse])
def roles(session: Database):
    return session.scalars(select(Role).order_by(Role.name, Role.id)).all()


@router.post("/roles", response_model=RoleResponse, status_code=201)
def create_role(body: RoleInput, session: Database):
    role = Role(**body.model_dump())
    session.add(role)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A role with that name already exists") from None
    return role


@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(role_id: UUID, body: RoleInput, session: Database):
    role = session.get(Role, role_id, with_for_update=True)
    if role is None:
        raise HTTPException(404, "Role not found")
    if role.capabilities != body.capabilities:
        role.revision += 1
    for key, value in body.model_dump().items():
        setattr(role, key, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A role with that name already exists") from None
    return role


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role_id: UUID, session: Database):
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "Role not found")
    session.delete(role)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Role is assigned to employees") from None
    return Response(status_code=204)


@router.get("/employees", response_model=list[EmployeeResponse])
def employees(session: Database):
    return session.scalars(select(Employee).order_by(Employee.name, Employee.id)).all()


@router.post("/employees", response_model=EmployeeResponse, status_code=201)
def create_employee(body: EmployeeInput, session: Database):
    # Hold the role row until the FK is committed, serializing concurrent deletion.
    if session.get(Role, body.role_id, with_for_update=True) is None:
        raise HTTPException(404, "Role not found")
    employee = Employee(**body.model_dump())
    session.add(employee)
    session.commit()
    return employee


@router.put("/employees/{employee_id}", response_model=EmployeeResponse)
def update_employee(employee_id: UUID, body: EmployeeInput, session: Database):
    employee = session.get(Employee, employee_id, with_for_update=True)
    if employee is None:
        raise HTTPException(404, "Employee not found")
    if session.get(Role, body.role_id, with_for_update=True) is None:
        raise HTTPException(404, "Role not found")
    for key, value in body.model_dump().items():
        setattr(employee, key, value)
    session.commit()
    return employee


@router.delete("/employees/{employee_id}", status_code=204)
def delete_employee(employee_id: UUID, session: Database):
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "Employee not found")
    session.delete(employee)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            409, "Employee is assigned to agents, including retained history"
        ) from None
    return Response(status_code=204)
