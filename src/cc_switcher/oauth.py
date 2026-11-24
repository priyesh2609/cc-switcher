"""Read-only helpers for interpreting Claude Code access-token expiry.

CCS does not perform OAuth token exchanges. Claude Code manages token renewal.
"""

from __future__ import annotations

import time
from typing import Any


class OAuthError(RuntimeError):
    """Legacy error shape retained for callers reporting old cached failures."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None,
                 server_error: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.server_error = server_error


class RefreshRejected(OAuthError):
    """Legacy refresh-rejection error shape."""


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def is_expired(login: dict, skew_seconds: int = 300, now_ms: int | None = None) -> bool:
    """Return whether the access token is missing or expires within the safety window.

    ``expiresAt`` is epoch milliseconds. A missing expiry is treated as non-expiring,
    matching Claude Code's convention; an unreadable expiry is treated as expired.
    """
    if not isinstance(login, dict) or not login.get("accessToken"):
        return True
    raw = login.get("expiresAt")
    if raw is None:
        return False
    expires_at = _to_float(raw)
    if expires_at is None:
        return True
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return expires_at - skew_seconds * 1000 <= now
