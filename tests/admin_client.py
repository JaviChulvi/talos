"""Give existing business-path integration tests a real, persisted admin session."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import insert

from backend.app.auth import COOKIE, hash_password, token_digest
from backend.app.models import Administrator, AdministratorSession

TEST_HASH = hash_password("integration admin password")


def administrator_client(app, sessions):
    token = secrets.token_urlsafe(32)
    with sessions.begin() as session:
        session.execute(
            insert(Administrator)
            .values(id=1, password_hash=TEST_HASH, failed_attempts=0)
            .on_conflict_do_nothing()
        )
        session.add(
            AdministratorSession(
                token_hash=token_digest(token), expires_at=datetime.now(UTC) + timedelta(hours=8)
            )
        )
    return TestClient(app, headers={"X-Talos-Request": "1"}, cookies={COOKIE: token})
