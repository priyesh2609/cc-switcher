"""OAuth token refresh for a *saved* Claude login.

Invariant I3: never call this on the live login. The live login is shared with running
Claude Code sessions, and a rotated refresh token would log them out. Callers (``tokens``)
decide liveness while holding ``fsutil.app_lock``; this module only does the HTTP exchange.

``http_post`` contract (injectable, default = real urllib)::

    http_post(url: str, payload: dict, *, headers: dict[str, str], timeout: float)
        -> (HTTP status, response body) or (HTTP status, response body, headers)

Headers are optional for injected transports; the default transport preserves them so a
429's Retry-After value can be respected. It returns the status for *every* HTTP response,
including 4xx/5xx. It raises
``OSError`` / ``urllib.error.URLError`` / ``TimeoutError`` for transport failures.

Nothing here puts a token into an exception message, a log line or a return value other than
the refreshed login itself.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from cc_switcher import paths

DEFAULT_EXPIRES_IN_SECONDS = 3600  # used only if the server omits expires_in on a 200
_SAFE_ERROR_CODES = frozenset({
    "invalid_grant", "invalid_request", "invalid_client", "invalid_scope", "unauthorized_client",
    "unsupported_grant_type", "access_denied", "server_error", "temporarily_unavailable",
    "rate_limit_error", "rate_limited", "too_many_requests", "authentication_error",
    "invalid_request_error", "api_error", "overloaded_error", "permission_error", "not_found_error",
})


class OAuthError(RuntimeError):
    """Transient failure (network, 5xx, rate limit, malformed reply). Safe to retry later."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None,
                 server_error: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.server_error = server_error


class RefreshRejected(OAuthError):
    """The server refused the refresh token (400/401/invalid_grant): the user must re-add the account."""


# --------------------------------------------------------------------------- expiry


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
    """True when the access token is missing or expires within ``skew_seconds``.

    ``expiresAt`` is epoch milliseconds. Like Claude Code, a login with no ``expiresAt`` at all
    (``None``) is treated as non-expiring; an unreadable value is treated as expired.
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


# --------------------------------------------------------------------------- HTTP


def _default_http_post(url: str, payload: dict, *, headers: dict[str, str], timeout: float):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        # Looked up at call time so tests can block/replace urllib.request.urlopen.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200) or 200, response.read(), getattr(response, "headers", None)
    except urllib.error.HTTPError as exc:
        response_headers = exc.headers
        try:
            body = exc.read()
        except Exception:  # pragma: no cover - defensive
            body = b""
        finally:
            exc.close()
        return exc.code, body, response_headers


def _parse_body(body: Any) -> dict | None:
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _error_code(body: dict | None) -> str:
    """A short, safe error identifier from an OAuth error body (e.g. ``invalid_grant``)."""
    if not body:
        return ""
    value = body.get("error")
    if isinstance(value, dict):  # {"error": {"type": "...", "message": "..."}}
        value = value.get("type") or value.get("code")
    # The server can echo arbitrary input in an error field, including a token.
    # Only known non-secret identifiers are allowed in messages or diagnostics.
    return value if isinstance(value, str) and value in _SAFE_ERROR_CODES else ""


def _scope_string(login: dict) -> str:
    scopes = login.get("scopes")
    if isinstance(scopes, str):
        return scopes.strip()
    if isinstance(scopes, (list, tuple)):
        return " ".join(s for s in scopes if isinstance(s, str) and s)
    return ""


# --------------------------------------------------------------------------- refresh


def refresh_login(
    login: dict,
    *,
    http_post: Callable[..., Any] | None = None,
    timeout: float = 15,
    now_ms: int | None = None,
) -> dict:
    """Exchange the login's refresh token for a new access token; returns an updated *copy*.

    Raises ``RefreshRejected`` when the refresh token is missing or refused (400/401/invalid_grant)
    and ``OAuthError`` for anything transient. The input dict is never mutated.
    """
    refresh_token = login.get("refreshToken") if isinstance(login, dict) else None
    if not refresh_token or not isinstance(refresh_token, str):
        raise RefreshRejected("This login has no refresh token; sign in again.")

    client_id = login.get("clientId")
    if not isinstance(client_id, str) or not client_id:
        client_id = paths.OAUTH_CLIENT_ID
    payload: dict[str, Any] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    scope = _scope_string(login)
    if scope:
        payload["scope"] = scope
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _user_agent(),
    }

    post = http_post or _default_http_post
    try:
        response = post(paths.OAUTH_TOKEN_URL, payload, headers=headers, timeout=timeout)
        status, body = response[:2]
        response_headers = response[2] if len(response) > 2 else None
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise OAuthError(f"Could not reach the token endpoint ({type(exc).__name__}).") from None

    parsed = _parse_body(body)
    code = _error_code(parsed)

    if status == 200:
        return _apply_response(login, parsed, now_ms)

    detail = f" ({code})" if code else ""
    if status in (400, 401) or code == "invalid_grant":
        raise RefreshRejected(f"The refresh token was rejected{detail}; sign in again.", status=status)
    if status == 429:
        from cc_switcher import usage

        retry_after = usage._parse_retry_after(response_headers)
        # Do not log arbitrary server text: it can contain a credential or account data.
        server_error = code if code in {"rate_limit_error", "rate_limited", "too_many_requests"} else None
        raise OAuthError(
            f"Token endpoint rate-limited the refresh{detail}.", status=status,
            retry_after=retry_after, server_error=server_error,
        )
    raise OAuthError(f"Token endpoint returned HTTP {status}{detail}.", status=status)


def _apply_response(login: dict, body: dict | None, now_ms: int | None) -> dict:
    if not body or not isinstance(body.get("access_token"), str) or not body["access_token"]:
        raise OAuthError("Token endpoint reply had no access token.", status=200)

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    updated = dict(login)  # shallow copy is enough: we only replace top-level values
    updated["accessToken"] = body["access_token"]

    expires_in = _to_float(body.get("expires_in"))
    if expires_in is None or expires_in <= 0:
        expires_in = DEFAULT_EXPIRES_IN_SECONDS
    updated["expiresAt"] = now + int(expires_in * 1000)

    new_refresh = body.get("refresh_token")
    rotated = isinstance(new_refresh, str) and bool(new_refresh) and new_refresh != login.get("refreshToken")
    if isinstance(new_refresh, str) and new_refresh:
        updated["refreshToken"] = new_refresh  # absent => keep the old one

    refresh_ttl = _to_float(body.get("refresh_token_expires_in"))
    if refresh_ttl is not None and refresh_ttl > 0:
        updated["refreshTokenExpiresAt"] = now + int(refresh_ttl * 1000)
    elif rotated:
        # A new refresh token without a stated lifetime: the old expiry no longer applies.
        updated["refreshTokenExpiresAt"] = None

    scope = body.get("scope")
    if isinstance(scope, str) and scope.strip():
        updated["scopes"] = scope.split()

    # Identity (accountUuid, organizationUuid), plan and tier fields are carried over untouched.
    return updated


def _user_agent() -> str:
    try:
        from cc_switcher import usage  # lazy: usage never imports oauth

        return usage.user_agent()
    except Exception:  # pragma: no cover - defensive
        return "claude-code/2.1.330"
