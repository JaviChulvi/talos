import json
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app import connections
from backend.app.connections import (
    ConnectionBindingError,
    ConnectionInput,
    CredentialsInput,
    load_bound_secrets,
    write_credential_version,
)


@pytest.fixture
def secret_directory(tmp_path, monkeypatch):
    root = tmp_path / "credentials"
    monkeypatch.setattr(
        connections, "get_settings", lambda: SimpleNamespace(connection_secrets_dir=root)
    )
    return root


def test_private_immutable_credential_files(secret_directory):
    identifier = uuid4()
    path = write_credential_version(identifier, {"token": "test-token", "account": "example"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(secret_directory.stat().st_mode) == 0o750
    assert list(secret_directory.iterdir()) == [path]
    assert load_bound_secrets({"crm": {"version_id": str(identifier), "fields": ["token"]}}) == {
        "crm": {"token": "test-token"}
    }
    with pytest.raises(FileExistsError):
        write_credential_version(identifier, {"token": "different-token"})
    assert json.loads(path.read_text())["token"] == "test-token"
    assert list(secret_directory.iterdir()) == [path]


def test_missing_corrupt_and_symlink_credentials_are_safe(secret_directory):
    identifier = uuid4()
    binding = {"crm": {"version_id": str(identifier), "fields": ["token"]}}
    with pytest.raises(ConnectionBindingError, match="Credentials unavailable for slot crm"):
        load_bound_secrets(binding)
    path = write_credential_version(identifier, {"token": "sensitive-token"})
    path.write_text("sensitive-invalid-json")
    with pytest.raises(ConnectionBindingError) as error:
        load_bound_secrets(binding)
    assert "sensitive" not in str(error.value)
    path.unlink()
    other = secret_directory / "other.json"
    other.write_text('{"token":"sensitive-token"}')
    path.symlink_to(other)
    with pytest.raises(ConnectionBindingError):
        load_bound_secrets(binding)


def test_connection_field_and_credential_validation():
    assert ConnectionInput(name="CRM", fields=["token", "account"]).fields == ["account", "token"]
    for fields in (["token", "token"], ["../token"], [], ["a-b"], ["1token"]):
        with pytest.raises(ValidationError):
            ConnectionInput(name="CRM", fields=fields)
    for values in ({"token": ""}, {"token": "one\ntwo"}, {"token": "a\x00b"}):
        with pytest.raises(ValidationError):
            CredentialsInput(values=values)
    request = CredentialsInput(values={"token": "never-in-repr"})
    assert "never-in-repr" not in str(request)
    assert "never-in-repr" not in request.model_dump_json()


def test_uncertain_database_commit_retains_durable_version(secret_directory):
    from fastapi import HTTPException

    connection = connections.Connection(id=uuid4(), name="CRM", fields=["token"], current_version=0)

    def uncertain_commit():
        raise OSError("sensitive database error")

    session = SimpleNamespace(
        get=lambda *args, **kwargs: connection,
        add=lambda value: None,
        flush=lambda: None,
        commit=uncertain_commit,
        rollback=lambda: None,
    )
    with pytest.raises(HTTPException) as error:
        connections.rotate_credentials(
            connection.id, CredentialsInput(values={"token": "private-token"}), session
        )
    assert error.value.status_code == 503
    assert "sensitive" not in error.value.detail
    # A commit may have succeeded before the connection failed. Never delete its file.
    retained = list(secret_directory.glob("*.json"))
    assert len(retained) == 1
    assert json.loads(retained[0].read_text()) == {"token": "private-token"}
