"""Pick a usable access token for a saved account, without ever endangering the live login.

Invariant I3: the live login ("Claude Code-credentials") is shared with running Claude Code
sessions. Refreshing it would rotate the refresh token underneath them and log them out, so:

* the live account's access token is only ever *read*; if it has expired we raise ``TokenStale``
  and wait for Claude Code to refresh it;
* any other account's saved login is refreshed here, and the new tokens are written back to its
  saved Keychain item.

"Is this account live?" is decided by reading the live Keychain item *while holding*
``fsutil.app_lock``; the refresh and the write-back happen under that same lock, so a concurrent
``switch_to`` cannot make the account live in between. (Claude Code itself does not take our
lock, which is why a refresh-token match against the live login also counts as "live".)
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from cc_switcher import creds, fsutil, keychain, oauth, paths, refresh_schedule, store
from cc_switcher.models import Account

logger = logging.getLogger(__name__)

# Seconds before expiry at which a token is considered unusable / due for refresh.
LIVE_SKEW_SECONDS = 30  # only avoids using a token that dies mid-request
SAVED_SKEW_SECONDS = 300


class NoCredentials(RuntimeError):
    """The account has no saved login (missing, unreadable or for a different identity)."""


class TokenStale(RuntimeError):
    """The live login's access token has expired; Claude Code will refresh it (I3)."""


class LiveLoginUnreadable(keychain.KeychainError):
    """The live Keychain item exists but can't be parsed, so nobody can be ruled out as live.

    A ``KeychainError`` so ``monitor`` reports it as ``keychain_error`` like any other
    unreadable-Keychain failure.
    """


_UNKNOWN = object()  # "the live item exists but we can't tell whose login it is"


def _live_login() -> Any:
    """The live ``claudeAiOauth`` object, None when nobody is logged in, or ``_UNKNOWN``.

    Fails closed (I3): an item we can't parse might still be in use by running sessions (they
    hold the tokens in memory), so it must not count as "nobody is live". A Keychain read
    failure propagates (``keychain.KeychainError``) for the same reason.
    """
    text = keychain.read(paths.claude_keychain_service())
    if text is None:
        return None  # no item: nobody is signed in
    try:
        blob = creds.parse_blob(text)
    except creds.CredentialsError:
        return _UNKNOWN
    return creds.get_login(blob)


def _live_token(login: dict[str, Any], now_ms: int | None) -> str:
    token = login.get("accessToken")
    if not token or oauth.is_expired(login, skew_seconds=LIVE_SKEW_SECONDS, now_ms=now_ms):
        raise TokenStale("The live login's access token has expired; Claude Code will refresh it.")
    return token


def _saved_login(account: Account) -> dict[str, Any]:
    text = keychain.read(account.keychain_service)
    if not text:
        raise NoCredentials(f"No saved login for {account.name}.")
    try:
        login = creds.get_login(creds.parse_blob(text))
    except creds.CredentialsError as exc:
        raise NoCredentials(f"The saved login for {account.name} is unreadable.") from exc
    if login is None:
        raise NoCredentials(f"No saved login for {account.name}.")
    saved_id = creds.login_account_id(login)
    if saved_id is not None and saved_id != account.id:
        raise NoCredentials(f"The saved login for {account.name} belongs to a different account.")
    return login


PERSIST_ATTEMPTS = 3

# Refreshed logins the Keychain didn't keep, per account id: (the refresh token they replace,
# the refreshed login). In memory only, never on disk (I7). The server revoked the replaced
# refresh token when it issued the new one, so until this copy is saved it is the only usable
# one; the next ``access_token_for`` call for the account saves it before anything else.
_UNSAVED: dict[str, tuple[str | None, dict[str, Any]]] = {}


def _saved_login_or_none(account: Account) -> dict[str, Any] | None:
    try:
        return _saved_login(account)
    except NoCredentials:
        return None


def _persist(account: Account, login: dict[str, Any]) -> None:
    """Write ``login`` as the account's saved item and read it back.

    This write happens after the server rotated the tokens, so losing it logs the account out:
    ``security -i`` can exit 0 without storing anything, hence the read-back (identity, refresh
    and access token) and up to ``PERSIST_ATTEMPTS`` writes.
    """
    payload = creds.dump_blob(creds.saved_item(login))
    service, attr = account.keychain_service, paths.keychain_account_name()
    for _attempt in range(PERSIST_ATTEMPTS):
        try:
            keychain.write(service, attr, payload)
        except keychain.KeychainError:
            continue
        try:
            stored = _saved_login_or_none(account)
        except keychain.KeychainError:
            stored = None
        if (
            stored is not None
            and stored.get("refreshToken") == login.get("refreshToken")
            and stored.get("accessToken") == login.get("accessToken")
        ):
            return
    raise keychain.KeychainError(
        f"The refreshed login for {account.name} could not be saved to Keychain (is it locked?). "
        "This cc-switcher process keeps it in memory and saves it on its next try; if the process "
        "exits first, the old login no longer works and the account needs `ccs add`."
    )


def _pending_kept_login_locked(account: Account) -> tuple[str | None, dict[str, Any]] | None:
    """``(replaced refresh token, kept login)`` if a kept login still applies to ``account``.

    The caller holds ``fsutil.app_lock``. The entry is dropped (and None returned) when
    something newer was saved since (e.g. ``ccs add`` re-added the account), or when the
    account was removed (no saved item and no registry row).
    """
    pending = _UNSAVED.get(account.id)
    if pending is None:
        return None
    replaced_refresh_token, _kept = pending
    current = _saved_login_or_none(account)
    if current is None and store.get_account(account.id) is None:
        _UNSAVED.pop(account.id, None)  # removed meanwhile (`ccs remove`): don't recreate it
        return None
    if current is not None and current.get("refreshToken") != replaced_refresh_token:
        _UNSAVED.pop(account.id, None)  # e.g. the account was re-added with `ccs add` meanwhile
        return None
    return pending


def _forget_kept_login(account_id: str) -> None:
    """The kept login is safe elsewhere now (e.g. written live, so the next live-to-saved copy saves it)."""
    _UNSAVED.pop(account_id, None)


def _save_kept_login_locked(account: Account) -> None:
    """Save a refreshed login an earlier call couldn't. The caller holds ``fsutil.app_lock``.

    Only for an account that is NOT live: if the live login were the revoked predecessor, the
    next live-to-saved copy (``switcher.save_active_login`` and every I5 step, in any process)
    would overwrite the saved successor with it. ``switcher`` puts such a kept login live
    first instead. Writes only the saved item; nothing is refreshed (I3). Raises
    ``keychain.KeychainError`` while the Keychain still won't keep it.
    """
    pending = _pending_kept_login_locked(account)
    if pending is None:
        return
    _persist(account, pending[1])
    _UNSAVED.pop(account.id, None)
    logger.info("Saved the refreshed login kept in memory for account %s", account.id)


def access_token_for(
    account: Account,
    *,
    http_post: Callable[..., Any] | None = None,
    now_ms: int | None = None,
) -> str:
    """Return an access token for ``account``.

    Raises ``NoCredentials`` (nothing usable saved), ``TokenStale`` (live login expired; never
    refreshed here), ``oauth.RefreshRejected`` (saved login can no longer be refreshed: re-add the
    account), ``oauth.OAuthError`` (transient), ``LiveLoginUnreadable`` (a refresh is due but the
    live item can't be parsed, so the account might be live), ``keychain.KeychainError`` and
    ``fsutil.LockTimeout``.
    """
    with fsutil.app_lock():
        live = _live_login()
        known_live = live if isinstance(live, dict) else None
        if known_live is not None and creds.login_account_id(known_live) == account.id:
            return _live_token(known_live, now_ms)

        # A write-back an earlier call couldn't confirm (N2). Only reached when the account is
        # not live, so a kept login is never used for a live account (I3).
        _save_kept_login_locked(account)
        login = _saved_login(account)

        # Defence in depth for I3: a saved login that shares its refresh token with the live one
        # is the same grant, even if the live login lacks identity fields.
        if (
            known_live is not None
            and login.get("refreshToken")
            and login.get("refreshToken") == known_live.get("refreshToken")
        ):
            return _live_token(known_live, now_ms)

        if not oauth.is_expired(login, skew_seconds=SAVED_SKEW_SECONDS, now_ms=now_ms):
            return login["accessToken"]  # using a valid access token rotates nothing

        if live is _UNKNOWN:
            raise LiveLoginUnreadable(
                "Can't tell which login is live: Claude Code's Keychain item is unreadable. "
                "Run `claude` and use /login."
            )

        post_attempted = False
        post_response = None

        def scheduled_post(url, payload, *, headers, timeout):
            nonlocal post_attempted, post_response
            # oauth validates the refresh token and builds the request before reaching here.
            # The existing lock protects both the reservation and the actual POST.
            registered = store.load_accounts()
            by_ref = {refresh_schedule._reference(item.id): item for item in registered}
            current_registered = account.id in {item.id for item in registered}

            def still_eligible(ref: str) -> bool:
                # Public callers normally use registry accounts. Preserve the existing
                # behavior for direct callers with an unregistered Account object.
                if not current_registered:
                    return True
                other = by_ref.get(ref)
                if other is None:
                    return False
                if known_live is not None and creds.login_account_id(known_live) == other.id:
                    return False
                try:
                    other_login = _saved_login(other)
                except (NoCredentials, keychain.KeychainError):
                    return False  # no POST possible now; it may rejoin on a later poll
                if (known_live is not None and other_login.get("refreshToken")
                        and other_login.get("refreshToken") == known_live.get("refreshToken")):
                    return False
                return bool(other_login.get("refreshToken")) and oauth.is_expired(
                    other_login, skew_seconds=SAVED_SKEW_SECONDS, now_ms=now_ms
                )

            refresh_schedule.reserve(account.id, is_eligible=still_eligible)
            post_attempted = True
            post_response = (http_post or oauth._default_http_post)(url, payload, headers=headers, timeout=timeout)
            return post_response

        try:
            refreshed = oauth.refresh_login(login, http_post=scheduled_post, now_ms=now_ms)
            try:
                _persist(account, refreshed)
            except Exception:
                # The server may already have revoked the old token; keep the replacement
                # even if an unexpected write error prevents confirming it in Keychain.
                _UNSAVED[account.id] = (login.get("refreshToken"), refreshed)
                logger.warning("Refreshed login for account %s could not be saved; kept in memory", account.id)
                raise
            logger.info("Refreshed saved login for account %s", account.id)
            return refreshed["accessToken"]
        finally:
            if post_attempted:
                try:
                    # Completion runs after saving or retaining rotated credentials, but
                    # still under the lock. A 429's Retry-After is persisted here too.
                    refresh_schedule.complete(account.id, post_response)
                except Exception:
                    # Leave the in-flight marker for conservative recovery; scheduling
                    # errors must never discard a successfully rotated credential.
                    logger.warning("Could not finish token refresh scheduling metadata")
