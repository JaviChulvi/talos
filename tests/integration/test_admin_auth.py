"""Administrator invariants against real PostgreSQL, including concurrent commands."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.app.auth import bootstrap, reset_password, verify_password
from backend.app.models import Administrator, AdministratorSession
from tests.integration.test_agents import database_engine  # noqa: F401

pytestmark = pytest.mark.integration
PASSWORD = "  contraseña segura 🔑  "


@pytest.fixture
def auth_sessions(database_engine):  # noqa: F811
    with database_engine.begin() as connection:
        connection.execute(text("TRUNCATE administrator CASCADE"))
    return sessionmaker(database_engine, expire_on_commit=False)


def test_bootstrap_is_singleton_under_concurrency(auth_sessions):
    barrier = Barrier(2)

    def create(password):
        with auth_sessions() as session:
            barrier.wait()
            try:
                bootstrap(session, password)
                return password
            except ValueError:
                return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(create, [PASSWORD, "a different password"]))
    winner = next(result for result in results if result)
    assert results.count(None) == 1
    with auth_sessions() as session:
        admin = session.scalar(select(Administrator))
        assert verify_password(winner, admin.password_hash)
        assert winner not in admin.password_hash
        with pytest.raises(ValueError, match="already exists"):
            bootstrap(session, "another valid password")
        assert verify_password(winner, session.get(Administrator, 1).password_hash)


def test_reset_revokes_all_sessions_and_preserves_exact_password(auth_sessions):
    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
        session.add_all(
            [
                AdministratorSession(
                    token_hash=value * 64, expires_at=datetime.now(UTC) + timedelta(hours=8)
                )
                for value in ["a", "b"]
            ]
        )
        session.commit()
        reset_password(session, "new password with spaces  ")
        assert not session.scalars(select(AdministratorSession)).all()
        encoded = session.get(Administrator, 1).password_hash
        assert verify_password("new password with spaces  ", encoded)
        assert not verify_password("new password with spaces", encoded)


def test_reset_requires_bootstrap(auth_sessions):
    with auth_sessions() as session, pytest.raises(ValueError, match="not set up"):
        reset_password(session, PASSWORD)
