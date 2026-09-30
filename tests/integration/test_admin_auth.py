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


def test_failed_reset_rolls_back_password_and_session_deletion(auth_sessions):
    from sqlalchemy.exc import SQLAlchemyError

    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
        session.add(
            AdministratorSession(
                token_hash="c" * 64, expires_at=datetime.now(UTC) + timedelta(hours=8)
            )
        )
        session.commit()
    with auth_sessions.begin() as session:
        session.execute(
            text("""
            CREATE FUNCTION reject_session_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'simulated reset failure'; END; $$
        """)
        )
        session.execute(
            text("""
            CREATE TRIGGER reject_session_delete BEFORE DELETE ON administrator_sessions
            FOR EACH ROW EXECUTE FUNCTION reject_session_delete()
        """)
        )
    try:
        with pytest.raises(SQLAlchemyError), auth_sessions() as session:
            reset_password(session, "replacement password")
        with auth_sessions() as session:
            assert verify_password(PASSWORD, session.get(Administrator, 1).password_hash)
            assert session.get(AdministratorSession, "c" * 64) is not None
    finally:
        with auth_sessions.begin() as session:
            session.execute(text("DROP TRIGGER reject_session_delete ON administrator_sessions"))
            session.execute(text("DROP FUNCTION reject_session_delete()"))


@pytest.fixture
def auth_app(auth_sessions, monkeypatch):
    from backend.app.config import get_settings
    from backend.app.db import get_db
    from backend.app.main import create_app

    monkeypatch.setattr(get_settings(), "admin_cookie_secure", False)

    def database():
        with auth_sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = database
    return app


def browser(app, restart=False):
    from fastapi.testclient import TestClient

    if restart:
        from backend.app.main import create_app

        fresh_app = create_app()
        fresh_app.dependency_overrides.update(app.dependency_overrides)
        app = fresh_app
    return TestClient(app, headers={"X-Talos-Request": "1"})


def test_session_restart_expiry_and_logout(auth_app, auth_sessions):
    from backend.app.auth import COOKIE

    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    first = browser(auth_app)
    assert first.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    assert first.get("/api/v1/agents").status_code == 200
    assert PASSWORD not in first.get("/api/v1/auth/session").text
    cookie = first.cookies.get(COOKIE)
    with auth_sessions() as session:
        row = session.scalar(select(AdministratorSession))
        expiry = row.expires_at
        assert 28795 < (expiry - datetime.now(UTC)).total_seconds() <= 28800
        assert cookie != row.token_hash and cookie not in row.token_hash
    restarted = browser(auth_app, restart=True)
    restarted.cookies.update(first.cookies)
    assert restarted.get("/api/v1/agents").status_code == 200
    with auth_sessions() as session:
        assert session.scalar(select(AdministratorSession)).expires_at == expiry
    assert first.post("/api/v1/auth/logout").status_code == 204
    assert restarted.get("/api/v1/agents").status_code == 401
    assert first.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    with auth_sessions.begin() as session:
        session.scalar(select(AdministratorSession)).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert first.get("/api/v1/agents").status_code == 401
    assert not first.get("/api/v1/auth/session").json()["authenticated"]


def test_concurrent_throttling_survives_restart(auth_app, auth_sessions):
    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    barrier = Barrier(8)

    def guess(_):
        barrier.wait()
        return browser(auth_app).post("/api/v1/auth/login", json={"password": "wrong"}).status_code

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(guess, range(8)))
    assert results.count(401) == 4
    assert results.count(429) == 4
    restarted = browser(auth_app, restart=True)
    assert restarted.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 429
    with auth_sessions.begin() as session:
        admin = session.get(Administrator, 1)
        assert admin.failed_attempts == 5
        admin.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
    assert restarted.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
    with auth_sessions() as session:
        assert session.get(Administrator, 1).failed_attempts == 0


def test_every_management_route_requires_authentication(auth_app, auth_sessions):
    from uuid import uuid4

    client = browser(auth_app)
    assert client.get("/api/v1/auth/session").json() == {
        "setup_required": True,
        "authenticated": False,
    }
    paths = auth_app.openapi()["paths"]
    checked = 0
    for path, methods in paths.items():
        if not path.startswith("/api/v1/") or path.startswith("/api/v1/auth/"):
            continue
        for method in methods:
            url = path
            for parameter in methods[method].get("parameters", []):
                if parameter["in"] == "path":
                    url = url.replace("{" + parameter["name"] + "}", str(uuid4()))
            result = client.request(method, url, json={} if method != "get" else None)
            assert result.status_code == 401, (method, path, result.text)
            checked += 1
    assert checked >= 70
    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    assert client.get("/api/v1/agents").status_code == 401
    client.cookies.set("talos_admin", "invalid")
    assert client.get("/api/v1/agents").status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/agents"])
def test_mutations_reject_cross_site_and_simple_requests(auth_app, path):
    client = browser(auth_app)
    assert (
        client.post(path, json={}, headers={"Origin": "https://attacker.example"}).status_code
        == 403
    )
    assert client.post(path, json={}, headers={"Origin": "null"}).status_code == 403
    assert client.post(path, json={}, headers={"X-Talos-Request": ""}).status_code == 403
    assert (
        client.post(path, json={}, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    )
    assert (
        client.post(path, json={}, headers={"Origin": "http://127.0.0.1:8000"}).status_code != 403
    )


def test_reset_serializes_with_login_and_revokes_its_result(auth_app, auth_sessions, monkeypatch):
    from threading import Event

    from backend.app import auth

    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    entered, release = Event(), Event()
    original = auth.verify_password

    def delayed(password, encoded):
        entered.set()
        assert release.wait(10)
        return original(password, encoded)

    monkeypatch.setattr(auth, "verify_password", delayed)
    client = browser(auth_app)
    with ThreadPoolExecutor(2) as pool:
        login = pool.submit(client.post, "/api/v1/auth/login", json={"password": PASSWORD})
        assert entered.wait(10)

        def reset():
            with auth_sessions() as session:
                reset_password(session, "replacement password")

        reset_result = pool.submit(reset)
        release.set()
        assert login.result().status_code == 204
        reset_result.result()
    assert client.get("/api/v1/agents").status_code == 401
    assert client.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"password": "replacement password"}).status_code
        == 204
    )


def test_database_failure_denies_access(auth_app):
    from sqlalchemy.exc import OperationalError

    from backend.app.db import get_db

    def failed_database():
        raise OperationalError("private internal details", {}, RuntimeError("unavailable"))
        yield

    auth_app.dependency_overrides[get_db] = failed_database
    client = browser(auth_app)
    for path in ["/api/v1/agents", "/api/v1/auth/session"]:
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "Service unavailable"}
    assert client.post("/api/v1/auth/logout").status_code == 503


def test_secure_cookie_and_no_secret_response(auth_app, auth_sessions, monkeypatch, caplog):
    from backend.app.config import get_settings

    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    monkeypatch.setattr(get_settings(), "admin_cookie_secure", True)
    client = browser(auth_app)
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 204 and not response.content
    cookie = response.headers["set-cookie"]
    assert all(
        value in cookie for value in ["HttpOnly", "SameSite=strict", "Secure", "Max-Age=28800"]
    )
    assert PASSWORD not in caplog.text
    invalid = client.post("/api/v1/auth/login", json={"password": PASSWORD * 10})
    assert invalid.status_code == 422 and PASSWORD not in invalid.text
