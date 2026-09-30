"""Session and cooldown persistence across actual API process restarts."""

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from backend.app.auth import bootstrap
from tests.integration.test_admin_auth import PASSWORD, auth_sessions, database_engine  # noqa: F401

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def api_process(engine):
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.app.main:app",
                "--fd",
                str(listener.fileno()),
                "--no-proxy-headers",
                "--no-access-log",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "TALOS_DATABASE_URL": url.render_as_string(hide_password=False),
                "TALOS_ADMIN_COOKIE_SECURE": "false",
                "TALOS_ALLOWED_HOSTS": '["127.0.0.1"]',
            },
            pass_fds=(listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers={"X-Talos-Request": "1"}, timeout=1
            ) as client:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise AssertionError("API process exited before readiness")
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "API did not become ready"
                    time.sleep(0.05)
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_session_and_cooldown_survive_real_api_restart(auth_sessions):  # noqa: F811
    with auth_sessions() as session:
        bootstrap(session, PASSWORD)
    with api_process(auth_sessions.kw["bind"]) as first:
        assert first.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 204
        cookies = dict(first.cookies)
        assert first.get("/api/v1/agents").status_code == 200
        for attempt in range(5):
            response = first.post("/api/v1/auth/login", json={"password": "incorrect"})
            assert response.status_code == (429 if attempt == 4 else 401)
    with api_process(auth_sessions.kw["bind"]) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get("/api/v1/agents").status_code == 200
        assert restarted.get("/api/v1/auth/session").json()["authenticated"]
        assert restarted.post("/api/v1/auth/login", json={"password": PASSWORD}).status_code == 429
        assert restarted.post("/api/v1/auth/logout").status_code == 204
        restarted.cookies.update(cookies)
        assert restarted.get("/api/v1/agents").status_code == 401
