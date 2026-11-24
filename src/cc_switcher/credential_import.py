"""Offline import of private credential copies into NEW saved CCS accounts only.

The source file remains untouched. Nothing here activates an account, invokes
Claude, refreshes a token, reads usage, or copies connector credentials.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from cc_switcher import claude_refresh, creds, fsutil, keychain, paths, store
from cc_switcher.models import Account, make_account_id, utcnow

MAX_FILE_BYTES = 1_048_576
_LOGIN_FIELDS = frozenset({
    "accessToken", "refreshToken", "expiresAt", "refreshTokenExpiresAt", "scopes",
    "subscriptionType", "subscriptionTypeSource", "rateLimitTier", "clientId",
    "accountUuid", "organizationUuid",
})


class CredentialImportError(ValueError):
    """Safe user-facing error: never includes source contents or token values."""


def _read_private_file(path: Path) -> bytes:
    """Bound the read and reject symlinks/FIFOs before reading their contents."""
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise CredentialImportError("Use a regular, privately owned credential file, not a link or special file.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise CredentialImportError("Credential file is accessible to others. Run chmod 600 on the file first.")
        if info.st_size > MAX_FILE_BYTES:
            raise CredentialImportError("Credential file is too large (maximum 1 MiB).")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise CredentialImportError("Credential file is too large (maximum 1 MiB).")
        return data
    except CredentialImportError:
        raise
    except OSError:
        raise CredentialImportError("Could not read the private file; check its path, ownership and permissions.") from None
    finally:
        if fd != -1:
            os.close(fd)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_json(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8-sig").strip()
        hex_text = text[2:] if text.startswith("0x") else text
        if hex_text and len(hex_text) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", hex_text):
            text = bytes.fromhex(hex_text).decode("utf-8-sig")
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite number")))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, RecursionError, OverflowError):
        raise CredentialImportError("Credential file must contain one valid JSON object; its contents were not displayed.") from None


def _validate_login(blob: dict[str, Any]) -> dict[str, Any]:
    raw = blob.get(creds.LOGIN_KEY) if creds.LOGIN_KEY in blob else blob
    if not isinstance(raw, dict):
        raise CredentialImportError("File has no Claude account login object.")
    login = {key: value for key, value in raw.items() if key in _LOGIN_FIELDS}
    for name in ("accountUuid", "organizationUuid"):
        value = login.get(name)
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
                raise ValueError
        except (ValueError, AttributeError):
            raise CredentialImportError("The login needs valid accountUuid and organizationUuid values; identities cannot be inferred offline.") from None
        login[name] = value.lower()
    for name in ("accessToken", "refreshToken"):
        value = login.get(name)
        if value is not None and (not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
            raise CredentialImportError("Credential token fields must be strings without whitespace or control characters.")
    if not (login.get("accessToken") or login.get("refreshToken")):
        raise CredentialImportError("The login contains neither an access token nor a refresh token.")
    for name in ("expiresAt", "refreshTokenExpiresAt"):
        value = login.get(name)
        if value is None:
            continue
        try:
            valid = not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= value <= 2**53 and math.isfinite(value)
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise CredentialImportError("Credential expiry fields must be finite nonnegative millisecond timestamps or null.")
    scopes = login.get("scopes")
    if scopes is not None and not (isinstance(scopes, list) and all(isinstance(s, str) and s and not any(c.isspace() or ord(c) < 32 for c in s) for s in scopes)):
        raise CredentialImportError("Credential scopes must be a list of scope-name strings.")
    for name in ("subscriptionType", "subscriptionTypeSource", "rateLimitTier", "clientId"):
        if login.get(name) is not None and not isinstance(login[name], str):
            raise CredentialImportError("Credential metadata fields have an invalid type.")
    return login


def _registered_accounts() -> list[Account]:
    """Do not let tolerant normal registry loading overwrite a damaged registry."""
    file = paths.accounts_file()
    if not file.exists() and not file.is_symlink():
        return []
    try:
        data = _parse_json(_read_private_file(file))
        if data.get("version") != store.SCHEMA_VERSION or not isinstance(data.get("accounts"), list):
            raise ValueError
        accounts = [Account.from_dict(row) for row in data["accounts"]]
        if any(not isinstance(a.id, str) or not isinstance(a.label, str) for a in accounts):
            raise ValueError
        if len({a.id for a in accounts}) != len(accounts):
            raise ValueError
        return accounts
    except (CredentialImportError, ValueError, TypeError, AttributeError):
        raise CredentialImportError("Account registry is unreadable or damaged; import stopped without replacing it.") from None


def import_file(path: str | Path, *, label: str = "") -> Account:
    """Import only into a new saved item, never the active item or an existing account.

    If A already has saved grant R2 and a file contains R1, importing refuses A
    rather than deciding that R1 is newer. Token validity is never tested online.
    """
    login = _validate_login(_parse_json(_read_private_file(Path(path).expanduser())))
    label = label.strip()
    if len(label) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise CredentialImportError("Use a label of at most 64 characters without control characters.")
    ident = (login["accountUuid"], login["organizationUuid"])
    plan = login.get("subscriptionType")
    account = Account(id=make_account_id(*ident), email="", account_uuid=ident[0], organization_uuid=ident[1],
                      label=label, subscription_type=plan if plan in ("pro", "max", "team", "enterprise") else "",
                      added_at=utcnow().isoformat())
    payload = creds.dump_blob(creds.saved_item(login))
    service, attr = account.keychain_service, paths.keychain_account_name()
    # Match the wrapper's stdin bound before invoking it; never take its oversized argv branch.
    command_length = len(f'add-generic-password -U -a "{attr}" -s "{service}" -X "{payload.encode("utf-8").hex()}"\n')
    if command_length > keychain.STDIN_COMMAND_LIMIT:
        raise CredentialImportError("Login exceeds the safe Keychain stdin size; no credentials were written.")

    with fsutil.app_lock():
        try:
            claude_refresh.guard_locked()
            claude_refresh.guard_no_login_locked()
        except claude_refresh.RecoveryRequired:
            raise CredentialImportError("Credential recovery or an active login must finish before file import.") from None
        accounts = _registered_accounts()
        if any(a.id == account.id for a in accounts):
            raise CredentialImportError("This account is already registered; file import never replaces saved logins.")
        if label and any(a.label.casefold() == label.casefold() for a in accounts):
            raise CredentialImportError("That label is already in use; choose another label.")
        try:
            for saved_account in accounts:
                saved = creds.get_login(creds.parse_blob(keychain.read(saved_account.keychain_service)))
                if saved is None or any(saved.get(field) is not None and not isinstance(saved[field], str) for field in ("accessToken", "refreshToken")):
                    raise CredentialImportError("An existing saved login could not be checked for duplicate credentials; import stopped.")
                if any(login.get(field) and login.get(field) == saved.get(field) for field in ("accessToken", "refreshToken")):
                    raise CredentialImportError("These credentials are already saved under another account; the duplicate grant was not imported.")
            if keychain.read(service) is not None or keychain.read_account_attr(service) is not None:
                raise CredentialImportError("A saved Keychain item already exists for this account; it was not overwritten.")
            live_blobs = [creds.parse_blob(keychain.read(paths.claude_keychain_service()))]
            secure_override = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
            secure_dir = (Path(secure_override) if secure_override else paths.home() / ".claude") if secure_override is not None else paths.claude_config_dir()
            fallback = secure_dir / ".credentials.json"
            if os.path.lexists(fallback):
                live_blobs.append(_parse_json(_read_private_file(fallback)))
            for blob in live_blobs:
                live = creds.get_login(blob)
                if live is not None and (creds.login_account_id(live) == account.id or any(
                    login.get(field) and login.get(field) == live.get(field) for field in ("accessToken", "refreshToken")
                )):
                    raise CredentialImportError("This login is already active. Use plain ccs import to save the current credentials instead.")
        except (keychain.KeychainError, creds.CredentialsError):
            raise CredentialImportError("Could not verify existing Keychain state; nothing was overwritten.") from None
        try:
            keychain.write(service, attr, payload)
            if creds.parse_blob(keychain.read(service)) != creds.saved_item(login):
                raise CredentialImportError("Keychain did not keep the complete login. No account was registered; retain the source file for recovery.")
        except (keychain.KeychainError, creds.CredentialsError):
            raise CredentialImportError("Keychain import could not be verified. Retain the source file; a saved item may exist but no account was registered.") from None
        try:
            store.save_accounts([*accounts, account])
        except OSError:
            raise CredentialImportError("Login was saved to Keychain, but account registration failed. Retain the source file and investigate before reimporting.") from None
    return account
