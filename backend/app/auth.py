"""The built-in admin account: host-only setup and recovery."""

import argparse
import base64
import getpass
import hashlib
import math
import secrets
import sys
import warnings
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.db import Database, session_factory
from backend.app.models import Administrator, AdministratorSession

COOKIE = "talos_admin"
SESSION_SECONDS = 8 * 60 * 60
COOLDOWN_SECONDS = 60
router = APIRouter(prefix="/api/v1/auth")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_session(request: Request, session: Session) -> AdministratorSession | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        return None
    return session.scalar(
        select(AdministratorSession)
        .join(Administrator)
        .where(
            AdministratorSession.token_hash == token_digest(token),
            AdministratorSession.expires_at > datetime.now(UTC),
        )
    )


def require_admin(request: Request, session: Database) -> None:
    if current_session(request, session) is None:
        raise HTTPException(401, "Sign in required")
    # End the admission read before handlers start their own explicit transactions.
    session.rollback()


@router.get("/session")
def session_status(request: Request, response: Response, session: Database):
    response.headers["Cache-Control"] = "no-store"
    return {
        "setup_required": session.get(Administrator, 1) is None,
        "authenticated": current_session(request, session) is not None,
    }


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Short guesses must count as failed attempts too; setup alone enforces minimum length.
    password: SecretStr = Field(max_length=128)


@router.post("/login", status_code=204)
def login(body: LoginInput, request: Request, response: Response, session: Database):
    admin = session.get(Administrator, 1, with_for_update=True)
    if admin is None:
        raise HTTPException(401, "Administrator setup required")
    now = datetime.now(UTC)
    if admin.cooldown_until and admin.cooldown_until > now:
        raise HTTPException(
            429,
            "Too many attempts; try again shortly",
            headers={"Retry-After": str(math.ceil((admin.cooldown_until - now).total_seconds()))},
        )
    if admin.cooldown_until:
        admin.failed_attempts = 0
        admin.cooldown_until = None
    if not verify_password(body.password.get_secret_value(), admin.password_hash):
        admin.failed_attempts += 1
        if admin.failed_attempts == 5:
            admin.cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)
        session.commit()  # Persist before raising: request rollback must not undo throttling.
        if admin.cooldown_until:
            raise HTTPException(
                429, "Too many attempts; try again shortly", headers={"Retry-After": "60"}
            )
        raise HTTPException(401, "Incorrect password")
    admin.failed_attempts = 0
    admin.cooldown_until = None
    # Replace this browser's previous session; cleanup needs no extra service.
    old_token = request.cookies.get(COOKIE, "")
    session.execute(
        delete(AdministratorSession).where(
            (AdministratorSession.expires_at <= now)
            | (AdministratorSession.token_hash == token_digest(old_token))
        )
    )
    token = secrets.token_urlsafe(32)
    session.add(
        AdministratorSession(
            token_hash=token_digest(token), expires_at=now + timedelta(seconds=SESSION_SECONDS)
        )
    )
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_SECONDS,
        expires=now + timedelta(seconds=SESSION_SECONDS),
        httponly=True,
        secure=get_settings().admin_cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, session: Database):
    session.execute(
        delete(AdministratorSession).where(
            AdministratorSession.token_hash == token_digest(request.cookies.get(COOKIE, ""))
        )
    )
    session.commit()
    response.delete_cookie(
        COOKIE,
        path="/",
        httponly=True,
        secure=get_settings().admin_cookie_secure,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def validate_password(password: str) -> None:
    if not 15 <= len(password) <= 128:
        raise ValueError("Password must contain 15–128 characters")


def password_kdf(salt: bytes) -> Scrypt:
    # OWASP's scrypt baseline; cryptography is already a platform dependency.
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1)


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    key = password_kdf(salt).derive(password.encode("utf-8"))
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def verify_password(password: str, encoded: str) -> bool:
    scheme, salt, key = encoded.split("$")
    if scheme != "scrypt":
        raise ValueError("Unsupported password hash")
    try:
        password_kdf(base64.b64decode(salt, validate=True)).verify(
            password.encode("utf-8"), base64.b64decode(key, validate=True)
        )
        return True
    except InvalidKey:
        return False


def bootstrap(session: Session, password: str) -> None:
    session.add(Administrator(id=1, password_hash=hash_password(password)))
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if getattr(error.orig, "sqlstate", None) != "23505":
            raise
        raise ValueError("Administrator already exists; use reset-password") from None


def reset_password(session: Session, password: str) -> None:
    encoded = hash_password(password)
    # Login also locks this singleton, so a concurrent login cannot retain the old password.
    admin = session.get(Administrator, 1, with_for_update=True)
    if admin is None:
        raise ValueError("Administrator is not set up; use bootstrap")
    admin.password_hash = encoded
    admin.failed_attempts = 0
    admin.cooldown_until = None
    session.execute(delete(AdministratorSession))
    session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up or recover Talos's built-in admin")
    parser.add_argument("command", choices=["bootstrap", "reset-password"])
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("Run in an interactive terminal so the password can be hidden", file=sys.stderr)
        return 1
    try:
        with warnings.catch_warnings():
            # Refuse getpass's echoing fallback if terminal echo control is unavailable.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("New admin password: ")
            validate_password(password)
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords do not match")
        with session_factory()() as session:
            if args.command == "bootstrap":
                bootstrap(session, password)
            else:
                reset_password(session, password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Database unavailable; no administrator change completed", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Cannot hide password input; use an interactive terminal", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled", file=sys.stderr)
        return 1
    print(
        "Administrator created"
        if args.command == "bootstrap"
        else "Password reset; sessions revoked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
