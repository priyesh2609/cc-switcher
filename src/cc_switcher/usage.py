"""Fetch and parse Anthropic's OAuth usage endpoint.

``fetch_usage_raw`` does one GET and maps every failure to a ``UsageError`` with a stable
``code``. ``parse_usage`` turns the (loosely specified, passthrough) response into a
``UsageSnapshot`` and never raises on shapes it does not recognise: the server adds codename
fields (``cinder_cove``, ``wattle_ember`` ...) without notice.

``opener`` contract (injectable, default = ``urllib.request.urlopen`` resolved at call time):
either a callable ``opener(request, timeout=...)`` or an object with ``.open(request, timeout=...)``,
returning a response with ``.read()`` (and optionally ``.status``/``.getcode()``/``.close()``).
HTTP error statuses are signalled the way urllib does it: by raising ``urllib.error.HTTPError``.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from cc_switcher import paths
from cc_switcher.models import (
    WINDOW_LABELS,
    BreakdownRow,
    ExtraUsage,
    PromoCredit,
    ResetGrant,
    ResetStatus,
    UsageSnapshot,
    UsageWindow,
    utcnow,
)

FALLBACK_CLAUDE_CODE_VERSION = "2.1.330"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Top-level keys with their own parser: never mistaken for a window or a promo credit.
_RESERVED_KEYS = frozenset({"extra_usage", "spend", "limits", "cedar_ember", "seven_day_breakdown"})

# Largest minor-unit exponent we accept from the server (ISO 4217 goes up to 4).
MAX_DECIMAL_PLACES = 6

# Unscoped `limits[]` rows describe a headline window we already have; they only contribute severity.
_HEADLINE_FOR_LIMIT_KIND = {
    "session": "five_hour",
    "five_hour": "five_hour",
    "weekly_all": "seven_day",
    "weekly": "seven_day",
    "seven_day": "seven_day",
}

# Redeemable limit resets (what Claude Code's /limit-reset reads). DISPLAY ONLY.
RESETS_QUERY = "cedar_ember=1&skip_spend=1"
# Hard rules: never request the impression-recording variant of the usage endpoint and never
# touch the endpoint that *spends* a reset. Both are refused here, whatever a caller passes.
_FORBIDDEN_URL_PARTS = ("at_wall", "reset_rate_limits")
_SECRET_LIKE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


class UsageError(RuntimeError):
    """A failed usage fetch. ``code`` is one of auth_expired | rate_limited | network | http_<n> | bad_response."""

    def __init__(self, code: str, message: str = "", retry_after: float | None = None,
                 server_error: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.retry_after = retry_after
        self.server_error = server_error


# --------------------------------------------------------------------------- Claude Code version

_version_cache: str | None = None


def _find_claude() -> str | None:
    configured, selected = paths.configured_claude_binary()
    if configured:
        return selected
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        paths.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _default_runner(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)


def claude_code_version(runner: Callable[[list[str]], Any] | None = None) -> str:
    """Installed Claude Code version (``claude --version``), cached per process.

    ``runner(argv)`` must return an object with ``.returncode`` and ``.stdout`` (like
    ``subprocess.CompletedProcess``). Any failure falls back to ``FALLBACK_CLAUDE_CODE_VERSION``.
    """
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    version = FALLBACK_CLAUDE_CODE_VERSION
    try:
        binary = _find_claude()
        if binary or runner is not None:
            run = runner or _default_runner
            result = run([binary or "claude", "--version"])
            if getattr(result, "returncode", 1) == 0:
                match = re.search(r"\d+\.\d+\.\d+", str(getattr(result, "stdout", "") or ""))
                if match:
                    version = match.group(0)
    except Exception:
        pass  # version is only used for a User-Agent; never fail on it
    _version_cache = version
    return version


def user_agent() -> str:
    return f"claude-code/{claude_code_version()}"


# --------------------------------------------------------------------------- fetch


def _open(opener: Any, request: urllib.request.Request, timeout: float):
    if opener is None:
        return urllib.request.urlopen(request, timeout=timeout)
    if hasattr(opener, "open"):
        return opener.open(request, timeout=timeout)
    return opener(request, timeout=timeout)


def _parse_retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    seconds: float | None = None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - utcnow()).total_seconds()
        except (TypeError, ValueError):
            return None
    if not math.isfinite(seconds):
        return None
    # A server-provided wait is not shortened: a 2-hour instruction must not cause
    # a retry after only 1 hour. Extremely large waits are handled safely by the caller.
    return max(seconds, 0.0)


def _safe_server_error(body: Any) -> str | None:
    """Keep only known response types; never keep server free text or credential values."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    code = error.get("type") or error.get("code") if isinstance(error, dict) else error
    return code if isinstance(code, str) and code in {"rate_limit_error", "rate_limited", "too_many_requests"} else None


def _error_for_status(status: int, headers: Any, body: Any = None) -> UsageError:
    if status in (401, 403):
        return UsageError("auth_expired", f"Usage endpoint rejected the token (HTTP {status}).")
    if status == 429:
        return UsageError(
            "rate_limited", "Usage endpoint rate-limited the request.", _parse_retry_after(headers),
            server_error=_safe_server_error(body),
        )
    return UsageError(f"http_{status}", f"Usage endpoint returned HTTP {status}.")


def _get_json(url: str, access_token: str, *, opener: Any, timeout: float) -> dict:
    """GET ``url`` with the OAuth headers and return the decoded JSON object. Raises ``UsageError``.

    Always a GET, and never a URL that records an offer impression or spends a reset.
    """
    lowered = url.lower()
    if any(part in lowered for part in _FORBIDDEN_URL_PARTS):
        raise ValueError("refusing to request an endpoint that has side effects on the account")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": paths.OAUTH_BETA_HEADER,
            "User-Agent": user_agent(),
        },
        method="GET",
    )
    response = None
    try:
        response = _open(opener, request, timeout)
        status = getattr(response, "status", None)
        if status is None and hasattr(response, "getcode"):
            status = response.getcode()
        body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read(8192) if exc.code == 429 else None
        except Exception:
            error_body = None
        error = _error_for_status(exc.code, getattr(exc, "headers", None), error_body)
        try:
            exc.close()
        except Exception:  # pragma: no cover - defensive
            pass
        raise error from None
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise UsageError("network", f"Could not reach the usage endpoint ({type(exc).__name__}).") from None
    finally:
        if response is not None and hasattr(response, "close"):
            try:
                response.close()
            except Exception:  # pragma: no cover - defensive
                pass

    if status not in (None, 200):
        raise _error_for_status(int(status), getattr(response, "headers", None), body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
        raise UsageError("bad_response", "Usage endpoint returned an unreadable body.")
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError("bad_response", "Usage endpoint did not return JSON.") from None
    if not isinstance(data, dict):
        raise UsageError("bad_response", "Usage endpoint returned a non-object JSON value.")
    return data


def fetch_usage_raw(access_token: str, *, opener: Any = None, timeout: float = 10) -> dict:
    """GET the usage endpoint and return the decoded JSON object. Raises ``UsageError``."""
    return _get_json(paths.USAGE_URL, access_token, opener=opener, timeout=timeout)


def fetch_resets_raw(access_token: str, *, opener: Any = None, timeout: float = 10) -> dict:
    """GET the usage endpoint with ``?cedar_ember=1&skip_spend=1`` (redeemable limit resets).

    Read-only. This is the request Claude Code's ``/limit-reset`` makes to *list* resets; it never
    claims one. Raises ``UsageError`` like ``fetch_usage_raw``.
    """
    return _get_json(f"{paths.USAGE_URL}?{RESETS_QUERY}", access_token, opener=opener, timeout=timeout)


# --------------------------------------------------------------------------- primitives


def _num(value: Any) -> float | None:
    """A finite float from int/float/numeric string, else None (bool is not a number here)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().rstrip("%"))
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_datetime(value: Any) -> datetime | None:
    """ISO-8601 (``Z`` or offset; naive means UTC) or epoch seconds/milliseconds -> aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if re.fullmatch(r"-?\d+(\.\d+)?", text) and len(text) >= 9:
                value = float(text)
            else:
                if text[-1] in "Zz":
                    text = text[:-1] + "+00:00"
                parsed = datetime.fromisoformat(text)
                return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            seconds = float(value)
            if not math.isfinite(seconds):
                return None
            if abs(seconds) > 1e11:  # milliseconds
                seconds /= 1000.0
            return datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _scrub(value: Any) -> Any:
    """Copy of a JSON value with anything that looks like an Anthropic token redacted (defence in depth)."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_LIKE.sub("[redacted]", value)
    return value


# --------------------------------------------------------------------------- parse


def humanize_key(key: str) -> str:
    """``claude_code`` -> ``Claude Code``: words capitalised, underscores gone (``5h`` stays ``5h``)."""
    words = [w for w in re.split(r"[_\s]+", key.strip()) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or key


def window_label(key: str) -> str:
    """A readable label for a usage window key.

    Known keys use ``WINDOW_LABELS``. Anything else the server invents (``cinder_cove``,
    ``seven_day_omelette``) is humanised; the ``seven_day_`` / ``five_hour_`` prefixes follow the existing
    ``Weekly · X`` / ``Session (5h) · X`` pattern. Composite keys such as ``limit:weekly:Fable`` are
    already built from display names and are returned as they are.
    """
    known = WINDOW_LABELS.get(key)
    if known:
        return known
    if ":" in key:
        return key
    for prefix, label in (("seven_day_", "Weekly"), ("five_hour_", "Session (5h)")):
        if key.startswith(prefix) and key[len(prefix):].strip("_"):
            return f"{label} · {humanize_key(key[len(prefix):])}"
    return humanize_key(key)


def _window_from(key: str, label: str, data: dict, *, severity: str | None = None) -> UsageWindow | None:
    utilization = _num(data.get("utilization"))
    resets_at = parse_datetime(data.get("resets_at"))
    if utilization is None and resets_at is None:
        return None
    return UsageWindow(
        key=key,
        label=label,
        utilization=utilization,
        resets_at=resets_at,
        severity=severity or _text(data.get("severity")),
    )


def _promo_from(key: str, data: dict, now: datetime) -> PromoCredit:
    limit = _num(data.get("limit_dollars"))
    used = _num(data.get("used_dollars"))
    remaining = _num(data.get("remaining_dollars"))
    if remaining is None and limit is not None and used is not None:
        remaining = max(limit - used, 0.0)
    utilization = _num(data.get("utilization"))
    if utilization is None and limit and used is not None and limit > 0:
        utilization = used / limit * 100.0

    spans: list[tuple[datetime | None, datetime | None]] = []
    periods = data.get("periods")
    if isinstance(periods, list):
        for period in periods:
            if isinstance(period, dict):
                spans.append((parse_datetime(period.get("starts_at")), parse_datetime(period.get("ends_at"))))
    current_end = next(
        (end for start, end in spans if end is not None and (start is None or start <= now) and now < end),
        None,
    )
    all_ends = [end for _, end in spans if end is not None]
    resets_at = parse_datetime(data.get("resets_at")) or current_end
    ends_at = parse_datetime(data.get("ends_at")) or (max(all_ends) if all_ends else None)

    return PromoCredit(
        key=key,
        label=_text(data.get("label")) or humanize_key(key),
        limit_dollars=limit,
        used_dollars=used,
        remaining_dollars=remaining,
        utilization=utilization,
        resets_at=resets_at,
        ends_at=ends_at,
    )


def _has_dollar_amount(data: dict) -> bool:
    """Does this block hold a real dollar amount? Plain windows carry ``limit_dollars: null`` and friends
    so the mere presence of a ``*_dollars`` key says nothing."""
    return any(isinstance(k, str) and k.endswith("_dollars") and _num(v) is not None for k, v in data.items())


def _scope_names(scope: Any) -> list[str]:
    """display_name of ``scope.model`` and ``scope.surface`` (whichever are present)."""
    names: list[str] = []
    if isinstance(scope, dict):
        for part in ("model", "surface"):
            node = scope.get(part)
            if isinstance(node, dict):
                name = _text(node.get("display_name")) or _text(node.get("name"))
                if name:
                    names.append(name)
    return names


def _kind_label(kind: str) -> str:
    lowered = kind.lower()
    if "five" in lowered or "session" in lowered or "5h" in lowered:
        return "Session (5h)"
    if "seven" in lowered or "week" in lowered:
        return "Weekly"
    if "month" in lowered:
        return "Monthly"
    if "day" in lowered:
        return "Daily"
    return kind.replace("_", " ").strip().capitalize() or "Limit"


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _decimal_places(value: Any) -> int | None:
    """A minor-unit exponent (``2`` for USD): a whole number from 0 to ``MAX_DECIMAL_PLACES``, else None."""
    places = _count(value)
    return places if places is not None and places <= MAX_DECIMAL_PLACES else None


def _extra_usage_from(data: dict) -> ExtraUsage:
    limit = _num(data.get("monthly_limit"))
    used = _num(data.get("used_credits"))
    utilization = _num(data.get("utilization"))
    if utilization is None and limit and used is not None and limit > 0:
        utilization = used / limit * 100.0
    return ExtraUsage(
        enabled=bool(data.get("is_enabled")),
        monthly_limit=limit,
        used_credits=used,
        utilization=utilization,
        currency=_text(data.get("currency")),
        disabled_reason=_text(data.get("disabled_reason")),
        spend_limit_reached=_opt_bool(data.get("spend_limit_reached")),
        user_disabled=_opt_bool(data.get("user_disabled")),
        decimal_places=_decimal_places(data.get("decimal_places")),
    )


def _money_node(node: Any) -> tuple[float | None, int | None, str | None]:
    """(amount_minor, exponent, currency) of a ``spend`` money node ``{amount_minor, currency, exponent}``."""
    if not isinstance(node, dict):
        return None, None, None
    return _num(node.get("amount_minor")), _decimal_places(node.get("exponent")), _text(node.get("currency"))


def _extra_usage_from_spend(data: dict) -> ExtraUsage | None:
    """Build ``ExtraUsage`` from the newer ``spend`` block (used only when ``extra_usage`` is absent).

    ``spend`` has no "limit reached" or "user disabled" flags, so those stay None rather than guessed
    (a caller can compare used and limit itself). Returns None when the block holds nothing useful.
    """
    used, used_exp, used_cur = _money_node(data.get("used"))
    limit, limit_exp, limit_cur = _money_node(data.get("limit"))
    enabled = _opt_bool(data.get("enabled"))
    if used is None and limit is None and enabled is None:
        return None
    exponent = limit_exp if limit_exp is not None else used_exp
    if used is not None and used_exp is not None and exponent is not None and used_exp != exponent:
        used = used * 10.0 ** (exponent - used_exp)  # bring both amounts to the same minor unit
    utilization = _num(data.get("percent"))
    if utilization is None and limit and used is not None and limit > 0:
        utilization = used / limit * 100.0
    return ExtraUsage(
        enabled=bool(enabled),
        monthly_limit=limit,
        used_credits=used,
        utilization=utilization,
        currency=used_cur or limit_cur,
        disabled_reason=_text(data.get("disabled_reason")),
        decimal_places=exponent,
    )


def parse_usage(raw: dict, account_id: str, fetched_at: datetime | None = None) -> UsageSnapshot:
    """Normalise a usage response. Never raises; an unusable payload yields ``ok=False, error="bad_response"``."""
    fetched_at = fetched_at or utcnow()
    if not isinstance(raw, dict):
        return UsageSnapshot(account_id=account_id, fetched_at=fetched_at, ok=False, error="bad_response")

    windows: list[UsageWindow] = []
    promos: list[PromoCredit] = []
    extra: ExtraUsage | None = None

    # 1. The documented windows, in a stable order.
    for key, label in WINDOW_LABELS.items():
        data = raw.get(key)
        if isinstance(data, dict):
            try:
                window = _window_from(key, label, data)
            except Exception:
                window = None
            if window:
                windows.append(window)

    # 2. Anything else the server added at top level (codenames and passthrough keys).
    for key, data in raw.items():
        if key in WINDOW_LABELS or key in _RESERVED_KEYS or not isinstance(data, dict):
            continue
        try:
            if _has_dollar_amount(data):
                promos.append(_promo_from(str(key), data, fetched_at))
            elif "utilization" in data or "resets_at" in data:
                window = _window_from(str(key), _text(data.get("label")) or window_label(str(key)), data)
                if window:
                    windows.append(window)
        except Exception:
            continue

    # 3. Scoped limits (per model / per surface) become extra windows.
    limits = raw.get("limits")
    if isinstance(limits, list):
        for row in limits:
            try:
                _merge_limit_row(windows, row)
            except Exception:
                continue

    # 4. Paid extra usage: `extra_usage` wins; the newer `spend` block is only a fallback.
    extra_raw = raw.get("extra_usage")
    if isinstance(extra_raw, dict):
        try:
            extra = _extra_usage_from(extra_raw)
        except Exception:
            extra = None
    spend_raw = raw.get("spend")
    if extra is None and isinstance(spend_raw, dict):
        try:
            extra = _extra_usage_from_spend(spend_raw)
        except Exception:
            extra = None

    # 5. "What is using your weekly limit".
    breakdown, breakdown_as_of = parse_breakdown(raw.get("seven_day_breakdown"))

    return UsageSnapshot(
        account_id=account_id,
        fetched_at=fetched_at,
        ok=True,
        windows=tuple(windows),
        extra_usage=extra,
        promos=tuple(promos),
        breakdown=breakdown,
        breakdown_as_of=breakdown_as_of,
        raw=_scrub(raw),
    )


def _carry_headline_severity(windows: list[UsageWindow], row: dict) -> None:
    """Copy an unscoped limit row's ``severity`` onto the headline window it describes, when that window
    has none of its own."""
    kind = _text(row.get("kind"))
    key = _HEADLINE_FOR_LIMIT_KIND.get(kind.lower()) if kind else None
    severity = _text(row.get("severity"))
    if key is None or severity is None:
        return
    for index, existing in enumerate(windows):
        if existing.key == key:
            if existing.severity is None:
                windows[index] = replace(existing, severity=severity)
            return


def _merge_limit_row(windows: list[UsageWindow], row: Any) -> None:
    if not isinstance(row, dict):
        return
    names = _scope_names(row.get("scope"))
    if not names:
        _carry_headline_severity(windows, row)  # unscoped limits duplicate five_hour / seven_day: no new window
        return
    kind = _text(row.get("kind")) or "limit"
    percent = _num(row.get("percent"))
    resets_at = parse_datetime(row.get("resets_at"))
    if percent is None and resets_at is None:
        return
    severity = _text(row.get("severity"))
    key = f"limit:{kind}:{'/'.join(names)}"
    label = f"{_kind_label(kind)} · {' · '.join(names)}"

    for index, existing in enumerate(windows):
        if existing.key == key:
            return
        if existing.label.casefold() == label.casefold():
            # Same thing already reported by a dedicated key (e.g. seven_day_opus): keep it, add severity.
            if existing.severity is None and severity:
                windows[index] = replace(existing, severity=severity)
            return
    windows.append(UsageWindow(key=key, label=label, utilization=percent, resets_at=resets_at, severity=severity))


# --------------------------------------------------------------------------- weekly breakdown


def _breakdown_row_from(data: Any) -> BreakdownRow | None:
    """One breakdown row, or None when malformed (not an object, no key, or no number to show)."""
    if not isinstance(data, dict):
        return None
    key = _text(data.get("key"))
    percent = _num(data.get("percent"))
    of_limit = _num(data.get("percent_of_limit"))
    if key is None or (percent is None and of_limit is None):
        return None
    return BreakdownRow(
        key=key,
        display_name=_text(data.get("display_name")) or humanize_key(key),
        percent=percent,
        percent_of_limit=of_limit,
    )


def parse_breakdown(block: Any) -> tuple[tuple[BreakdownRow, ...], datetime | None]:
    """Parse ``seven_day_breakdown`` = ``{as_of, window_started_at, rows: [...]}``. Never raises.

    Returns ``(rows, as_of)``; ``((), None)`` when the block is absent, not an object or has no usable
    row. Malformed rows are skipped. ``window_started_at`` is not kept.
    """
    if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
        return (), None
    rows: list[BreakdownRow] = []
    for item in block["rows"]:
        try:
            row = _breakdown_row_from(item)
        except Exception:
            row = None
        if row is not None:
            rows.append(row)
    if not rows:
        return (), None
    return tuple(rows), parse_datetime(block.get("as_of"))


# --------------------------------------------------------------------------- redeemable resets

_RESET_TYPES = frozenset({"session", "weekly", "full"})
_RESET_DEFAULT_LABELS = {"session": "Session reset", "weekly": "Weekly reset", "full": "Full reset"}


def _count(value: Any) -> int | None:
    """A non-negative whole number (``2`` or ``2.0``), else None. Bools and strings are not counts."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _flag(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _reset_grant_from(index: int, data: Any) -> ResetGrant | None:
    """One grant, or None when it is malformed (not an object, or ``resets_left`` is not an int >= 0)."""
    if not isinstance(data, dict):
        return None
    left = _count(data.get("resets_left"))
    if left is None:
        return None
    total = _count(data.get("resets_total"))
    raw_type = _text(data.get("type"))
    kind = raw_type.lower() if raw_type and raw_type.lower() in _RESET_TYPES else None
    raw_id = data.get("id")
    grant_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) and str(raw_id) else f"grant:{index}"
    return ResetGrant(
        id=grant_id,
        label=_text(data.get("label")) or _RESET_DEFAULT_LABELS.get(kind or "", "Limit reset"),
        type=kind,
        resets_total=max(total if total is not None else left, left),
        resets_left=left,
        usable_now=_flag(data.get("usable_now"), False),
        use_requires_limit=_flag(data.get("use_requires_limit"), True),
        gives_back_weekly=_flag(data.get("gives_back_weekly"), False),
        paused=_flag(data.get("paused"), False),
        starts_at=parse_datetime(data.get("starts_at")),
        ends_at=parse_datetime(data.get("ends_at")),
        unavailable_reason=_text(data.get("unavailable_reason")),
    )


def parse_resets(block: dict | None) -> ResetStatus | None:
    """Parse the ``cedar_ember`` block (redeemable limit resets). Never raises.

    None when ``block`` is absent or not an object. Malformed grants are skipped; every field other
    than a grant's ``resets_left`` is optional and falls back to a default. ``eligible`` defaults to
    "has at least one grant" when the server omits it. Fields we do not display (``exhausted``,
    ``clears``, ``percent_used``, ``blocking``, ``next_grant_id``, ``event_props``) are ignored.

    The cache stores a ``ResetStatus`` in this same shape, so ``parse_resets`` also reads it back.
    """
    if not isinstance(block, dict):
        return None
    grants: list[ResetGrant] = []
    rows = block.get("grants")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            try:
                grant = _reset_grant_from(index, row)
            except Exception:
                grant = None
            if grant is not None:
                grants.append(grant)
    eligible = block.get("eligible")
    return ResetStatus(
        eligible=eligible if isinstance(eligible, bool) else bool(grants),
        ineligible_reason=_text(block.get("ineligible_reason")),
        at_limit=_flag(block.get("at_limit"), False),
        grants=tuple(grants),
        weekly_resets_at=parse_datetime(block.get("weekly_resets_at")),
        cooldown_until=parse_datetime(block.get("cooldown_until")),
    )


# --------------------------------------------------------------------------- formatting


def format_reset(resets_at: datetime | None, now: datetime | None = None) -> str:
    """Time until a reset: ``now``, ``42m``, ``3h 05m``, ``2d 4h``; ``—`` when unknown."""
    if resets_at is None:
        return "—"
    now = now or utcnow()
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    seconds = int((resets_at - now).total_seconds())
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{max(minutes, 1)}m"


_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€"}
_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "XPF", "PYG", "RWF"})


def format_money(minor_units: float | None, currency: str | None, decimal_places: int | None = None) -> str:
    """Format an amount given in minor units (cents for USD): ``$12.34``, ``€5.00``, ``12.34 GBP``.

    ``decimal_places`` is the server's minor-unit exponent (``ExtraUsage.decimal_places``). When it is
    missing or not a whole number from 0 to 6, the exponent is derived from the currency code.
    """
    number = _num(minor_units)
    if number is None:
        return "—"
    code = (currency or "USD").strip().upper() or "USD"
    if isinstance(decimal_places, int) and not isinstance(decimal_places, bool) and 0 <= decimal_places <= MAX_DECIMAL_PLACES:
        exponent = decimal_places
    else:
        exponent = 0 if code in _ZERO_DECIMAL_CURRENCIES else 2
    try:
        amount = Decimal(repr(number)) / (Decimal(10) ** exponent)
        amount = amount.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # pragma: no cover - finite floats always convert
        return "—"
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):,.{exponent}f}"
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{sign}{symbol}{text}" if symbol else f"{sign}{text} {code}"


def format_percent(value: float | None) -> str:
    """``42%`` (half rounds up) or ``—`` when unknown."""
    number = _num(value)
    if number is None:
        return "—"
    return f"{int(math.floor(max(number, 0.0) + 0.5))}%"
