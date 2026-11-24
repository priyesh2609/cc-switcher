"""Usage monitoring: refresh every saved account, cache the result, log history, raise alerts.

Files (all 0600 in the 0700 app dir, written atomically, never containing tokens):

* ``paths.usage_cache_file()``   {"version", "snapshots", "backoff", "errors", "resets_cache", "alerts"}:
  the stored snapshot per account, the per-account 429 backoff, the per-account failure record
  ({"last_error", "last_error_at"}), the per-account redeemable-resets gate
  ({"fetched_at", "next_check_at", "status"}) and the alert-dedupe state (so a restart never re-notifies).
* ``paths.usage_history_file()`` compact JSONL, one line per account per *successful* refresh,
  trimmed to ~5 MB.

A failed refresh must not erase numbers that were true a few minutes ago. Unless the failure is in
``PERMANENT_ERRORS`` (the login itself is unusable), the last good snapshot stays stored and the
failure is recorded beside it in ``errors``. ``load_cache()`` therefore keeps returning the last good
snapshot (its ``fetched_at`` is how old the data is) and ``cache_status()`` says whether the most
recent attempt failed. Only ``refresh_account`` returns the raw outcome of one attempt.

Locking (I6): ``refresh_account`` never holds the app lock itself (``tokens.access_token_for`` takes
it, and it is not re-entrant, so the HTTP call to the usage endpoint runs outside it). The public
functions that read-modify-write app files (``refresh_all``'s final persist, ``save_cache``,
``append_history``, ``detect_alerts``) each take it exactly once, never nested.

Error codes on a failed snapshot (``UsageSnapshot.error``): ``no_credentials``, ``token_stale``,
``auth_expired``, ``rate_limited``, ``network``, ``http_<n>``, ``bad_response`` plus three
monitor-only codes: ``keychain_error`` (Keychain unreadable/locked), ``busy`` (another cc-switcher
operation held the lock too long) and ``internal_error`` (unexpected exception; never raised).
``describe_error(code)`` turns any code into a short human sentence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from cc_switcher import fsutil, keychain, oauth, paths, store, tokens, usage
from cc_switcher.models import (
    Account,
    BreakdownRow,
    ExtraUsage,
    PromoCredit,
    ResetStatus,
    UsageSnapshot,
    UsageWindow,
    utcnow,
)

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
HISTORY_MAX_BYTES = 5 * 1024 * 1024
HISTORY_TRIM_TO_BYTES = 4 * 1024 * 1024
DEFAULT_DELAY_BETWEEN_ACCOUNTS = 0.5  # seconds

BACKOFF_BASE_SECONDS = 60
BACKOFF_MIN_RETRY_AFTER = 30
BACKOFF_MAX_SECONDS = 3600

# Redeemable limit resets are fetched with a second request, at most this often per account.
RESETS_REFRESH_INTERVAL = timedelta(minutes=30)
RESETS_FAILURE_RETRY = timedelta(minutes=15)
RESETS_EXPIRY_WARNING = timedelta(hours=48)

# Failures that mean the saved login itself is unusable: the old numbers no longer describe anything the
# user can use, so they replace the stored snapshot. Every other failure (rate limit, network, a live token
# waiting for Claude Code to refresh it, Keychain/lock trouble, HTTP 5xx, bugs) leaves the last good
# snapshot in place.
PERMANENT_ERRORS = frozenset({"auth_expired", "no_credentials"})

# Extra-usage ("usage credits") alerts: percentages of the monthly limit, announced once per month.
EXTRA_USAGE_THRESHOLDS = (80, 100)

FIRED_RETENTION = timedelta(days=14)
RESET_HIGH_PERCENT = 80.0  # a window must have been at least this high ...
RESET_LOW_PERCENT = 20.0  # ... and fall below this after its reset time to count as "reset"

ERROR_MESSAGES = {
    "no_credentials": "No saved login. Add the account again.",
    "token_stale": "Waiting for Claude Code to refresh this login.",
    "auth_expired": "Sign-in expired. Add the account again.",
    "rate_limited": "Rate-limited by the usage endpoint; will retry shortly.",
    "network": "Could not reach Anthropic.",
    "bad_response": "Unexpected reply from the usage endpoint.",
    "keychain_error": "Could not read the Keychain (locked or waiting for a prompt?).",
    "busy": "Another cc-switcher operation is running.",
    "internal_error": "Unexpected error while fetching usage.",
}


def describe_error(code: str | None) -> str:
    """Short human sentence for an ``UsageSnapshot.error`` code."""
    if not code:
        return ""
    if code in ERROR_MESSAGES:
        return ERROR_MESSAGES[code]
    if code.startswith("http_"):
        return f"Usage endpoint error (HTTP {code[5:]})."
    return code


# --------------------------------------------------------------------------- snapshot <-> dict


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def snapshot_to_dict(s: UsageSnapshot) -> dict[str, Any]:
    """JSON-safe dict (datetimes as ISO strings). Contains the raw response, never tokens."""
    return {
        "account_id": s.account_id,
        "fetched_at": _iso(s.fetched_at),
        "ok": s.ok,
        "error": s.error,
        "windows": [
            {
                "key": w.key,
                "label": w.label,
                "utilization": w.utilization,
                "resets_at": _iso(w.resets_at),
                "severity": w.severity,
            }
            for w in s.windows
        ],
        "extra_usage": (
            {
                "enabled": s.extra_usage.enabled,
                "monthly_limit": s.extra_usage.monthly_limit,
                "used_credits": s.extra_usage.used_credits,
                "utilization": s.extra_usage.utilization,
                "currency": s.extra_usage.currency,
                "disabled_reason": s.extra_usage.disabled_reason,
                "spend_limit_reached": s.extra_usage.spend_limit_reached,
                "user_disabled": s.extra_usage.user_disabled,
                "decimal_places": s.extra_usage.decimal_places,
            }
            if s.extra_usage
            else None
        ),
        "promos": [
            {
                "key": p.key,
                "label": p.label,
                "limit_dollars": p.limit_dollars,
                "used_dollars": p.used_dollars,
                "remaining_dollars": p.remaining_dollars,
                "utilization": p.utilization,
                "resets_at": _iso(p.resets_at),
                "ends_at": _iso(p.ends_at),
            }
            for p in s.promos
        ],
        "resets": resets_to_dict(s.resets),
        "breakdown": [
            {
                "key": row.key,
                "display_name": row.display_name,
                "percent": row.percent,
                "percent_of_limit": row.percent_of_limit,
            }
            for row in s.breakdown
        ],
        "breakdown_as_of": _iso(s.breakdown_as_of),
        "raw": s.raw,
    }


def resets_to_dict(status: ResetStatus | None) -> dict[str, Any] | None:
    """JSON-safe form of a ``ResetStatus``; uses the server's own field names so ``usage.parse_resets`` reads it back."""
    if status is None:
        return None
    return {
        "eligible": status.eligible,
        "ineligible_reason": status.ineligible_reason,
        "at_limit": status.at_limit,
        "weekly_resets_at": _iso(status.weekly_resets_at),
        "cooldown_until": _iso(status.cooldown_until),
        "grants": [
            {
                "id": g.id,
                "label": g.label,
                "type": g.type,
                "resets_total": g.resets_total,
                "resets_left": g.resets_left,
                "usable_now": g.usable_now,
                "use_requires_limit": g.use_requires_limit,
                "gives_back_weekly": g.gives_back_weekly,
                "paused": g.paused,
                "starts_at": _iso(g.starts_at),
                "ends_at": _iso(g.ends_at),
                "unavailable_reason": g.unavailable_reason,
            }
            for g in status.grants
        ],
    }


def _rows(value: Any) -> list[Any]:
    """A stored list section, or [] when the cache holds something else there (damaged file)."""
    return value if isinstance(value, list) else []


def snapshot_from_dict(d: dict[str, Any]) -> UsageSnapshot:
    """Inverse of ``snapshot_to_dict``. Tolerant of missing optional fields; raises ``ValueError``
    only when ``account_id`` / ``fetched_at`` are unusable."""
    if not isinstance(d, dict):
        raise ValueError("snapshot is not an object")
    account_id = d.get("account_id")
    fetched_at = usage.parse_datetime(d.get("fetched_at"))
    if not isinstance(account_id, str) or not account_id or fetched_at is None:
        raise ValueError("snapshot lacks account_id/fetched_at")

    windows = []
    for row in _rows(d.get("windows")):
        if isinstance(row, dict) and isinstance(row.get("key"), str):
            windows.append(
                UsageWindow(
                    key=row["key"],
                    label=str(row.get("label") or usage.window_label(row["key"])),
                    utilization=usage._num(row.get("utilization")),
                    resets_at=usage.parse_datetime(row.get("resets_at")),
                    severity=row.get("severity") if isinstance(row.get("severity"), str) else None,
                )
            )

    extra = None
    xr = d.get("extra_usage")
    if isinstance(xr, dict):
        extra = ExtraUsage(
            enabled=bool(xr.get("enabled")),
            monthly_limit=usage._num(xr.get("monthly_limit")),
            used_credits=usage._num(xr.get("used_credits")),
            utilization=usage._num(xr.get("utilization")),
            currency=xr.get("currency") if isinstance(xr.get("currency"), str) else None,
            disabled_reason=xr.get("disabled_reason") if isinstance(xr.get("disabled_reason"), str) else None,
            # absent in caches written before these were parsed
            spend_limit_reached=usage._opt_bool(xr.get("spend_limit_reached")),
            user_disabled=usage._opt_bool(xr.get("user_disabled")),
            decimal_places=usage._decimal_places(xr.get("decimal_places")),
        )

    promos = []
    for row in _rows(d.get("promos")):
        if isinstance(row, dict) and isinstance(row.get("key"), str):
            promos.append(
                PromoCredit(
                    key=row["key"],
                    label=str(row.get("label") or usage.humanize_key(row["key"])),
                    limit_dollars=usage._num(row.get("limit_dollars")),
                    used_dollars=usage._num(row.get("used_dollars")),
                    remaining_dollars=usage._num(row.get("remaining_dollars")),
                    utilization=usage._num(row.get("utilization")),
                    resets_at=usage.parse_datetime(row.get("resets_at")),
                    ends_at=usage.parse_datetime(row.get("ends_at")),
                )
            )

    breakdown = []
    for row in _rows(d.get("breakdown")):
        if isinstance(row, dict) and isinstance(row.get("key"), str) and row["key"]:
            breakdown.append(
                BreakdownRow(
                    key=row["key"],
                    display_name=str(row.get("display_name") or usage.humanize_key(row["key"])),
                    percent=usage._num(row.get("percent")),
                    percent_of_limit=usage._num(row.get("percent_of_limit")),
                )
            )

    raw = d.get("raw")
    return UsageSnapshot(
        account_id=account_id,
        fetched_at=fetched_at,
        ok=bool(d.get("ok")),
        error=d.get("error") if isinstance(d.get("error"), str) else None,
        windows=tuple(windows),
        extra_usage=extra,
        promos=tuple(promos),
        resets=usage.parse_resets(d.get("resets")),  # absent in caches written before resets existed
        breakdown=tuple(breakdown),
        breakdown_as_of=usage.parse_datetime(d.get("breakdown_as_of")),
        raw=raw if isinstance(raw, dict) else None,
    )


# --------------------------------------------------------------------------- cache file


def _empty_state() -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "snapshots": {},
        "backoff": {},
        "errors": {},
        "resets_cache": {},
        "alerts": {"fired": {}, "auth_alerted": [], "high": {}, "resets_left": {}, "extra_usage": {}},
    }


def _read_state() -> dict[str, Any]:
    """The cache file with every section guaranteed to exist and be the right type. No lock."""
    data = fsutil.read_json(paths.usage_cache_file(), {})
    state = _empty_state()
    if not isinstance(data, dict):
        return state
    for section in ("snapshots", "backoff", "errors", "resets_cache"):
        if isinstance(data.get(section), dict):
            state[section] = data[section]
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
        if isinstance(alerts.get("fired"), dict):
            state["alerts"]["fired"] = alerts["fired"]
        if isinstance(alerts.get("auth_alerted"), list):
            state["alerts"]["auth_alerted"] = [x for x in alerts["auth_alerted"] if isinstance(x, str)]
        if isinstance(alerts.get("high"), dict):
            state["alerts"]["high"] = alerts["high"]
        if isinstance(alerts.get("resets_left"), dict):
            state["alerts"]["resets_left"] = alerts["resets_left"]
        if isinstance(alerts.get("extra_usage"), dict):
            state["alerts"]["extra_usage"] = alerts["extra_usage"]
    return state


def _write_state(state: dict[str, Any]) -> None:
    fsutil.ensure_private_dir(paths.app_dir())
    fsutil.atomic_write_json(paths.usage_cache_file(), state)


def _snapshots_from_state(state: dict[str, Any]) -> dict[str, UsageSnapshot]:
    result: dict[str, UsageSnapshot] = {}
    for account_id, row in state["snapshots"].items():
        try:
            result[account_id] = snapshot_from_dict(row)
        except Exception:
            continue  # a damaged row must not take the others down
    return result


def load_cache() -> dict[str, UsageSnapshot]:
    """The last stored snapshot per account id (no network, no lock: writes are atomic)."""
    return _snapshots_from_state(_read_state())


def _stored_good(state: dict[str, Any], account_id: str) -> bool:
    """Is there a usable, successful snapshot stored for this account?"""
    row = state["snapshots"].get(account_id)
    if not isinstance(row, dict) or row.get("ok") is not True:
        return False
    try:
        snapshot_from_dict(row)
    except Exception:
        return False
    return True


def _merge_attempt(state: dict[str, Any], account_id: str, snapshot: UsageSnapshot) -> UsageSnapshot:
    """Fold one refresh attempt into the cache ``state`` (in place; the caller holds the lock).

    * success: stored, and it clears the failure record if it is not older than the failure;
    * permanent failure (``PERMANENT_ERRORS``): replaces the stored snapshot;
    * any other failure: recorded in ``errors``; the last good snapshot stays stored. With no good
      snapshot to protect, the error snapshot is stored so the account still shows *why* it has no numbers.

    Returns the *effective* snapshot, i.e. what ``load_cache()`` will return for the account.
    """
    if snapshot.ok:
        state["snapshots"][account_id] = snapshot_to_dict(snapshot)
        record = state["errors"].get(account_id)
        failed_at = usage.parse_datetime(record.get("last_error_at")) if isinstance(record, dict) else None
        if failed_at is None or snapshot.fetched_at >= failed_at:  # a success *after* the failure clears it
            state["errors"].pop(account_id, None)
        return snapshot

    code = snapshot.error or "unknown"
    state["errors"][account_id] = {"last_error": code, "last_error_at": _iso(snapshot.fetched_at)}
    if code not in PERMANENT_ERRORS and _stored_good(state, account_id):
        return snapshot_from_dict(state["snapshots"][account_id])
    state["snapshots"][account_id] = snapshot_to_dict(snapshot)
    return snapshot


def save_cache(snapshots: dict[str, UsageSnapshot]) -> None:
    """Store exactly these accounts' snapshots (others are dropped; backoff and alert state are kept).

    Failed snapshots follow the rules in the module docstring: a transient failure does not replace the
    account's last good snapshot, it is recorded as ``last_error`` beside it.
    """
    with fsutil.app_lock():
        state = _read_state()
        state["snapshots"] = {k: v for k, v in state["snapshots"].items() if k in snapshots}
        state["errors"] = {k: v for k, v in state["errors"].items() if k in snapshots}
        for account_id, snapshot in snapshots.items():
            _merge_attempt(state, account_id, snapshot)
        _write_state(state)


def cache_status(account_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """What the cache knows about one account (no network, no lock)::

        {"snapshot":      UsageSnapshot | None,  # exactly what load_cache() holds for the account
         "age_seconds":   float | None,          # age of the numbers; None when there are none
         "last_error":    str | None,            # code of the most recent *failed* attempt, else None
         "last_error_at": datetime | None,
         "stale":         bool}                  # True when the most recent attempt failed

    ``snapshot`` is a failed snapshot (``ok=False``) only when there are no usable numbers: the login is
    unusable (``auth_expired``, ``no_credentials``) or no refresh has ever succeeded. In that case
    ``age_seconds`` is None. Caches written before failures were recorded separately are read the same way.
    """
    state = _read_state()
    snapshot: UsageSnapshot | None = None
    row = state["snapshots"].get(account_id)
    if isinstance(row, dict):
        try:
            snapshot = snapshot_from_dict(row)
        except Exception:
            snapshot = None

    last_error: str | None = None
    last_error_at: datetime | None = None
    record = state["errors"].get(account_id)
    if isinstance(record, dict) and isinstance(record.get("last_error"), str) and record["last_error"]:
        last_error = record["last_error"]
        last_error_at = usage.parse_datetime(record.get("last_error_at"))
    elif snapshot is not None and not snapshot.ok:
        last_error, last_error_at = snapshot.error or "unknown", snapshot.fetched_at

    age: float | None = None
    if snapshot is not None and snapshot.ok:
        age = max(((now or utcnow()) - snapshot.fetched_at).total_seconds(), 0.0)
    result = {
        "snapshot": snapshot,
        "age_seconds": age,
        "last_error": last_error,
        "last_error_at": last_error_at,
        "stale": last_error is not None,
    }
    row = state["backoff"].get(account_id)
    if (last_error == "rate_limited" and isinstance(row, dict)
            and isinstance(row.get("endpoint"), str) and row["endpoint"] in {"oauth_token", "usage"}):
        server_error = row.get("server_error")
        if not isinstance(server_error, str) or server_error not in {"rate_limit_error", "rate_limited", "too_many_requests"}:
            server_error = None
        deadline = usage.parse_datetime(row.get("next_allowed_at"))
        retry_after = row.get("retry_after_seconds")
        if (not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool)
                or not math.isfinite(retry_after) or retry_after < 0):
            retry_after = None
        result["rate_limit"] = {
            "endpoint": row["endpoint"],
            "server_error": server_error,
            "next_allowed_at": _iso(deadline) if deadline else None,
            "retry_after_seconds": retry_after,
            "account_ref": hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12],
        }
    return result


# --------------------------------------------------------------------------- history


def _history_line(s: UsageSnapshot) -> str:
    entry: dict[str, Any] = {"t": _iso(s.fetched_at), "id": s.account_id, "ok": True}
    if s.windows:
        entry["u"] = {w.key: w.utilization for w in s.windows if w.utilization is not None}
        entry["rs"] = {w.key: _iso(w.resets_at) for w in s.windows if w.resets_at is not None}
    extra = s.extra_usage
    if extra and (extra.used_credits is not None or extra.monthly_limit is not None):  # enabled or not
        entry["x"] = {
            "used": s.extra_usage.used_credits,
            "limit": s.extra_usage.monthly_limit,
            "cur": s.extra_usage.currency,
        }
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=False)


def _append_history_unlocked(snapshots: dict[str, UsageSnapshot]) -> None:
    good = [s for s in snapshots.values() if s.ok]  # failures are not data; they live in the cache's `errors`
    if not good:
        return
    path = paths.usage_history_file()
    fsutil.ensure_private_dir(paths.app_dir())
    text = "".join(_history_line(s) + "\n" for s in good)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > HISTORY_MAX_BYTES:
        _trim_history(path)


def _trim_history(path) -> None:
    """Keep the newest ~HISTORY_TRIM_TO_BYTES, cut on a line boundary."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) <= HISTORY_MAX_BYTES:
        return
    tail = data[-HISTORY_TRIM_TO_BYTES:]
    newline = tail.find(b"\n")
    tail = tail[newline + 1 :] if newline != -1 else b""
    fsutil.atomic_write_text(path, tail.decode("utf-8", errors="ignore"))


def append_history(snapshots: dict[str, UsageSnapshot]) -> None:
    """Append one compact JSONL line per *successful* snapshot (failed ones are skipped); trims to ~5 MB."""
    with fsutil.app_lock():
        _append_history_unlocked(snapshots)


# --------------------------------------------------------------------------- refresh


def _error_snapshot(account_id: str, code: str, fetched_at: datetime) -> UsageSnapshot:
    return UsageSnapshot(account_id=account_id, fetched_at=fetched_at, ok=False, error=code)


@dataclass(frozen=True)
class _Outcome:
    snapshot: UsageSnapshot
    retry_after: float | None = None
    resets_row: dict[str, Any] | None = None  # new redeemable-resets gate row to persist; None = unchanged
    rate_limit_endpoint: str | None = None
    server_error: str | None = None


def _deadline(now: datetime, seconds: float) -> datetime:
    """Honor even long server waits without overflowing datetime's representable range."""
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=now.tzinfo)


def _resets_row(fetched_at: str | None, next_check_at: datetime, status: ResetStatus | None) -> dict[str, Any]:
    return {"fetched_at": fetched_at, "next_check_at": _iso(next_check_at), "status": resets_to_dict(status)}


def _resolve_resets(
    token: str,
    row: dict[str, Any] | None,
    now: datetime,
    opener: Any,
    *,
    fixed_now: bool = False,
) -> tuple[ResetStatus | None, dict[str, Any] | None]:
    """Redeemable resets for an account: reuse the cached status until its gate opens, else fetch.

    Returns (status, new gate row or None when nothing changed). Never raises: a failed fetch keeps
    the previous status (or None) and retries after ``RESETS_FAILURE_RETRY``. Read-only on Anthropic's
    side: this only lists resets, it never claims one.
    """
    row = row if isinstance(row, dict) else {}
    try:
        previous = usage.parse_resets(row.get("status"))
    except Exception:  # a damaged cache row just means "nothing cached"
        previous = None
    next_check = usage.parse_datetime(row.get("next_check_at"))
    if next_check is not None and now < next_check:
        return previous, None
    previous_fetched_at = row.get("fetched_at") if isinstance(row.get("fetched_at"), str) else None
    try:
        raw = usage.fetch_resets_raw(token, opener=opener)
        status = usage.parse_resets(raw.get("cedar_ember"))  # absent block = not offered to this account
    except usage.UsageError as exc:
        wait_seconds = RESETS_FAILURE_RETRY.total_seconds()
        if exc.retry_after:
            wait_seconds = max(wait_seconds, exc.retry_after)
        received_at = now if fixed_now else utcnow()
        return previous, _resets_row(previous_fetched_at, _deadline(received_at, wait_seconds), previous)
    except Exception:
        logger.exception("Unexpected failure fetching redeemable resets")
        return previous, _resets_row(previous_fetched_at, now + RESETS_FAILURE_RETRY, previous)
    return status, _resets_row(_iso(now), now + RESETS_REFRESH_INTERVAL, status)


def _refresh(
    account: Account,
    *,
    http_post: Callable[..., Any] | None,
    opener: Any,
    resets_row: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> _Outcome:
    """Fetch one account (usage, then redeemable resets when due). Never raises."""
    fetched_at = now or utcnow()

    def fail(code: str, retry_after: float | None = None, *,
             endpoint: str | None = None, server_error: str | None = None) -> _Outcome:
        if code == "rate_limited" and endpoint:
            # Never log account names, token values, response bodies, or server free text.
            account_ref = hashlib.sha256(account.id.encode("utf-8")).hexdigest()[:12]
            logger.warning(
                "%s account_ref=%s endpoint=%s status=429 retry_after_seconds=%s server_error=%s",
                _iso(fetched_at), account_ref, endpoint, retry_after, server_error or "unknown",
            )
        return _Outcome(
            _error_snapshot(account.id, code, fetched_at), retry_after,
            rate_limit_endpoint=endpoint, server_error=server_error,
        )

    try:
        token = tokens.access_token_for(account, http_post=http_post)
        raw = usage.fetch_usage_raw(token, opener=opener)
        snapshot = usage.parse_usage(raw, account.id, fetched_at=fetched_at)
    except tokens.NoCredentials:
        return fail("no_credentials")
    except tokens.TokenStale:
        return fail("token_stale")
    except oauth.RefreshRejected:
        return fail("auth_expired")
    except oauth.OAuthError as exc:
        if exc.status == 429:
            return fail("rate_limited", exc.retry_after, endpoint="oauth_token", server_error=exc.server_error)
        if exc.status is not None and exc.status >= 500:
            return fail(f"http_{exc.status}")
        return fail("network")
    except usage.UsageError as exc:
        return fail(exc.code, exc.retry_after, endpoint="usage" if exc.code == "rate_limited" else None,
                    server_error=exc.server_error if exc.code == "rate_limited" else None)
    except keychain.KeychainError:
        return fail("keychain_error")
    except fsutil.LockTimeout:
        return fail("busy")
    except Exception:
        logger.exception("Unexpected failure refreshing usage for %s", account.id)
        return fail("internal_error")

    # Resets are a bonus: whatever happens here, the usage snapshot stands.
    try:
        status, new_row = _resolve_resets(token, resets_row, now or fetched_at, opener, fixed_now=now is not None)
    except Exception:  # belt and braces: nothing in here may take the usage snapshot down
        logger.exception("Unexpected failure resolving redeemable resets")
        status, new_row = None, None
    return _Outcome(replace(snapshot, resets=status), None, new_row)


def _persist_resets_rows(rows: dict[str, dict[str, Any]]) -> None:
    """Best-effort write of redeemable-resets gate rows (one lock, no other cache section touched)."""
    try:
        with fsutil.app_lock():
            state = _read_state()
            state["resets_cache"].update(rows)
            _write_state(state)
    except (fsutil.LockTimeout, OSError):
        logger.warning("Could not persist redeemable-resets state", exc_info=True)


def refresh_account(
    account: Account,
    *,
    http_post: Callable[..., Any] | None = None,
    opener: Any = None,
    now: datetime | None = None,
) -> UsageSnapshot:
    """Fetch current usage for one account. Never raises: any failure is ``UsageSnapshot(ok=False, error=code)``.

    ``snapshot.resets`` (redeemable limit resets) is fetched at most every 30 minutes per account;
    in between, the cached status is reused. A resets failure never fails the snapshot.

    The only thing written is that resets gate/cache row. The snapshot, history and 429 backoff are
    ``refresh_all``'s job.
    """
    row = _read_state()["resets_cache"].get(account.id)
    outcome = _refresh(account, http_post=http_post, opener=opener, resets_row=row, now=now)
    if outcome.resets_row is not None:
        _persist_resets_rows({account.id: outcome.resets_row})
    return outcome.snapshot


def _backoff_seconds(retry_after: float | None, failures: int) -> float:
    if retry_after is not None and retry_after > 0:
        # Do not retry early when the server asks for longer than our default cap.
        return max(retry_after, BACKOFF_MIN_RETRY_AFTER)
    seconds = BACKOFF_BASE_SECONDS * (2 ** max(failures - 1, 0))
    return min(seconds, BACKOFF_MAX_SECONDS)


def _backoff_until(state: dict[str, Any], account_id: str) -> datetime | None:
    row = state["backoff"].get(account_id)
    if not isinstance(row, dict):
        return None
    return usage.parse_datetime(row.get("next_allowed_at"))


def refresh_all(
    accounts: list[Account] | None = None,
    *,
    http_post: Callable[..., Any] | None = None,
    opener: Any = None,
    delay_between: float = DEFAULT_DELAY_BETWEEN_ACCOUNTS,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    reuse_fresh_seconds: float = 0,
) -> dict[str, UsageSnapshot]:
    """Refresh each account in turn (default: every saved account), persist cache + history, return snapshots.

    The returned snapshots are what ``load_cache()`` now holds: after a transient failure that is the
    account's *last good* snapshot (see the module docstring), not the failed attempt. Whether the
    attempt failed is in ``cache_status(id)``; ``refresh_account`` returns a single raw attempt.

    * Sequential, with ``delay_between`` seconds between accounts that were actually fetched.
    * When ``reuse_fresh_seconds`` is positive, a recent successful snapshot with no later
      failure is reused. Explicit CLI refreshes keep the default (zero) and fetch normally.
    * An account that got a 429 is skipped until its ``next_allowed_at`` (the server's Retry-After,
      else 60s doubling per consecutive 429, capped at 1h). While skipped it keeps its stored snapshot
      (or a ``rate_limited`` error snapshot if it has none) and is not logged to history.
    * With ``accounts=None`` the cache is also pruned of accounts that are no longer saved.
    * A failure to persist (lock timeout, disk error) is logged, never raised: the fresh
      snapshots are still returned.
    """
    full_refresh = accounts is None
    account_list = store.load_accounts() if full_refresh else list(accounts or [])
    fixed_now = now  # None in production: each attempt is then stamped with the real time it ran at
    now = now or utcnow()
    previous_state = _read_state()
    previous = _snapshots_from_state(previous_state)

    results: dict[str, UsageSnapshot] = {}
    fetched: dict[str, UsageSnapshot] = {}
    backoff_updates: dict[str, dict[str, Any] | None] = {}  # None = clear
    resets_updates: dict[str, dict[str, Any]] = {}
    did_fetch = False

    for account in account_list:
        allowed_at = _backoff_until(previous_state, account.id)
        if allowed_at is not None and now < allowed_at:
            results[account.id] = previous.get(account.id) or _error_snapshot(account.id, "rate_limited", now)
            continue
        cached = previous.get(account.id)
        last_failure = previous_state["errors"].get(account.id)
        has_failure = isinstance(last_failure, dict) and bool(last_failure.get("last_error"))
        if reuse_fresh_seconds > 0 and cached is not None and cached.ok and not has_failure:
            age = (now - cached.fetched_at).total_seconds()
            if 0 <= age < reuse_fresh_seconds:
                results[account.id] = cached
                continue
        if did_fetch and delay_between > 0:
            sleep(delay_between)
        did_fetch = True

        outcome = _refresh(
            account,
            http_post=http_post,
            opener=opener,
            resets_row=previous_state["resets_cache"].get(account.id),
            now=fixed_now,
        )
        snapshot, retry_after = outcome.snapshot, outcome.retry_after
        fetched[account.id] = snapshot
        if outcome.resets_row is not None:
            resets_updates[account.id] = outcome.resets_row

        if snapshot.error == "rate_limited":
            old = previous_state["backoff"].get(account.id)
            failures = (int(old.get("failures", 0)) if isinstance(old, dict) else 0) + 1
            wait = _backoff_seconds(retry_after, failures)
            # Retry-After starts when the response arrives, not when this poll began.
            # A request that takes 15 seconds followed by Retry-After: 120 must
            # not be retried until at least 135 seconds after the poll started.
            received_at = fixed_now if fixed_now is not None else utcnow()
            backoff_updates[account.id] = {
                "next_allowed_at": _iso(_deadline(received_at, wait)),
                "failures": failures,
                "endpoint": outcome.rate_limit_endpoint,
                "server_error": outcome.server_error,
                "retry_after_seconds": retry_after,
            }
        elif snapshot.ok:
            backoff_updates[account.id] = None

    effective: dict[str, UsageSnapshot] = {}
    try:
        with fsutil.app_lock():
            state = _read_state()  # fresh: another process may have written alert state meanwhile
            for account_id, snapshot in fetched.items():  # real attempts only; skipped accounts are untouched
                effective[account_id] = _merge_attempt(state, account_id, snapshot)
            state["resets_cache"].update(resets_updates)
            for account_id, update in backoff_updates.items():
                if update is None:
                    state["backoff"].pop(account_id, None)
                else:
                    state["backoff"][account_id] = update
            if full_refresh:
                keep = {a.id for a in account_list}
                for section in ("snapshots", "backoff", "errors", "resets_cache"):
                    state[section] = {k: v for k, v in state[section].items() if k in keep}
            _write_state(state)
            _append_history_unlocked(fetched)
    except (fsutil.LockTimeout, OSError):
        logger.warning("Could not persist usage cache/history", exc_info=True)

    # If persisting failed part-way, still answer exactly as the cache would have.
    for account_id, snapshot in fetched.items():
        if account_id not in effective:
            effective[account_id] = _merge_attempt(previous_state, account_id, snapshot)
    results.update(effective)
    return {account.id: results[account.id] for account in account_list if account.id in results}  # registry order


# --------------------------------------------------------------------------- alerts


@dataclass(frozen=True)
class Alert:
    account_id: str
    kind: str  # "threshold" | "reset" | "auth_expired" | "resets" | "extra_usage"
    window_key: str | None
    threshold: int | None
    title: str
    message: str


def _window_phrase(window: UsageWindow) -> str:
    if window.key == "five_hour":
        return "session limit"
    if window.key == "seven_day":
        return "weekly limit"
    label = window.label
    if label.startswith("Weekly · "):
        return f"weekly {label[len('Weekly · '):]} limit"
    return f"{label} limit"


def _resets_message(resets_at: datetime | None, now: datetime) -> str:
    if resets_at is None:
        return "Reset time unknown"
    text = usage.format_reset(resets_at, now)
    return "Resets now" if text == "now" else f"Resets in {text}"


def _reset_bucket(resets_at: datetime | None) -> str:
    """Resets timestamps jitter by sub-second amounts between fetches; bucket to the nearest minute."""
    if resets_at is None:
        return "none"
    return str((int(resets_at.timestamp()) + 30) // 60)


def _fired_key(account_id: str, window_key: str, threshold: int, resets_at: datetime | None) -> str:
    return f"{account_id}|{window_key}|{threshold}|{_reset_bucket(resets_at)}"


def detect_alerts(
    snapshots: dict[str, UsageSnapshot],
    accounts: list[Account],
    thresholds: list[int],
    *,
    now: datetime | None = None,
) -> list[Alert]:
    """New alerts for ``snapshots``; each fires once and is remembered in the cache file.

    * ``threshold``: a window reached a threshold. Fires once per (account, window, threshold,
      resets_at). If several thresholds are crossed at once only the highest is reported.
    * ``auth_expired``: once per account until a later snapshot is ok again.
    * ``reset``: a window that had been >= 80% is below 20% after its resets_at passed.
    * ``extra_usage``: paid extra usage is *enabled* and reached 80% / 100% of its monthly limit. Once per
      account, threshold and calendar month (UTC month of the snapshot's ``fetched_at``); never when extra
      usage is disabled, however high its numbers are.
    * ``resets``: redeemable limit resets (display only): the usable count went from 0 to more than 0,
      or a usable grant is within 48h of its ``ends_at``. Both fire once (the second is folded into the
      first when they coincide).
    """
    now = now or utcnow()
    levels = sorted({int(t) for t in thresholds})
    alerts: list[Alert] = []

    with fsutil.app_lock():
        state = _read_state()
        fired: dict[str, str] = state["alerts"]["fired"]
        auth_alerted: set[str] = set(state["alerts"]["auth_alerted"])
        high: dict[str, dict[str, Any]] = state["alerts"]["high"]
        resets_seen: dict[str, Any] = state["alerts"]["resets_left"]
        extra_seen: dict[str, Any] = state["alerts"]["extra_usage"]

        for account in accounts:
            snapshot = snapshots.get(account.id)
            if snapshot is None:
                continue
            if not snapshot.ok:
                if snapshot.error == "auth_expired" and account.id not in auth_alerted:
                    auth_alerted.add(account.id)
                    alerts.append(
                        Alert(
                            account.id,
                            "auth_expired",
                            None,
                            None,
                            f"{account.name}: sign-in expired",
                            "Add this account again to resume usage monitoring.",
                        )
                    )
                continue
            auth_alerted.discard(account.id)

            account_high = high.setdefault(account.id, {})
            for window in snapshot.windows:
                if window.utilization is None:
                    continue
                alerts.extend(_threshold_alerts(account, window, levels, fired, now))
                reset_alert = _reset_alert(account, window, account_high, now)
                if reset_alert:
                    alerts.append(reset_alert)
            alerts.extend(_vanished_reset_alerts(account, snapshot, account_high, now))
            alerts.extend(_resets_alerts(account, snapshot.resets, resets_seen, fired, now))
            alerts.extend(_extra_usage_alerts(account, snapshot, extra_seen))
            if not account_high:
                high.pop(account.id, None)

        # Housekeeping: forget removed accounts and old fired markers.
        known = {a.id for a in accounts}
        state["alerts"]["fired"] = {
            k: v
            for k, v in fired.items()
            if k.split("|", 1)[0] in known and _recent(v, now)
        }
        state["alerts"]["auth_alerted"] = sorted(auth_alerted & known)
        state["alerts"]["high"] = {k: v for k, v in high.items() if k in known}
        state["alerts"]["resets_left"] = {k: v for k, v in resets_seen.items() if k in known}
        state["alerts"]["extra_usage"] = {k: v for k, v in extra_seen.items() if k in known}
        _write_state(state)

    return alerts


def _recent(fired_at: Any, now: datetime) -> bool:
    when = usage.parse_datetime(fired_at)
    return when is not None and now - when <= FIRED_RETENTION


def _threshold_alerts(
    account: Account,
    window: UsageWindow,
    levels: list[int],
    fired: dict[str, str],
    now: datetime,
) -> list[Alert]:
    utilization = window.utilization or 0.0
    crossed = [t for t in levels if utilization >= t]

    # A window with no reset time has no period to dedupe against: re-arm once it drops below.
    if window.resets_at is None:
        for t in levels:
            if utilization < t:
                fired.pop(_fired_key(account.id, window.key, t, None), None)

    new = [t for t in crossed if _fired_key(account.id, window.key, t, window.resets_at) not in fired]
    if not new:
        return []
    for t in new:
        fired[_fired_key(account.id, window.key, t, window.resets_at)] = _iso(now) or ""
    top = max(new)
    phrase = _window_phrase(window)
    title = f"{account.name}: {phrase} reached" if top >= 100 else f"{account.name}: {phrase} {top}%"
    return [
        Alert(
            account.id,
            "threshold",
            window.key,
            top,
            title,
            _resets_message(window.resets_at, now),
        )
    ]


def _reset_alert(
    account: Account,
    window: UsageWindow,
    account_high: dict[str, Any],
    now: datetime,
) -> Alert | None:
    utilization = window.utilization or 0.0
    if utilization >= RESET_HIGH_PERCENT:
        account_high[window.key] = {
            "utilization": utilization,
            "resets_at": _iso(window.resets_at),
            "label": window.label,
        }
        return None
    record = account_high.get(window.key)
    if utilization < RESET_LOW_PERCENT and isinstance(record, dict):
        reset_time = usage.parse_datetime(record.get("resets_at"))
        if reset_time is None or reset_time <= now:
            account_high.pop(window.key, None)
            return Alert(
                account.id,
                "reset",
                window.key,
                None,
                f"{account.name}: {_window_phrase(window)} reset",
                f"Usage is back to {usage.format_percent(utilization)}.",
            )
    return None


def _vanished_reset_alerts(
    account: Account,
    snapshot: UsageSnapshot,
    account_high: dict[str, Any],
    now: datetime,
) -> list[Alert]:
    """A window that was high and is now absent (the server reports ``null`` once a window ends) counts
    as reset when its recorded reset time has passed."""
    present = {w.key for w in snapshot.windows if w.utilization is not None}
    alerts: list[Alert] = []
    for key in [k for k in account_high if k not in present]:
        record = account_high[key]
        reset_time = usage.parse_datetime(record.get("resets_at")) if isinstance(record, dict) else None
        if reset_time is None or reset_time > now:
            continue  # unknown or still in the future: keep waiting
        account_high.pop(key, None)
        label = str(record.get("label") or key)
        phrase = _window_phrase(UsageWindow(key=key, label=label, utilization=0.0, resets_at=None))
        alerts.append(
            Alert(account.id, "reset", key, None, f"{account.name}: {phrase} reset", "Usage is back to 0%.")
        )
    return alerts


# --------------------------------------------------------------------------- redeemable-reset alerts


def _grant_usable(grant: Any, now: datetime) -> bool:
    """A grant that still counts: not paused, resets left, already started, not yet expired."""
    return (
        not grant.paused
        and grant.resets_left > 0
        and (grant.starts_at is None or grant.starts_at <= now)
        and (grant.ends_at is None or grant.ends_at > now)
    )


def _resets_alerts(
    account: Account,
    status: ResetStatus | None,
    seen: dict[str, Any],
    fired: dict[str, str],
    now: datetime,
) -> list[Alert]:
    """Alerts for redeemable limit resets. Unlike ``ResetStatus.resets_left`` (a display sum), this
    ignores paused, not-yet-started and expired grants, and an account the server calls ineligible."""
    usable = [g for g in (status.grants if status is not None and status.eligible else ()) if _grant_usable(g, now)]
    available = sum(g.resets_left for g in usable)
    previous = seen.get(account.id)
    previous = previous if isinstance(previous, int) and not isinstance(previous, bool) else 0
    seen[account.id] = available

    expiring = [g for g in usable if g.ends_at is not None and g.ends_at - now <= RESETS_EXPIRY_WARNING]

    def key(grant: Any) -> str:
        return _fired_key(account.id, f"resets-expiry:{grant.id}", 0, grant.ends_at)

    if available > 0 and previous == 0:
        for grant in expiring:  # folded into this alert; do not announce the same grants again
            fired[key(grant)] = _iso(now) or ""
        soonest = min((g.ends_at for g in usable if g.ends_at is not None), default=None)
        plural = "" if available == 1 else "s"
        message = (
            f"Use within {usage.format_reset(soonest, now)}"
            if soonest is not None
            else "Run /limit-reset in Claude Code when you hit a limit."
        )
        return [Alert(account.id, "resets", None, None, f"{account.name}: {available} limit reset{plural} available", message)]

    fresh = [g for g in expiring if key(g) not in fired]
    if not fresh:
        return []
    for grant in fresh:
        fired[key(grant)] = _iso(now) or ""
    soonest_grant = min(fresh, key=lambda g: g.ends_at)
    return [
        Alert(
            account.id,
            "resets",
            None,
            None,
            f"{account.name}: limit reset expires in {usage.format_reset(soonest_grant.ends_at, now)}",
            f"{soonest_grant.label}: {soonest_grant.resets_left} left",
        )
    ]


# --------------------------------------------------------------------------- extra-usage alerts


def _utc_month(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m")


def _extra_usage_percent(extra: ExtraUsage) -> float | None:
    """How much of the monthly limit is used: the server's ``utilization``, else used / limit."""
    if extra.utilization is not None:
        return extra.utilization
    if extra.used_credits is not None and extra.monthly_limit and extra.monthly_limit > 0:
        return extra.used_credits / extra.monthly_limit * 100.0
    return None


def _extra_usage_alerts(account: Account, snapshot: UsageSnapshot, seen: dict[str, Any]) -> list[Alert]:
    """Threshold alerts for paid extra usage.

    State per account is ``{"month": "YYYY-MM", "fired": [80, ...]}``. It is kept apart from the window
    alerts' ``fired`` markers because those are pruned after 14 days, which would announce the same
    threshold again halfway through a month. A new month starts a fresh record, so everything re-arms.
    A snapshot from an *earlier* month than the record (stale data) is ignored.
    """
    extra = snapshot.extra_usage
    if extra is None or not extra.enabled:
        return []
    percent = _extra_usage_percent(extra)
    if percent is None:
        return []

    month = _utc_month(snapshot.fetched_at)
    record = seen.get(account.id)
    record_month = record.get("month") if isinstance(record, dict) else None
    if isinstance(record_month, str) and record_month > month:
        return []
    already: set[int] = set()
    if isinstance(record, dict) and record_month == month and isinstance(record.get("fired"), list):
        already = {t for t in record["fired"] if isinstance(t, int) and not isinstance(t, bool)}

    new = [t for t in EXTRA_USAGE_THRESHOLDS if percent >= t and t not in already]
    if not new:
        return []
    seen[account.id] = {"month": month, "fired": sorted(already | set(new))}

    def money(amount: float | None) -> str:
        return usage.format_money(amount, extra.currency, extra.decimal_places)

    top = max(new)  # several thresholds crossed at once: announce the highest, consume them all
    title = f"{account.name}: extra usage {top}%"
    if extra.monthly_limit is not None:
        title += f" of {money(extra.monthly_limit)}"
    if extra.used_credits is None:
        message = "Extra usage this month"
    elif extra.remaining is None:
        message = f"{money(extra.used_credits)} used this month"
    else:
        message = f"{money(extra.used_credits)} used, {money(extra.remaining)} left this month"
    return [Alert(account.id, "extra_usage", None, top, title, message)]
