"""Auto-switch decisions. No I/O and no globals: everything comes in as arguments.

Both functions look only at the windows named in ``settings.auto_switch_windows``
(default ``five_hour`` and ``seven_day``), and neither acts on old numbers: a snapshot older than
``max_age_seconds`` (by its ``fetched_at``) counts as unknown. The only clock access is the ``now``
argument's default (``utcnow()``); pass ``now`` to make a call fully deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cc_switcher.models import Account, Settings, UsageSnapshot, utcnow

# The active account is what the decision is about: its numbers must be recent (3 default polls).
ACTIVE_MAX_AGE_SECONDS = 900.0
# A candidate was "available" when last seen; it may have been used heavily since (6 default polls).
CANDIDATE_MAX_AGE_SECONDS = 1800.0


def _is_stale(snapshot: UsageSnapshot, max_age_seconds: float | None, now: datetime) -> bool:
    """Older than ``max_age_seconds`` (strictly: exactly that old is still fresh). None disables the check.
    A snapshot dated in the future (clock skew) is treated as fresh."""
    if max_age_seconds is None:
        return False
    fetched_at = snapshot.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (now - fetched_at).total_seconds() > max_age_seconds


def _watched(snapshot: UsageSnapshot, settings: Settings) -> list[float]:
    """Known utilizations of the watched windows (windows the server did not report are skipped)."""
    keys = set(settings.auto_switch_windows)
    return [
        w.utilization
        for w in snapshot.windows
        if w.key in keys and w.utilization is not None
    ]


def _percent(snapshot: UsageSnapshot, key: str) -> float:
    window = snapshot.window(key)
    return window.utilization if window is not None and window.utilization is not None else 0.0


def should_switch(
    active: UsageSnapshot | None,
    settings: Settings,
    *,
    max_age_seconds: float | None = ACTIVE_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """True when auto-switch is on and the active account's snapshot is ok, no older than
    ``max_age_seconds`` (default 900) and any watched window has reached ``settings.auto_switch_threshold``.
    An unknown, errored or outdated snapshot never triggers."""
    if not settings.auto_switch_enabled or active is None or not active.ok:
        return False
    if _is_stale(active, max_age_seconds, now or utcnow()):
        return False
    known = _watched(active, settings)
    return bool(known) and max(known) >= settings.auto_switch_threshold


def choose_target(
    active_id: str | None,
    accounts: list[Account],
    snapshots: dict[str, UsageSnapshot],
    settings: Settings,
    *,
    max_age_seconds: float | None = CANDIDATE_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> Account | None:
    """The best account to switch to, or None.

    Candidates are accounts other than ``active_id`` with an *ok* snapshot no older than
    ``max_age_seconds`` (default 1800) that reports at least one watched window and is below
    ``settings.auto_switch_threshold`` on every watched window it reports. A watched window the server
    omitted (e.g. ``five_hour`` is null when no session is running) counts as 0% for ranking. Accounts with no snapshot, an errored or outdated snapshot or no
    watched data are never chosen. Ranking: lowest weekly %, then lowest session %, then registry order.
    """
    now = now or utcnow()
    candidates: list[tuple[float, float, Account]] = []
    for account in accounts:
        if account.id == active_id:
            continue
        snapshot = snapshots.get(account.id)
        if snapshot is None or not snapshot.ok or _is_stale(snapshot, max_age_seconds, now):
            continue
        known = _watched(snapshot, settings)
        if not known or max(known) >= settings.auto_switch_threshold:
            continue
        candidates.append((_percent(snapshot, "seven_day"), _percent(snapshot, "five_hour"), account))
    if not candidates:
        return None
    # min() returns the first minimal element, so ties keep registry order.
    return min(candidates, key=lambda item: (item[0], item[1]))[2]
