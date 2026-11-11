"""Credential redaction (AC.4)."""

from qobuz_mcp.config import redact


def test_auth_secrets_redacted():
    text = "request failed for app_id=app911 token=tok-xyz at endpoint"
    cleaned = redact(text, ["app911", "tok-xyz"])
    assert "app911" not in cleaned
    assert "tok-xyz" not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_ignores_missing_secrets():
    assert redact("nothing to hide", [None, ""]) == "nothing to hide"


def test_header_value_partial_redaction():
    text = "GET failed; request had X-User-Auth-Token: tok123456 and x-app-id=app42"
    cleaned = redact(text, ["value-not-present-in-text"])
    assert "tok123456" not in cleaned
    assert "app42" not in cleaned
    assert "[REDACTED]" in cleaned
