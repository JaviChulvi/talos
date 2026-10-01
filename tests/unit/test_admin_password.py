import pytest

from backend.app.auth import hash_password, validate_password, verify_password


@pytest.mark.parametrize("password", ["x" * 14, "x" * 129])
def test_password_limits(password):
    with pytest.raises(ValueError, match="15–128"):
        validate_password(password)


@pytest.mark.parametrize("password", [" " * 15, "ñ🔑 " * 42 + "ñ🔑"])
def test_passwords_are_not_trimmed_or_normalized(password):
    first, second = hash_password(password), hash_password(password)
    assert first != second
    assert verify_password(password, first)
    assert not verify_password(password + "!", first)


def test_commands_use_hidden_confirmation_and_safe_output(monkeypatch, capsys):
    from backend.app import auth

    monkeypatch.setattr("sys.argv", ["auth", "bootstrap"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts = []
    answers = iter(["valid secret password", "different secret password"])
    monkeypatch.setattr(
        auth.getpass, "getpass", lambda prompt: prompts.append(prompt) or next(answers)
    )
    assert auth.main() == 1
    assert len(prompts) == 2
    output = capsys.readouterr()
    assert "do not match" in output.err
    assert "secret" not in output.err + output.out
