"""Config validation, README/code agreement, and fail-closed behavior (AC.5)."""

import pytest

from pathlib import Path

from qobuz_mcp.config import REQUIRED_ENV_VARS, config_status, load_client
from qobuz_mcp.qobuz_api import QobuzClient

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_documented_config_validation():
    # Every required session-route variable must be documented in the README.
    for var in REQUIRED_ENV_VARS:
        assert var in README, f"{var} missing from README"
    # The write-enablement opt-in must be documented.
    assert "QOBUZ_ENABLE_WRITES" in README
    # The manual-copy policy and its limits must be documented.
    assert "manually" in README and "your own" in README
    assert "Do not automate" in README
    assert "not Qobuz-issued" in README
    # The official alternative route stays documented for context.
    assert "api@qobuz.com" in README
    # Validation itself works offline from a plain dict.
    assert config_status({}).credentials_present is False
    assert config_status({"QOBUZ_APP_ID": "a"}).credentials_present is False
    assert (
        config_status(
            {"QOBUZ_APP_ID": "a", "QOBUZ_USER_AUTH_TOKEN": "t"}
        ).credentials_present
        is True
    )


def test_config_missing_fails_closed():
    assert load_client({}) is None
    assert load_client({"QOBUZ_APP_ID": "only-id"}) is None
    assert load_client({"QOBUZ_USER_AUTH_TOKEN": "only-token"}) is None


def test_config_builds_client_without_network():
    client = load_client({"QOBUZ_APP_ID": "a", "QOBUZ_USER_AUTH_TOKEN": "t"})
    assert isinstance(client, QobuzClient)
    assert client.auth_token == "t"
    assert client.user_id is None
    assert client.validation_passed is False  # writes stay gated by default


def test_write_enablement_flag():
    env = {
        "QOBUZ_APP_ID": "a",
        "QOBUZ_USER_AUTH_TOKEN": "t",
        "QOBUZ_USER_ID": "7",
    }
    enabled = load_client({**env, "QOBUZ_ENABLE_WRITES": "1"})
    assert enabled.validation_passed is True
    assert enabled.user_id == 7
    for flag in ("0", "", "false", "no"):
        assert load_client({**env, "QOBUZ_ENABLE_WRITES": flag}).validation_passed is False


def test_malformed_user_id_rejected():
    with pytest.raises(ValueError):
        load_client(
            {
                "QOBUZ_APP_ID": "a",
                "QOBUZ_USER_AUTH_TOKEN": "t",
                "QOBUZ_USER_ID": "not-a-number",
            }
        )
