"""Cross-process OAuth POST scheduling; all functions require the app lock.

Only hashed account references and clocks are stored, never credentials. An in-flight marker
survives a crash; the next caller starts a full 180-second recovery wait before any new POST.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Callable

from cc_switcher import fsutil, paths, store, usage

INTERVAL = 180.0
QUEUE_LEASE = 900.0


class RefreshQueued(RuntimeError):
    """The saved token refresh will be retried on a later poll."""


class ScheduleError(RuntimeError):
    """Scheduling metadata cannot be safely read or written; no POST is allowed."""


def _reference(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def _clock() -> dict[str, float]:
    return {"wall": time.time(), "mono": time.monotonic()}


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_clock(value: Any) -> bool:
    return isinstance(value, dict) and all(_finite_number(value.get(k)) for k in ("wall", "mono"))


def _read() -> dict[str, Any]:
    path = paths.refresh_schedule_file()
    if not path.exists():
        return {"version": 1, "last": None, "inflight": False, "queue": [], "retry": {}}
    try:
        data = fsutil.read_json(path, None)
    except (ValueError, RecursionError):
        raise ScheduleError("Token refresh schedule is unreadable; no refresh was attempted.") from None
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("inflight"), bool)
            or not isinstance(data.get("queue"), list) or not isinstance(data.get("retry"), dict)
            or (data.get("last") is not None and not _valid_clock(data["last"]))):
        raise ScheduleError("Token refresh schedule is unreadable; no refresh was attempted.")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
           or not _valid_clock(row.get("seen")) for row in data["queue"]):
        raise ScheduleError("Token refresh schedule is unreadable; no refresh was attempted.")
    if any(not isinstance(k, str) or not isinstance(v, dict) or not _valid_clock(v.get("at"))
           or not _finite_number(v.get("wait")) or v["wait"] < 0 for k, v in data["retry"].items()):
        raise ScheduleError("Token refresh schedule is unreadable; no refresh was attempted.")
    return data


def _write(state: dict[str, Any]) -> None:
    fsutil.ensure_private_dir(paths.app_dir())
    fsutil.atomic_write_json(paths.refresh_schedule_file(), state)


def _elapsed(now: dict[str, float], then: dict[str, float], seconds: float) -> bool:
    # Both clocks must advance: a backward wall-clock adjustment delays rather than accelerates.
    # A monotonic reset on reboot likewise waits until the previous value is passed.
    return now["wall"] - then["wall"] >= seconds and now["mono"] - then["mono"] >= seconds


def reserve(account_id: str, *, is_eligible: Callable[[str], bool] | None = None) -> None:
    """Reserve under the caller's lock, distinguishing storage failure from HTTP failure."""
    try:
        _reserve(account_id, is_eligible=is_eligible)
    except OSError:
        raise ScheduleError("Cannot access token refresh scheduling metadata; no refresh was attempted.") from None


def _reserve(account_id: str, *, is_eligible: Callable[[str], bool] | None = None) -> None:
    """Reserve immediately before POST; raise rather than wait for a slot.

    ``is_eligible`` checks hashed queued account references under the caller's existing lock.
    """
    state, now, ref = _read(), _clock(), _reference(account_id)
    poll = store.load_settings().poll_interval_seconds
    if not _finite_number(poll):
        poll = 300
    lease = max(QUEUE_LEASE, min(max(poll, 0), 86400) * 2)

    clocks = ([state["last"]] if state["last"] is not None else [])
    clocks += [row["seen"] for row in state["queue"]]
    clocks += [row["at"] for row in state["retry"].values()]
    if any(now["mono"] < previous["mono"] for previous in clocks):
        # The monotonic clock reset, normally after reboot. Start a fresh global wait;
        # preserving the old monotonic value could otherwise block for the old uptime.
        previous = state["last"]
        state["last"] = {"wall": max(now["wall"], previous["wall"]) if previous else now["wall"],
                         "mono": now["mono"]}
        state["inflight"] = False
        # An unknown clock change must not shorten a server's wait: restart its full
        # duration for each affected account, retaining later wall times on rollback.
        for retry in state["retry"].values():
            retry["at"] = {"wall": max(now["wall"], retry["at"]["wall"]), "mono": now["mono"]}
        # Abandoned queue entries can be pruned using wall time; retain the order of
        # the others, with new monotonic timestamps so their leases can expire.
        state["queue"] = [row for row in state["queue"] if now["wall"] - row["seen"]["wall"] < lease]
        for row in state["queue"]:
            row["seen"] = {"wall": max(now["wall"], row["seen"]["wall"]), "mono": now["mono"]}
    elif state["inflight"]:
        previous = state["last"]
        # If either clock went backwards, preserve its later value; this can only extend the wait.
        state["last"] = {k: max(now[k], previous[k]) if previous else now[k] for k in now}
        state["inflight"] = False
    state["queue"] = [row for row in state["queue"] if not _elapsed(now, row["seen"], lease)]
    retry = state["retry"].get(ref)
    if retry and not _elapsed(now, retry["at"], retry["wait"]):
        state["queue"] = [row for row in state["queue"] if row["id"] != ref]
        _write(state)
        raise RefreshQueued("Token refresh is queued behind the provider's retry deadline.")
    state["retry"].pop(ref, None)
    row = next((row for row in state["queue"] if row["id"] == ref), None)
    if row:
        row["seen"] = now
    else:
        state["queue"].append({"id": ref, "seen": now})
    last = state["last"]
    if last is None or _elapsed(now, last, INTERVAL):
        # Only the head can block this request. Drop heads that no longer need a POST,
        # but never remove the requesting account that has just been validated.
        while state["queue"][0]["id"] != ref and is_eligible is not None:
            if is_eligible(state["queue"][0]["id"]):
                break
            state["queue"].pop(0)
    if (last is not None and not _elapsed(now, last, INTERVAL)) or state["queue"][0]["id"] != ref:
        _write(state)
        raise RefreshQueued("Token refresh is queued; it will retry on a later poll.")
    state["queue"].pop(0)
    state["last"] = now
    state["inflight"] = True
    _write(state)


def complete(account_id: str, response: Any = None) -> None:
    """Record completion even after errors; a 429 separately gates this account."""
    state, now = _read(), _clock()
    previous = state["last"]
    state["last"] = {k: max(now[k], previous[k]) if previous else now[k] for k in now}
    state["inflight"] = False
    if response is not None and len(response) > 0 and response[0] == 429:
        headers = response[2] if len(response) > 2 else None
        wait = usage._parse_retry_after(headers)
        state["retry"][_reference(account_id)] = {"at": now, "wait": max(wait or 60.0, 30.0)}
    _write(state)
