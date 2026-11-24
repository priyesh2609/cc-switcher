"""Pure functions over Claude Code's credentials blob. No I/O.

The live Keychain item ("Claude Code-credentials") is one JSON object holding
several independent things side by side, e.g.::

    {
      "claudeAiOauth":        {...},   # the Claude login  <- the ONLY part cc-switcher swaps
      "mcpOAuth":             {...},   # remote-MCP tokens, keyed "name|sha256(type,url,headers)[:16]"
      "mcpOAuthClientConfig": {...}    # MCP client secrets
    }

Swapping the whole blob (what the reference switcher did) also swaps every MCP
token, which is why MCP servers kept asking to sign in after a switch.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from cc_switcher.models import make_account_id

LOGIN_KEY = "claudeAiOauth"


class CredentialsError(ValueError):
    pass


def parse_blob(text: str | None) -> dict[str, Any]:
    """Parse a Keychain secret into a dict. Empty/None -> {}. Non-object JSON -> CredentialsError."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialsError("Credentials item is not valid JSON") from exc
    if not isinstance(data, dict):
        raise CredentialsError("Credentials item is not a JSON object")
    return data


def dump_blob(blob: dict[str, Any]) -> str:
    """Serialize compactly, ASCII only (Claude Code reads any valid JSON).

    ASCII because an item whose bytes aren't printable text makes ``security -w`` print it
    as hex. ``\\uXXXX`` escapes are lossless, lone surrogates included.
    """
    return json.dumps(blob, separators=(",", ":"), ensure_ascii=True)


def get_login(blob: dict[str, Any]) -> dict[str, Any] | None:
    """The ``claudeAiOauth`` object, or None if absent / logged out (empty tokens)."""
    login = blob.get(LOGIN_KEY)
    if not isinstance(login, dict):
        return None
    if not (login.get("accessToken") or login.get("refreshToken")):
        return None
    return login


def identity(login: dict[str, Any] | None) -> tuple[str, str] | None:
    """(accountUuid, organizationUuid) of a login, or None when either is missing."""
    if not login:
        return None
    account_uuid = login.get("accountUuid")
    org_uuid = login.get("organizationUuid")
    if not account_uuid or not org_uuid:
        return None
    return str(account_uuid), str(org_uuid)


def login_account_id(login: dict[str, Any] | None) -> str | None:
    ident = identity(login)
    return make_account_id(*ident) if ident else None


def saved_item(login: dict[str, Any]) -> dict[str, Any]:
    """What cc-switcher stores per account: the login only, never MCP tokens."""
    return {LOGIN_KEY: copy.deepcopy(login)}


def merge_login(live_blob: dict[str, Any], login: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``live_blob`` with only ``claudeAiOauth`` replaced.

    Everything else in the live item (``mcpOAuth``, ``mcpOAuthClientConfig`` and any
    key a future Claude Code adds) is carried over untouched.
    """
    merged = copy.deepcopy(live_blob)
    merged[LOGIN_KEY] = copy.deepcopy(login)
    return merged


def non_login_parts(blob: dict[str, Any]) -> dict[str, Any]:
    """Everything except the login (used to verify a switch preserved MCP state)."""
    return {k: v for k, v in blob.items() if k != LOGIN_KEY}


# The fields that make an entry worth keeping, per MCP store. An entry without any of them
# carries nothing: Claude Code writes ``{"accessToken": ""}`` stubs for servers that need
# sign-in, and only ever reads ``clientSecret`` from ``mcpOAuthClientConfig`` entries.
MCP_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "mcpOAuth": ("accessToken", "refreshToken"),
    "mcpOAuthClientConfig": ("clientSecret",),
}


def restore_wiped_mcp(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Put back what a rewrite of the whole item wiped out, e.g. ``claude auth login``.

    Whole stores only. An MCP store (``mcpOAuth``, ``mcpOAuthClientConfig``) comes back from
    ``before`` when it is missing, null, not an object or empty in ``after`` while ``before``
    has entries. A store with any entries in ``after`` is left exactly as it is: an entry that
    is gone or cleared there may be the user's own sign-out (``claude mcp remove``, /mcp
    "Clear authentication"), and restoring it entry by entry would undo that. Other non-login
    keys missing from ``after`` come back as they were. The login is never touched.

    Use this where ``before`` is a snapshot taken before something we don't control ran.
    ``restore_missing_mcp`` (entry by entry) is for undoing a removal we ordered ourselves.
    """
    merged = copy.deepcopy(after)
    for key, old_value in before.items():
        if key == LOGIN_KEY:
            continue
        if key in MCP_SECRET_FIELDS:
            new_value = merged.get(key)
            if isinstance(old_value, dict) and old_value and not (isinstance(new_value, dict) and new_value):
                merged[key] = copy.deepcopy(old_value)
        elif key not in merged:
            merged[key] = copy.deepcopy(old_value)
    return merged


def _carries_secret(entry: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(entry, dict) and any(entry.get(f) for f in fields)


def restore_missing_mcp(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Union MCP state back after an external command (e.g. ``claude auth login``) rewrote the item.

    For each non-login key that is a dict (``mcpOAuth`` etc.), entries present in
    ``before`` but missing in ``after`` are added back. Entries present in ``after``
    win, because they may hold freshly rotated tokens. Non-dict keys missing from
    ``after`` are restored as-is.

    For the MCP stores (``MCP_SECRET_FIELDS``) two more rules apply: a store that is
    null or not an object in ``after`` counts as missing, and an ``after`` entry that
    carries no secret (a sign-in stub) does not shadow a ``before`` entry that does.
    Unknown keys get no such rules.
    """
    merged = copy.deepcopy(after)
    for key, old_value in before.items():
        if key == LOGIN_KEY:
            continue
        new_value = merged.get(key)
        fields = MCP_SECRET_FIELDS.get(key)
        if fields is not None and isinstance(old_value, dict) and not isinstance(new_value, dict):
            merged[key] = copy.deepcopy(old_value)
        elif isinstance(old_value, dict) and isinstance(new_value, dict):
            for entry_key, entry in old_value.items():
                if entry_key not in new_value or (
                    fields is not None
                    and _carries_secret(entry, fields)
                    and not _carries_secret(new_value[entry_key], fields)
                ):
                    new_value[entry_key] = copy.deepcopy(entry)
        elif key not in merged:
            merged[key] = copy.deepcopy(old_value)
    return merged
