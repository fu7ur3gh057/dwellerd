from dwellerd.logs.redact import redact_secrets


def test_named_secrets_are_redacted():
    line = 'login failed password="hunter 2" token=abc123 api_key: xyz789'
    clean = redact_secrets(line)

    assert "hunter 2" not in clean
    assert "abc123" not in clean
    assert "xyz789" not in clean
    assert clean.count("<redacted>") == 3


def test_json_and_headers_are_redacted():
    line = 'request {"access_token": "very-secret"} Authorization: Bearer deadbeef'
    clean = redact_secrets(line)

    assert "very-secret" not in clean
    assert "deadbeef" not in clean
    assert "access_token" in clean
    assert "Authorization" in clean


def test_url_password_is_redacted_but_context_remains():
    clean = redact_secrets("db postgres://app:s3cret@db.internal:5432/main failed")

    assert "s3cret" not in clean
    assert "postgres://app:<redacted>@db.internal:5432/main" in clean


def test_standalone_service_tokens_are_redacted():
    values = [
        "7528173406:AAExampleTokenValue1234567890",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
    ]

    for value in values:
        clean = redact_secrets(f"failure credential {value}")
        assert value not in clean
        assert "<redacted>" in clean


def test_normal_error_context_is_preserved():
    line = "ERROR connection refused for user app on db.internal:5432"
    assert redact_secrets(line) == line
