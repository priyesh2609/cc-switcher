"""``ccs`` command line.

Every command imports the modules it needs *inside* its handler, so ``ccs --version`` and
``ccs --help`` keep working even if one module is broken, and tests can swap a module out
with ``sys.modules`` / ``monkeypatch``.

Exit codes: 0 ok, 1 error, 2 bad usage. Errors print one line to stderr; set ``CCS_DEBUG=1``
to get the full traceback instead.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.util
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from cc_switcher import __version__

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2

CLAUDE_INSTALL_HINT = "Install it with `curl -fsSL https://claude.ai/install.sh | bash` (see https://docs.claude.com/en/docs/claude-code/setup)."
SWITCH_NOTE = "New `claude` sessions use this account immediately; running sessions pick it up shortly."

# Errors that get a one-line message instead of a traceback: (module, class name).
_KNOWN_ERRORS = (
    ("switcher", "SwitchError"),
    ("store", "AccountNotFound"),
    ("store", "AmbiguousAccount"),
    ("fsutil", "LockTimeout"),
    ("keychain", "KeychainError"),
    ("creds", "CredentialsError"),
    ("claude_state", "ClaudeStateError"),
    ("mcp_profile", "McpProfileError"),
    ("launchagent", "LaunchAgentError"),
    ("menubar", "MenubarError"),
    ("tokens", "NoCredentials"),
    ("tokens", "TokenStale"),
    ("oauth", "OAuthError"),
    ("usage", "UsageError"),
)


class CliError(Exception):
    """A problem to report in one line; exit status 1."""


class BadUsage(Exception):
    """Wrong arguments; exit status 2."""


def _mod(name: str) -> Any:
    return importlib.import_module(f"cc_switcher.{name}")


def _known_errors() -> tuple[type[BaseException], ...]:
    found: list[type[BaseException]] = []
    for module_name, class_name in _KNOWN_ERRORS:
        try:
            cls = getattr(_mod(module_name), class_name, None)
        except Exception:
            continue
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


# --------------------------------------------------------------------------- output helpers


def _unicode_ok() -> bool:
    return (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").startswith("utf")


def _sym(name: str) -> str:
    table = {
        "active": ("◉", "*"),
        "inactive": ("○", " "),
        "ok": ("✓", "OK"),
        "fail": ("✗", "FAIL"),
        "warn": ("!", "!"),
        "full": ("█", "#"),
        "empty": ("░", "-"),
        "dash": ("—", "-"),
        "dot": ("·", "|"),
        "ellipsis": ("…", "..."),
        "stale": ("~", "~"),
    }
    unicode_form, ascii_form = table[name]
    return unicode_form if _unicode_ok() else ascii_form


def _style(text: str, color: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "2", "bold": "1"}
    return f"\033[{codes[color]}m{text}\033[0m"


def _err(message: str) -> None:
    print(f"ccs: {message}", file=sys.stderr)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + _sym("ellipsis")


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Left-aligned columns separated by two spaces; trailing spaces trimmed."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Iterable[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    return "\n".join([line(headers), *(line(r) for r in rows)])


def _json_out(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _account_json(account: Any, active_id: str | None = None) -> dict[str, Any]:
    return {
        "id": account.id,
        "active": account.id == active_id,
        "email": account.email,
        "label": account.label,
        "organization_name": account.organization_name,
        "subscription_type": account.subscription_type,
        "added_at": account.added_at,
        "last_used_at": account.last_used_at,
    }


def _snapshot_json(snapshot: Any) -> dict[str, Any] | None:
    return _mod("monitor").snapshot_to_dict(snapshot) if snapshot is not None else None


# --------------------------------------------------------------------------- usage formatting
# Shared with the menu bar app (menubar.py imports error_text / short_error_text).


def error_text(code: str | None) -> str:
    """A usage error code in plain words, with what to do about it."""
    code = code or "unknown"
    if code == "auth_expired":
        return "sign-in expired — run `ccs add` again for this account"
    if code == "no_credentials":
        return "no saved login for this account — run `ccs add` and sign in again"
    if code == "token_stale":
        return "the login token has expired; Claude Code renews it the next time you use `claude`, then retry"
    if code == "rate_limited":
        return "A request was rate limited — retries follow the server's wait or backoff"
    if code == "network":
        return "couldn't reach Anthropic — check your internet connection"
    if code == "keychain_error":
        return "couldn't read the Keychain — unlock your login keychain (choose Always Allow if macOS asks)"
    if code == "busy":
        return "another cc-switcher operation is running — it refreshes again on the next cycle"
    if code == "internal_error":
        return "unexpected error while fetching usage — run `ccs doctor --online` for details"
    if code == "bad_response":
        return "unexpected reply from the usage endpoint — Claude Code may have changed it; check for a cc-switcher update"
    if code.startswith("http_"):
        return f"Anthropic answered with HTTP {code[5:]} — usually temporary, try again later"
    return f"usage unavailable ({code})"


def short_error_text(code: str | None) -> str:
    """Same as error_text but short enough for a table cell or menu line."""
    code = code or "unknown"
    table = {
        "auth_expired": "sign-in expired",
        "no_credentials": "no saved login",
        "token_stale": "token needs refresh",
        "rate_limited": "rate limited",
        "network": "offline",
        "bad_response": "bad response",
        "keychain_error": "Keychain locked",
        "busy": "busy",
        "internal_error": "internal error",
    }
    if code in table:
        return table[code]
    if code.startswith("http_"):
        return f"HTTP {code[5:]}"
    return code


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def age_text(seconds: float | None) -> str:
    """'just now', '12m ago', '3h ago', '2d ago'."""
    seconds = max(int(seconds or 0), 0)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _ago(then: datetime, now: datetime | None = None) -> str:
    return age_text(((now or datetime.now(timezone.utc)) - _aware(then)).total_seconds())


# A cache entry is what monitor.cache_status() returns for one account:
#   {"snapshot", "age_seconds", "last_error", "last_error_at", "stale"}
# "stale" means the most recent refresh failed, so the numbers (if any) are the last good ones.


def cache_entries(accounts: list[Any]) -> dict[str, dict[str, Any]]:
    """Cached usage plus its age and refresh health for each account. Reads local files only."""
    monitor = _mod("monitor")
    return {a.id: monitor.cache_status(a.id) for a in accounts}


def entry_age(entry: dict[str, Any]) -> float | None:
    """Seconds since the numbers were fetched; None when there are no numbers."""
    snapshot = entry.get("snapshot")
    if snapshot is None or not snapshot.ok:
        return None
    age = entry.get("age_seconds")
    if age is None:
        age = (datetime.now(timezone.utc) - _aware(snapshot.fetched_at)).total_seconds()
    return max(age, 0.0)


def entry_is_stale(entry: dict[str, Any]) -> bool:
    """Numbers are shown, but the latest refresh attempt failed."""
    snapshot = entry.get("snapshot")
    return bool(entry.get("stale")) and snapshot is not None and snapshot.ok


def stale_phrase(entry: dict[str, Any]) -> str:
    """'stale: updated 12m ago, last refresh: rate limited'."""
    return f"stale: updated {age_text(entry_age(entry))}, last refresh: {short_error_text(entry.get('last_error'))}"


def _entry_json(entry: dict[str, Any]) -> dict[str, Any]:
    age = entry.get("age_seconds")
    last_at = entry.get("last_error_at")
    result = {
        "usage": _snapshot_json(entry.get("snapshot")),
        "age_seconds": round(age, 1) if age is not None else None,
        "stale": bool(entry.get("stale")),
        "last_error": entry.get("last_error"),
        "last_error_at": last_at.isoformat() if hasattr(last_at, "isoformat") else last_at,
    }
    if isinstance(entry.get("rate_limit"), dict):
        result["rate_limit"] = entry["rate_limit"]
    return result


def _reset_phrase(resets_at: datetime | None) -> str:
    """'resets in 3h 05m (18:30)' / 'resets in 2d 4h (Thu 25 Nov)'; empty-safe."""
    if resets_at is None:
        return "reset time unknown"
    relative = _mod("usage").format_reset(resets_at)
    if relative == "now":
        return "resets now"
    when = _aware(resets_at).astimezone()
    hours_away = (_aware(resets_at) - datetime.now(timezone.utc)).total_seconds() / 3600
    absolute = when.strftime("%H:%M") if hours_away < 24 else when.strftime("%a %d %b")
    return f"resets in {relative} ({absolute})"


def _bar(percent: float | None, width: int = 10) -> str:
    if percent is None:
        return " " * width
    filled = round(min(max(percent, 0.0), 100.0) / 100 * width)
    return _sym("full") * filled + _sym("empty") * (width - filled)


def _money(minor_units: float | None, currency: str | None) -> str:
    return _mod("usage").format_money(minor_units, currency)


def _dollars(amount: float | None) -> str:
    return _money(None if amount is None else amount * 100, "USD")


_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€"}
_DISABLED_REASONS = {
    "org_level_disabled_until": "paused by your organization",
    "user_disabled": "turned off by you",
}


def humanize_reason(code: str | None) -> str:
    """'org_level_disabled_until' -> 'paused by your organization'; unknown codes -> words."""
    if not code:
        return ""
    return _DISABLED_REASONS.get(code) or code.replace("_", " ")


def extra_money(minor_units: float | None, extra: Any) -> str:
    """An extra-usage amount (minor units) in the account's currency, with the server's decimal places (default 2)."""
    if minor_units is None:
        return _sym("dash")
    places = extra.decimal_places if getattr(extra, "decimal_places", None) is not None else 2
    code = (extra.currency or "USD").strip().upper() or "USD"
    try:
        amount = Decimal(repr(float(minor_units))) / (Decimal(10) ** places)
        amount = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, OverflowError):
        return _sym("dash")
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.{places}f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{text}" if symbol else f"{sign}{text} {code}"


def _extra_percent(extra: Any) -> str | None:
    percent = extra.utilization
    if percent is None and extra.monthly_limit and extra.used_credits is not None:
        percent = extra.used_credits / extra.monthly_limit * 100
    return _mod("usage").format_percent(percent) if percent is not None else None


def _extra_off_text(extra: Any) -> str:
    reason = extra.disabled_reason or ("user_disabled" if getattr(extra, "user_disabled", None) else None)
    return f"off ({humanize_reason(reason)})" if reason else "off"


def extra_usage_text(extra: Any) -> str:
    """Extra usage ("usage credits") in one phrase.

    Examples (amounts are minor units, 1000 = $10.00):
      limit hit, off:   '$61.25 of $50.00 used (100%) · limit reached · off (paused by your organization)'
      under the limit:  '$3.20 of $10.00 used · $6.80 left'
      nothing known:    'off' / 'off (turned off by you)'
    """
    used, limit, remaining = extra.used_credits, extra.monthly_limit, extra.remaining
    if limit is None:
        if extra.enabled:
            return f"on, {extra_money(used, extra)} used, no monthly limit" if used is not None else "on, no monthly limit"
        parts = [f"{extra_money(used, extra)} used"] if used is not None else []
        return " · ".join([*parts, _extra_off_text(extra)])

    reached = getattr(extra, "spend_limit_reached", None) is True
    exhausted = reached or (remaining is not None and remaining <= 0)
    if used is None:
        head = f"{extra_money(limit, extra)} monthly limit"
    else:
        head = f"{extra_money(used, extra)} of {extra_money(limit, extra)} used"
        percent = _extra_percent(extra) if exhausted else None
        if percent:
            head += f" ({percent})"
    parts = [head]
    if reached:
        parts.append("limit reached")
    elif remaining is not None:
        parts.append(f"{extra_money(remaining, extra)} left")
    if not extra.enabled:
        parts.append(_extra_off_text(extra))
    return " · ".join(parts)


def extra_usage_has_data(extra: Any) -> bool:
    """Worth a line in the compact menu: enabled, or the server reported amounts."""
    return bool(extra.enabled or extra.monthly_limit is not None or extra.used_credits is not None)


def breakdown_text(snapshot: Any, short: bool = False) -> str | None:
    """'Weekly use: Claude Code 26% of limit' (rows with a share of the limit only); None when there are none."""
    rows = [r for r in snapshot.breakdown if r.percent_of_limit is not None and r.percent_of_limit > 0]
    if not rows:
        return None
    format_percent = _mod("usage").format_percent
    text = " · ".join(f"{r.display_name or r.key} {format_percent(r.percent_of_limit)}" for r in rows)
    if len(rows) == 1 and not short:
        text += " of limit"
    return f"Weekly use: {text}"


def _promo_text(promo: Any) -> str:
    remaining = promo.remaining_dollars
    if remaining is None and promo.limit_dollars is not None and promo.used_dollars is not None:
        remaining = max(promo.limit_dollars - promo.used_dollars, 0.0)
    text = f"{_dollars(remaining)} remaining of {_dollars(promo.limit_dollars)}"
    expiry = promo.ends_at or promo.resets_at
    if expiry is not None:
        relative = _mod("usage").format_reset(expiry)
        text += " (ends now)" if relative == "now" else f" (ends in {relative})"
    return text


def _live_grants(resets: Any) -> list[Any]:
    """Grants that still have resets to give and are not paused."""
    return [g for g in resets.grants if g.resets_left > 0 and not g.paused]


def resets_summary(resets: Any, dot: str | None = None) -> str:
    """Redeemable limit resets in one phrase: '1 left (weekly) · use by Tue 03 Dec', or 'none left'.

    Display only. The reset itself is spent inside Claude Code with `/limit-reset`; cc-switcher never does that.
    """
    if not resets.eligible:  # the server doesn't offer resets to this account at all (e.g. reason "surface")
        return "not offered on this account"
    live = _live_grants(resets)
    if not live:
        return "none left"
    kinds: list[str] = []
    for grant in live:
        if grant.type and grant.type not in kinds:
            kinds.append(grant.type)
    parts = [f"{sum(g.resets_left for g in live)} left" + (f" ({', '.join(kinds)})" if kinds else "")]
    expiry = resets.next_expiry
    if expiry is not None:
        parts.append(f"use by {_aware(expiry).astimezone().strftime('%a %d %b')}")
    if any(g.usable_now for g in live):
        parts.append("usable now")
    elif any(g.use_requires_limit for g in live):
        parts.append("usable at a limit")
    return f" {dot or _sym('dot')} ".join(parts)


def resets_usable_now(snapshot: Any) -> bool:
    """True when the account is at a limit and has a reset it can use right now."""
    resets = getattr(snapshot, "resets", None)
    if resets is None or not any(g.usable_now for g in _live_grants(resets)):
        return False
    return bool(resets.at_limit or any(w.utilization is not None and w.utilization >= 100 for w in snapshot.windows))


def _promo_head(promo: Any) -> str:
    label = (promo.label or "").strip()
    return "Promo credit" if label.lower() in ("", "promo", "promo credit") else f"Promo credit ({label})"


def account_headline(account: Any, active_id: str | None) -> str:
    marker = _sym("active") if account.id == active_id else _sym("inactive")
    who = [account.label, account.email] if (account.label or account.email) else [account.name]
    parts = [*who, account.organization_name, account.subscription_type.capitalize()]
    shown = [p for p in parts if p]
    return f"{marker} " + f" {_sym('dot')} ".join(shown)


def _rate_limit_detail(entry: dict[str, Any]) -> str | None:
    info = entry.get("rate_limit")
    if not isinstance(info, dict):
        return None
    raw_endpoint = info.get("endpoint")
    if not isinstance(raw_endpoint, str):
        return None
    endpoint = {"oauth_token": "token refresh", "usage": "usage request"}.get(raw_endpoint)
    if not endpoint:
        return None
    details = [f"HTTP 429: {endpoint}"]
    error = info.get("server_error")
    if isinstance(error, str) and error in {"rate_limit_error", "rate_limited", "too_many_requests"}:
        details.append(f"server: {error}")
    raw = info.get("next_allowed_at")
    if isinstance(raw, str):
        try:
            date = _aware(datetime.fromisoformat(raw.replace("Z", "+00:00"))).astimezone()
            details.append(f"retry no earlier than {date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        except (ValueError, OverflowError):
            pass
    return " · ".join(details)


def usage_block(account: Any, entry: dict[str, Any], active_id: str | None) -> list[str]:
    """Detailed lines for one account: every window, credits, promo credits, how fresh they are, or the error."""
    lines = [account_headline(account, active_id)]
    indent = "    "
    snapshot = entry.get("snapshot")
    if snapshot is None:
        lines.append(f"{indent}no usage data yet — run `ccs usage --refresh`")
        return lines
    if not snapshot.ok:
        lines.append(f"{indent}{_sym('warn')} {error_text(snapshot.error)}")
        detail = _rate_limit_detail(entry)
        if detail:
            lines.append(f"{indent}{detail}")
        lines.append(f"{indent}(last tried {_ago(snapshot.fetched_at)})")
        return lines

    usage = _mod("usage")
    stale = entry_is_stale(entry)
    if stale:  # before the numbers, so nobody reads them as current
        lines.append(f"{indent}{_sym('stale')} {stale_phrase(entry)}")
        lines.append(f"{indent}{_sym('warn')} the last refresh failed: {error_text(entry.get('last_error'))}")
        detail = _rate_limit_detail(entry)
        if detail:
            lines.append(f"{indent}{detail}")
    windows = list(snapshot.windows)
    if windows:
        label_width = max(len(w.label) for w in windows)
        for window in windows:
            pct = usage.format_percent(window.utilization)
            lines.append(
                f"{indent}{window.label.ljust(label_width)}  {pct:>5}  {_bar(window.utilization)}  "
                f"{_reset_phrase(window.resets_at)}"
            )
            if window.key == "seven_day" and breakdown_text(snapshot):
                lines.append(f"{indent}{breakdown_text(snapshot)}")
    else:
        lines.append(f"{indent}no usage windows reported")
    if breakdown_text(snapshot) and not snapshot.window("seven_day"):
        lines.append(f"{indent}{breakdown_text(snapshot)}")
    if snapshot.extra_usage is not None:
        lines.append(f"{indent}Extra usage: {extra_usage_text(snapshot.extra_usage)}")
    for promo in snapshot.promos:
        lines.append(f"{indent}{_promo_head(promo)}: {_promo_text(promo)}")
    if snapshot.resets is not None:
        lines.append(f"{indent}Resets: {resets_summary(snapshot.resets)}")
    if not stale:
        lines.append(f"{indent}(updated {age_text(entry_age(entry))})")
    return lines


def _next_reset_text(snapshot: Any) -> str:
    """When the account is usable again if blocked, else the soonest session/weekly reset."""
    windows = [w for w in (snapshot.session, snapshot.weekly) if w is not None and w.resets_at is not None]
    if not windows:
        return _sym("dash")
    full = [w for w in windows if (w.utilization or 0) >= 100]
    target = max(full, key=lambda w: w.resets_at) if full else min(windows, key=lambda w: w.resets_at)
    return _mod("usage").format_reset(target.resets_at)


def _credits_cell(snapshot: Any) -> str:
    parts: list[str] = []
    extra = snapshot.extra_usage
    if extra is not None:
        remaining = extra.remaining
        if remaining is not None:
            parts.append(f"{extra_money(remaining, extra)} extra" + (" (off)" if not extra.enabled and remaining > 0 else ""))
        elif extra.enabled:
            parts.append("extra on")
    promo_total = [p.remaining_dollars for p in snapshot.promos if p.remaining_dollars is not None]
    if promo_total:
        parts.append(f"{_dollars(sum(promo_total))} promo")
    return " + ".join(parts) if parts else _sym("dash")


def _limit_resets_cell(snapshot: Any) -> str:
    """Redeemable resets left, e.g. '1' or '2, usable now'; a dash when unknown."""
    resets = getattr(snapshot, "resets", None)
    if resets is None or not resets.eligible:
        return _sym("dash")
    count = sum(g.resets_left for g in _live_grants(resets))
    return f"{count}, usable now" if resets_usable_now(snapshot) else str(count)


def _summary_cells(entry: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """(session, weekly, next reset, credits, limit resets, updated) for a table row.

    Numbers from a failed refresh keep their last good value, marked with a trailing '~'.
    """
    dash = _sym("dash")
    snapshot = entry.get("snapshot")
    if snapshot is None:
        return dash, dash, "no data yet", dash, dash, dash
    if not snapshot.ok:
        return dash, dash, short_error_text(snapshot.error), dash, dash, dash
    usage = _mod("usage")
    mark = _sym("stale") if entry_is_stale(entry) else ""
    session = usage.format_percent(snapshot.session.utilization) + mark if snapshot.session else dash
    weekly = usage.format_percent(snapshot.weekly.utilization) + mark if snapshot.weekly else dash
    updated = age_text(entry_age(entry)) + (" (stale)" if mark else "")
    return session, weekly, _next_reset_text(snapshot), _credits_cell(snapshot), _limit_resets_cell(snapshot), updated


def accounts_table(accounts: list[Any], entries: dict[str, dict[str, Any]], active_id: str | None) -> str:
    headers = ["", "LABEL", "EMAIL", "ORG", "PLAN", "SESSION", "WEEKLY", "NEXT RESET", "CREDITS", "LIMIT RESETS", "UPDATED"]
    dash = _sym("dash")
    rows = []
    for a in accounts:
        session, weekly, next_reset, credits, limit_resets, updated = _summary_cells(entries.get(a.id, {}))
        rows.append(
            [
                _sym("active") if a.id == active_id else _sym("inactive"),
                a.label or dash,
                _clip(a.email or a.name, 32),
                _clip(a.organization_name or dash, 22),
                a.subscription_type.capitalize() or dash,
                session,
                weekly,
                next_reset,
                credits,
                limit_resets,
                updated,
            ]
        )
    return render_table(headers, rows)


# --------------------------------------------------------------------------- account commands


def _refresh(accounts: list[Any]) -> dict[str, Any]:
    """Save the live login (keeps rotated tokens), then fetch fresh usage for ``accounts``."""
    _mod("switcher").save_active_login()
    return _mod("monitor").refresh_all(accounts)


def _who(account: Any) -> str:
    """'work (me@example.com)' for a labelled account, else Account.name (email, or 'account 1a2b3c4d' if unknown)."""
    return f"{account.label} ({account.email})" if account.label and account.email else account.name


def _active_id(status: Any) -> str | None:
    return status.active.id if status.active is not None else None


def _load_entries(accounts: list[Any], refresh: bool) -> dict[str, dict[str, Any]]:
    """Refresh (when asked), then read the cache: it holds the last good numbers if a refresh failed."""
    if refresh:
        _refresh(accounts)
    return cache_entries(accounts)


def cmd_list(args: argparse.Namespace) -> int:
    switcher = _mod("switcher")
    status = switcher.status()
    entries = _load_entries(status.accounts, args.refresh)
    active_id = _active_id(status)

    if args.json:
        _json_out(
            [
                {"account": _account_json(a, active_id), **_entry_json(entries[a.id])}
                for a in status.accounts
            ]
        )
        return EXIT_OK

    if not status.accounts:
        print("No saved accounts yet.")
        print("  `ccs import`  saves the account you are signed in to right now")
        print("  `ccs add`     signs in to another account and saves it")
        return EXIT_OK
    print(accounts_table(status.accounts, entries, active_id))
    stale = [(a, entries[a.id]) for a in status.accounts if entry_is_stale(entries[a.id])]
    ages = [age for age in (entry_age(e) for e in entries.values()) if age is not None]
    if stale:
        print(f"\n{_sym('stale')} = stale: the last refresh failed, so these are the last good numbers.")
        for account, entry in stale:
            print(f"  {account.name}: {stale_phrase(entry)}")
        print("  `ccs usage <account>` explains each failure.")
    elif args.refresh:
        print("\nUsage fetched just now.")
    elif ages:
        print(f"\nUsage from cache (updated {age_text(min(ages))}). `ccs list --refresh` fetches fresh numbers.")
    else:
        print("\nNo usage data yet. `ccs list --refresh` fetches it.")
    if status.unregistered_live_login:
        print("The account you are signed in to is not saved yet — run `ccs import` to save it.")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    switcher = _mod("switcher")
    status = switcher.status()
    active = status.active
    entry = cache_entries([active])[active.id] if active else None

    if args.json:
        _json_out(
            {
                "live_account_id": status.live_account_id,
                "signed_in": status.live_account_id is not None,
                "unregistered_live_login": status.unregistered_live_login,
                "active": _account_json(active, active.id) if active else None,
                "accounts": [_account_json(a, _active_id(status)) for a in status.accounts],
                **(_entry_json(entry) if entry else {"usage": None, "age_seconds": None, "stale": False, "last_error": None, "last_error_at": None}),
            }
        )
        return EXIT_OK

    if status.live_account_id is None:
        print("Not signed in to Claude Code. Run `claude` and sign in, then `ccs import`.")
    elif active is None:
        print("Signed in, but this login is not saved in cc-switcher yet — run `ccs import`.")
    else:
        print("Active account")
        print("\n".join(usage_block(active, entry, active.id)))
    print(f"\nSaved accounts: {len(status.accounts)}")
    return EXIT_OK


def cmd_usage(args: argparse.Namespace) -> int:
    switcher, store = _mod("switcher"), _mod("store")
    status = switcher.status()
    accounts = [store.find_account(args.account)] if args.account else status.accounts
    entries = _load_entries(accounts, args.refresh)
    active_id = _active_id(status)

    if args.json:
        _json_out([{"account": _account_json(a, active_id), **_entry_json(entries[a.id])} for a in accounts])
        return EXIT_OK
    if not accounts:
        print("No saved accounts yet. Run `ccs import` first.")
        return EXIT_OK
    blocks = ["\n".join(usage_block(a, entries[a.id], active_id)) for a in accounts]
    print("\n\n".join(blocks))
    return EXIT_OK


def cmd_switch(args: argparse.Namespace) -> int:
    switcher, store = _mod("switcher"), _mod("store")
    account = store.find_account(args.query)
    if switcher.live_account_id() == account.id:
        print(f"{_who(account)} is already the active account.")
        return EXIT_OK
    switcher.switch_to(account.id)
    print(f"Switched to {_who(account)}.")
    print(SWITCH_NOTE)
    return EXIT_OK


def _percent_or_dash(window: Any) -> str:
    return _mod("usage").format_percent(window.utilization) if window is not None else _sym("dash")


def cmd_next(args: argparse.Namespace) -> int:
    switcher, store, monitor, autoswitch = (_mod(n) for n in ("switcher", "store", "monitor", "autoswitch"))
    status = switcher.status()
    if not [a for a in status.accounts if a.id != _active_id(status)]:
        raise CliError("There is no other saved account to switch to. Add one with `ccs add`.")
    snapshots = monitor.load_cache() if args.cached else _refresh(status.accounts)
    settings = store.load_settings()
    target = autoswitch.choose_target(_active_id(status), status.accounts, snapshots, settings)
    if target is None:
        minutes = int(getattr(autoswitch, "CANDIDATE_MAX_AGE_SECONDS", 1800) // 60)
        raise CliError(
            "No other account has room right now: each is at or above "
            f"{settings.auto_switch_threshold:g}% on a watched window, or has no usable usage data "
            f"(numbers older than {minutes} minutes count as unknown). "
            "Run `ccs usage --refresh` to see why."
        )
    switcher.switch_to(target.id)
    snap = snapshots.get(target.id)
    detail = ""
    if snap is not None and snap.ok:
        detail = f" (session {_percent_or_dash(snap.session)}, weekly {_percent_or_dash(snap.weekly)})"
    print(f"Switched to {_who(target)}{detail}.")
    print(SWITCH_NOTE)
    return EXIT_OK


def cmd_add(args: argparse.Namespace) -> int:
    switcher = _mod("switcher")
    if switcher.find_claude() is None:
        raise CliError(f"Claude Code is not installed. {CLAUDE_INSTALL_HINT}")
    print("Starting `claude auth login`. A browser window opens: sign in to the account you want to add.")
    print("If your browser is already signed in to a different Claude account, sign out first or use a private window.")
    before = switcher.status()
    live_before, registered_before = before.live_account_id, {a.id for a in before.accounts}
    try:
        account = switcher.add_account(label=args.label or "")
    except KeyboardInterrupt as exc:
        saved = getattr(exc, "account", None)  # switcher.LoginInterrupted: the login completed first
        if saved is None:
            raise
        what = f"the new account {_who(saved)}" if saved.id not in registered_before else _who(saved)
        print(f"Login was interrupted, but {what} was saved. It is now the active account.")
        return 130
    if account is None:
        now = switcher.status()
        if now.live_account_id == live_before:
            print("No new login was saved (sign-in cancelled or failed). Your previous login is unchanged.")
        else:  # e.g. the menu bar switched accounts during the sign-in
            who = _who(now.active) if now.active is not None else "see `ccs status`"
            print(f"No new login was saved (sign-in cancelled or failed). Claude Code's login changed meanwhile ({who}).")
        return EXIT_ERROR
    if account.id in registered_before and account.id != live_before:
        print(f"Claude Code is now using {_who(account)}.")  # e.g. the menu bar switched to it meanwhile
    else:
        print(f"Saved {_who(account)}. It is now the active account.")
    print(SWITCH_NOTE)
    return EXIT_OK


def cmd_import(args: argparse.Namespace) -> int:
    account = _mod("switcher").import_current(label=args.label or "")
    org = f" in {account.organization_name}" if account.organization_name else ""
    plan = f", {account.subscription_type.capitalize()}" if account.subscription_type else ""
    print(f"Saved {_who(account)}{org}{plan}.")
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    switcher, store = _mod("switcher"), _mod("store")
    account = store.find_account(args.query)
    switcher.remove_account(account.id, force=args.force)
    print(f"Removed {_who(account)} from cc-switcher. The Claude account itself is untouched.")
    return EXIT_OK


def cmd_label(args: argparse.Namespace) -> int:
    switcher, store = _mod("switcher"), _mod("store")
    account = store.find_account(args.query)
    updated = switcher.set_label(account.id, args.label)
    who = updated.email or f"account {updated.account_uuid[:8]}"  # not .name: that is the label just set
    print(f"{who} is now labelled '{updated.label}'." if updated.label else f"Cleared the label on {who}.")
    return EXIT_OK


# --------------------------------------------------------------------------- settings


def _format_setting(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return f"{value:g}"
    return str(value)


_TRUE = {"true", "on", "yes", "1", "enable", "enabled"}
_FALSE = {"false", "off", "no", "0", "disable", "disabled"}
_RANGES = {"poll_interval_seconds": (60, 86400), "auto_switch_threshold": (1, 100)}
_CHOICES = {"menubar_title": ("none", "session", "weekly", "both")}


def _split_list(values: list[str]) -> list[str]:
    return [piece for piece in re.split(r"[,\s]+", ",".join(values)) if piece]


def _coerce_setting(key: str, ftype: str, values: list[str]) -> Any:
    models = _mod("models")
    if ftype in ("bool", "int", "float", "str") and len(values) != 1:
        raise BadUsage(f"{key} takes exactly one value")
    if ftype == "bool":
        word = values[0].strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise BadUsage(f"{key} must be true or false")
    if ftype in ("int", "float"):
        try:
            number = int(values[0]) if ftype == "int" else float(values[0])
        except ValueError:
            raise BadUsage(f"{key} must be a number") from None
        low, high = _RANGES.get(key, (None, None))
        if low is not None and not (low <= number <= high):
            raise BadUsage(f"{key} must be between {low} and {high}")
        return number
    if ftype == "list[int]":
        try:
            numbers = sorted({int(piece) for piece in _split_list(values)})
        except ValueError:
            raise BadUsage(f"{key} must be a comma-separated list of whole numbers, e.g. 80,95,100") from None
        if not numbers or not all(1 <= n <= 100 for n in numbers):
            raise BadUsage(f"{key} needs one or more percentages between 1 and 100, e.g. 80,95,100")
        return numbers
    if ftype == "list[str]":
        names = _split_list(values)
        known = set(models.WINDOW_LABELS)
        bad = [n for n in names if n not in known and not n.startswith("limit:")]
        if not names or bad:
            raise BadUsage(f"{key} must list usage windows from: {', '.join(sorted(known))}")
        return names
    choices = _CHOICES.get(key)
    if choices and values[0] not in choices:
        raise BadUsage(f"{key} must be one of: {', '.join(choices)}")
    return values[0]


def cmd_settings(args: argparse.Namespace) -> int:
    store, fsutil, models = _mod("store"), _mod("fsutil"), _mod("models")
    types = {f.name: str(f.type) for f in dataclasses.fields(models.Settings)}
    key, values = args.key, args.value

    if key is not None and key not in types:
        raise BadUsage(f"unknown setting '{key}'. Settings: {', '.join(types)}")
    if key is None:
        current = store.load_settings()
        width = max(len(k) for k in types)
        for name in types:
            print(f"{name.ljust(width)} = {_format_setting(getattr(current, name))}")
        return EXIT_OK
    if not values:
        print(_format_setting(getattr(store.load_settings(), key)))
        return EXIT_OK

    new_value = _coerce_setting(key, types[key], values)
    with fsutil.app_lock():
        settings = store.load_settings()
        setattr(settings, key, new_value)
        store.save_settings(settings)
    print(f"{key} = {_format_setting(new_value)}")
    print("The menu bar app picks this up within a minute.")
    return EXIT_OK


# --------------------------------------------------------------------------- mcp

_AUTH_WORDS = {
    "signed_in": "signed in",
    "needs_sign_in": "needs sign-in",
    "header_auth": "header token",
    "not_oauth": "local (no sign-in)",
    "not_configured": "not installed here",
}
_PLACEHOLDER = re.compile(r"\$\{(CCS_[A-Z0-9_]+)\}")


def _mcp_status(args: argparse.Namespace) -> int:
    rows = _mod("mcp_profile").status()
    if not rows:
        print("No MCP servers yet. `ccs mcp catalog` lists ready-made ones; `ccs mcp add <names>` puts them in your profile.")
        return EXIT_OK
    table = [
        [
            r.name,
            r.type,
            "yes" if r.in_profile else "no",
            "yes" if r.configured else "no",
            _AUTH_WORDS.get(r.auth, r.auth),
            _clip(r.url or _sym("dash"), 48),
        ]
        for r in rows
    ]
    print(render_table(["SERVER", "TYPE", "IN PROFILE", "INSTALLED", "SIGN-IN", "URL"], table))
    hints = []
    if any(r.auth == "needs_sign_in" for r in rows):
        hints.append("Sign in once per Mac: run `claude`, type /mcp, pick the server. Switching accounts never signs you out of these.")
    if any(r.in_profile and not r.configured for r in rows):
        hints.append("Profile servers missing here: run `ccs mcp apply`.")
    if any(r.configured and not r.in_profile for r in rows):
        hints.append("Installed servers not in your profile: `ccs mcp export` adds them (they are kept either way).")
    if hints:
        print("\n" + "\n".join(hints))
    return EXIT_OK


def _mcp_catalog(args: argparse.Namespace) -> int:
    catalog = _mod("mcp_profile").CATALOG
    for name, config in catalog.items():
        print(f"{name}  {config.get('url', '')}")
        note = config.get("note")
        if note:
            print(textwrap.fill(note, width=78, initial_indent="    ", subsequent_indent="    "))
    print("\nAdd some to your profile with `ccs mcp add <names>`, then install them with `ccs mcp apply`.")
    return EXIT_OK


def _mcp_export(args: argparse.Namespace) -> int:
    mcp_profile = _mod("mcp_profile")
    target = Path(args.path).expanduser() if args.path else None
    written = mcp_profile.export_profile(target)
    print(f"Wrote MCP profile to {written}")
    print("It holds server addresses only. Values that look secret were replaced with ${CCS_...} placeholders.")
    try:
        text = Path(written).read_text(encoding="utf-8")
    except OSError:
        text = ""
    variables = sorted(set(_PLACEHOLDER.findall(text)))
    if variables:
        print("Set these environment variables before `ccs mcp apply` on another Mac: " + ", ".join(variables))
    print("Copy the file to the other Mac and run `ccs mcp apply <file>` there.")
    return EXIT_OK


def _mcp_add(args: argparse.Namespace) -> int:
    mcp_profile = _mod("mcp_profile")
    catalog = mcp_profile.CATALOG
    unknown = [n for n in args.names if n not in catalog]
    if unknown:
        raise BadUsage(f"unknown catalog server(s): {', '.join(unknown)}. Known: {', '.join(catalog)}")
    mcp_profile.add_to_profile(list(args.names))
    print(f"Added to your MCP profile: {', '.join(args.names)}")
    for name in args.names:
        note = catalog[name].get("note")
        if note and "OAuth" not in note:  # OAuth servers all sign in the same way; only call out the odd ones
            print(textwrap.fill(f"{name}: {note}", width=78, subsequent_indent="    "))
    print("Install them on this Mac with `ccs mcp apply`.")
    return EXIT_OK


_CHANGE_MARK = {"add": "+", "update": "~", "remove": "-", "unchanged": "=", "skip": "!"}


def _describe_change(change: Any) -> str:
    detail = ""
    config = change.config or {}
    if change.action in ("add", "update", "unchanged") and config:
        detail = config.get("url") or " ".join([str(config.get("command", "")), *map(str, config.get("args", []))]).strip()
    if change.reason:
        detail = f"{detail}  {change.reason}".strip()
    text = f"  {_CHANGE_MARK.get(change.action, '?')} {change.action:<9} {change.name}"
    return f"{text}  {detail}".rstrip()


def _mcp_apply(args: argparse.Namespace) -> int:
    mcp_profile = _mod("mcp_profile")
    profile = mcp_profile.load_profile(Path(args.path).expanduser()) if args.path else None
    changes = mcp_profile.apply_profile(profile, prune=args.prune, dry_run=args.dry_run)
    if not changes:
        print("The profile is empty — nothing to apply.")
        return EXIT_OK
    if args.dry_run:
        print("Dry run — nothing was changed. Plan:")
    for change in changes:
        print(_describe_change(change))
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.action] = counts.get(change.action, 0) + 1
    print("\n" + ", ".join(f"{n} {action}" for action, n in counts.items()))
    unresolved = [c for c in changes if c.action == "skip" and c.reason.startswith("set ")]
    resign = [c for c in changes if c.action == "skip" and mcp_profile.RESIGN_PREFIX in c.reason]
    if unresolved:
        print("Skipped servers need their ${CCS_...} variable set in your environment; set it and run again.")
    if resign:
        print("The config changed but a sign-in could not be kept: open /mcp in `claude` and sign in to " + ", ".join(c.name for c in resign) + " again.")
    if not args.dry_run and any(c.action in ("add", "update") for c in changes):
        print("Next: run `claude`, type /mcp and sign in to any server marked 'needs authentication' (once per Mac).")
    return EXIT_ERROR if mcp_profile.has_failures(changes) else EXIT_OK


def cmd_mcp(args: argparse.Namespace) -> int:
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "status": _mcp_status,
        "catalog": _mcp_catalog,
        "export": _mcp_export,
        "add": _mcp_add,
        "apply": _mcp_apply,
    }
    return handlers[args.mcp_command](args)


# --------------------------------------------------------------------------- agent / menubar


def cmd_menubar(args: argparse.Namespace) -> int:
    return int(_mod("menubar").main() or 0)


def cmd_agent(args: argparse.Namespace) -> int:
    launchagent = _mod("launchagent")
    paths = _mod("paths")
    if args.agent_command == "install":
        launchagent.install()
        print(f"Installed {paths.launch_agent_plist()}")
        print("The menu bar app now starts at login, and is starting now.")
        print(f"Logs: {paths.log_dir()}")
    elif args.agent_command == "uninstall":
        removed = launchagent.uninstall()
        print("Removed the LaunchAgent. The menu bar app no longer starts at login." if removed else "The LaunchAgent was not installed.")
    else:
        installed, running = launchagent.is_installed(), launchagent.is_running()
        print(f"Start at login: {'on' if installed else 'off'}")
        print(f"Menu bar app:   {'running' if running else 'not running'}")
        if not installed:
            print("Turn it on with `ccs agent install`.")
    return EXIT_OK


# --------------------------------------------------------------------------- doctor


@dataclass
class Check:
    level: str  # "ok" | "fail" | "warn"
    text: str
    hint: str = ""


def _run_command(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _check_platform() -> list[Check]:
    if sys.platform != "darwin":
        return [Check("fail", f"not macOS ({sys.platform})", "cc-switcher only works on macOS 12 or later")]
    version = platform.mac_ver()[0]
    major = int(version.split(".")[0]) if version[:1].isdigit() else 0
    if major and major < 12:
        return [Check("fail", f"macOS {version} is too old", "macOS 12 or later is required")]
    return [Check("ok", f"macOS {version or '(unknown version)'}")]


def _check_claude() -> list[Check]:
    claude = _mod("switcher").find_claude()
    if claude is None:
        return [Check("fail", "Claude Code (`claude`) not found", CLAUDE_INSTALL_HINT)]
    try:
        result = _run_command([claude, "--version"])
        version = (result.stdout or "").strip().splitlines()[0] if result.returncode == 0 and (result.stdout or "").strip() else ""
    except Exception:
        version = ""
    if not version:
        return [Check("warn", f"found {claude} but `claude --version` did not answer", "Run `claude --version` yourself to see why")]
    return [Check("ok", f"Claude Code {version} ({claude})")]


def _check_live_login() -> tuple[list[Check], dict | None]:
    keychain, creds, paths = _mod("keychain"), _mod("creds"), _mod("paths")
    service = paths.claude_keychain_service()
    try:
        raw = keychain.read(service)
    except Exception as exc:
        return [Check("fail", f"cannot read Keychain item '{service}': {exc}", "Unlock your login keychain; if macOS asks, choose Always Allow")], None
    if raw is None:
        return [Check("fail", f"no Claude Code login in the Keychain ('{service}')", "Run `claude` and sign in (or `claude auth login`)")], None
    try:
        blob = creds.parse_blob(raw)
    except Exception as exc:
        return [Check("fail", f"Keychain item '{service}' is unreadable: {exc}", "Sign in again with `claude auth login`")], None
    login = creds.get_login(blob)
    if login is None:
        return [Check("fail", f"Keychain item '{service}' has no Claude login (signed out?)", "Run `claude` and sign in (or `claude auth login`)")], None
    plan = login.get("subscriptionType") or "unknown plan"
    return [Check("ok", f"live Claude login found in the Keychain item '{service}' ({plan})")], login


def _check_registered(login: dict | None) -> list[Check]:
    store, creds = _mod("store"), _mod("creds")
    accounts = store.load_accounts()
    if login is None:
        return []
    live_id = creds.login_account_id(login)
    match = next((a for a in accounts if a.id == live_id), None) if live_id else None
    if match is not None:
        return [Check("ok", f"live login is saved as {match.name}")]
    return [Check("warn", "the live login is not saved in cc-switcher yet", "Run `ccs import` to save it")]


def _check_saved_items() -> list[Check]:
    store, keychain, creds = _mod("store"), _mod("keychain"), _mod("creds")
    accounts = store.load_accounts()
    if not accounts:
        return [Check("warn", "no accounts saved yet", "Run `ccs import` to save the account you are signed in to")]
    problems: list[Check] = []
    fix = "Run `ccs add` and sign in to this account again (or `ccs remove` it)"
    for account in accounts:
        try:
            raw = keychain.read(account.keychain_service)
        except Exception as exc:
            problems.append(Check("fail", f"{account.name}: cannot read its saved login: {exc}", "Unlock your login keychain"))
            continue
        if raw is None:
            problems.append(Check("fail", f"{account.name}: no saved login in the Keychain", fix))
            continue
        try:
            login = creds.get_login(creds.parse_blob(raw))
        except Exception:
            login = None
        if login is None:
            problems.append(Check("fail", f"{account.name}: the saved login is empty or damaged", fix))
        elif creds.login_account_id(login) != account.id:
            problems.append(Check("fail", f"{account.name}: the saved login belongs to a different account", fix))
    return problems or [Check("ok", f"saved logins present for all {len(accounts)} account(s)")]


def _check_permissions() -> list[Check]:
    paths = _mod("paths")
    folder = paths.app_dir()
    if not folder.exists():
        return [Check("ok", f"{folder} not created yet (it appears when you save an account)")]
    loose: list[str] = []
    try:
        if stat.S_IMODE(folder.stat().st_mode) & 0o077:
            loose.append(str(folder))
        for entry in folder.iterdir():
            if entry.is_file() and not entry.is_symlink() and stat.S_IMODE(entry.stat().st_mode) & 0o077:
                loose.append(str(entry))
    except OSError as exc:
        return [Check("warn", f"could not inspect {folder}: {exc}")]
    if loose:
        quoted = shlex.quote(str(folder))
        return [Check("fail", f"too-open permissions: {', '.join(loose)}", f"chmod 700 {quoted} && chmod 600 {quoted}/*")]
    return [Check("ok", f"{folder} is private (0700, files 0600)")]


def _check_launchagent() -> list[Check]:
    launchagent, paths = _mod("launchagent"), _mod("paths")
    if not launchagent.is_installed():
        return [Check("warn", "start at login is off", "Run `ccs agent install` to start the menu bar app at login")]
    if launchagent.is_running():
        return [Check("ok", "menu bar app is installed and running")]
    return [
        Check(
            "warn",
            "the LaunchAgent is installed but the menu bar app is not running",
            f"Run `ccs agent install` to reload it; errors go to {paths.log_dir() / 'menubar.err.log'}",
        )
    ]


def _check_rumps() -> list[Check]:
    if importlib.util.find_spec("rumps") is None:
        return [Check("fail", "the menu bar library (rumps) is not installed", "Re-run scripts/install.sh")]
    return [Check("ok", "menu bar library (rumps) installed")]


def _check_mcp() -> list[Check]:
    try:
        rows = [r for r in _mod("mcp_profile").status() if r.configured]
    except Exception as exc:
        return [Check("warn", f"could not read MCP servers: {exc}")]
    if not rows:
        return [Check("ok", "no user-scope MCP servers (add some with `ccs mcp add <names>`)")]
    checks = []
    for row in rows:
        word = _AUTH_WORDS.get(row.auth, row.auth)
        if row.auth == "needs_sign_in":
            checks.append(Check("warn", f"MCP {row.name}: needs sign-in", "Run `claude`, type /mcp and sign in to it (once per Mac)"))
        else:
            checks.append(Check("ok", f"MCP {row.name}: {word}"))
    return checks


def _check_online() -> list[Check]:
    switcher, monitor, usage = _mod("switcher"), _mod("monitor"), _mod("usage")
    account = switcher.active_account()
    if account is None:
        return [Check("warn", "no active saved account, so no usage check", "Run `ccs import` first")]
    snapshot = monitor.refresh_account(account)
    if snapshot.ok:
        session = usage.format_percent(snapshot.session.utilization) if snapshot.session else _sym("dash")
        weekly = usage.format_percent(snapshot.weekly.utilization) if snapshot.weekly else _sym("dash")
        return [Check("ok", f"usage for {account.name}: session {session}, weekly {weekly}")]
    return [Check("fail", f"usage for {account.name}: {error_text(snapshot.error)}")]


def _doctor_checks(online: bool) -> list[Check]:
    results: list[Check] = []

    def run(fn: Callable[[], list[Check]]) -> None:
        try:
            results.extend(fn())
        except Exception as exc:
            results.append(Check("fail", f"check {fn.__name__.lstrip('_')} crashed: {exc}", "Re-run with CCS_DEBUG=1 for details"))

    run(_check_platform)
    run(_check_claude)
    run(_check_rumps)
    login: dict | None = None
    try:
        live_checks, login = _check_live_login()
        results.extend(live_checks)
    except Exception as exc:
        results.append(Check("fail", f"check live_login crashed: {exc}"))
    run(lambda: _check_registered(login))
    run(_check_saved_items)
    run(_check_permissions)
    run(_check_launchagent)
    run(_check_mcp)
    if online:
        run(_check_online)
    return results


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = _doctor_checks(args.online)
    marks = {
        "ok": _style(_sym("ok"), "green"),
        "fail": _style(_sym("fail"), "red"),
        "warn": _style(_sym("warn"), "yellow"),
    }
    for check in checks:
        print(f"{marks[check.level]} {check.text}")
        if check.hint:
            print(f"    fix: {check.hint}")
    failures = sum(c.level == "fail" for c in checks)
    warnings = sum(c.level == "warn" for c in checks)
    if failures:
        print(f"\n{failures} problem(s), {warnings} warning(s).")
    elif warnings:
        print(f"\nNo problems, {warnings} warning(s).")
    else:
        print("\nAll checks passed.")
    if not args.online:
        print("(Run `ccs doctor --online` to also test fetching usage.)")
    return EXIT_ERROR if failures else EXIT_OK


# --------------------------------------------------------------------------- parser / main


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Switch Claude Code accounts without losing your MCP sign-ins. Run with no command to list accounts.",
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, handler: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=handler)
        return p

    p = add("list", cmd_list, "list saved accounts with usage (default command)")
    p.add_argument("--refresh", action="store_true", help="fetch fresh usage instead of using the cache")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = add("status", cmd_status, "show the active account and its usage")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = add("usage", cmd_usage, "detailed usage for one or all accounts")
    p.add_argument("account", nargs="?", help="label, email or id (default: all accounts)")
    p.add_argument("--refresh", action="store_true", help="fetch fresh usage instead of using the cache")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = add("switch", cmd_switch, "make an account the active one")
    p.add_argument("query", help="label, email or id (a unique prefix works)")

    p = add("next", cmd_next, "switch to the best other account with room left")
    p.add_argument("--cached", action="store_true", help="use cached usage instead of fetching fresh numbers")

    p = add("add", cmd_add, "sign in to another Claude account and save it")
    p.add_argument("--label", help="nickname for the account, e.g. work")

    p = add("import", cmd_import, "save the account you are signed in to right now")
    p.add_argument("--label", help="nickname for the account")

    p = add("remove", cmd_remove, "forget a saved account (the Claude account itself is untouched)")
    p.add_argument("query", help="label, email or id")
    p.add_argument("--force", action="store_true", help="allow removing the active account")

    p = add("label", cmd_label, "set or clear an account nickname")
    p.add_argument("query", help="label, email or id")
    p.add_argument("label", help="new nickname (use '' to clear)")

    p = add("settings", cmd_settings, "show or change settings")
    p.add_argument("key", nargs="?", help="setting name (omit to list all)")
    p.add_argument("value", nargs="*", help="new value (omit to show the current one)")

    p = add("mcp", cmd_mcp, "manage the fixed set of MCP servers")
    mcp_sub = p.add_subparsers(dest="mcp_command", metavar="<action>", required=True)
    mcp_sub.add_parser("status", help="show servers and their sign-in state")
    mcp_sub.add_parser("catalog", help="list ready-made vendor servers")
    q = mcp_sub.add_parser("export", help="write the user-scope servers to a profile file")
    q.add_argument("path", nargs="?", help="profile file (default: the app data folder)")
    q = mcp_sub.add_parser("add", help="add catalog servers to the profile")
    q.add_argument("names", nargs="+", help="catalog names, see `ccs mcp catalog`")
    q = mcp_sub.add_parser("apply", help="install the profile's servers on this Mac")
    q.add_argument("path", nargs="?", help="profile file (default: the app data folder)")
    q.add_argument("--prune", action="store_true", help="also remove user-scope servers that are not in the profile")
    q.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")

    add("menubar", cmd_menubar, "run the menu bar app")

    p = add("agent", cmd_agent, "start the menu bar app at login")
    agent_sub = p.add_subparsers(dest="agent_command", metavar="<action>", required=True)
    agent_sub.add_parser("install", help="install and start the LaunchAgent")
    agent_sub.add_parser("uninstall", help="remove the LaunchAgent")
    agent_sub.add_parser("status", help="show whether it is installed and running")

    p = add("doctor", cmd_doctor, "check the installation and find problems")
    p.add_argument("--online", action="store_true", help="also fetch usage for the active account")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help / --version exit 0, bad usage exits 2
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)

    handler = getattr(args, "func", None)
    if handler is None:  # no command given: same as `ccs list`
        handler = cmd_list
        args = argparse.Namespace(refresh=False, json=False)

    try:
        return handler(args)
    except BadUsage as exc:
        _err(str(exc))
        return EXIT_USAGE
    except CliError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except Exception as exc:
        if os.environ.get("CCS_DEBUG") == "1":
            raise
        if isinstance(exc, _known_errors()):
            _err(str(exc))
        else:
            _err(f"unexpected {type(exc).__name__}: {exc} (run with CCS_DEBUG=1 for details)")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
