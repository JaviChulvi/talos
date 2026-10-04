"""Local administrator records; user identities are not login accounts."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.agents import Database
from backend.app.capabilities import CAPABILITIES
from backend.app.models import AgentProfile, SetupRevision, User
from backend.app.usage import user_budget

router = APIRouter(prefix="/api/v1")


class AgentProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    description: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")
    capabilities: list[str] = Field(default_factory=list)
    setup_revision_id: UUID | None = None
    connector_grants: list[str] = Field(default_factory=list)
    connection_bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def known_capabilities(cls, value):
        if set(value) - CAPABILITIES.keys():
            raise ValueError("Unknown capability")
        return sorted(set(value))


class AgentProfileResponse(AgentProfileInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    revision: int


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]*$")
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    profile_id: UUID
    connection_overrides: dict[str, str] = Field(default_factory=dict)


class UserResponse(UserInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID


@router.get("/capabilities")
def capabilities():
    return [{"id": key, **value} for key, value in CAPABILITIES.items()]


@router.get("/profiles", response_model=list[AgentProfileResponse])
def profiles(session: Database):
    return session.scalars(select(AgentProfile).order_by(AgentProfile.name, AgentProfile.id)).all()


def validate_bindings(bindings: dict, session: Database):
    from backend.app.connections import ConnectionBindingError, validate_connection_bindings

    try:
        validate_connection_bindings(session, bindings)
    except ConnectionBindingError as error:
        raise HTTPException(400, str(error)) from None


def validate_profile_setup(body: AgentProfileInput, session: Database):
    validate_bindings(body.connection_bindings, session)
    if body.setup_revision_id is None:
        if body.connector_grants or body.connection_bindings:
            raise HTTPException(400, "Choose a setup before granting connectors")
        return
    revision = session.get(SetupRevision, body.setup_revision_id, with_for_update=True)
    if revision is None:
        raise HTTPException(400, "Setup revision not found")
    if set(body.connection_bindings) - {
        slot["id"] for slot in revision.manifest["connection_slots"]
    }:
        raise HTTPException(400, "Connection slot is not in the selected setup")
    if set(body.connector_grants) - {c["id"] for c in revision.manifest["connectors"]}:
        raise HTTPException(400, "Connector is not in the selected setup")


@router.post("/profiles", response_model=AgentProfileResponse, status_code=201)
def create_profile(body: AgentProfileInput, session: Database):
    validate_profile_setup(body, session)
    profile = AgentProfile(**body.model_dump())
    session.add(profile)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A profile with that name already exists") from None
    return profile


@router.put("/profiles/{profile_id}", response_model=AgentProfileResponse)
def update_profile(profile_id: UUID, body: AgentProfileInput, session: Database):
    profile = session.get(AgentProfile, profile_id, with_for_update=True)
    if profile is None:
        raise HTTPException(404, "Agent profile not found")
    validate_profile_setup(body, session)
    if (
        profile.capabilities != body.capabilities
        or profile.setup_revision_id != body.setup_revision_id
        or profile.connector_grants != body.connector_grants
        or profile.connection_bindings != body.connection_bindings
    ):
        profile.revision += 1
    for key, value in body.model_dump().items():
        setattr(profile, key, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A profile with that name already exists") from None
    return profile


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: UUID, session: Database):
    profile = session.get(AgentProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Agent profile not found")
    session.delete(profile)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Agent profile is assigned to users") from None
    return Response(status_code=204)


@router.get("/users", response_model=list[UserResponse])
def users(session: Database):
    return session.scalars(select(User).order_by(User.name, User.id)).all()


@router.post("/users", response_model=UserResponse, status_code=201)
def create_user(body: UserInput, session: Database):
    # Hold the profile row until the FK is committed, serializing concurrent deletion.
    if session.get(AgentProfile, body.profile_id, with_for_update=True) is None:
        raise HTTPException(404, "Agent profile not found")
    validate_bindings(body.connection_overrides, session)
    user = User(**body.model_dump())
    session.add(user)
    session.commit()
    return user


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(user_id: UUID, body: UserInput, session: Database):
    user = session.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(404, "User not found")
    if session.get(AgentProfile, body.profile_id, with_for_update=True) is None:
        raise HTTPException(404, "Agent profile not found")
    validate_bindings(body.connection_overrides, session)
    for key, value in body.model_dump().items():
        setattr(user, key, value)
    session.commit()
    return user


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: UUID, session: Database):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    session.delete(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "User is assigned to agents, including retained history") from None
    return Response(status_code=204)


class BudgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_budget_usd: Decimal | None = Field(
        ..., ge=0, max_digits=24, decimal_places=12, allow_inf_nan=False
    )


@router.get("/users/{user_id}/budget")
def get_budget(user_id: UUID, session: Database):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user_budget(session, user)


@router.put("/users/{user_id}/budget")
def put_budget(user_id: UUID, body: BudgetInput, session: Database):
    with session.begin():
        user = session.get(User, user_id, with_for_update=True)
        if user is None:
            raise HTTPException(404, "User not found")
        user.monthly_budget_usd = body.monthly_budget_usd
        session.flush()
        return user_budget(session, user)
