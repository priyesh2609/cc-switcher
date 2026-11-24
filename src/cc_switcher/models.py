"""Plain data types shared by every module. No I/O here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_account_id(account_uuid: str, organization_uuid: str) -> str:
    """Stable id for one login: the same email can belong to several orgs (e.g. personal Max + a Team seat)."""
    return f"{account_uuid}:{organization_uuid}"


def _from_dict(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, ignoring unknown keys (forward compatible)."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


# --------------------------------------------------------------------------- accounts


@dataclass
class Account:
    """One saved Claude login. Metadata only; tokens live in Keychain under ``saved_login_service``."""

    id: str  # make_account_id(account_uuid, organization_uuid)
    email: str
    account_uuid: str
    organization_uuid: str
    organization_name: str = ""
    display_name: str = ""
    subscription_type: str = ""  # e.g. "max", "pro", "team", "enterprise"
    rate_limit_tier: str = ""
    label: str = ""  # user nickname, e.g. "work"
    oauth_account: dict[str, Any] | None = None  # snapshot of ~/.claude.json "oauthAccount"
    added_at: str = ""  # ISO-8601 UTC
    last_used_at: str = ""  # ISO-8601 UTC, set when switched to

    @property
    def name(self) -> str:
        """What to show a human: the label, else the email, else a short id (email can be unknown
        when Claude Code's profile cache was stale at import time)."""
        return self.label or self.email or f"account {self.account_uuid[:8]}"

    @property
    def keychain_service(self) -> str:
        from cc_switcher.paths import saved_login_service

        return saved_login_service(self.id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return _from_dict(cls, data)


@dataclass
class Settings:
    poll_interval_seconds: int = 300
    notifications_enabled: bool = True
    # Notify once each time a window crosses one of these percentages.
    alert_thresholds: list[int] = field(default_factory=lambda: [80, 95, 100])
    auto_switch_enabled: bool = False
    # Switch away from the active account when any watched window reaches this percent.
    auto_switch_threshold: float = 98.0
    auto_switch_windows: list[str] = field(default_factory=lambda: ["five_hour", "seven_day"])
    # Show this in the menu bar title: "none" | "session" | "weekly" | "both"
    menubar_title: str = "session"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return _from_dict(cls, data)


# --------------------------------------------------------------------------- usage


WINDOW_LABELS: dict[str, str] = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
    "seven_day_oauth_apps": "Weekly · Apps",
    "seven_day_cowork": "Weekly · Cowork",
}


@dataclass(frozen=True)
class UsageWindow:
    key: str  # "five_hour", "seven_day", "seven_day_opus", ... or "limit:<kind>:<scope>"
    label: str
    utilization: float | None  # percent, 0-100 (can exceed 100)
    resets_at: datetime | None
    severity: str | None = None  # server-provided, e.g. "approaching", "used_up"


@dataclass(frozen=True)
class ExtraUsage:
    """Paid extra usage ("usage credits") for the billing period.

    Amounts are in *minor* units of ``currency`` (cents for USD), as the endpoint sends them.
    """

    enabled: bool
    monthly_limit: float | None
    used_credits: float | None
    utilization: float | None
    currency: str | None = None
    disabled_reason: str | None = None
    # The server may report that a cap was reached even when extra usage is off.
    spend_limit_reached: bool | None = None
    user_disabled: bool | None = None
    decimal_places: int | None = None  # minor-unit exponent (2 for USD); None -> assume 2

    @property
    def remaining(self) -> float | None:
        if self.monthly_limit is None or self.used_credits is None:
            return None
        return max(self.monthly_limit - self.used_credits, 0.0)


@dataclass(frozen=True)
class PromoCredit:
    """A dollar-denominated grant the endpoint reports (e.g. limited-time promo credit)."""

    key: str
    label: str
    limit_dollars: float | None
    used_dollars: float | None
    remaining_dollars: float | None
    utilization: float | None
    resets_at: datetime | None
    ends_at: datetime | None = None


@dataclass(frozen=True)
class BreakdownRow:
    """One row of the server's weekly breakdown ("what is using your weekly limit")."""

    key: str
    display_name: str
    percent: float | None  # share of total usage
    percent_of_limit: float | None  # share of the weekly limit


@dataclass(frozen=True)
class ResetGrant:
    """One redeemable limit-reset grant (Claude Code's `/limit-reset`, server block ``cedar_ember``)."""

    id: str
    label: str
    type: str | None  # "session" | "weekly" | "full" | None
    resets_total: int
    resets_left: int
    usable_now: bool = False
    use_requires_limit: bool = True  # only usable while you're at a usage limit
    gives_back_weekly: bool = False
    paused: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None  # grant expiry ("use by")
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ResetStatus:
    """Redeemable resets for an account. Display only: cc-switcher never claims a reset."""

    eligible: bool
    ineligible_reason: str | None = None
    at_limit: bool = False
    grants: tuple[ResetGrant, ...] = ()
    weekly_resets_at: datetime | None = None
    cooldown_until: datetime | None = None

    @property
    def resets_left(self) -> int:
        return sum(g.resets_left for g in self.grants if not g.paused)

    @property
    def next_expiry(self) -> datetime | None:
        ends = [g.ends_at for g in self.grants if g.ends_at and g.resets_left > 0 and not g.paused]
        return min(ends, default=None)


@dataclass(frozen=True)
class UsageSnapshot:
    account_id: str
    fetched_at: datetime
    ok: bool
    # None when ok. Otherwise one of: "no_credentials", "auth_expired", "token_stale",
    # "rate_limited", "network", "http_<code>", "bad_response".
    error: str | None = None
    windows: tuple[UsageWindow, ...] = ()
    extra_usage: ExtraUsage | None = None
    promos: tuple[PromoCredit, ...] = ()
    resets: ResetStatus | None = None  # None = unknown / not offered to this account
    breakdown: tuple[BreakdownRow, ...] = ()  # from raw["seven_day_breakdown"]["rows"]
    breakdown_as_of: datetime | None = None
    raw: dict[str, Any] | None = None

    def window(self, key: str) -> UsageWindow | None:
        return next((w for w in self.windows if w.key == key), None)

    @property
    def session(self) -> UsageWindow | None:
        return self.window("five_hour")

    @property
    def weekly(self) -> UsageWindow | None:
        return self.window("seven_day")

    def max_utilization(self, keys: list[str] | None = None) -> float | None:
        values = [
            w.utilization
            for w in self.windows
            if w.utilization is not None and (keys is None or w.key in keys)
        ]
        return max(values, default=None)
