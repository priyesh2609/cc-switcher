from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from cc_switcher import autoswitch
from cc_switcher.models import Account, Settings, UsageSnapshot, UsageWindow

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """Snapshots in this file are stamped NOW, so make NOW the module's default clock."""
    monkeypatch.setattr(autoswitch, "utcnow", lambda: NOW)


def account(name: str) -> Account:
    return Account(id=f"id-{name}", email=f"{name}@example.com", account_uuid=f"u-{name}", organization_uuid="org", label=name)


def snap(account_id: str, session=None, weekly=None, extra: dict[str, float] | None = None, ok=True, error=None, age=0.0) -> UsageSnapshot:
    windows = []
    if session is not None:
        windows.append(UsageWindow("five_hour", "Session (5h)", session, None))
    if weekly is not None:
        windows.append(UsageWindow("seven_day", "Weekly", weekly, None))
    for key, value in (extra or {}).items():
        windows.append(UsageWindow(key, key, value, None))
    return UsageSnapshot(
        account_id=account_id, fetched_at=NOW - timedelta(seconds=age), ok=ok, error=error, windows=tuple(windows)
    )


def settings(**kw) -> Settings:
    kw.setdefault("auto_switch_enabled", True)
    return Settings(**kw)  # defaults: threshold 98, windows five_hour + seven_day


A, B, C, D = (account(n) for n in "abcd")


# --------------------------------------------------------------------------- should_switch


def test_should_switch_when_a_watched_window_reaches_the_threshold():
    assert autoswitch.should_switch(snap("id-a", session=98.0, weekly=10), settings()) is True
    assert autoswitch.should_switch(snap("id-a", session=10, weekly=99.5), settings()) is True


def test_should_not_switch_just_below_the_threshold():
    assert autoswitch.should_switch(snap("id-a", session=97.99, weekly=97.99), settings()) is False


def test_should_not_switch_when_auto_switch_is_off():
    assert autoswitch.should_switch(snap("id-a", session=100, weekly=100), settings(auto_switch_enabled=False)) is False


def test_should_not_switch_on_unknown_or_errored_data():
    assert autoswitch.should_switch(None, settings()) is False
    assert autoswitch.should_switch(snap("id-a", ok=False, error="network"), settings()) is False
    # even if a stale errored snapshot still carries numbers, an error is never a trigger
    errored_with_numbers = snap("id-a", session=100, weekly=100, ok=False, error="rate_limited")
    assert autoswitch.should_switch(errored_with_numbers, settings()) is False
    assert autoswitch.should_switch(snap("id-a"), settings()) is False  # ok but no windows at all


def test_should_only_look_at_the_watched_windows():
    s = snap("id-a", session=10, weekly=10, extra={"seven_day_opus": 100})
    assert autoswitch.should_switch(s, settings()) is False
    assert autoswitch.should_switch(s, settings(auto_switch_windows=["seven_day_opus"])) is True


def test_should_switch_honours_a_custom_threshold():
    s = snap("id-a", session=60, weekly=10)
    assert autoswitch.should_switch(s, settings(auto_switch_threshold=60.0)) is True
    assert autoswitch.should_switch(s, settings(auto_switch_threshold=61.0)) is False


def test_should_switch_ignores_windows_with_unknown_utilization():
    s = UsageSnapshot("id-a", NOW, True, windows=(UsageWindow("five_hour", "Session (5h)", None, None),))
    assert autoswitch.should_switch(s, settings()) is False


# --------------------------------------------------------------------------- choose_target: ranking


def test_prefers_the_lowest_weekly_then_the_lowest_session():
    snapshots = {
        "id-a": snap("id-a", session=99, weekly=99),
        "id-b": snap("id-b", session=10, weekly=60),
        "id-c": snap("id-c", session=90, weekly=20),  # lowest weekly wins despite a busy session
        "id-d": snap("id-d", session=5, weekly=20),  # same weekly as c, calmer session
    }
    assert autoswitch.choose_target("id-a", [A, B, C, D], snapshots, settings()) is D


def test_ties_keep_registry_order():
    snapshots = {"id-b": snap("id-b", session=30, weekly=30), "id-c": snap("id-c", session=30, weekly=30)}
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is B
    assert autoswitch.choose_target("id-a", [A, C, B], snapshots, settings()) is C


def test_the_active_account_is_never_its_own_target():
    snapshots = {"id-a": snap("id-a", session=0, weekly=0), "id-b": snap("id-b", session=50, weekly=50)}
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings()) is B


def test_without_an_active_account_every_account_is_a_candidate():
    snapshots = {"id-a": snap("id-a", session=0, weekly=0), "id-b": snap("id-b", session=50, weekly=50)}
    assert autoswitch.choose_target(None, [A, B], snapshots, settings()) is A


# --------------------------------------------------------------------------- choose_target: exclusions


@pytest.mark.parametrize(
    "error",
    ["no_credentials", "auth_expired", "token_stale", "rate_limited", "network", "http_500", "bad_response", "keychain_error", "busy"],
)
def test_errored_snapshots_are_never_chosen(error):
    snapshots = {"id-b": snap("id-b", ok=False, error=error)}
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings()) is None


def test_errored_snapshot_is_skipped_even_if_it_has_attractive_leftover_numbers():
    snapshots = {
        "id-b": snap("id-b", session=0, weekly=0, ok=False, error="rate_limited"),
        "id-c": snap("id-c", session=40, weekly=40),
    }
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is C


def test_accounts_without_a_snapshot_are_never_chosen():
    assert autoswitch.choose_target("id-a", [A, B, C], {"id-c": snap("id-c", session=40, weekly=40)}, settings()) is C
    assert autoswitch.choose_target("id-a", [A, B], {}, settings()) is None


def test_ok_snapshot_with_no_watched_data_is_unknown_and_never_chosen():
    snapshots = {
        "id-b": snap("id-b"),  # no windows
        "id-c": snap("id-c", extra={"seven_day_opus": 1}),  # only an unwatched window
    }
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is None


def test_candidates_at_or_over_the_threshold_on_any_watched_window_are_excluded():
    snapshots = {
        "id-b": snap("id-b", session=98.0, weekly=1),  # exactly at the threshold: out
        "id-c": snap("id-c", session=1, weekly=98.1),
        "id-d": snap("id-d", session=97.9, weekly=97.9),
    }
    assert autoswitch.choose_target("id-a", [A, B, C, D], snapshots, settings()) is D


def test_returns_none_when_everyone_is_exhausted_or_there_are_no_accounts():
    snapshots = {"id-b": snap("id-b", session=100, weekly=100), "id-c": snap("id-c", session=100, weekly=99)}
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is None
    assert autoswitch.choose_target("id-a", [], snapshots, settings()) is None


# --------------------------------------------------------------------------- choose_target: partial data


def test_a_window_the_server_left_out_counts_as_unused_for_ranking():
    # five_hour is null when no session is running: that account is *fresher*, not unknown.
    snapshots = {
        "id-b": snap("id-b", session=30, weekly=30),
        "id-c": snap("id-c", weekly=30),  # no five_hour window reported
    }
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is C


def test_an_account_reporting_only_the_session_window_can_be_chosen():
    snapshots = {"id-b": snap("id-b", session=20)}
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings()) is B


def test_only_watched_windows_disqualify_a_candidate():
    snapshots = {"id-b": snap("id-b", session=10, weekly=99.9)}
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings()) is None
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings(auto_switch_windows=["five_hour"])) is B


def test_unwatched_windows_do_not_disqualify_a_candidate():
    snapshots = {"id-b": snap("id-b", session=10, weekly=10, extra={"seven_day_opus": 100})}
    assert autoswitch.choose_target("id-a", [A, B], snapshots, settings()) is B


def test_a_custom_threshold_applies_to_candidates():
    snapshots = {"id-b": snap("id-b", session=60, weekly=60), "id-c": snap("id-c", session=40, weekly=40)}
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings(auto_switch_threshold=50.0)) is C
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings(auto_switch_threshold=30.0)) is None


# --------------------------------------------------------------------------- scenario + purity


def test_scenario_active_account_exhausted_one_peer_down_one_ok():
    """work is at 99% weekly; home could not be fetched; side is at 40%/30%: switch to side."""
    work, home, side = account("work"), account("home"), account("side")
    snapshots = {
        work.id: snap(work.id, session=70, weekly=99),
        home.id: snap(home.id, ok=False, error="network"),
        side.id: snap(side.id, session=30, weekly=40),
    }
    cfg = settings()

    assert autoswitch.should_switch(snapshots[work.id], cfg) is True
    assert autoswitch.choose_target(work.id, [work, home, side], snapshots, cfg) is side


def test_decisions_do_not_mutate_their_inputs():
    accounts = [A, B, C]
    snapshots = {"id-b": snap("id-b", session=10, weekly=10), "id-c": snap("id-c", session=5, weekly=5)}
    cfg = settings()
    before = (copy.deepcopy(accounts), copy.deepcopy(snapshots), copy.deepcopy(cfg))

    autoswitch.choose_target("id-a", accounts, snapshots, cfg)
    autoswitch.should_switch(snapshots["id-b"], cfg)

    assert (accounts, snapshots, cfg) == before


# --------------------------------------------------------------------------- never act on old numbers


def test_default_ages_are_15_and_30_minutes():
    assert (autoswitch.ACTIVE_MAX_AGE_SECONDS, autoswitch.CANDIDATE_MAX_AGE_SECONDS) == (900.0, 1800.0)


def test_should_switch_active_snapshot_age_boundary():
    """The active account is at 99% weekly; whether we act depends only on how old that reading is."""
    at_limit = settings()
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=0), at_limit) is True
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=899.9), at_limit) is True
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=900), at_limit) is True  # exactly 15 min: still fresh
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=900.1), at_limit) is False
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=3 * 3600), at_limit) is False


def test_choose_target_candidate_age_boundary():
    accounts = [A, B]
    for age, expected in [(0, B), (1799.9, B), (1800, B), (1800.1, None), (6 * 3600, None)]:
        snapshots = {"id-b": snap("id-b", session=10, weekly=10, age=age)}
        assert autoswitch.choose_target("id-a", accounts, snapshots, settings()) is expected, age


def test_a_stale_candidate_loses_to_a_fresh_worse_one():
    """b looks best (10%) but that reading is 40 minutes old; c at 60% was read a minute ago."""
    snapshots = {
        "id-b": snap("id-b", session=10, weekly=10, age=2400),
        "id-c": snap("id-c", session=60, weekly=60, age=60),
    }
    assert autoswitch.choose_target("id-a", [A, B, C], snapshots, settings()) is C


def test_active_and_candidate_limits_are_independent():
    # 20 minutes old: too old to trigger a switch away from the active account ...
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=1200), settings()) is False
    # ... but still recent enough for a candidate.
    assert autoswitch.choose_target("id-a", [A, B], {"id-b": snap("id-b", weekly=10, age=1200)}, settings()) is B


def test_custom_max_age_applies_to_both_functions():
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=60), settings(), max_age_seconds=59) is False
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=60), settings(), max_age_seconds=60) is True
    candidate = {"id-b": snap("id-b", weekly=10, age=3000)}
    assert autoswitch.choose_target("id-a", [A, B], candidate, settings(), max_age_seconds=2999) is None
    assert autoswitch.choose_target("id-a", [A, B], candidate, settings(), max_age_seconds=3000) is B


def test_max_age_none_disables_the_check():
    ancient = 30 * 86400
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=ancient), settings(), max_age_seconds=None) is True
    assert autoswitch.choose_target("id-a", [A, B], {"id-b": snap("id-b", weekly=10, age=ancient)}, settings(), max_age_seconds=None) is B


def test_age_is_measured_against_the_now_argument():
    s = snap("id-a", weekly=99, age=0)  # fetched at NOW
    assert autoswitch.should_switch(s, settings(), now=NOW + timedelta(seconds=900)) is True
    assert autoswitch.should_switch(s, settings(), now=NOW + timedelta(seconds=901)) is False
    candidate = {"id-b": snap("id-b", weekly=10)}
    assert autoswitch.choose_target("id-a", [A, B], candidate, settings(), now=NOW + timedelta(seconds=1800)) is B
    assert autoswitch.choose_target("id-a", [A, B], candidate, settings(), now=NOW + timedelta(seconds=1801)) is None


def test_default_clock_is_the_current_time(monkeypatch):
    s = snap("id-a", weekly=99)
    monkeypatch.setattr(autoswitch, "utcnow", lambda: NOW + timedelta(hours=1))
    assert autoswitch.should_switch(s, settings()) is False


def test_future_and_naive_timestamps_are_handled():
    skewed = snap("id-a", weekly=99, age=-3600)  # "fetched" an hour in the future: clock skew
    assert autoswitch.should_switch(skewed, settings()) is True

    naive = UsageSnapshot("id-a", NOW.replace(tzinfo=None) - timedelta(seconds=100), True,
                          windows=(UsageWindow("seven_day", "Weekly", 99.0, None),))
    assert autoswitch.should_switch(naive, settings()) is True  # naive means UTC, like everywhere else
    assert autoswitch.should_switch(naive, settings(), max_age_seconds=99) is False


def test_staleness_does_not_override_the_other_rules():
    assert autoswitch.should_switch(snap("id-a", weekly=10, age=1), settings()) is False  # fresh but below threshold
    assert autoswitch.should_switch(snap("id-a", weekly=99, ok=False, error="network", age=1), settings()) is False
    assert autoswitch.should_switch(snap("id-a", weekly=99, age=1), settings(auto_switch_enabled=False)) is False
    # a fresh candidate at the threshold is still excluded
    assert autoswitch.choose_target("id-a", [A, B], {"id-b": snap("id-b", weekly=98, age=1)}, settings()) is None


def test_scenario_overnight_idle_active_account_is_not_switched_on_old_numbers():
    """Laptop closed overnight: the cache still says work is at 99%, last read 9 hours ago. Do nothing."""
    work, side = account("work"), account("side")
    snapshots = {
        work.id: snap(work.id, session=40, weekly=99, age=9 * 3600),
        side.id: snap(side.id, session=5, weekly=5, age=9 * 3600),
    }
    assert autoswitch.should_switch(snapshots[work.id], settings()) is False
    assert autoswitch.choose_target(work.id, [work, side], snapshots, settings()) is None
