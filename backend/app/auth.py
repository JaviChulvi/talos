"""The built-in admin account: host-only setup and recovery."""

import argparse
import base64
import getpass
import secrets
import sys

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db import session_factory
from backend.app.models import Administrator, AdministratorSession


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
