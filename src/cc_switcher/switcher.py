"""Core account switching: import, save, switch, add, remove.

What makes this different from a whole-item swap:

- Only ``claudeAiOauth`` is replaced in the live Keychain item. The live item is
  only ever written as ``creds.merge_login(<fresh live>, login)`` or with MCP state
  put back by ``creds.restore_missing_mcp`` / ``creds.restore_wiped_mcp`` (I1), so
  ``mcpOAuth`` / ``mcpOAuthClientConfig`` stay put across accounts.
- "Active" is derived from the live token's identity, never from a stored flag (I4).
- The live login is saved into its account's item before it is overwritten,
  auto-importing it when it isn't registered yet (I5).
- Items are updated in place (``security add-generic-password -U``), never
  deleted and re-added, so Claude Code keeps its Keychain access.

Locking (I6): every public function that reads-then-writes takes
``fsutil.app_lock()`` exactly once and delegates to ``_..._locked`` helpers,
which never take it. ``claude auth login`` runs with the lock released.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from cc_switcher import claude_state, creds, fsutil, keychain, paths, store, tokens
from cc_switcher.models import Account, make_account_id, utcnow

# Extra places to look for `claude`: a LaunchAgent runs with a minimal PATH.
# ~/.local/bin is checked first (native installer); it is computed from paths.home().
SYSTEM_BIN_DIRS: tuple[Path, ...] = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
AUTH_STATUS_TIMEOUT_SECONDS = 20
# How often a read-modify-write of the live item starts over because Claude Code changed the
# item between our read and our write.
LIVE_WRITE_ATTEMPTS = 3

_READ_FILE: Any = object()  # sentinel: read ~/.claude.json's oauthAccount now

LoginRunner = Callable[[list[str]], int]
StatusRunner = Callable[[list[str]], Any]  # returns a CompletedProcess-like object or (returncode, stdout)


class SwitchError(RuntimeError):
    """A user-facing failure. Messages are one short line and never contain token values."""


class LiveCredentialsUnreadable(SwitchError, creds.CredentialsError):
    """Claude Code's live Keychain item exists but is not a JSON object."""


class UnknownAccount(SwitchError, store.AccountNotFound):
    """No registry row for the given account id."""


class _LabelTaken(SwitchError):
    """Another account already uses the requested label."""


class LoginInterrupted(KeyboardInterrupt):
    """``add_account`` was interrupted after ``claude auth login`` had stored a new login.

    The login was saved anyway (I5); ``account`` is it. Still a KeyboardInterrupt, so callers
    that only know about interrupts keep working.
    """

    def __init__(self, account: Account) -> None:
        super().__init__(f"Login was interrupted, but {_name(account)} was saved.")
        self.account = account


@dataclass
class Status:
    live_account_id: str | None  # identity of the live login, registered or not
    active: Account | None  # registered account matching it
    unregistered_live_login: bool  # live login exists but isn't saved yet
    accounts: list[Account]


# --------------------------------------------------------------------------- read-only queries


def find_claude() -> str | None:
    """Path of `claude`, respecting an explicit binary before PATH and install dirs."""
    configured, selected = paths.configured_claude_binary()
    if configured:
        return selected
    found = shutil.which("claude")
    if found:
        return found
    for directory in (paths.home() / ".local" / "bin", *SYSTEM_BIN_DIRS):
        candidate = directory / "claude"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def live_blob() -> dict[str, Any]:
    """Claude Code's live credentials item, parsed. ``{}`` when there is none.

    Raises ``LiveCredentialsUnreadable`` (a SwitchError and a CredentialsError)
    when the item exists but isn't a JSON object.
    """
    return _parse_live(keychain.read(paths.claude_keychain_service()))


def live_account_id() -> str | None:
    """Account id of the live login (registered or not), or None when logged out."""
    return creds.login_account_id(creds.get_login(live_blob()))


def status() -> Status:
    """Who is live, whether they're registered, and every saved account. Read-only, no lock.

    A damaged live item is reported as "no live login" here (Claude Code can't use it
    either); ``live_blob()`` / ``live_account_id()`` raise instead.
    """
    accounts = store.load_accounts()
    try:
        blob = live_blob()
    except LiveCredentialsUnreadable:
        blob = {}
    login = creds.get_login(blob)
    live_id = creds.login_account_id(login)
    active = next((a for a in accounts if a.id == live_id), None) if live_id else None
    return Status(
        live_account_id=live_id,
        active=active,
        unregistered_live_login=login is not None and active is None,
        accounts=accounts,
    )


def active_account() -> Account | None:
    return status().active


def claude_auth_status(runner: StatusRunner | None = None) -> dict[str, Any] | None:
    """Parsed ``claude auth status --json``, or None when unavailable.

    Claude Code prints the JSON even when it exits 1 (logged out), so stdout is
    parsed regardless of the exit code. Note: its ``email``/``orgId``/``orgName``
    come from ``~/.claude.json``'s ``oauthAccount``, so they can be stale too.
    """
    claude = find_claude()
    if runner is None:
        if claude is None:
            return None
        runner = _capture_runner
    argv = [claude or "claude", "auth", "status", "--json"]
    try:
        result = runner(argv)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    _, stdout = _result_parts(result)
    try:
        data = json.loads(stdout) if stdout and stdout.strip() else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- public operations


def import_current(label: str = "", runner: StatusRunner | None = None) -> Account:
    """Save the live login as an account (or refresh an existing one) and return it.

    ``runner`` runs ``claude auth status --json`` (only used when the email is unknown).
    """
    with fsutil.app_lock():
        live = live_blob()
        login = creds.get_login(live)
        if login is None:
            raise SwitchError("Claude Code isn't logged in. Log in with `claude auth login` first.")
        return _import_login_locked(login, label, auth_status=True, status_runner=runner)


def save_active_login() -> Account | None:
    """Copy the live login into its account's saved item when they differ. Returns the active account."""
    with fsutil.app_lock():
        return _save_active_login_locked()


def switch_to(account_id: str) -> Account:
    """Make ``account_id`` the live Claude login, keeping every MCP sign-in. Returns the account."""
    with fsutil.app_lock():
        return _switch_to_locked(account_id)


def add_account(
    label: str = "",
    runner: LoginRunner | None = None,
    status_runner: StatusRunner | None = None,
) -> Account | None:
    """Log in to another account with ``claude auth login`` and save it.

    Returns the new (or re-authenticated) account, also when ``claude auth login``
    exited non-zero but left a different account logged in. Returns None when no new
    login appeared (cancelled / failed); the previous login and MCP sign-ins are then
    left in place or restored. The live login may still differ from before the call
    (e.g. the menu bar switched accounts meanwhile), so callers should re-check it.
    """
    claude = find_claude()
    if claude is None and runner is None:
        raise SwitchError("Couldn't find the `claude` command. Is Claude Code installed?")
    argv = [claude or "claude", "auth", "login"]
    login_runner = runner or _login_runner

    # Phase 1 (lock): keep the outgoing login (I5) and snapshot the item to repair MCP state later.
    with fsutil.app_lock():
        before = _add_prepare_locked()

    # Phase 2 (NO lock): interactive login; can take minutes.
    try:
        rc = login_runner(argv)
    except BaseException as exc:
        # Still finish: restore MCP state, and save a login that completed before the interrupt.
        with fsutil.app_lock():
            saved = _add_finish_locked(before, rc=None, label=label, status_runner=status_runner)
        if isinstance(exc, OSError):
            raise SwitchError(f"Couldn't run `claude auth login`: {exc.strerror or exc}") from exc
        if isinstance(exc, KeyboardInterrupt) and saved is not None:
            raise LoginInterrupted(saved) from exc
        raise

    # Phase 3 (lock): put back MCP stores the login wiped (I1); import the new login.
    with fsutil.app_lock():
        return _add_finish_locked(before, rc=rc, label=label, status_runner=status_runner)


def remove_account(account_id: str, force: bool = False) -> None:
    """Delete an account's saved login and registry row. Refuses the active account unless ``force``."""
    with fsutil.app_lock():
        account = store.get_account(account_id)
        if account is None:
            # Clean up an orphaned saved item if there is one; otherwise it's a bad id.
            if keychain.delete(paths.saved_login_service(account_id)):
                return
            raise UnknownAccount(f"No saved account with id {account_id}. Run `ccs list`.")
        if not force and _live_account_id_or_none() == account_id:
            raise SwitchError(
                f"{_name(account)} is the active account. Switch to another account first, or use --force."
            )
        # Keychain first: if the registry write then fails, running remove again finishes the job.
        keychain.delete(account.keychain_service)
        store.remove_account(account_id)


def set_label(account_id: str, label: str) -> Account:
    """Set (or clear, with "") an account's nickname. Labels are unique, case-insensitively."""
    with fsutil.app_lock():
        account = store.get_account(account_id)
        if account is None:
            raise UnknownAccount(f"No saved account with id {account_id}. Run `ccs list`.")
        label = label.strip()
        _check_label_free(label, account_id)
        updated = replace(account, label=label)
        store.upsert_account(updated)
        return updated


# --------------------------------------------------------------------------- locked helpers


def _save_active_login_locked() -> Account | None:
    try:
        live = live_blob()
    except LiveCredentialsUnreadable:
        return None
    return _sync_live_locked(live, auto_import=False)


def _sync_live_locked(
    live: dict[str, Any], *, auto_import: bool, oauth_account: Any = _READ_FILE
) -> Account | None:
    """Save the live login into its account's item and refresh that account's metadata.

    Unregistered live logins are imported when ``auto_import`` (I5), else ignored.
    ``oauth_account`` is ``~/.claude.json``'s value when the caller already read it.
    Returns the live login's account, or None.
    """
    login = creds.get_login(live)
    ident = creds.identity(login)
    if login is None or ident is None:
        return None
    account_id = make_account_id(*ident)
    account = store.get_account(account_id)
    if account is None:
        if not auto_import:
            return None
        return _import_login_locked(
            login, "", auth_status=False, status_runner=None, oauth_account=oauth_account
        )
    _write_saved_login(account_id, login)
    updated = _with_login_facts(account, login, _profile_from_claude_json(ident, oauth_account))
    if updated != account:
        store.upsert_account(updated)
    return updated


def _import_login_locked(
    login: dict[str, Any],
    label: str,
    *,
    auth_status: bool,
    status_runner: StatusRunner | None,
    oauth_account: Any = _READ_FILE,
) -> Account:
    """Register ``login`` (create or refresh its account) and write its saved item (I2).

    ``auth_status`` allows one ``claude auth status --json`` call when the email is
    unknown; ``login`` must then be the live login (it is re-read afterwards).
    """
    ident = creds.identity(login)
    if ident is None:
        raise SwitchError(
            "This Claude login has no account id, so it can't be saved. Log in again with `claude auth login`."
        )
    account_id = make_account_id(*ident)
    label = label.strip()
    if label:
        _check_label_free(label, account_id)
    existing = store.get_account(account_id)

    profile = _profile_from_claude_json(ident, oauth_account)
    if auth_status and not profile.get("email") and not (existing and existing.email):
        profile = _profile_from_auth_status(ident, status_runner)
        # `claude auth status` may have refreshed the live tokens: save the current ones.
        fresh = creds.get_login(live_blob())
        if creds.login_account_id(fresh) != account_id:
            raise SwitchError("The Claude login changed while importing. Try again.")
        login = fresh

    now = utcnow().isoformat()
    base = existing or Account(
        id=account_id,
        email="",
        account_uuid=ident[0],
        organization_uuid=ident[1],
        added_at=now,
    )
    account = _with_login_facts(base, login, profile)
    if label:
        account = replace(account, label=label)
    if not account.added_at:
        account = replace(account, added_at=now)

    # Saved item before registry row, so the registry never points at a missing item.
    _write_saved_login(account_id, login)
    store.upsert_account(account)
    return account


def _settle_kept_logins_locked(service: str, acct_attr: str, target: Account) -> None:
    """Deal with refreshed logins ``tokens`` keeps in memory because the Keychain dropped their
    save. Their saved items still hold refresh tokens the server revoked.

    Only the two accounts this switch touches are handled, so the lock is held for a bounded
    number of `security` calls (S4); other accounts' kept logins are saved by the refresh path.

    - The live account, when its live login IS the revoked predecessor (another process switched
      to it with the stale saved copy): the kept login is written live FIRST, guarded by a
      re-read and verified, and only then saved, by the I5 step that follows. Saving it while the
      dead predecessor stayed live would let the next live-to-saved copy (save_active_login, any
      I5 step, a switch in another process) overwrite the only valid copy whenever this switch
      then failed (S1). If it can't be put live it isn't saved either and stays in memory.
      Nothing is refreshed (I3). If the live login is anything else (a newer /login, say), it
      wins: I5 saves it, and the kept copy is never used while the account is live.
    - The target, when it isn't live: its kept login is saved now (R2), so the switch writes that
      refreshed login live instead of the revoked one. No live-to-saved copy can reach it,
      because the target isn't live. If saving fails, the switch stops before changing anything.
    """
    raw = keychain.read(service)
    try:
        live = creds.parse_blob(raw)
    except creds.CredentialsError:
        live = {}  # the switch loop reports a damaged item
    live_login = creds.get_login(live)
    live_id = creds.login_account_id(live_login)
    live_account = store.get_account(live_id) if live_id else None
    if live_account is not None:
        _put_kept_login_live_locked(service, acct_attr, live_account, raw, live, is_target=live_id == target.id)
    if live_id != target.id:
        try:
            tokens._save_kept_login_locked(target)
        except keychain.KeychainError as exc:
            raise SwitchError(
                f"Couldn't switch to {_name(target)}, so nothing was changed: its refreshed login "
                "could not be saved to Keychain (is it locked?). Try again in a moment."
            ) from exc


def _put_kept_login_live_locked(
    service: str, acct_attr: str, account: Account, raw: str | None, live: dict[str, Any], *, is_target: bool
) -> None:
    """Replace a live login that is the revoked predecessor of ``account``'s kept login (see above)."""
    pending = tokens._pending_kept_login_locked(account)
    live_login = creds.get_login(live) or {}
    if pending is None:
        return
    replaced, kept = pending
    if not replaced or live_login.get("refreshToken") != replaced or kept.get("refreshToken") == replaced:
        return  # the live login isn't the revoked predecessor: it wins
    new_text = creds.dump_blob(creds.merge_login(live, kept))  # I1: only claudeAiOauth changes
    if keychain.read(service) != raw:
        return  # changed meanwhile (e.g. a newer /login): that wins; the kept copy stays in memory
    try:
        keychain.write(service, acct_attr, new_text)
        stored = creds.get_login(creds.parse_blob(keychain.read(service)))
    except (keychain.KeychainError, creds.CredentialsError):
        stored = None
    if stored is None or stored.get("refreshToken") != kept.get("refreshToken"):
        if is_target:
            raise SwitchError(
                f"Couldn't switch to {_name(account)}: its refreshed login couldn't be put into "
                "Keychain (is the login keychain locked?). Try again in a moment."
            )
        return  # not saved either, so nothing can overwrite it; it stays in memory
    tokens._forget_kept_login(account.id)  # live now; the I5 step saves it


def _switch_to_locked(account_id: str) -> Account:
    target = store.get_account(account_id)
    if target is None:
        raise UnknownAccount(f"No saved account with id {account_id}. Run `ccs list`.")

    service = paths.claude_keychain_service()

    # Everything that doesn't depend on the live item happens before it is read (these
    # `security` calls and the ~/.claude.json parse take tens of ms each), so the window in
    # which Claude Code can change the item under us stays small.
    acct_attr = keychain.read_account_attr(service) or paths.keychain_account_name()
    # Refreshed logins kept in memory for the live account and the target go into Keychain
    # first, before the target's saved login is read (R2, S1, S4; see the helper).
    _settle_kept_logins_locked(service, acct_attr, target)
    target_login = _read_saved_login(account_id)
    oauth_in_file = claude_state.read_oauth_account()
    target_oauth = target.oauth_account
    if not _oauth_matches(target_oauth, target.account_uuid, target.organization_uuid):
        target_oauth = None  # Claude Code refetches the profile when the key is absent

    raw = keychain.read(service)
    for _attempt in range(LIVE_WRITE_ATTEMPTS):
        live = _parse_live(raw)
        live_login = creds.get_login(live)
        live_id = creds.login_account_id(live_login)

        # I5: keep the outgoing login (rotated tokens) before it is overwritten.
        if live_login is not None:
            if live_id is None:
                raise SwitchError(
                    "The current Claude login has no account id, so it can't be saved before switching. "
                    "Log in again with `claude auth login`, then retry."
                )
            if not (isinstance(oauth_in_file, dict) and oauth_in_file.get("accountUuid") == live_login.get("accountUuid")):
                # The profile read up front belongs to someone else: the live login may have
                # changed since (e.g. /login mid-switch), and the file may already describe it.
                oauth_in_file = claude_state.read_oauth_account()
            try:
                synced = _sync_live_locked(live, auto_import=True, oauth_account=oauth_in_file)
            except (keychain.KeychainError, OSError) as exc:
                raise SwitchError(
                    f"Couldn't save the current login, so nothing was changed: {_reason(exc)}."
                ) from exc
            if live_id == account_id:
                return synced or target  # already live; nothing to write

        if target_login is None:
            raise SwitchError(
                f"The saved login for {_name(target)} is missing or damaged. Re-add it with `ccs add`."
            )

        new_blob = creds.merge_login(live, target_login)  # I1: only claudeAiOauth changes
        new_text = creds.dump_blob(new_blob)
        # Re-read right before the write. If Claude Code changed the item since `raw` (it
        # refreshed the login or an MCP token), saving and merging start over on the fresh
        # content, so neither its new login (I5) nor its new MCP tokens (I1) are written over.
        latest = keychain.read(service)
        if latest == raw:
            break
        raw = latest
    else:
        raise SwitchError("Claude Code changed its login while switching. Try again.")

    # Residual race: a write by Claude Code that lands after `latest` was read and before the
    # write below completes (one `security` call) is overwritten. A write that lands after ours
    # is caught by _verify_live.
    # From the write on, any failure must put the previous login back. The write itself is
    # inside: a `security` timeout can still have applied it.
    try:
        keychain.write(service, acct_attr, new_text)
        _verify_live(service, acct_attr, account_id, new_blob)
        claude_state.write_oauth_account(target_oauth)
    except BaseException as exc:
        try:
            in_place = _restore_live_login(service, acct_attr, live, raw, target_login)
        except Exception as restore_exc:
            raise SwitchError(
                f"Couldn't switch to {_name(target)} ({_reason(exc)}), and putting the previous login "
                "back also failed. Run `claude` and use /login."
            ) from restore_exc
        if isinstance(exc, Exception):
            outcome = (
                "Your previous login is back in place."
                if in_place
                else "Claude Code's login changed meanwhile, so it was left as is."
            )
            raise SwitchError(f"Couldn't switch to {_name(target)}: {_reason(exc)}. {outcome}") from exc
        raise

    target = replace(target, last_used_at=utcnow().isoformat())
    try:
        store.upsert_account(target)
    except OSError:
        pass  # bookkeeping only; the switch itself succeeded
    return target


def _add_prepare_locked() -> dict[str, Any]:
    try:
        before = live_blob()
    except LiveCredentialsUnreadable:
        return {}  # nothing recoverable; logging in again is the fix
    # I5: `claude auth login` replaces the live login, so save (or auto-import) it first.
    # Claude Code does not revoke the old login on a successful `auth login`
    # (it logs out with preserveInProcessTokens), so the saved copy stays usable.
    # A login without an identity can't be saved; adding a login is then the fix.
    _sync_live_locked(before, auto_import=True)
    return before


def _add_finish_locked(
    before: dict[str, Any],
    *,
    rc: int | None,
    label: str,
    status_runner: StatusRunner | None,
) -> Account | None:
    service = paths.claude_keychain_service()
    acct_attr = keychain.read_account_attr(service) or paths.keychain_account_name()
    before_login = creds.get_login(before)

    raw = keychain.read(service)
    for _attempt in range(LIVE_WRITE_ATTEMPTS):
        try:
            after = creds.parse_blob(raw)
        except creds.CredentialsError:
            after = {}  # damaged by the login attempt: rebuild from the snapshot below
            damaged = True
        else:
            damaged = False

        after_login = creds.get_login(after)
        if after_login is not None and after_login != before_login:
            # I1, the login rewrote the item: restore entry by entry. Claude Code's post-login
            # logout wipes the item just before the new login lands, and a running session can
            # write MCP tokens in between, leaving a partial store (e.g. only a refreshed sentry).
            # Entries in `after` win (they're newer); a stub never shadows real tokens. A sign-out
            # made mid-login may come back; a live sign-in is never lost (round-3 R1).
            fixed = creds.restore_missing_mcp(after, before)
        else:
            # Cancelled or unchanged: the login didn't rewrite the item, so only whole wiped
            # stores come back and a sign-out made meanwhile (`claude mcp remove`, /mcp "Clear
            # authentication") sticks.
            fixed = creds.restore_wiped_mcp(after, before)
        if before_login is not None and after_login is None:
            # The login is gone or emptied (cancelled / failed): put the previous one back.
            # A login that still has tokens is never replaced, even if it differs from our
            # snapshot: a running session may have refreshed it (the server then revoked the
            # snapshot's tokens) or another account may have been switched in meanwhile.
            fixed = creds.merge_login(fixed, before_login)

        # A damaged item is only overwritten when the snapshot gives us something to put back.
        if not (fixed if damaged else fixed != after):
            break
        latest = keychain.read(service)  # see _switch_to_locked: never write over a newer item
        if latest == raw:
            keychain.write(service, acct_attr, creds.dump_blob(fixed))
            break
        raw = latest
    else:
        raise SwitchError(
            "Claude Code kept changing its login, so cc-switcher couldn't put your MCP sign-ins back. "
            "Run `ccs add` again."
        )

    after_id = creds.login_account_id(after_login)
    changed = after_login is not None and after_login != before_login
    if changed and after_id is None:
        raise SwitchError(
            "Claude Code is now logged in, but that login has no account id, so cc-switcher couldn't "
            "save it. Log in again with `claude auth login`."
        )
    # A new login: `auth login` succeeded, or, whatever its exit code, a different account is now
    # live that cc-switcher didn't put there itself (it isn't that account's saved login). A new
    # login is never left unsaved (I5). Same account with new tokens and rc != 0 is a running
    # session's refresh, and a registered account's own saved login was switched in meanwhile.
    new_login = changed and (
        rc == 0
        or (after_id != creds.login_account_id(before_login) and _read_saved_login(after_id) != after_login)
    )
    if not new_login:
        if after_login is not None:
            _sync_live_locked(fixed, auto_import=False)  # keep a refreshed login's saved copy current
        return None

    try:
        try:
            return _import_login_locked(after_login, label, auth_status=True, status_runner=status_runner)
        except _LabelTaken as exc:
            account = _import_login_locked(after_login, "", auth_status=True, status_runner=status_runner)
            raise SwitchError(f"Added {_name(account)} without a label. {exc}") from exc
    except (keychain.KeychainError, OSError) as exc:
        raise SwitchError(
            f"Claude Code is now logged in to the new account, but cc-switcher couldn't save it: "
            f"{_reason(exc)}. Run `ccs import`."
        ) from exc


# --------------------------------------------------------------------------- small helpers


def _parse_live(raw: str | None) -> dict[str, Any]:
    try:
        return creds.parse_blob(raw)
    except creds.CredentialsError as exc:
        raise LiveCredentialsUnreadable(
            "Claude Code's login in Keychain is damaged. Run `claude` and use /login, then try again."
        ) from exc


def _live_account_id_or_none() -> str | None:
    try:
        return live_account_id()
    except LiveCredentialsUnreadable:
        return None


def _read_saved_login(account_id: str) -> dict[str, Any] | None:
    """The account's saved login, or None when missing, damaged, or belonging to someone else."""
    try:
        blob = creds.parse_blob(keychain.read(paths.saved_login_service(account_id)))
    except creds.CredentialsError:
        return None
    login = creds.get_login(blob)
    if creds.login_account_id(login) != account_id:
        return None
    return login


def _write_saved_login(account_id: str, login: dict[str, Any]) -> bool:
    """Store ``{"claudeAiOauth": login}`` (I2) unless that is already stored. True when written."""
    service = paths.saved_login_service(account_id)
    wanted = creds.saved_item(login)
    try:
        current = creds.parse_blob(keychain.read(service))
    except creds.CredentialsError:
        current = None
    if current == wanted:
        return False
    acct_attr = keychain.read_account_attr(service) or paths.keychain_account_name()
    keychain.write(service, acct_attr, creds.dump_blob(wanted))
    # `security -i` can exit 0 without storing anything: read it back before relying on it.
    stored = _read_saved_login(account_id)
    if stored is None or stored.get("refreshToken") != login.get("refreshToken"):
        raise keychain.KeychainError("Keychain didn't keep the saved login (is the login keychain locked?)")
    return True


def _reread_live(service: str) -> dict[str, Any]:
    try:
        return creds.parse_blob(keychain.read(service))
    except creds.CredentialsError as exc:
        raise SwitchError("Keychain returned a damaged item after the write") from exc


def _verify_live(service: str, acct_attr: str, account_id: str, written: dict[str, Any]) -> None:
    """Re-read the live item: it must hold the target login and no wiped MCP store.

    If something wrote after us, its MCP state is kept exactly as written; only a store it
    wiped out entirely (missing, null or empty) is put back with ``restore_wiped_mcp`` onto
    the fresh read (I1). A single entry that is gone or cleared is left alone: it can't be
    told apart from the user signing out of that server right after our write.
    """
    check = _reread_live(service)
    if creds.login_account_id(creds.get_login(check)) != account_id:
        raise SwitchError("Keychain didn't keep the new login")
    repaired = creds.restore_wiped_mcp(check, written)
    if repaired == check:
        return
    keychain.write(service, acct_attr, creds.dump_blob(repaired))
    check = _reread_live(service)
    if (
        creds.login_account_id(creds.get_login(check)) != account_id
        or creds.restore_wiped_mcp(check, written) != check
    ):
        raise SwitchError("MCP sign-ins changed during the switch")


def _restore_live_login(
    service: str,
    acct_attr: str,
    before: dict[str, Any],
    raw_before: str | None,
    written_login: dict[str, Any] | None,
) -> bool:
    """Undo a failed switch without dropping MCP state written since (I1).

    Only our own write is taken back: if the live login is now neither the one we wrote nor
    empty, someone else (the user's /login, a session) put it there and it is left alone.
    MCP state is kept as it is now, except whole stores that were wiped (``restore_wiped_mcp``),
    so a sign-out made during the switch sticks. Returns True when the previous login is what's
    live afterwards.
    """
    try:
        current: dict[str, Any] | None = creds.parse_blob(keychain.read(service))
    except Exception:
        current = None
    if current is None:
        # Can't read what's there now: put back exactly what was there before.
        keychain.write(service, acct_attr, raw_before if raw_before is not None else creds.dump_blob(before))
        return True
    restored = creds.restore_wiped_mcp(current, before)
    if creds.get_login(current) is None or current.get(creds.LOGIN_KEY) == written_login:
        if creds.LOGIN_KEY in before:
            restored[creds.LOGIN_KEY] = copy.deepcopy(before[creds.LOGIN_KEY])
        else:
            restored.pop(creds.LOGIN_KEY, None)
    if restored != current:
        keychain.write(service, acct_attr, creds.dump_blob(restored))
    return restored.get(creds.LOGIN_KEY) == before.get(creds.LOGIN_KEY)


def _oauth_matches(oauth_account: Any, account_uuid: str, organization_uuid: str) -> bool:
    return (
        isinstance(oauth_account, dict)
        and str(oauth_account.get("accountUuid") or "") == account_uuid
        and str(oauth_account.get("organizationUuid") or "") == organization_uuid
    )


def _profile_from_claude_json(ident: tuple[str, str], oauth_account: Any = _READ_FILE) -> dict[str, Any]:
    """Account fields we can trust from ``~/.claude.json``'s ``oauthAccount`` for this login.

    The file can belong to another account (stale after a switch or a /login), so:
    email and display name only when ``accountUuid`` matches (they are per person);
    org name and the full snapshot only when the organization matches too (the same
    person can hold a personal plan and a Team seat). ``oauth_account`` is the file's
    value when the caller already read it.
    """
    if oauth_account is _READ_FILE:
        oauth_account = claude_state.read_oauth_account()
    if not isinstance(oauth_account, dict) or str(oauth_account.get("accountUuid") or "") != ident[0]:
        return {}
    profile: dict[str, Any] = {
        "email": _text(oauth_account.get("emailAddress")),
        "display_name": _text(oauth_account.get("displayName")),
    }
    if _oauth_matches(oauth_account, *ident):
        profile["organization_name"] = _text(oauth_account.get("organizationName"))
        profile["oauth_account"] = copy.deepcopy(oauth_account)
    return {k: v for k, v in profile.items() if v}


def _profile_from_auth_status(ident: tuple[str, str], runner: StatusRunner | None) -> dict[str, Any]:
    info = claude_auth_status(runner)
    # It may have refreshed ~/.claude.json; that is the better source when it now matches.
    profile = _profile_from_claude_json(ident)
    if profile.get("email") or not info:
        return profile
    oauth_account = claude_state.read_oauth_account()
    if oauth_account is not None and str(oauth_account.get("accountUuid") or "") != ident[0]:
        return profile  # its email came from that other account's oauthAccount
    if str(info.get("orgId") or "") != ident[1]:
        return profile
    email = _text(info.get("email"))
    if email:
        profile["email"] = email
    org_name = _text(info.get("orgName"))
    if org_name and "organization_name" not in profile:
        profile["organization_name"] = org_name
    return profile


def _with_login_facts(account: Account, login: dict[str, Any], profile: dict[str, Any]) -> Account:
    """Account updated with the login's plan facts and any trusted profile fields."""
    updates: dict[str, Any] = {}
    for field_name, key in (("subscription_type", "subscriptionType"), ("rate_limit_tier", "rateLimitTier")):
        value = _text(login.get(key))
        if value:
            updates[field_name] = value
    updates.update(profile)
    return replace(account, **updates)


def _check_label_free(label: str, account_id: str) -> None:
    if not label:
        return
    for other in store.load_accounts():
        if other.id != account_id and other.label.lower() == label.lower():
            raise _LabelTaken(f"The label '{label}' is already used by {other.email or other.id}.")


def _name(account: Account) -> str:
    return account.name or f"account {account.account_uuid[:8]}"


def _reason(exc: BaseException) -> str:
    text = str(exc).strip().rstrip(".")
    return text or type(exc).__name__


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _result_parts(result: Any) -> tuple[int | None, str]:
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1] or ""
    return getattr(result, "returncode", None), getattr(result, "stdout", "") or ""


def _capture_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=AUTH_STATUS_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def _login_runner(argv: list[str]) -> int:
    return subprocess.run(argv).returncode
