"""JWT authentication helpers.

Issues and verifies short-lived access tokens for the API. The signing key comes
from the environment; there is deliberately no in-code fallback.
"""

from __future__ import annotations

import os
import time
from typing import Any

import jwt

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 900
LEEWAY_SECONDS = 30


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired or not trusted."""


def _signing_key() -> str:
    key = os.environ.get("JWT_SIGNING_KEY")
    if not key:
        raise AuthError("JWT_SIGNING_KEY is not configured")
    return key


def issue_token(user_id: str, scopes: list[str], ttl: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint an access token for ``user_id``."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "scopes": sorted(scopes),
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _signing_key(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Verify and decode a token, or raise :class:`AuthError`."""
    try:
        return jwt.decode(
            token,
            _signing_key(),
            algorithms=[ALGORITHM],
            leeway=LEEWAY_SECONDS,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("token is not valid") from exc


def is_expired(payload: dict[str, Any], now: int | None = None) -> bool:
    """True when the token's expiry has passed. A token with no ``exp`` is rejected."""
    exp = payload.get("exp")
    if exp is None:
        return True
    return int(exp) <= (now if now is not None else int(time.time()))


def has_scope(payload: dict[str, Any], required: str) -> bool:
    """Authorisation check. Absent scopes deny, never allow."""
    scopes = payload.get("scopes")
    if not isinstance(scopes, list):
        return False
    return required in scopes


def authorize(token: str, required_scope: str) -> dict[str, Any]:
    """Decode, check expiry, then check scope. Every step must pass."""
    payload = decode_token(token)
    if is_expired(payload):
        raise AuthError("token has expired")
    if not has_scope(payload, required_scope):
        raise AuthError(f"token lacks required scope {required_scope!r}")
    return payload
