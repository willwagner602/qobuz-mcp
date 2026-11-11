"""Local configuration for qobuz-mcp.

Credential route (operator-authorized): the MANUAL web-session route. The
operator personally copies values from their own logged-in play.qobuz.com
session and supplies them via the local process environment:

- QOBUZ_APP_ID           the web player's app_id (required)
- QOBUZ_USER_AUTH_TOKEN  the session token (required)
- QOBUZ_USER_ID          your numeric user id (recommended; needed for the
                         ownership checks that gate remove/delete)
- QOBUZ_ENABLE_WRITES    opt-in ("1"/"true"/"yes") that exposes playlist
                         write tools after the operator has run the
                         read-only smoke test; the flag is self-attestation,
                         not a programmatic check

No password is ever requested, stored, or transmitted by this module, and
browser credential extraction is never automated — the copy is the
operator's manual action. This is NOT Qobuz-issued authorization; see the
README for the full risk disclosure.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

REQUIRED_ENV_VARS = ("QOBUZ_APP_ID", "QOBUZ_USER_AUTH_TOKEN")
_WRITE_ENABLE_VALUES = frozenset({"1", "true", "yes"})


@dataclass(frozen=True)
class ConfigStatus:
    app_id_present: bool
    user_token_present: bool
    user_id_present: bool
    enable_writes_requested: bool

    @property
    def credentials_present(self) -> bool:
        return self.app_id_present and self.user_token_present


def config_status(env: dict[str, str] | None = None) -> ConfigStatus:
    env = os.environ if env is None else env
    return ConfigStatus(
        app_id_present=bool(env.get("QOBUZ_APP_ID")),
        user_token_present=bool(env.get("QOBUZ_USER_AUTH_TOKEN")),
        user_id_present=bool(env.get("QOBUZ_USER_ID")),
        enable_writes_requested=_writes_enabled(env),
    )


def _writes_enabled(env: dict[str, str]) -> bool:
    return env.get("QOBUZ_ENABLE_WRITES", "").strip().lower() in _WRITE_ENABLE_VALUES


_HEADER_VALUE_RE = re.compile(
    r"(?i)\b(x-app-id|x-user-auth-token|authorization|cookie)\b([:=]\s*)\S+"
)


def redact(text: str, secrets: list[str | None]) -> str:
    """Replace exact secret strings, then scrub credential-bearing header values.

    The header pass catches partial-token leaks (e.g. a truncated token in an
    upstream error message) that exact-string replacement would miss.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _HEADER_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def load_client(env: dict[str, str] | None = None):
    """Build a session-authenticated QobuzClient from the environment.

    Returns None when the required session variables are absent; never
    performs network I/O. Writes stay gated unless QOBUZ_ENABLE_WRITES is
    set to a truthy value (the operator's explicit opt-in).
    """
    from .qobuz_api import QobuzClient

    env = os.environ if env is None else env
    app_id = env.get("QOBUZ_APP_ID")
    token = env.get("QOBUZ_USER_AUTH_TOKEN")
    if not app_id or not token:
        return None

    client = QobuzClient(app_id=app_id)
    user_id: int | None = None
    raw_user_id = env.get("QOBUZ_USER_ID")
    if raw_user_id:
        try:
            user_id = int(raw_user_id)
        except ValueError:
            raise ValueError(
                "QOBUZ_USER_ID must be an integer; refusing to start with "
                "an unidentifiable user (ownership checks would fail closed)"
            )
    client.set_session(auth_token=token, user_id=user_id)
    client.validation_passed = _writes_enabled(env)
    return client
