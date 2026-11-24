"""Experimental isolated Claude renewal for explicitly trusted CLI installations.

Disabled by default: enable ``claude_refresh_enabled`` to accept the trusted-code
boundary. This is not an OS sandbox; managed hooks and internal requests remain
Claude-owned. Offline tests inject fake children rather than using real accounts.

Every public function requires the caller to hold ``fsutil.app_lock``. The caller
also owns refresh_schedule reservation/completion and verifies the live grant.
No HTTP client, OAuth POST, authentication flow, logging, or token-bearing argv
is implemented here. ``refresh_locked`` returns only a fully read-back saved login.

Crash recovery is deliberately manual: R1 may become R2 while the parent is dead
and its child is still running. A later process finding the journal must not launch
another child or switch R1 live. No PID/age test can prove the old child stopped.
Conflicting/unreadable copies and uncertain launches keep that global exclusion.
For a definitely reaped child, an opaque failure instead records a per-grant retry
block: a hidden Retry-After of 3600 seconds must not become an automatic retry at
180 seconds. Switching/re-login remain allowed; a different refresh grant releases
only that account's block. The old direct-POST engine did not provide this journal.

A killed child may have rotated R1 but died before persisting R2. The unchanged
copy cannot restore R2; reauthentication can still be necessary. This is not a
perfect rotation guarantee. Safe-mode is not an OS sandbox: managed settings and
managed hooks remain Claude-owned. The CLI itself can put unexpectedly oversized
successors on security's argv; CCS guards its own writes and rejects oversized
seeds but cannot retroactively prevent that external CLI behavior.

Recovery material is intentionally retained, even after successful reconciliation,
until a separate, explicitly reviewed cleanup/recovery workflow is implemented.
Private .credentials.json fallback is the approved exception when Keychain fails.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from cc_switcher import creds, fsutil, keychain, paths
from cc_switcher.models import Account

CHILD_TIMEOUT_SECONDS = 90
REAP_TIMEOUT_SECONDS = 5
MAX_CREDENTIAL_BYTES = 1_048_576


class RecoveryRequired(RuntimeError):
    """Unknown child/storage ownership; global operation must be recovered manually."""


class RefreshFailed(RuntimeError):
    """No proven successful CLI result; never contains child output or credentials."""


class RefreshDisabled(RefreshFailed):
    """The trusted-CLI experiment has not been explicitly enabled."""


class NeedsLogin(RefreshFailed):
    """A refresh token is provably absent. No authentication is initiated."""


class RetryBlocked(RefreshFailed):
    """Unknown previous outcome: automatic retries of this grant are paused."""


_RECOVERY = "An isolated Claude operation needs manual recovery; credentials were retained."
_FAILED = "Claude refresh did not establish success; automatic retry is paused for this saved grant."


def _journal() -> Path:
    return paths.app_dir() / "claude-refresh-operation.json"


def _login_marker() -> Path:
    return paths.app_dir() / "claude-login-operation.json"


def _retry_file() -> Path:
    return paths.app_dir() / "claude-refresh-retry-blocks.json"


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise RecoveryRequired(_RECOVERY) from None


def _private_dir(path: Path) -> None:
    if _exists(path) and path.is_symlink():
        raise RecoveryRequired(_RECOVERY)
    fsutil.ensure_private_dir(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RecoveryRequired(_RECOVERY)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_private_json(path: Path, *, dir_fd: int | None = None) -> Any:
    # Do not follow a child-created symlink, accept a hardlink, or silently default
    # an unreadable file to {}. A malformed R2 must never be mistaken for no R2.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RecoveryRequired(_RECOVERY)
        if info.st_size > MAX_CREDENTIAL_BYTES:
            raise RecoveryRequired(_RECOVERY)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            return json.load(stream)
    finally:
        if fd != -1:
            os.close(fd)


def _durable_json(path: Path, value: Any) -> None:
    _private_dir(path.parent)
    if _exists(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RecoveryRequired(_RECOVERY)
    fsutil.atomic_write_json(path, value, mode=0o600)
    _sync_dir(path.parent)  # fsutil fsyncs contents; persist the rename as well
    if _read_private_json(path) != value:
        raise RecoveryRequired(_RECOVERY)


def _remove_owned(path: Path, nonce: str) -> None:
    current = _read_private_json(path)
    if not isinstance(current, dict) or current.get("nonce") != nonce:
        raise RecoveryRequired(_RECOVERY)
    path.unlink()
    _sync_dir(path.parent)


def guard_locked() -> None:
    """Refuse ANY existing refresh journal, even corrupt/old; do not infer child death."""
    if _exists(_journal()):
        raise RecoveryRequired(_RECOVERY)


def guard_no_login_locked() -> None:
    """Refuse an active or abandoned interactive-login ownership marker."""
    if _exists(_login_marker()):
        raise RecoveryRequired("An interactive Claude login is active or needs manual recovery.")


def begin_login_locked() -> str:
    """Reserve interactive-login ownership before the caller releases the app lock."""
    guard_locked()
    guard_no_login_locked()
    nonce = uuid.uuid4().hex
    try:
        _durable_json(_login_marker(), {"version": 1, "nonce": nonce})
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None
    return nonce


def end_login_locked(nonce: str) -> None:
    """Remove only the caller's marker, AFTER the login child and reconciliation ended."""
    try:
        _remove_owned(_login_marker(), nonce)
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None


def _account_ref(account: Account) -> str:
    return hashlib.sha256(account.id.encode("utf-8")).hexdigest()


def _grant_fingerprint(login: dict[str, Any]) -> str:
    # High-entropy OAuth refresh tokens cannot be recovered from this digest. Do
    # not key the gate on expiry/metadata: changing an email does not change R1.
    token = login.get("refreshToken")
    if not isinstance(token, str) or not token:
        raise NeedsLogin("The saved login has no refresh token; sign in again to refresh it.")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _retry_blocks() -> dict[str, dict[str, str]]:
    if not _exists(_retry_file()):
        return {}
    try:
        data = _read_private_json(_retry_file())
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("blocks"), dict):
            raise ValueError
        blocks = data["blocks"]
        for ref, row in blocks.items():
            if not isinstance(ref, str) or not re.fullmatch("[0-9a-f]{64}", ref):
                raise ValueError
            if not isinstance(row, dict) or set(row) != {"grant_fingerprint", "reason"}:
                raise ValueError
            if not isinstance(row["grant_fingerprint"], str) or not re.fullmatch("[0-9a-f]{64}", row["grant_fingerprint"]):
                raise ValueError
            if row["reason"] != "cli_outcome_unknown":
                raise ValueError
        return blocks
    except Exception:
        raise RecoveryRequired("The automatic-refresh retry gate is unreadable; no child was started.") from None


def guard_retry_locked(account: Account, login: dict[str, Any]) -> None:
    """Call before scheduler reservation. Different grants release only their own block."""
    blocks = _retry_blocks()
    ref = _account_ref(account)
    if ref not in blocks:
        return
    if blocks[ref]["grant_fingerprint"] == _grant_fingerprint(login):
        raise RetryBlocked(_FAILED + " Signing in again may help; the exact CLI failure is unknown.")
    # Do not let an arbitrary/stale input erase the gate. Verify the saved grant.
    try:
        if _saved_login(account) != login:
            raise RecoveryRequired(_RECOVERY)
        del blocks[ref]
        _durable_json(_retry_file(), {"version": 1, "blocks": blocks})
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None


def _block_retry(account: Account, login: dict[str, Any]) -> None:
    blocks = _retry_blocks()
    blocks[_account_ref(account)] = {"grant_fingerprint": _grant_fingerprint(login), "reason": "cli_outcome_unknown"}
    _durable_json(_retry_file(), {"version": 1, "blocks": blocks})


def _validate_login(account: Account, login: Any) -> dict[str, Any]:
    if not isinstance(login, dict) or creds.identity(login) != (account.account_uuid, account.organization_uuid) or creds.login_account_id(login) != account.id:
        raise RefreshFailed("The saved login identity cannot be verified; no child was started.")
    if not isinstance(login.get("refreshToken"), str) or not login["refreshToken"]:
        raise NeedsLogin("The saved login has no refresh token; sign in again to refresh it.")
    if login.get("accessToken") is not None and not isinstance(login["accessToken"], str):
        raise RefreshFailed("The saved access token has an invalid format; no child was started.")
    return login


def _login_from_blob(account: Account, blob: Any) -> dict[str, Any]:
    if not isinstance(blob, dict) or set(blob) != {creds.LOGIN_KEY}:
        raise RecoveryRequired(_RECOVERY)
    return _validate_login(account, blob[creds.LOGIN_KEY])


def _saved_login(account: Account) -> dict[str, Any]:
    return _login_from_blob(account, creds.parse_blob(keychain.read(account.keychain_service)))


def _assert_distinct_live(account: Account, login: dict[str, Any]) -> None:
    blobs = [creds.parse_blob(keychain.read(paths.claude_keychain_service()))]
    secure_override = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    secure_directory = (Path(secure_override) if secure_override else paths.home() / ".claude") if secure_override is not None else paths.claude_config_dir()
    fallback = secure_directory / ".credentials.json"
    if _exists(fallback):
        # Read-only: the live configuration is not ours to chmod or rewrite.
        if fallback.is_symlink():
            raise RefreshFailed("The live credential identity cannot be verified; no child was started.")
        blobs.append(creds.parse_blob(fallback.read_text(encoding="utf-8")))
    for blob in blobs:
        live = creds.get_login(blob)
        if live is None:
            if creds.LOGIN_KEY in blob and blob[creds.LOGIN_KEY] not in (None, {}):
                raise RefreshFailed("The live credential identity cannot be verified; no child was started.")
            continue
        if creds.identity(live) is None:
            raise RefreshFailed("The live credential identity cannot be verified; no child was started.")
        if creds.login_account_id(live) == account.id or any(login.get(field) and login.get(field) == live.get(field) for field in ("refreshToken", "accessToken")):
            raise RefreshFailed("The saved grant may be live; no child was started.")


def _require_trusted_cli_opt_in() -> None:
    """Require explicit consent to the trusted-CLI experiment, not an OS sandbox.

    A managed hook can leave group 100 for group 200 and write R2 after group
    100 disappears. The opt-in accepts this uncontained, trusted-code boundary;
    killpg(100, 0) -> ESRCH alone does not prove that group 200 does not exist.
    """
    from cc_switcher import store

    if store.load_settings().claude_refresh_enabled is not True:
        raise RefreshDisabled(
            "Automatic CLI renewal is disabled; enable claude_refresh_enabled "
            "only on a trusted Claude Code installation. No child was started."
        )


def _find_claude() -> str | None:
    configured, selected = paths.configured_claude_binary()
    if configured:
        return selected
    found = shutil.which("claude")
    if found:
        return str(Path(found).absolute())
    for candidate in (paths.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _isolated_service(directory: Path) -> str:
    value = unicodedata.normalize("NFC", str(directory))
    return "Claude Code-credentials-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _safe_payload(service: str, attr: str, login: dict[str, Any]) -> str:
    payload = creds.dump_blob(creds.saved_item(login))
    keychain._check_attr(service, "service")
    keychain._check_attr(attr, "account")
    command = f'add-generic-password -U -a "{attr}" -s "{service}" -X "{payload.encode("utf-8").hex()}"\n'
    if len(command) > keychain.STDIN_COMMAND_LIMIT:
        raise RefreshFailed("Credential payload exceeds the safe stdin limit; no argv write was attempted.")
    return payload


def _read_isolated(service: str, attr: str) -> dict[str, Any] | None:
    # keychain._scope only knows the *live* environment. Do not mutate os.environ
    # in a multithreaded app; explicitly scope this separate security read instead.
    unscoped = keychain.read(service)
    result = keychain._run(["find-generic-password", "-a", attr, "-s", service, "-w"])
    if result.returncode == 44:
        if unscoped is not None:
            raise RecoveryRequired(_RECOVERY)  # existing foreign-account collision
        return None
    if result.returncode != 0:
        raise RecoveryRequired(_RECOVERY)
    scoped = keychain._decode_hex_output(result.stdout.rstrip("\n"))
    if creds.parse_blob(unscoped) != creds.parse_blob(scoped):
        raise RecoveryRequired(_RECOVERY)
    return creds.parse_blob(scoped)


def _inventory(account: Account, directory: Path, service: str, attr: str) -> list[dict[str, Any]]:
    """Inspect both stores through a validated directory; preserve staged successors."""
    candidates = []
    failed = False
    # If the operation directory is a symlink, never traverse it to read/chmod
    # an outside .credentials.json. Anchor subsequent reads to this directory FD.
    try:
        _private_dir(directory)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RecoveryRequired(_RECOVERY)
        names = os.listdir(directory_fd)
        # R2 may be fully flushed to .credentials.json.tmp.1234abcd while the
        # final file and Keychain still hold R1. Never clear recovery in that case.
        if any(name.startswith(".credentials.json.") or name.startswith("..credentials.json.") for name in names):
            raise RecoveryRequired(_RECOVERY)
        try:
            blob = _read_isolated(service, attr)
            if blob is not None:
                candidates.append(_login_from_blob(account, blob))
        except Exception:
            failed = True
        try:
            if ".credentials.json" in names:
                candidates.append(_login_from_blob(account, _read_private_json(Path(".credentials.json"), dir_fd=directory_fd)))
        except Exception:
            failed = True
        if failed:
            raise RecoveryRequired(_RECOVERY) from None
        return candidates
    finally:
        os.close(directory_fd)


def _observe_uncertain(account: Account, directory: Path, service: str, attr: str) -> None:
    # Observe both stores on launch/error/interrupt exits without promoting a
    # snapshot a surviving child could immediately supersede. The journal stays.
    try:
        _inventory(account, directory, service, attr)
    except Exception:
        pass


def _child_env(directory: Path, attr: str) -> dict[str, str]:
    # Allowlist rather than a provider blacklist: credential FD, API helper, WIF,
    # Node injection, proxies, debug destinations and future overrides stay out.
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR") if name in os.environ}
    env.update({
        "HOME": str(directory), "USER": attr, "LOGNAME": attr,
        "CLAUDE_CONFIG_DIR": str(directory), "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(directory),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
    })
    return env


def _advance(operation: dict[str, Any], stage: str) -> None:
    current = _read_private_json(_journal())
    if not isinstance(current, dict) or current.get("nonce") != operation["nonce"]:
        raise RecoveryRequired(_RECOVERY)
    operation["stage"] = stage
    _durable_json(_journal(), operation)


def _prepare(account: Account, login: dict[str, Any], attr: str) -> tuple[Path, str, dict[str, Any]]:
    app = paths.app_dir().absolute()
    _private_dir(app)
    root = app / "claude-refresh"
    _private_dir(root)
    # The CLI hashes the config path to only 32 bits. UUID directories alone do
    # not prevent a hash collision; verify absence before ever copying a grant.
    for _ in range(8):
        directory = Path(tempfile.mkdtemp(prefix="op-", dir=root))
        _private_dir(directory)
        service = _isolated_service(directory)
        if service != paths.claude_keychain_service() and keychain.read(service) is None:
            break
    else:
        raise RefreshFailed("Could not allocate a distinct isolated credential store; no child was started.")
    operation = {"version": 1, "nonce": uuid.uuid4().hex, "directory": directory.name, "account_ref": _account_ref(account), "stage": "prepared"}
    _durable_json(_journal(), operation)
    _sync_dir(root)
    payload = _safe_payload(service, attr, login)
    try:
        keychain.write(service, attr, payload)
    except keychain.KeychainError:
        # A failing write can still have stored data. Retain both, then inventory
        # before launch; use the approved plaintext fallback, never live Keychain.
        _durable_json(directory / ".credentials.json", creds.saved_item(login))
    candidates = _inventory(account, directory, service, attr)
    if not candidates:
        _durable_json(directory / ".credentials.json", creds.saved_item(login))
        candidates = _inventory(account, directory, service, attr)
    if not candidates or any(candidate != login for candidate in candidates):
        raise RecoveryRequired(_RECOVERY)
    # No copied global settings, CLAUDE.md, MCP config or profile secrets.
    _durable_json(directory / ".claude.json", {"hasCompletedOnboarding": True})
    return directory, service, operation


def _usable(login: dict[str, Any]) -> bool:
    expires = login.get("expiresAt")
    return isinstance(login.get("accessToken"), str) and bool(login["accessToken"]) and not isinstance(expires, bool) and isinstance(expires, (int, float)) and math.isfinite(expires) and expires > time.time() * 1000 + 30_000


def _persist(account: Account, before: dict[str, Any], candidate: dict[str, Any], attr: str) -> None:
    current = _saved_login(account)
    if current != before and current != candidate:
        raise RecoveryRequired(_RECOVERY)
    if current != candidate:
        payload = _safe_payload(account.keychain_service, attr, candidate)
        keychain.write(account.keychain_service, attr, payload)
    if _saved_login(account) != candidate:
        raise RecoveryRequired(_RECOVERY)


def _kill_group(child: subprocess.Popen) -> None:
    # Do this BEFORE wait/reaping: its zombie PID cannot yet be reused as a
    # different process-group ID. A timeout at 90s must stop same-group hook
    # descendants too, not just the immediate CLI that started them at 10s.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _group_quiescent(child: subprocess.Popen) -> bool:
    # After reaping, merely observe. Never signal a potentially reused group.
    # EPERM or any other uncertain answer is not proof that the group is gone.
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        return True
    return False


def refresh_locked(account: Account, login: dict[str, Any]) -> dict[str, Any]:
    """Reconcile and return verified saved login; caller owns lock and spacing slot.

    ``RecoveryRequired`` keeps the global journal/material. ``RefreshFailed``
    after a reaped child leaves a per-account retry gate but releases that global
    journal. ``NeedsLogin`` means a provably absent refresh token; ``RetryBlocked``
    means only that automatic retries are paused, not that authentication expired.
    Never swallow an interrupt: preserve the journal and re-raise it.
    """
    guard_locked()
    guard_no_login_locked()
    try:
        login = copy.deepcopy(_validate_login(account, login))
        if _saved_login(account) != login:
            raise RefreshFailed("The saved login changed; no child was started.")
        guard_retry_locked(account, login)
        _require_trusted_cli_opt_in()
        _assert_distinct_live(account, login)
        binary = _find_claude()
        if binary is None:
            raise RefreshFailed("Claude CLI is unavailable; no child was started.")
        attr = paths.keychain_account_name()
        # Check both the eventual saved write and worst-case isolated-service name.
        _safe_payload(account.keychain_service, attr, login)
        _safe_payload("Claude Code-credentials-00000000", attr, login)
    except RefreshFailed:
        raise
    except Exception:
        raise RefreshFailed("Credential ownership could not be verified; no child was started.") from None

    try:
        directory, service, operation = _prepare(account, login, attr)
        # Claude itself does not honor our lock. Recheck at the last controllable
        # point before launch; this cannot control already-running external CLIs.
        _assert_distinct_live(account, login)
        if _saved_login(account) != login:
            raise RecoveryRequired(_RECOVERY)
        _advance(operation, "launching")
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None

    argv = [binary, "--safe-mode", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--tools", "", "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-session-persistence", "--prompt-suggestions", "false", "--setting-sources", "", "--settings", '{"disableAllHooks":true}', "--model", "haiku", "--effort", "low", "--max-budget-usd", "0.10", "--output-format", "json", "--max-turns", "1", "-p", "Reply with OK only."]
    try:
        child = subprocess.Popen(argv, cwd=directory, env=_child_env(directory, attr), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, umask=0o077)
    except BaseException as exc:
        _observe_uncertain(account, directory, service, attr)
        if not isinstance(exc, Exception):
            raise
        raise RecoveryRequired(_RECOVERY) from None

    failed = False
    try:
        code = child.wait(timeout=CHILD_TIMEOUT_SECONDS)
        failed = code != 0
    except subprocess.TimeoutExpired:
        failed = True
        try:
            _kill_group(child)
            child.wait(timeout=REAP_TIMEOUT_SECONDS)
        except BaseException as exc:
            _observe_uncertain(account, directory, service, attr)
            if not isinstance(exc, Exception):
                raise
            raise RecoveryRequired(_RECOVERY) from None
    except BaseException as exc:
        _observe_uncertain(account, directory, service, attr)
        if not isinstance(exc, Exception):
            raise
        raise RecoveryRequired(_RECOVERY) from None

    try:
        # Prove same-group quiescence BEFORE the credential snapshot: a hook
        # can write R2 immediately before its group disappears. Reading R1 first
        # and observing ESRCH second would incorrectly promote the old snapshot.
        try:
            quiescent = _group_quiescent(child)
        except Exception:
            _observe_uncertain(account, directory, service, attr)
            raise RecoveryRequired(_RECOVERY) from None
        # Always inventory after success, error OR timeout, before reporting any.
        candidates = _inventory(account, directory, service, attr)
        if not quiescent:
            raise RecoveryRequired(_RECOVERY)
        _advance(operation, "reaped")
        changed = []
        for candidate in candidates:
            if candidate != login and candidate not in changed:
                changed.append(candidate)
        if len(changed) > 1:
            raise RecoveryRequired(_RECOVERY)
        candidate = changed[0] if changed else login
        _persist(account, login, candidate, attr)
        failed = failed or not _usable(candidate)
        if failed:
            _block_retry(account, candidate)
        # Read back again immediately before releasing global exclusion.
        if _saved_login(account) != candidate:
            raise RecoveryRequired(_RECOVERY)
        _remove_owned(_journal(), operation["nonce"])
    except Exception:
        raise RecoveryRequired(_RECOVERY) from None
    if failed:
        raise RefreshFailed(_FAILED) from None
    return copy.deepcopy(candidate)
