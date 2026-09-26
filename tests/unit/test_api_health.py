from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_unready_api_does_not_hide_errors(monkeypatch):
    monkeypatch.setattr("backend.app.main.database_ready", lambda: False)
    client = TestClient(create_app())
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    assert client.get("/api/missing").status_code == 404
    assert (
        client.post("/api/missing", headers={"origin": "https://attacker.example"}).status_code
        == 403
    )


def test_ready_api_reports_configured_services(monkeypatch):
    monkeypatch.setattr("backend.app.main.database_ready", lambda: True)
    client = TestClient(create_app())
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/status").json()["worker"] == "configured"
