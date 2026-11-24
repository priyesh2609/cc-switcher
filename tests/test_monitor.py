from __future__ import annotations

import io
import json
import os
import stat
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from cc_switcher import creds, fsutil, keychain, monitor, paths, store, tokens, usage
from cc_switcher.models import (
    Account,
    ExtraUsage,
    PromoCredit,
    UsageSnapshot,
    UsageWindow,
    Settings,
    make_account_id,
)

from conftest import ACCOUNT_A, ACCOUNT_B, SAMPLE_MCP_OAUTH, make_login

T0 = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
FAR_FUTURE_MS = 4_102_444_800_000
EXPIRED_MS = 1_000


@pytest.fixture(autouse=True)
def _no_real_version_probe(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")


def account_for(ident, email, label="") -> Account:
    return Account(
        id=make_account_id(*ident),
        email=email,
        account_uuid=ident[0],
        organization_uuid=ident[1],
        label=label,
    )


ACCT_A = account_for(ACCOUNT_A, "a@example.com", "work")
ACCT_B = account_for(ACCOUNT_B, "b@example.com", "home")


def usage_payload(session=37.0, weekly=62.0) -> dict:
    return {
        "five_hour": {"utilization": session, "resets_at": "2026-11-22T14:59:59.943648+00:00"},
        "seven_day": {"utilization": weekly, "resets_at": "2026-11-27T08:00:00+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2026-11-27T08:00:00Z"},
        "cinder_cove": {"utilization": 5.5, "resets_at": "2026-11-27T08:00:00+00:00"},
        "wattle_ember": {
            "label": "Holiday credit",
            "limit_dollars": 50.0,
            "used_dollars": 12.5,
            "remaining_dollars": 37.5,
            "periods": [{"starts_at": "2026-11-01T00:00:00Z", "ends_at": "2026-11-30T00:00:00Z"}],
        },
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 5000,
            "used_credits": 1234,
            "utilization": 24.68,
            "currency": "USD",
            "disabled_reason": None,
        },
        "limits": [
            {
                "kind": "weekly",
                "percent": 41.0,
                "resets_at": "2026-11-27T08:00:00+00:00",
                "severity": "approaching",
                "is_active": True,
                "scope": {"model": {"display_name": "Fable"}, "surface": None},
            }
        ],
        "future_passthrough": {"anything": True},
    }


# --------------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, body: bytes, status=200):
        self._body, self.status = body, status

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def close(self):
        pass


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", code, "err", headers or {}, io.BytesIO(b"{}"))


class FakeUsageServer:
    """opener= fake. Replies per access token.

    ``calls`` lists the tokens of plain usage requests, ``resets_calls`` those of redeemable-resets
    requests (``?cedar_ember=1``); ``requests`` keeps (method, url) for every request.
    """

    def __init__(self, default=None, cedar=None):
        self.default = usage_payload() if default is None else default
        self.cedar = cedar  # the cedar_ember block returned to resets requests (None = block absent)
        self.replies: dict[str, object] = {}
        self.resets_replies: dict[str, object] = {}
        self.calls: list[str] = []
        self.resets_calls: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.on_call = None

    def __call__(self, request, timeout=None):
        if self.on_call:
            self.on_call()
        token = request.get_header("Authorization").removeprefix("Bearer ")
        self.requests.append((request.get_method(), request.full_url))
        if "cedar_ember=1" in request.full_url:
            self.resets_calls.append(token)
            reply = self.resets_replies.get(token)
            if reply is None:
                reply = {**self.default, **({"cedar_ember": self.cedar} if self.cedar is not None else {})}
        else:
            self.calls.append(token)
            reply = self.replies.get(token, self.default)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, bytes):
            return FakeResponse(reply)
        return FakeResponse(json.dumps(reply).encode())


class FakePost:
    def __init__(self, status=200, body=None, raises=None):
        self.status = status
        self.body = body if body is not None else {
            "access_token": "sk-ant-oat01-REFRESHED",
            "refresh_token": "sk-ant-ort01-ROTATED",
            "expires_in": 28800,
        }
        self.raises = raises
        self.calls = 0

    def __call__(self, url, payload, *, headers, timeout):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.status, self.body


class ForbiddenPost:
    def __call__(self, *args, **kwargs):
        raise AssertionError("a token refresh was attempted")


def lock_is_held() -> bool:
    try:
        with fsutil.app_lock(timeout=0.05):
            return False
    except fsutil.LockTimeout:
        return True


def put_live(fake, login):
    fake.put_json(
        paths.claude_keychain_service(),
        {"claudeAiOauth": login, "mcpOAuth": json.loads(json.dumps(SAMPLE_MCP_OAUTH))},
    )


def put_saved(fake, account, login):
    fake.put_json(account.keychain_service, creds.saved_item(login))


@pytest.fixture
def world(fake_keychain):
    """Two registered accounts: A is live with a valid token, B is saved with a valid token."""
    store.save_accounts([ACCT_A, ACCT_B])
    put_live(fake_keychain, make_login(ACCOUNT_A, "live-a", FAR_FUTURE_MS))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved-a", FAR_FUTURE_MS))
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "saved-b", FAR_FUTURE_MS))
    return fake_keychain


def read_cache_file() -> dict:
    return json.loads(paths.usage_cache_file().read_text())


def history_lines() -> list[dict]:
    path = paths.usage_history_file()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# =========================================================================== snapshot <-> dict


def test_snapshot_round_trips_through_json():
    snap = usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0)
    assert snap.promos and snap.extra_usage and len(snap.windows) > 3

    wire = json.loads(json.dumps(monitor.snapshot_to_dict(snap)))
    back = monitor.snapshot_from_dict(wire)

    assert back == snap
    assert back.raw["future_passthrough"] == {"anything": True}


def test_error_snapshot_round_trips():
    snap = UsageSnapshot(ACCT_A.id, T0, ok=False, error="http_503")
    assert monitor.snapshot_from_dict(json.loads(json.dumps(monitor.snapshot_to_dict(snap)))) == snap


def test_snapshot_dict_is_json_safe_and_uses_iso_strings():
    wire = monitor.snapshot_to_dict(usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0))
    json.dumps(wire)
    assert wire["fetched_at"] == "2026-11-22T10:00:00+00:00"
    assert wire["windows"][0]["resets_at"].endswith("+00:00")


def test_snapshot_from_dict_tolerates_missing_and_garbled_optional_parts():
    minimal = {"account_id": "x", "fetched_at": "2026-11-22T10:00:00+00:00", "ok": True}
    snap = monitor.snapshot_from_dict(minimal)
    assert snap.windows == () and snap.extra_usage is None and snap.promos == () and snap.raw is None

    messy = dict(minimal, windows=[None, {"label": "no key"}, {"key": "five_hour", "utilization": "x"}], promos=[3], extra_usage="x", raw=[1])
    snap = monitor.snapshot_from_dict(messy)
    assert [w.key for w in snap.windows] == ["five_hour"]
    assert snap.windows[0].utilization is None
    assert snap.extra_usage is None and snap.promos == () and snap.raw is None


@pytest.mark.parametrize("bad", [None, [], {}, {"account_id": "x"}, {"fetched_at": "2026-11-22T10:00:00Z"}, {"account_id": "x", "fetched_at": "never"}])
def test_snapshot_from_dict_rejects_unusable_rows(bad):
    with pytest.raises(ValueError):
        monitor.snapshot_from_dict(bad)


# =========================================================================== cache file


def test_empty_cache(world):
    assert monitor.load_cache() == {}


def test_save_and_load_cache():
    snaps = {
        ACCT_A.id: usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0),
        ACCT_B.id: UsageSnapshot(ACCT_B.id, T0, ok=False, error="network"),
    }
    monitor.save_cache(snaps)
    assert monitor.load_cache() == snaps


def test_cache_files_are_private():
    monitor.save_cache({ACCT_A.id: usage.parse_usage({}, ACCT_A.id, T0)})
    assert stat.S_IMODE(os.stat(paths.usage_cache_file()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths.app_dir()).st_mode) == 0o700


@pytest.mark.parametrize("content", ["", "not json", "[]", '"x"', "null", '{"snapshots": []}', '{"snapshots": "x"}'])
def test_damaged_cache_reads_as_empty(content):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(content)
    assert monitor.load_cache() == {}


def test_one_damaged_row_does_not_lose_the_others():
    good = monitor.snapshot_to_dict(usage.parse_usage({}, ACCT_A.id, T0))
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"snapshots": {ACCT_A.id: good, "bad": {"nope": 1}, "worse": 5}}))
    assert list(monitor.load_cache()) == [ACCT_A.id]


def test_save_cache_keeps_backoff_and_alert_state():
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(
        json.dumps(
            {
                "snapshots": {},
                "backoff": {ACCT_B.id: {"next_allowed_at": "2026-11-22T11:00:00+00:00", "failures": 2}},
                "alerts": {"fired": {"k": "2026-11-22T10:00:00+00:00"}, "auth_alerted": [ACCT_B.id], "high": {}},
            }
        )
    )
    monitor.save_cache({ACCT_A.id: usage.parse_usage({}, ACCT_A.id, T0)})

    saved = read_cache_file()
    assert saved["backoff"][ACCT_B.id]["failures"] == 2
    assert saved["alerts"]["auth_alerted"] == [ACCT_B.id]
    assert list(saved["snapshots"]) == [ACCT_A.id]


def test_save_cache_replaces_the_snapshot_set():
    monitor.save_cache({ACCT_A.id: usage.parse_usage({}, ACCT_A.id, T0), ACCT_B.id: usage.parse_usage({}, ACCT_B.id, T0)})
    monitor.save_cache({ACCT_B.id: usage.parse_usage({}, ACCT_B.id, T0)})
    assert list(monitor.load_cache()) == [ACCT_B.id]


# =========================================================================== refresh_account


def test_refresh_account_success_for_a_saved_account(world):
    server = FakeUsageServer()

    snap = monitor.refresh_account(ACCT_B, http_post=ForbiddenPost(), opener=server)

    assert snap.ok and snap.error is None and snap.account_id == ACCT_B.id
    assert server.calls == ["sk-ant-oat01-fake-saved-b"]
    assert snap.session.utilization == 37.0
    assert snap.window("limit:weekly:Fable").utilization == 41.0
    assert snap.promos[0].label == "Holiday credit"
    assert snap.extra_usage.currency == "USD"
    assert snap.raw["future_passthrough"] == {"anything": True}


def test_refresh_account_uses_the_live_token_for_the_live_account(world):
    server = FakeUsageServer()
    monitor.refresh_account(ACCT_A, http_post=ForbiddenPost(), opener=server)
    assert server.calls == ["sk-ant-oat01-fake-live-a"]


def test_refresh_account_does_the_whole_chain_for_an_expired_saved_login(world, monkeypatch):
    from cc_switcher import claude_refresh

    store.save_settings(Settings(claude_refresh_enabled=True))

    original = make_login(ACCOUNT_B, "saved-b", EXPIRED_MS)
    put_saved(world, ACCT_B, original)
    server = FakeUsageServer()
    calls = []

    def refresh_locked(account, login):
        calls.append((account.id, login))
        assert lock_is_held()
        updated = dict(login, accessToken="sk-ant-oat01-REFRESHED",
                       refreshToken="sk-ant-ort01-ROTATED", expiresAt=FAR_FUTURE_MS)
        put_saved(world, account, updated)
        return updated

    monkeypatch.setattr(claude_refresh, "refresh_locked", refresh_locked)
    snap = monitor.refresh_account(ACCT_B, http_post=ForbiddenPost(), opener=server)

    assert snap.ok
    assert calls == [(ACCT_B.id, original)]
    assert server.calls == ["sk-ant-oat01-REFRESHED"]  # usage was fetched with the fresh token
    stored = world.get_json(ACCT_B.keychain_service)["claudeAiOauth"]
    assert stored["refreshToken"] == "sk-ant-ort01-ROTATED"
    assert stored["accountUuid"] == ACCOUNT_B[0] and stored["organizationUuid"] == ACCOUNT_B[1]


def test_the_usage_request_is_made_outside_the_app_lock(world):
    held = []
    server = FakeUsageServer()
    server.on_call = lambda: held.append(lock_is_held())

    monitor.refresh_account(ACCT_B, opener=server)

    assert len(held) == 2  # the usage request and the resets request
    assert not any(held)


@pytest.mark.parametrize(
    "setup, expected",
    [
        ("no_saved_item", "no_credentials"),
        ("live_expired", "token_stale"),
        ("refresh_needs_login", "auth_expired"),
        ("refresh_opaque_failure", "refresh_paused"),
        ("refresh_retry_blocked", "refresh_paused"),
        ("refresh_recovery_required", "refresh_recovery_required"),
        ("usage_401", "auth_expired"),
        ("usage_403", "auth_expired"),
        ("usage_429", "rate_limited"),
        ("usage_500", "http_500"),
        ("usage_404", "http_404"),
        ("usage_unreachable", "network"),
        ("usage_timeout", "network"),
        ("usage_html", "bad_response"),
        ("keychain_locked", "keychain_error"),
        ("lock_timeout", "busy"),
        ("unexpected", "internal_error"),
    ],
)
def test_every_failure_becomes_an_error_snapshot_and_nothing_raises(world, monkeypatch, setup, expected):
    server = FakeUsageServer()
    post = ForbiddenPost()
    account = ACCT_B

    if setup == "no_saved_item":
        world.items.pop(ACCT_B.keychain_service)
    elif setup == "live_expired":
        put_live(world, make_login(ACCOUNT_B, "live-b", EXPIRED_MS))
    elif setup.startswith("refresh_"):
        from cc_switcher import claude_refresh

        store.save_settings(Settings(claude_refresh_enabled=True))
        put_saved(world, ACCT_B, make_login(ACCOUNT_B, "saved-b", EXPIRED_MS))
        failure = {
            "refresh_needs_login": claude_refresh.NeedsLogin,
            "refresh_opaque_failure": claude_refresh.RefreshFailed,
            "refresh_retry_blocked": claude_refresh.RetryBlocked,
            "refresh_recovery_required": claude_refresh.RecoveryRequired,
        }[setup]

        def fail_refresh(account, login):
            raise failure("simulated isolated renewal failure")

        monkeypatch.setattr(claude_refresh, "refresh_locked", fail_refresh)
    elif setup.startswith("usage_"):
        server.replies["sk-ant-oat01-fake-saved-b"] = {
            "usage_401": http_error(401),
            "usage_403": http_error(403),
            "usage_429": http_error(429, {"Retry-After": "45"}),
            "usage_500": http_error(500),
            "usage_404": http_error(404),
            "usage_unreachable": urllib.error.URLError("no route"),
            "usage_timeout": TimeoutError("slow"),
            "usage_html": b"<html>Just a moment...</html>",
        }[setup]
    elif setup == "keychain_locked":
        def read(service):
            raise keychain.KeychainError("locked")

        monkeypatch.setattr(keychain, "read", read)
    elif setup == "lock_timeout":
        def locked(timeout=30.0):
            raise fsutil.LockTimeout("busy")

        monkeypatch.setattr(fsutil, "app_lock", locked)
    elif setup == "unexpected":
        def boom(*a, **k):
            raise ValueError("bug")

        monkeypatch.setattr(tokens, "access_token_for", boom)

    snap = monitor.refresh_account(account, http_post=post, opener=server)

    assert isinstance(snap, UsageSnapshot)
    assert (snap.ok, snap.error) == (False, expected)
    assert snap.account_id == ACCT_B.id
    assert snap.windows == () and snap.raw is None
    if setup.startswith("refresh_"):
        assert server.calls == [] and server.resets_calls == []


def test_error_snapshots_never_contain_tokens(world):
    server = FakeUsageServer()
    server.replies["sk-ant-oat01-fake-saved-b"] = http_error(401)
    snap = monitor.refresh_account(ACCT_B, opener=server)
    assert "sk-ant" not in json.dumps(monitor.snapshot_to_dict(snap))


def test_describe_error_covers_every_code():
    codes = [
        "no_credentials", "auth_expired", "token_stale", "rate_limited", "network",
        "http_502", "bad_response", "keychain_error", "busy", "internal_error",
    ]
    for code in codes:
        assert monitor.describe_error(code), code
    assert "502" in monitor.describe_error("http_502")
    assert monitor.describe_error(None) == ""
    assert monitor.describe_error("something_new") == "something_new"


# =========================================================================== refresh_all


def test_refresh_all_fetches_every_saved_account_and_persists(world):
    server = FakeUsageServer()

    results = monitor.refresh_all(opener=server, delay_between=0)

    assert set(results) == {ACCT_A.id, ACCT_B.id}
    assert all(s.ok for s in results.values())
    assert server.calls == ["sk-ant-oat01-fake-live-a", "sk-ant-oat01-fake-saved-b"]  # registry order
    assert monitor.load_cache() == results
    assert len(history_lines()) == 2


def test_recent_success_can_be_reused_during_a_burst(world):
    """Three refreshes in 9 seconds should not issue three usage requests."""
    server = FakeUsageServer()
    first = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0, reuse_fresh_seconds=60)
    second = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=2), reuse_fresh_seconds=60)
    third = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=9), reuse_fresh_seconds=60)
    assert first[ACCT_B.id] == second[ACCT_B.id] == third[ACCT_B.id]
    assert len(server.calls) == 1
    assert len(history_lines()) == 1
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=60), reuse_fresh_seconds=60)
    assert len(server.calls) == 2


def test_recent_stale_snapshot_is_not_reused_for_a_retry(world):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    token = "sk-ant-oat01-fake-saved-b"
    server.replies[token] = urllib.error.URLError("down")
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=1))
    assert monitor.cache_status(ACCT_B.id)["stale"]
    server.replies.pop(token)
    result = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=2), reuse_fresh_seconds=60)
    assert result[ACCT_B.id].fetched_at == T0 + timedelta(seconds=2)
    assert len(server.calls) == 3
    assert not monitor.cache_status(ACCT_B.id)["stale"]


def test_refresh_all_with_an_explicit_subset_leaves_other_cache_entries(world):
    server = FakeUsageServer()
    monitor.refresh_all(opener=server, delay_between=0)
    first_a = monitor.load_cache()[ACCT_A.id]
    server.calls.clear()

    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0)

    assert list(results) == [ACCT_B.id]
    assert server.calls == ["sk-ant-oat01-fake-saved-b"]
    cache = monitor.load_cache()
    assert set(cache) == {ACCT_A.id, ACCT_B.id}
    assert cache[ACCT_A.id] == first_a


def test_full_refresh_prunes_accounts_that_were_removed(world):
    monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)
    store.remove_account(ACCT_B.id)

    monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)

    assert list(monitor.load_cache()) == [ACCT_A.id]


def test_refresh_all_with_no_accounts(world):
    store.save_accounts([])
    assert monitor.refresh_all(opener=FakeUsageServer(), delay_between=0) == {}


def test_a_failing_account_does_not_stop_the_others(world):
    world.items.pop(ACCT_A.keychain_service)
    put_live(world, make_login(ACCOUNT_B, "live-b", FAR_FUTURE_MS))  # A is no longer live and has no saved login
    results = monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)
    assert results[ACCT_A.id].error == "no_credentials"
    assert results[ACCT_B.id].ok


def test_delay_is_applied_between_fetched_accounts_only(world):
    sleeps = []
    monitor.refresh_all(opener=FakeUsageServer(), delay_between=0.5, sleep=sleeps.append)
    assert sleeps == [0.5]  # two accounts: one pause, none before the first or after the last

    sleeps.clear()
    monitor.refresh_all(opener=FakeUsageServer(), delay_between=0, sleep=sleeps.append)
    assert sleeps == []


def test_refresh_all_does_not_hold_the_lock_while_fetching(world):
    held = []
    server = FakeUsageServer()
    server.on_call = lambda: held.append(lock_is_held())
    monitor.refresh_all(opener=server, delay_between=0)
    assert len(held) == 4 and not any(held)  # usage + resets for each of the two accounts
    assert not lock_is_held()


def test_cache_and_history_never_contain_tokens(world):
    monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)
    for path in (paths.usage_cache_file(), paths.usage_history_file()):
        text = path.read_text()
        assert "sk-ant" not in text
        assert "accessToken" not in text and "refreshToken" not in text


def test_persist_failures_are_logged_not_raised(world, monkeypatch):
    def broken(path, data, mode=0o600):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil, "atomic_write_json", broken)
    results = monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)
    assert all(s.ok for s in results.values())


def test_persist_lock_timeout_is_not_raised(world, monkeypatch):
    real = fsutil.app_lock
    calls = {"n": 0}

    def flaky(timeout=30.0):
        calls["n"] += 1
        if calls["n"] > 2:  # let the two token lookups through, fail the final persist
            raise fsutil.LockTimeout("busy")
        return real(timeout)

    monkeypatch.setattr(fsutil, "app_lock", flaky)
    results = monitor.refresh_all(opener=FakeUsageServer(), delay_between=0)
    assert all(s.ok for s in results.values())


# --------------------------------------------------------------------------- 429 backoff


def rate_limited_server(retry_after=None):
    server = FakeUsageServer()
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    server.replies["sk-ant-oat01-fake-saved-b"] = http_error(429, headers)
    return server


def backoff_row(account=ACCT_B):
    return read_cache_file()["backoff"].get(account.id)


def test_opaque_cli_failure_keeps_stale_usage_without_inventing_429_details(world, monkeypatch, caplog):
    from cc_switcher import claude_refresh

    store.save_settings(Settings(claude_refresh_enabled=True))

    old = usage.parse_usage(usage_payload(session=37.0), ACCT_B.id, fetched_at=T0)
    monitor.save_cache({ACCT_B.id: old})
    put_saved(world, ACCT_B, make_login(ACCOUNT_B, "saved-b", EXPIRED_MS))
    secret = "sk-ant-ort01-fake-saved-b"
    calls = []

    def fail_refresh(account, login):
        calls.append(account.id)
        raise claude_refresh.RefreshFailed(secret)

    monkeypatch.setattr(claude_refresh, "refresh_locked", fail_refresh)
    server = FakeUsageServer()
    with caplog.at_level("WARNING"):
        result = monitor.refresh_all([ACCT_B], http_post=ForbiddenPost(), opener=server,
                                     delay_between=0, now=T0 + timedelta(minutes=5))
    entry = monitor.cache_status(ACCT_B.id, now=T0 + timedelta(minutes=5))
    assert calls == [ACCT_B.id]
    assert result[ACCT_B.id] == old and entry["snapshot"] == old
    assert entry["stale"] and entry["last_error"] == "refresh_paused"
    assert entry["age_seconds"] == 300
    assert backoff_row() is None and "rate_limit" not in entry
    assert server.calls == [] and server.resets_calls == []
    assert secret not in caplog.text
    assert secret not in json.dumps(entry, default=str)


@pytest.mark.parametrize("endpoint, server_error", [([], None), ({}, None), ("oauth_token", []), ("usage", {})])
def test_damaged_rate_limit_diagnostics_do_not_break_cache_status(world, endpoint, server_error):
    state = monitor._empty_state()
    state["errors"][ACCT_B.id] = {"last_error": "rate_limited", "last_error_at": T0.isoformat()}
    state["backoff"][ACCT_B.id] = {"endpoint": endpoint, "server_error": server_error}
    fsutil.ensure_private_dir(paths.app_dir())
    fsutil.atomic_write_json(paths.usage_cache_file(), state)
    status = monitor.cache_status(ACCT_B.id, now=T0)
    assert status["last_error"] == "rate_limited"
    if "rate_limit" in status:
        assert status["rate_limit"]["server_error"] is None


def test_server_retry_window_starts_when_the_response_arrives(world, monkeypatch):
    """A 15-second request plus Retry-After 120s must wait until T0 + 135s."""
    clock = {"now": T0}
    monkeypatch.setattr(monitor, "utcnow", lambda: clock["now"])
    server = rate_limited_server(120)
    server.on_call = lambda: clock.update(now=T0 + timedelta(seconds=15))
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0)
    assert backoff_row()["next_allowed_at"] == (T0 + timedelta(seconds=135)).isoformat()


def test_usage_429_logs_only_allowlisted_response_type(world, caplog):
    server = FakeUsageServer()
    secret = "sk-ant-oat01-fake-saved-b"
    body = json.dumps({"error": {"type": "rate_limit_error", "message": secret}}).encode()
    server.replies[secret] = urllib.error.HTTPError(
        "https://api.anthropic.com/api/oauth/usage", 429, "err", {"Retry-After": "120"}, io.BytesIO(body),
    )
    with caplog.at_level("WARNING"):
        monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    assert backoff_row()["server_error"] == "rate_limit_error"
    assert "server_error=rate_limit_error" in caplog.text
    assert secret not in caplog.text


def test_usage_429_records_usage_endpoint_without_logging_credentials(world, caplog):
    server = rate_limited_server(120)
    with caplog.at_level("WARNING"):
        result = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    assert result[ACCT_B.id].error == "rate_limited"
    assert backoff_row()["endpoint"] == "usage"
    assert "usage" in caplog.text and "429" in caplog.text
    assert "sk-ant-oat01-fake-saved-b" not in caplog.text


def test_429_with_retry_after_sets_a_backoff_and_it_is_respected(world):
    server = rate_limited_server(retry_after=120)

    first = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    assert first[ACCT_B.id].error == "rate_limited"
    assert backoff_row()["next_allowed_at"] == (T0 + timedelta(seconds=120)).isoformat()
    assert backoff_row()["failures"] == 1
    assert len(server.calls) == 1

    # 60 s later we are still inside the backoff: no request is made, the last snapshot is returned.
    second = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=60))
    assert len(server.calls) == 1
    assert second[ACCT_B.id].error == "rate_limited"

    # After it expires the account is fetched again.
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(seconds=121))
    assert len(server.calls) == 2


def test_a_skipped_account_keeps_its_previous_snapshot_and_is_not_logged(world):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    good = monitor.load_cache()[ACCT_B.id]
    assert len(history_lines()) == 1
    # Plant a backoff as if a later poll had been rate-limited, but with an ok snapshot still cached.
    state = read_cache_file()
    state["backoff"][ACCT_B.id] = {"next_allowed_at": (T0 + timedelta(minutes=10)).isoformat(), "failures": 1}
    paths.usage_cache_file().write_text(json.dumps(state))
    server.calls.clear()

    results = monitor.refresh_all([ACCT_A, ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(minutes=1))

    assert results[ACCT_B.id] == good  # untouched, with its old fetched_at
    assert server.calls == ["sk-ant-oat01-fake-live-a"]  # only A was fetched
    assert [line["id"] for line in history_lines()][1:] == [ACCT_A.id]  # B was not logged again


def test_backoff_without_retry_after_doubles_up_to_the_cap(world):
    server = rate_limited_server()
    waits = []
    now = T0
    for _ in range(9):
        monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=now)
        row = backoff_row()
        waits.append(int((datetime.fromisoformat(row["next_allowed_at"]) - now).total_seconds()))
        now = datetime.fromisoformat(row["next_allowed_at"]) + timedelta(seconds=1)
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]


def test_short_retry_after_has_a_floor_and_large_server_wait_is_respected(world):
    monitor.refresh_all([ACCT_B], opener=rate_limited_server(5), delay_between=0, now=T0)
    assert backoff_row()["next_allowed_at"] == (T0 + timedelta(seconds=monitor.BACKOFF_MIN_RETRY_AFTER)).isoformat()

    monitor.refresh_all([ACCT_B], opener=rate_limited_server(10**7), delay_between=0, now=T0 + timedelta(hours=2))
    assert backoff_row()["next_allowed_at"] == (T0 + timedelta(hours=2, seconds=10**7)).isoformat()


def test_extreme_server_wait_does_not_overflow_or_retry_early(world):
    server = rate_limited_server(10**30)
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    deadline = datetime.fromisoformat(backoff_row()["next_allowed_at"])
    assert deadline.year == 9999
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + timedelta(days=1))
    assert len(server.calls) == 1


def test_success_clears_the_backoff(world):
    monitor.refresh_all([ACCT_B], opener=rate_limited_server(30), delay_between=0, now=T0)
    assert backoff_row() is not None

    results = monitor.refresh_all([ACCT_B], opener=FakeUsageServer(), delay_between=0, now=T0 + timedelta(minutes=5))

    assert results[ACCT_B.id].ok
    assert backoff_row() is None


def test_other_errors_do_not_start_a_backoff(world):
    server = FakeUsageServer()
    server.replies["sk-ant-oat01-fake-saved-b"] = urllib.error.URLError("down")
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    assert backoff_row() is None


def test_backoff_is_per_account(world):
    server = rate_limited_server(300)
    monitor.refresh_all(opener=server, delay_between=0, now=T0)  # B is limited, A is fine

    server.calls.clear()
    results = monitor.refresh_all(opener=server, delay_between=0, now=T0 + timedelta(seconds=30))

    assert server.calls == ["sk-ant-oat01-fake-live-a"]
    assert results[ACCT_A.id].ok and results[ACCT_B.id].error == "rate_limited"


def test_a_backed_off_account_with_no_cached_snapshot_reports_rate_limited(world):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(
        json.dumps({"backoff": {ACCT_B.id: {"next_allowed_at": (T0 + timedelta(minutes=5)).isoformat(), "failures": 1}}})
    )
    server = FakeUsageServer()

    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)

    assert server.calls == []
    assert (results[ACCT_B.id].ok, results[ACCT_B.id].error) == (False, "rate_limited")
    # an account that was skipped is reported, never recorded: no snapshot and no failure were written for it
    state = read_cache_file()
    assert state["snapshots"] == {} and state["errors"] == {}


def test_refresh_account_ignores_backoff_and_writes_only_the_resets_gate(world):
    fsutil.ensure_private_dir(paths.app_dir())
    monitor.refresh_account(ACCT_B, opener=FakeUsageServer())

    assert not paths.usage_history_file().exists()
    cache = read_cache_file()
    assert cache["snapshots"] == {} and cache["backoff"] == {}  # snapshot/backoff are refresh_all's job
    assert list(cache["resets_cache"]) == [ACCT_B.id]


# =========================================================================== history


def snapshot_with(account_id, session=10.0, weekly=20.0, when=T0, ok=True, error=None, extra=None):
    windows = ()
    if ok:
        windows = (
            UsageWindow("five_hour", "Session (5h)", session, when + timedelta(hours=4)),
            UsageWindow("seven_day", "Weekly", weekly, when + timedelta(days=5)),
            UsageWindow("seven_day_opus", "Weekly · Opus", None, None),
        )
    return UsageSnapshot(account_id, when, ok=ok, error=error, windows=windows, extra_usage=extra)


def test_history_line_is_compact_and_has_no_secrets_or_raw_payload():
    extra = ExtraUsage(True, 5000, 1234, 24.7, "USD")
    snap = snapshot_with(ACCT_A.id, extra=extra)
    snap = UsageSnapshot(**{**snap.__dict__, "raw": usage_payload()})

    monitor.append_history({ACCT_A.id: snap})

    (line,) = history_lines()
    assert line == {
        "t": "2026-11-22T10:00:00+00:00",
        "id": ACCT_A.id,
        "ok": True,
        "u": {"five_hour": 10.0, "seven_day": 20.0},
        "rs": {"five_hour": "2026-11-22T14:00:00+00:00", "seven_day": "2026-11-27T10:00:00+00:00"},
        "x": {"used": 1234, "limit": 5000, "cur": "USD"},
    }
    raw_line = paths.usage_history_file().read_text()
    assert raw_line.count("\n") == 1 and ", " not in raw_line  # one compact line
    assert "future_passthrough" not in raw_line


def test_history_logs_only_successful_snapshots_and_skips_disabled_extra_usage():
    snaps = {
        ACCT_A.id: snapshot_with(ACCT_A.id, ok=False, error="auth_expired"),
        ACCT_B.id: snapshot_with(ACCT_B.id, extra=ExtraUsage(False, None, None, None)),
    }
    monitor.append_history(snaps)

    (b,) = history_lines()  # the failed snapshot left no trace
    assert b["id"] == ACCT_B.id and b["ok"] is True
    assert "x" not in b and "err" not in b


def test_history_file_is_not_even_created_for_failures_only():
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id, ok=False, error="network")})
    assert not paths.usage_history_file().exists()


def test_history_appends_one_line_per_account_per_refresh():
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id), ACCT_B.id: snapshot_with(ACCT_B.id)})
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id, session=11)})
    assert [(r["id"], r["u"]["five_hour"]) for r in history_lines()] == [(ACCT_A.id, 10.0), (ACCT_B.id, 10.0), (ACCT_A.id, 11.0)]


def test_history_file_is_private():
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id)})
    assert stat.S_IMODE(os.stat(paths.usage_history_file()).st_mode) == 0o600


def test_empty_history_append_creates_nothing():
    monitor.append_history({})
    assert not paths.usage_history_file().exists()


def test_history_is_trimmed_to_newest_lines_on_a_line_boundary(monkeypatch):
    monkeypatch.setattr(monitor, "HISTORY_MAX_BYTES", 2000)
    monkeypatch.setattr(monitor, "HISTORY_TRIM_TO_BYTES", 1000)

    for n in range(60):
        monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id, session=float(n), when=T0 + timedelta(minutes=n))})

    path = paths.usage_history_file()
    assert path.stat().st_size <= 2000
    rows = history_lines()  # every line still parses: no torn line at the cut
    sessions = [r["u"]["five_hour"] for r in rows]
    assert sessions == sorted(sessions) and sessions[-1] == 59.0  # newest kept, order intact
    assert sessions[0] > 0  # oldest dropped
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_history_below_the_limit_is_never_rewritten(monkeypatch):
    called = []
    monkeypatch.setattr(monitor, "_trim_history", lambda path: called.append(path))
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id)})
    assert called == []


def test_append_history_takes_the_lock_once(monkeypatch):
    real = fsutil.app_lock
    entered = []

    def counting(timeout=30.0):
        entered.append(1)
        return real(timeout)

    monkeypatch.setattr(fsutil, "app_lock", counting)
    monitor.append_history({ACCT_A.id: snapshot_with(ACCT_A.id)})
    assert len(entered) == 1


# =========================================================================== detect_alerts


def win(key, utilization, resets_at=T0 + timedelta(days=2, hours=4), label=None):
    labels = {"five_hour": "Session (5h)", "seven_day": "Weekly"}
    return UsageWindow(key, label or labels.get(key, key), utilization, resets_at)


def ok_snap(account, *windows, when=T0):
    return UsageSnapshot(account.id, when, ok=True, windows=tuple(windows))


def alerts_for(snapshots, accounts=(ACCT_A, ACCT_B), thresholds=(80, 95, 100), now=T0):
    return monitor.detect_alerts(snapshots, list(accounts), list(thresholds), now=now)


def test_alert_title_and_message_are_short_and_human():
    (alert,) = alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 95.4))})

    assert alert == monitor.Alert(ACCT_A.id, "threshold", "seven_day", 95, "work: weekly limit 95%", "Resets in 2d 4h")


@pytest.mark.parametrize(
    "window, utilization, title",
    [
        (win("five_hour", 80), 80, "work: session limit 80%"),
        (win("seven_day", 100), 100, "work: weekly limit reached"),
        (win("seven_day", 130), 130, "work: weekly limit reached"),
        (win("limit:weekly:Fable", 96, label="Weekly · Fable"), 96, "work: weekly Fable limit 95%"),
        (win("cinder_cove", 85, label="cinder_cove"), 85, "work: cinder_cove limit 80%"),
    ],
)
def test_alert_titles_by_window(window, utilization, title):
    (alert,) = alerts_for({ACCT_A.id: ok_snap(ACCT_A, window)})
    assert alert.title == title


def test_alert_falls_back_to_the_email_when_there_is_no_label():
    unlabeled = account_for(ACCOUNT_B, "b@example.com")
    (alert,) = alerts_for({unlabeled.id: ok_snap(unlabeled, win("seven_day", 85))}, accounts=[unlabeled])
    assert alert.title == "b@example.com: weekly limit 80%"


def test_alert_message_when_reset_is_unknown_or_due():
    (unknown,) = alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 85, resets_at=None))})
    assert unknown.message == "Reset time unknown"

    (due,) = alerts_for({ACCT_B.id: ok_snap(ACCT_B, win("seven_day", 85, resets_at=T0 - timedelta(minutes=1)))})
    assert due.message == "Resets now"


def test_a_threshold_fires_once_per_window_threshold_and_period():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 82))}
    assert [a.threshold for a in alerts_for(snaps)] == [80]
    assert alerts_for(snaps) == []  # same state again: silent

    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))}
    assert [a.threshold for a in alerts_for(snaps)] == [95]  # 80 was already announced
    assert alerts_for(snaps) == []

    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 100))}
    assert [a.threshold for a in alerts_for(snaps)] == [100]
    assert alerts_for(snaps) == []


def test_dedupe_state_survives_a_restart():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))}
    assert len(alerts_for(snaps)) == 1

    # "Restart": nothing is kept in memory, the cache file is the only state.
    fired = read_cache_file()["alerts"]["fired"]
    assert len(fired) == 2  # 80 and 95 both recorded
    assert alerts_for(snaps) == []


def test_jumping_over_several_thresholds_reports_only_the_highest():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 100))}
    assert [(a.threshold, a.title) for a in alerts_for(snaps)] == [(100, "work: weekly limit reached")]

    # The skipped lower thresholds were consumed too, so they do not trickle out later.
    assert alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 97))}) == []


def test_a_new_reset_period_fires_again():
    first = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 96, resets_at=T0 + timedelta(hours=2)))}
    assert len(alerts_for(first)) == 1
    assert alerts_for(first) == []

    next_period = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 96, resets_at=T0 + timedelta(hours=7)))}
    assert [a.threshold for a in alerts_for(next_period, now=T0 + timedelta(hours=5))] == [95]


def test_subsecond_jitter_in_resets_at_does_not_re_alert():
    base = T0 + timedelta(hours=2)
    a = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 96, resets_at=base.replace(microsecond=943648) - timedelta(seconds=1)))}
    b = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 96, resets_at=base.replace(microsecond=100000)))}
    assert len(alerts_for(a)) == 1
    assert alerts_for(b) == []


def test_windows_and_accounts_are_tracked_independently():
    snaps = {
        ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 85), win("seven_day", 85)),
        ACCT_B.id: ok_snap(ACCT_B, win("seven_day", 85)),
    }
    found = alerts_for(snaps)
    assert sorted((a.account_id, a.window_key) for a in found) == sorted(
        [(ACCT_A.id, "five_hour"), (ACCT_A.id, "seven_day"), (ACCT_B.id, "seven_day")]
    )
    assert alerts_for(snaps) == []


def test_below_every_threshold_and_unknown_utilization_are_silent():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 79.9), win("seven_day", None))}
    assert alerts_for(snaps) == []


def test_exact_threshold_counts():
    assert [a.threshold for a in alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 80.0))})] == [80]


def test_custom_thresholds_are_honoured_and_may_be_unsorted_or_repeated():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 71))}
    assert [a.threshold for a in alerts_for(snaps, thresholds=[90, 70, 70, 50])] == [70]
    assert alerts_for({ACCT_B.id: ok_snap(ACCT_B, win("seven_day", 99))}, thresholds=[]) == []


def test_a_window_without_a_reset_time_re_arms_when_it_drops_below():
    high = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96, resets_at=None))}
    low = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 10, resets_at=None))}
    assert len(alerts_for(high)) == 1
    assert alerts_for(high) == []
    alerts_for(low)
    assert [a.threshold for a in alerts_for(high)] == [95]


def test_snapshots_for_unknown_accounts_are_ignored_and_their_state_is_forgotten():
    ghost = account_for(("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c"), "ghost@example.com")
    snaps = {ghost.id: ok_snap(ghost, win("seven_day", 99)), ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 99))}
    assert [a.account_id for a in alerts_for(snaps, accounts=[ghost, ACCT_A])] == [ghost.id, ACCT_A.id]

    alerts_for(snaps, accounts=[ACCT_A])  # ghost removed from the registry
    assert all(key.startswith(ACCT_A.id) for key in read_cache_file()["alerts"]["fired"])


def test_old_fired_markers_are_pruned():
    snaps = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))}
    alerts_for(snaps, now=T0)
    assert read_cache_file()["alerts"]["fired"]

    alerts_for({}, now=T0 + monitor.FIRED_RETENTION + timedelta(seconds=1))
    assert read_cache_file()["alerts"]["fired"] == {}


def test_detect_alerts_preserves_snapshots_and_backoff_in_the_cache(world):
    monitor.refresh_all([ACCT_B], opener=rate_limited_server(300), delay_between=0, now=T0)
    before = read_cache_file()

    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))})

    after = read_cache_file()
    assert after["snapshots"] == before["snapshots"]
    assert after["backoff"] == before["backoff"]


def test_detect_alerts_takes_the_lock_once(monkeypatch):
    real = fsutil.app_lock
    entered = []

    def counting(timeout=30.0):
        entered.append(1)
        return real(timeout)

    monkeypatch.setattr(fsutil, "app_lock", counting)
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))})
    assert len(entered) == 1


# --------------------------------------------------------------------------- auth_expired alerts


def err_snap(account, code, when=T0):
    return UsageSnapshot(account.id, when, ok=False, error=code)


def test_auth_expired_alerts_once_until_the_account_recovers():
    expired = {ACCT_A.id: err_snap(ACCT_A, "auth_expired")}

    (alert,) = alerts_for(expired)
    assert (alert.kind, alert.window_key, alert.threshold) == ("auth_expired", None, None)
    assert alert.title == "work: sign-in expired"
    assert "again" in alert.message
    assert alerts_for(expired) == []

    # a transient error in between is not a recovery ...
    assert alerts_for({ACCT_A.id: err_snap(ACCT_A, "network")}) == []
    assert alerts_for(expired) == []

    # ... an ok snapshot is; a later expiry alerts again.
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 10))})
    assert [a.kind for a in alerts_for(expired)] == ["auth_expired"]


@pytest.mark.parametrize("code", ["no_credentials", "token_stale", "network", "rate_limited", "http_500", "bad_response", "keychain_error"])
def test_other_errors_do_not_alert(code):
    assert alerts_for({ACCT_A.id: err_snap(ACCT_A, code)}) == []


def test_auth_expired_is_tracked_per_account():
    both = {ACCT_A.id: err_snap(ACCT_A, "auth_expired"), ACCT_B.id: err_snap(ACCT_B, "auth_expired")}
    assert [a.account_id for a in alerts_for(both)] == [ACCT_A.id, ACCT_B.id]
    assert alerts_for(both) == []


def test_error_snapshots_do_not_trigger_threshold_alerts_from_stale_numbers():
    stale = UsageSnapshot(ACCT_A.id, T0, ok=False, error="network", windows=(win("seven_day", 99),))
    assert alerts_for({ACCT_A.id: stale}) == []


# --------------------------------------------------------------------------- reset alerts


def test_reset_alert_when_a_high_window_drops_after_its_reset_time():
    resets = T0 + timedelta(hours=1)
    high = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 90, resets_at=resets))}
    assert [a.kind for a in alerts_for(high)] == ["threshold"]

    after = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 3, resets_at=resets + timedelta(hours=5)))}
    (alert,) = alerts_for(after, now=resets + timedelta(minutes=5))

    assert (alert.kind, alert.window_key, alert.threshold) == ("reset", "five_hour", None)
    assert alert.title == "work: session limit reset"
    assert alert.message == "Usage is back to 3%."
    assert alerts_for(after, now=resets + timedelta(minutes=10)) == []  # once


def test_no_reset_alert_before_the_reset_time_has_passed():
    resets = T0 + timedelta(hours=1)
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 90, resets_at=resets))})

    early = {ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 3, resets_at=resets))}
    assert alerts_for(early, now=T0 + timedelta(minutes=30)) == []  # e.g. the server revised the figure


def test_no_reset_alert_for_a_window_that_never_was_high():
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 70))})
    assert alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 1))}, now=T0 + timedelta(days=1)) == []


def test_a_partial_drop_does_not_count_as_a_reset_and_the_record_is_kept():
    resets = T0 + timedelta(hours=1)
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 90, resets_at=resets))})
    later = T0 + timedelta(hours=2)

    assert [a for a in alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 50))}, now=later) if a.kind == "reset"] == []
    (alert,) = alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 2))}, now=later)
    assert alert.kind == "reset"


def test_reset_alert_when_a_high_window_vanishes_after_its_reset_time():
    """The server reports five_hour: null once the window ends; that counts as back to 0%."""
    resets = T0 + timedelta(hours=1)
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 90, resets_at=resets), win("seven_day", 10))})

    gone = {ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 10))}
    assert alerts_for(gone, now=T0 + timedelta(minutes=30)) == []  # not due yet
    (alert,) = alerts_for(gone, now=resets + timedelta(minutes=1))
    assert (alert.kind, alert.window_key, alert.title) == ("reset", "five_hour", "work: session limit reset")
    assert alerts_for(gone, now=resets + timedelta(minutes=2)) == []


def test_error_snapshots_neither_clear_nor_fire_reset_state():
    resets = T0 + timedelta(hours=1)
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("five_hour", 90, resets_at=resets))})
    assert alerts_for({ACCT_A.id: err_snap(ACCT_A, "network")}, now=resets + timedelta(hours=1)) == []
    assert read_cache_file()["alerts"]["high"][ACCT_A.id]["five_hour"]["utilization"] == 90


def test_alert_state_file_stays_private_and_token_free(world):
    alerts_for({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 96))})
    assert stat.S_IMODE(os.stat(paths.usage_cache_file()).st_mode) == 0o600
    assert "sk-ant" not in paths.usage_cache_file().read_text()


# =========================================================================== end to end


def test_poll_cycle_refresh_then_alert_then_quiet(world):
    server = FakeUsageServer(usage_payload(session=96, weekly=30))

    snaps = monitor.refresh_all(opener=server, delay_between=0)
    first = monitor.detect_alerts(snaps, [ACCT_A, ACCT_B], [80, 95, 100], now=T0)
    assert sorted((a.account_id, a.window_key, a.threshold) for a in first) == sorted(
        [(ACCT_A.id, "five_hour", 95), (ACCT_B.id, "five_hour", 95)]
    )

    snaps = monitor.refresh_all(opener=server, delay_between=0)
    assert monitor.detect_alerts(snaps, [ACCT_A, ACCT_B], [80, 95, 100], now=T0) == []
    assert len(history_lines()) == 4  # two accounts, two refreshes


# =========================================================================== redeemable resets (cedar_ember)

from cc_switcher.models import ResetGrant, ResetStatus  # noqa: E402

TOKEN_B = "sk-ant-oat01-fake-saved-b"
TOKEN_A = "sk-ant-oat01-fake-live-a"


def cedar_block(weekly_left=2) -> dict:
    return {
        "eligible": True,
        "ineligible_reason": None,
        "at_limit": False,
        "exhausted": [],
        "grants": [
            {
                "id": "grant_weekly_01",
                "label": "Weekly reset",
                "type": "weekly",
                "resets_total": 3,
                "resets_left": weekly_left,
                "starts_at": "2026-11-01T00:00:00Z",
                "ends_at": "2026-12-01T00:00:00+00:00",
                "clears": ["seven_day"],
                "paused": False,
                "usable_now": True,
                "use_requires_limit": True,
                "percent_used": {"seven_day": 62.0},
                "blocking": [],
                "gives_back_weekly": True,
                "unavailable_reason": None,
            },
            {
                "id": "grant_session_paused",
                "label": "Session reset",
                "type": "session",
                "resets_total": 5,
                "resets_left": 5,
                "paused": True,
                "usable_now": False,
                "unavailable_reason": "paused_by_admin",
            },
            {"id": "grant_full_expired", "label": "Full reset", "type": "full", "resets_total": 1, "resets_left": 1,
             "ends_at": "2026-10-01T00:00:00Z", "usable_now": False, "unavailable_reason": "expired"},
            {"id": "broken", "resets_left": -3},
        ],
        "next_grant_id": "grant_weekly_01",
        "weekly_resets_at": "2026-11-27T08:00:00Z",
        "cooldown_until": None,
        "event_props": {"anything": True},
    }


def resets_row(account=ACCT_B) -> dict | None:
    return read_cache_file()["resets_cache"].get(account.id)


MIN = timedelta(minutes=1)


def test_refresh_account_fetches_resets_with_a_second_read_only_request(world):
    server = FakeUsageServer(cedar=cedar_block())

    snap = monitor.refresh_account(ACCT_B, opener=server, now=T0)

    assert snap.ok
    assert [url for _, url in server.requests] == [
        "https://api.anthropic.com/api/oauth/usage",
        "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1",
    ]
    assert server.calls == [TOKEN_B] and server.resets_calls == [TOKEN_B]
    assert snap.resets.eligible is True
    assert [g.id for g in snap.resets.grants] == ["grant_weekly_01", "grant_session_paused", "grant_full_expired"]
    assert snap.resets.weekly_resets_at == datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    assert snap.window("five_hour").utilization == 37.0  # the usage half of the snapshot is untouched


def test_nothing_the_monitor_sends_can_claim_a_reset_or_record_an_impression(world):
    server = FakeUsageServer(cedar=cedar_block())
    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    monitor.refresh_all(opener=server, delay_between=0, now=T0 + 31 * MIN)

    assert len(server.requests) == 8
    assert {method for method, _ in server.requests} == {"GET"}
    assert not [url for _, url in server.requests if "at_wall" in url or "reset_rate_limits" in url]


def test_resets_are_reused_for_30_minutes_then_fetched_again(world):
    server = FakeUsageServer(cedar=cedar_block(weekly_left=2))
    first = monitor.refresh_account(ACCT_B, opener=server, now=T0)
    assert first.resets.resets_left == 3  # 2 usable + the expired-but-unpaused 1 (display sum)
    assert len(server.resets_calls) == 1
    assert resets_row()["next_check_at"] == (T0 + 30 * MIN).isoformat()
    assert resets_row()["fetched_at"] == T0.isoformat()

    server.cedar = cedar_block(weekly_left=1)  # the server moved on, but we must not ask yet
    again = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 29 * MIN)
    assert len(server.resets_calls) == 1
    assert len(server.calls) == 2  # usage itself is still fetched every time
    assert again.resets == first.resets

    fresh = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 30 * MIN)
    assert len(server.resets_calls) == 2
    assert fresh.resets.resets_left == 2
    assert resets_row()["next_check_at"] == (T0 + 60 * MIN).isoformat()


def test_refresh_all_applies_the_same_30_minute_gate_per_account(world):
    server = FakeUsageServer(cedar=cedar_block())

    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    assert sorted(server.resets_calls) == sorted([TOKEN_A, TOKEN_B])

    monitor.refresh_all(opener=server, delay_between=0, now=T0 + 10 * MIN)
    assert len(server.resets_calls) == 2 and len(server.calls) == 4

    results = monitor.refresh_all(opener=server, delay_between=0, now=T0 + 31 * MIN)
    assert len(server.resets_calls) == 4
    assert results[ACCT_A.id].resets is not None and results[ACCT_B.id].resets is not None


def test_gates_are_independent_per_account(world):
    server = FakeUsageServer(cedar=cedar_block())
    monitor.refresh_account(ACCT_A, opener=server, now=T0)
    monitor.refresh_account(ACCT_B, opener=server, now=T0 + 20 * MIN)

    monitor.refresh_account(ACCT_A, opener=server, now=T0 + 31 * MIN)  # A is due again
    monitor.refresh_account(ACCT_B, opener=server, now=T0 + 31 * MIN)  # B is not (fetched at +20)

    assert server.resets_calls == [TOKEN_A, TOKEN_B, TOKEN_A]


def test_the_cache_keeps_resets_so_a_second_process_reuses_them(world):
    server = FakeUsageServer(cedar=cedar_block())
    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    cached = monitor.load_cache()[ACCT_B.id]
    assert cached.resets is not None and cached.resets.grants[0].id == "grant_weekly_01"

    server2 = FakeUsageServer(cedar=None)  # would answer "no block" if asked
    snap = monitor.refresh_account(ACCT_B, opener=server2, now=T0 + 5 * MIN)
    assert server2.resets_calls == []
    assert snap.resets == cached.resets


def test_an_absent_block_means_not_offered_and_is_not_refetched_for_30_minutes(world):
    server = FakeUsageServer(cedar=None)

    snap = monitor.refresh_account(ACCT_B, opener=server, now=T0)
    assert snap.ok and snap.resets is None
    assert resets_row()["status"] is None and resets_row()["next_check_at"] == (T0 + 30 * MIN).isoformat()

    monitor.refresh_account(ACCT_B, opener=server, now=T0 + 29 * MIN)
    assert len(server.resets_calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        http_error(500),
        http_error(401),
        http_error(403),
        http_error(429, {"Retry-After": "30"}),
        urllib.error.URLError("down"),
        TimeoutError("slow"),
        b"<html>Just a moment</html>",
        b"[]",
    ],
)
def test_a_resets_failure_never_fails_the_snapshot(world, reply):
    server = FakeUsageServer(cedar=cedar_block())
    server.resets_replies[TOKEN_B] = reply

    snap = monitor.refresh_account(ACCT_B, opener=server, now=T0)

    assert snap.ok and snap.error is None
    assert snap.session.utilization == 37.0 and snap.promos and snap.extra_usage
    assert snap.resets is None  # nothing cached yet
    # a failure (even a 429 asking for less) waits at least the failure-retry interval
    assert resets_row()["next_check_at"] == (T0 + monitor.RESETS_FAILURE_RETRY).isoformat()


def test_a_failed_resets_fetch_is_retried_after_15_minutes_not_every_poll(world):
    server = FakeUsageServer(cedar=cedar_block())
    server.resets_replies[TOKEN_B] = http_error(500)

    monitor.refresh_account(ACCT_B, opener=server, now=T0)
    assert resets_row()["next_check_at"] == (T0 + 15 * MIN).isoformat()
    assert resets_row()["fetched_at"] is None  # we have never had a good answer

    monitor.refresh_account(ACCT_B, opener=server, now=T0 + 14 * MIN)
    assert len(server.resets_calls) == 1

    del server.resets_replies[TOKEN_B]
    snap = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 15 * MIN)
    assert len(server.resets_calls) == 2 and snap.resets is not None


def test_resets_retry_window_starts_after_its_response(world, monkeypatch):
    clock = {"now": T0}
    monkeypatch.setattr(monitor, "utcnow", lambda: clock["now"])
    server = FakeUsageServer(cedar=cedar_block())
    server.on_call = lambda: clock.update(now=T0 + timedelta(seconds=10 if not server.requests else 25))
    server.resets_replies[TOKEN_B] = http_error(429, {"Retry-After": "1200"})
    monitor.refresh_account(ACCT_B, opener=server)
    assert resets_row()["next_check_at"] == (T0 + timedelta(seconds=1225)).isoformat()


def test_a_rate_limited_resets_fetch_honours_long_server_retry_after(world):
    server = FakeUsageServer(cedar=cedar_block())
    server.resets_replies[TOKEN_B] = http_error(429, {"Retry-After": "2400"})
    monitor.refresh_account(ACCT_B, opener=server, now=T0)
    assert resets_row()["next_check_at"] == (T0 + 40 * MIN).isoformat()

    server.resets_replies[TOKEN_B] = http_error(429, {"Retry-After": "99999"})
    monitor.refresh_account(ACCT_B, opener=server, now=T0 + 41 * MIN)
    assert resets_row()["next_check_at"] == (T0 + 41 * MIN + timedelta(seconds=99999)).isoformat()


def test_resets_429_does_not_start_the_accounts_usage_backoff(world):
    server = FakeUsageServer(cedar=cedar_block())
    server.resets_replies[TOKEN_B] = http_error(429, {"Retry-After": "600"})

    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)

    assert results[ACCT_B.id].ok
    assert read_cache_file()["backoff"] == {}


def test_a_failed_refetch_keeps_the_previous_resets(world):
    server = FakeUsageServer(cedar=cedar_block(weekly_left=2))
    good = monitor.refresh_account(ACCT_B, opener=server, now=T0)
    assert good.resets is not None

    server.resets_replies[TOKEN_B] = urllib.error.URLError("down")
    later = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 31 * MIN)

    assert later.ok and later.resets == good.resets
    assert resets_row()["fetched_at"] == T0.isoformat()  # still the time of the last *good* answer
    assert resets_row()["next_check_at"] == (T0 + 31 * MIN + monitor.RESETS_FAILURE_RETRY).isoformat()
    assert usage.parse_resets(resets_row()["status"]) == good.resets


def test_a_usage_failure_does_not_touch_the_cached_resets(world):
    server = FakeUsageServer(cedar=cedar_block())
    good = monitor.refresh_account(ACCT_B, opener=server, now=T0)
    row_before = resets_row()

    server.replies[TOKEN_B] = http_error(500)
    failed = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 5 * MIN)
    assert (failed.ok, failed.error, failed.resets) == (False, "http_500", None)
    assert resets_row() == row_before
    assert len(server.resets_calls) == 1  # no resets request without a good usage answer

    del server.replies[TOKEN_B]
    recovered = monitor.refresh_account(ACCT_B, opener=server, now=T0 + 10 * MIN)
    assert recovered.resets == good.resets and len(server.resets_calls) == 1


def test_an_unexpected_error_while_parsing_resets_is_contained(world, monkeypatch):
    def boom(block):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(usage, "parse_resets", boom)
    snap = monitor.refresh_account(ACCT_B, opener=FakeUsageServer(cedar=cedar_block()), now=T0)
    assert snap.ok and snap.resets is None


def test_a_failure_while_recording_resets_is_contained_too(world, monkeypatch):
    """The outermost guard: even a bug in building the gate row must not cost the account its usage."""

    def boom(status):
        raise RuntimeError("serialiser bug")

    monkeypatch.setattr(monitor, "resets_to_dict", boom)

    snap = monitor.refresh_account(ACCT_B, opener=FakeUsageServer(cedar=cedar_block()), now=T0)

    assert snap.ok and snap.session.utilization == 37.0
    assert snap.resets is None


def test_a_damaged_gate_row_just_means_fetch_again(world):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"resets_cache": {ACCT_B.id: "garbage", ACCT_A.id: {"next_check_at": 5, "status": []}}}))
    server = FakeUsageServer(cedar=cedar_block())

    monitor.refresh_all(opener=server, delay_between=0, now=T0)

    assert sorted(server.resets_calls) == sorted([TOKEN_A, TOKEN_B])


def test_resets_gate_persistence_failure_is_not_fatal(world, monkeypatch):
    real = fsutil.app_lock
    calls = {"n": 0}

    def flaky(timeout=30.0):
        calls["n"] += 1
        if calls["n"] > 1:  # the token lookup is the first lock; fail the persist
            raise fsutil.LockTimeout("busy")
        return real(timeout)

    monkeypatch.setattr(fsutil, "app_lock", flaky)
    snap = monitor.refresh_account(ACCT_B, opener=FakeUsageServer(cedar=cedar_block()), now=T0)
    assert snap.ok and snap.resets is not None


def test_removed_accounts_lose_their_resets_row_on_a_full_refresh(world):
    monitor.refresh_all(opener=FakeUsageServer(cedar=cedar_block()), delay_between=0, now=T0)
    assert set(read_cache_file()["resets_cache"]) == {ACCT_A.id, ACCT_B.id}
    store.remove_account(ACCT_B.id)

    monitor.refresh_all(opener=FakeUsageServer(cedar=cedar_block()), delay_between=0, now=T0 + MIN)

    assert set(read_cache_file()["resets_cache"]) == {ACCT_A.id}


def test_history_is_unchanged_by_resets(world):
    monitor.refresh_all(opener=FakeUsageServer(cedar=cedar_block()), delay_between=0, now=T0)
    assert all(set(line) <= {"t", "id", "ok", "err", "u", "rs", "x"} for line in history_lines())


# --------------------------------------------------------------------------- snapshot shape


def test_snapshot_dict_shape_with_resets():
    snap = usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0)
    snap = UsageSnapshot(**{**snap.__dict__, "resets": usage.parse_resets(cedar_block())})

    wire = monitor.snapshot_to_dict(snap)

    assert list(wire) == [
        "account_id", "fetched_at", "ok", "error", "windows", "extra_usage", "promos", "resets",
        "breakdown", "breakdown_as_of", "raw",
    ]
    assert list(wire["resets"]) == ["eligible", "ineligible_reason", "at_limit", "weekly_resets_at", "cooldown_until", "grants"]
    assert wire["resets"]["weekly_resets_at"] == "2026-11-27T08:00:00+00:00"
    assert list(wire["resets"]["grants"][0]) == [
        "id", "label", "type", "resets_total", "resets_left", "usable_now", "use_requires_limit",
        "gives_back_weekly", "paused", "starts_at", "ends_at", "unavailable_reason",
    ]
    assert wire["resets"]["grants"][0]["ends_at"] == "2026-12-01T00:00:00+00:00"
    assert wire["resets"]["grants"][1]["paused"] is True
    json.dumps(wire)


def test_snapshot_with_resets_round_trips_through_json():
    snap = usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0)
    snap = UsageSnapshot(**{**snap.__dict__, "resets": usage.parse_resets(cedar_block())})

    back = monitor.snapshot_from_dict(json.loads(json.dumps(monitor.snapshot_to_dict(snap))))

    assert back == snap
    assert back.resets == snap.resets


def test_snapshot_without_resets_serialises_none_and_round_trips():
    snap = usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0)
    assert snap.resets is None
    wire = monitor.snapshot_to_dict(snap)
    assert wire["resets"] is None
    assert monitor.snapshot_from_dict(wire).resets is None


def test_cache_files_written_before_resets_existed_still_load(world):
    old = monitor.snapshot_to_dict(usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0))
    del old["resets"]
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"version": 1, "snapshots": {ACCT_A.id: old}, "backoff": {}, "alerts": {"fired": {}}}))

    cache = monitor.load_cache()

    assert cache[ACCT_A.id].resets is None and cache[ACCT_A.id].ok
    # ... and the next refresh upgrades the file in place
    monitor.refresh_all([ACCT_A], opener=FakeUsageServer(cedar=cedar_block()), delay_between=0, now=T0)
    assert monitor.load_cache()[ACCT_A.id].resets is not None


@pytest.mark.parametrize("bad", ["x", 5, [], {"grants": "no"}])
def test_a_damaged_resets_row_in_a_snapshot_does_not_lose_the_snapshot(bad):
    wire = monitor.snapshot_to_dict(usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0))
    wire["resets"] = bad
    snap = monitor.snapshot_from_dict(wire)
    assert snap.ok and snap.windows
    assert snap.resets is None or snap.resets.grants == ()


# --------------------------------------------------------------------------- "resets" alerts


def grant(id="g1", left=2, ends_at=None, starts_at=None, paused=False, label="Weekly reset", type="weekly"):
    return ResetGrant(
        id=id, label=label, type=type, resets_total=max(left, 3), resets_left=left, usable_now=True,
        paused=paused, starts_at=starts_at, ends_at=ends_at,
    )


def status_of(*grants, eligible=True) -> ResetStatus:
    return ResetStatus(eligible=eligible, grants=tuple(grants))


def reset_snap(account, status, when=T0):
    return UsageSnapshot(account.id, when, ok=True, windows=(win("seven_day", 10),), resets=status)


def reset_alerts(snapshots, now=T0, accounts=(ACCT_A, ACCT_B)):
    return [a for a in alerts_for(snapshots, accounts=accounts, now=now) if a.kind == "resets"]


def test_alert_when_resets_become_available():
    far = T0 + timedelta(days=10)
    (alert,) = reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2, ends_at=far)))})

    assert alert == monitor.Alert(ACCT_A.id, "resets", None, None, "work: 2 limit resets available", "Use within 10d 0h")


def test_singular_title_and_message_without_an_expiry():
    (alert,) = reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=1)))})
    assert alert.title == "work: 1 limit reset available"
    assert alert.message == "Run /limit-reset in Claude Code when you hit a limit."


def test_availability_alert_counts_only_usable_grants():
    far = T0 + timedelta(days=10)
    status = status_of(
        grant("a", left=2, ends_at=far),
        grant("paused", left=5, paused=True),
        grant("expired", left=4, ends_at=T0 - timedelta(days=1)),
        grant("future", left=3, starts_at=T0 + timedelta(days=1), ends_at=far),
    )
    (alert,) = reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status)})
    assert alert.title == "work: 2 limit resets available"


def test_availability_alert_fires_once_and_again_only_after_dropping_to_zero():
    far = T0 + timedelta(days=10)
    two = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2, ends_at=far)))}
    three = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=3, ends_at=far)))}
    none = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=0, ends_at=far)))}

    assert len(reset_alerts(two)) == 1
    assert reset_alerts(two) == []
    assert reset_alerts(three) == []  # 2 -> 3 is not 0 -> more
    assert reset_alerts(none) == []
    assert len(reset_alerts(two)) == 1  # 0 -> 2 again


def test_a_first_ever_snapshot_that_already_has_resets_announces_them():
    assert len(reset_alerts({ACCT_B.id: reset_snap(ACCT_B, status_of(grant(left=1)))})) == 1


@pytest.mark.parametrize(
    "status",
    [
        None,
        status_of(),
        status_of(grant(left=0)),
        status_of(grant(left=3, paused=True)),
        status_of(grant(left=3, ends_at=T0 - timedelta(seconds=1))),
        status_of(grant(left=3, ends_at=T0)),  # expires this instant
        status_of(grant(left=3, starts_at=T0 + timedelta(hours=1))),
        status_of(grant(left=3), eligible=False),
    ],
)
def test_no_availability_alert_when_nothing_is_usable(status):
    assert reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status)}) == []


def test_availability_state_survives_a_restart_and_transient_errors():
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2)))}
    assert len(reset_alerts(snaps)) == 1

    assert read_cache_file()["alerts"]["resets_left"] == {ACCT_A.id: 2}  # the only state: in the file
    assert reset_alerts({ACCT_A.id: err_snap(ACCT_A, "network")}) == []  # an error says nothing about resets
    assert reset_alerts(snaps) == []


def test_an_ok_snapshot_without_resets_counts_as_zero_so_a_later_grant_is_announced():
    alerts_for({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2)))})
    alerts_for({ACCT_A.id: reset_snap(ACCT_A, None)})  # no longer offered
    assert len(reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=1)))})) == 1


def test_expiry_alert_within_48_hours():
    ends = T0 + timedelta(hours=36)
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2, ends_at=ends)))}
    assert len(reset_alerts(snaps)) == 1  # availability (0 -> 2) ...

    later = T0 + timedelta(hours=2)
    # ... and the expiry warning was folded into it, so nothing more is said about this grant
    assert reset_alerts(snaps, now=later) == []


def test_expiry_alert_for_a_grant_that_was_already_announced_as_available():
    far = T0 + timedelta(days=10)
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2, ends_at=far)))}
    assert len(reset_alerts(snaps)) == 1
    assert reset_alerts(snaps, now=T0 + timedelta(days=5)) == []

    near = T0 + timedelta(days=8, hours=1)  # 47 h before the grant ends
    (alert,) = reset_alerts(snaps, now=near)
    assert alert == monitor.Alert(ACCT_A.id, "resets", None, None, "work: limit reset expires in 1d 23h", "Weekly reset: 2 left")
    assert reset_alerts(snaps, now=near + timedelta(hours=1)) == []  # once


def test_expiry_window_boundaries():
    ends = T0 + timedelta(days=10)
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2, ends_at=ends)))}
    reset_alerts(snaps)  # announce availability first

    assert reset_alerts(snaps, now=ends - timedelta(hours=48, seconds=1)) == []  # just outside
    assert len(reset_alerts(snaps, now=ends - timedelta(hours=48))) == 1  # exactly 48 h counts


@pytest.mark.parametrize(
    "extra",
    [
        {"paused": True},
        {"left": 0},
        {"ends_at": None},
    ],
)
def test_no_expiry_alert_for_paused_empty_or_open_ended_grants(extra):
    base = T0 + timedelta(days=30)
    usable = grant("keep", left=1, ends_at=base)  # keeps the account "available" so only expiry is in play
    odd = grant("odd", **{"left": 2, "ends_at": T0 + timedelta(hours=5), **extra})
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(usable, odd))}
    alerts_for({ACCT_A.id: reset_snap(ACCT_A, status_of(usable))})  # availability announced already

    assert reset_alerts(snaps) == []


def test_no_expiry_alert_for_an_already_expired_grant():
    base = T0 + timedelta(days=30)
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status_of(grant("keep", left=1, ends_at=base), grant("old", left=2, ends_at=T0 - timedelta(hours=1))))}
    assert len(reset_alerts(snaps)) == 1  # availability only
    assert reset_alerts(snaps) == []


def test_availability_and_expiry_together_make_one_alert_that_states_the_deadline():
    ends = T0 + timedelta(hours=30)
    (alert,) = reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=3, ends_at=ends)))})
    assert alert.title == "work: 3 limit resets available"
    assert alert.message == "Use within 1d 6h"


def test_several_expiring_grants_produce_one_alert_for_the_soonest():
    base = T0 + timedelta(days=30)
    status = status_of(
        grant("far", left=1, ends_at=base),
        grant("soon", left=2, ends_at=T0 + timedelta(days=29, hours=20), label="Session reset"),
        grant("sooner", left=1, ends_at=T0 + timedelta(days=29, hours=10), label="Full reset"),
    )
    snaps = {ACCT_A.id: reset_snap(ACCT_A, status)}
    reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant("far", left=1, ends_at=base)))})  # availability announced

    (alert,) = reset_alerts(snaps, now=T0 + timedelta(days=28))
    assert alert.title == "work: limit reset expires in 1d 10h"
    assert alert.message == "Full reset: 1 left"
    assert reset_alerts(snaps, now=T0 + timedelta(days=28, hours=1)) == []  # the others were consumed too


def test_resets_alerts_are_tracked_per_account():
    snaps = {
        ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=1))),
        ACCT_B.id: reset_snap(ACCT_B, status_of(grant(left=4))),
    }
    found = reset_alerts(snaps)
    assert [(a.account_id, a.title) for a in found] == [
        (ACCT_A.id, "work: 1 limit reset available"),
        (ACCT_B.id, "home: 4 limit resets available"),
    ]
    assert reset_alerts(snaps) == []


def test_removed_accounts_lose_their_resets_alert_state():
    alerts_for({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=2))), ACCT_B.id: reset_snap(ACCT_B, status_of(grant(left=2)))})
    assert set(read_cache_file()["alerts"]["resets_left"]) == {ACCT_A.id, ACCT_B.id}

    alerts_for({}, accounts=[ACCT_A])

    assert set(read_cache_file()["alerts"]["resets_left"]) == {ACCT_A.id}


def test_resets_alerts_coexist_with_usage_alerts_on_one_snapshot():
    snap = UsageSnapshot(
        ACCT_A.id, T0, ok=True, windows=(win("seven_day", 96),), resets=status_of(grant(left=2)),
    )
    found = alerts_for({ACCT_A.id: snap})
    assert sorted(a.kind for a in found) == ["resets", "threshold"]
    assert alerts_for({ACCT_A.id: snap}) == []


def test_old_alert_state_without_a_resets_section_still_works():
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"alerts": {"fired": {}, "auth_alerted": [], "high": {}}}))
    assert len(reset_alerts({ACCT_A.id: reset_snap(ACCT_A, status_of(grant(left=1)))})) == 1


def test_end_to_end_resets_flow_from_http_to_alert(world):
    server = FakeUsageServer(cedar=cedar_block(weekly_left=2))
    snaps = monitor.refresh_all(opener=server, delay_between=0, now=T0)

    found = [a for a in monitor.detect_alerts(snaps, [ACCT_A, ACCT_B], [80, 95, 100], now=T0) if a.kind == "resets"]

    assert [(a.account_id, a.title) for a in found] == [
        (ACCT_A.id, "work: 2 limit resets available"),
        (ACCT_B.id, "home: 2 limit resets available"),
    ]
    snaps = monitor.refresh_all(opener=server, delay_between=0, now=T0 + 31 * MIN)
    assert [a for a in monitor.detect_alerts(snaps, [ACCT_A, ACCT_B], [80, 95, 100], now=T0 + 31 * MIN) if a.kind == "resets"] == []


# =========================================================================== a failure must not erase the last good numbers

TRANSIENT_CODES = ["rate_limited", "network", "token_stale", "busy", "keychain_error", "http_500", "http_502", "http_503", "internal_error"]
PERMANENT_CODES = ["auth_expired", "no_credentials"]


def good_snap(account=ACCT_B, session=62.0, weekly=41.0, when=T0) -> UsageSnapshot:
    return UsageSnapshot(
        account.id, when, ok=True,
        windows=(win("five_hour", session, resets_at=when + timedelta(hours=3)), win("seven_day", weekly, resets_at=when + timedelta(days=3))),
    )


def errors_row(account=ACCT_B):
    return read_cache_file()["errors"].get(account.id)


def test_cache_file_layout_has_a_separate_errors_section(world):
    monitor.refresh_all([ACCT_B], opener=FakeUsageServer(), delay_between=0, now=T0)
    assert list(read_cache_file()) == ["version", "snapshots", "backoff", "errors", "resets_cache", "alerts"]
    assert read_cache_file()["errors"] == {}


# --------------------------------------------------------------------------- the headline scenario


def test_429_after_a_good_refresh_keeps_62_and_41_and_reports_stale(world):
    """Session 62%, weekly 41% at 10:00. At 10:12 a 429 arrives. The user must still see 62% / 41%, 12 minutes old."""
    server = FakeUsageServer(usage_payload(session=62.0, weekly=41.0))
    first = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)[ACCT_B.id]
    assert first.ok

    server.replies[TOKEN_B] = http_error(429, {"Retry-After": "600"})
    returned = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 12 * MIN)[ACCT_B.id]

    kept = monitor.load_cache()[ACCT_B.id]
    assert kept == first  # the stored snapshot was not touched
    assert (kept.ok, kept.session.utilization, kept.weekly.utilization) == (True, 62.0, 41.0)
    assert kept.fetched_at == T0  # ... and fetched_at is how old the numbers are
    assert returned == first  # refresh_all answers exactly as load_cache does

    status = monitor.cache_status(ACCT_B.id, now=T0 + 12 * MIN)
    assert status["snapshot"] == first
    assert status["age_seconds"] == 720.0
    assert status["last_error"] == "rate_limited"
    assert status["last_error_at"] == T0 + 12 * MIN
    assert status["stale"] is True

    # the failure sits beside the snapshot, together with the backoff
    assert errors_row() == {"last_error": "rate_limited", "last_error_at": (T0 + 12 * MIN).isoformat()}
    assert backoff_row()["next_allowed_at"] == (T0 + 22 * MIN).isoformat()


def test_the_next_success_clears_the_error_and_replaces_the_numbers(world):
    server = FakeUsageServer(usage_payload(session=62.0, weekly=41.0))
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = http_error(429, {"Retry-After": "600"})
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 12 * MIN)

    server.replies[TOKEN_B] = usage_payload(session=70.0, weekly=43.0)
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 23 * MIN)  # backoff over

    status = monitor.cache_status(ACCT_B.id, now=T0 + 25 * MIN)
    assert (status["last_error"], status["last_error_at"], status["stale"]) == (None, None, False)
    assert status["snapshot"].session.utilization == 70.0 and status["snapshot"].fetched_at == T0 + 23 * MIN
    assert status["age_seconds"] == 120.0
    assert errors_row() is None and backoff_row() is None


def test_auth_expired_after_a_good_refresh_makes_the_numbers_go_away(world):
    server = FakeUsageServer(usage_payload(session=62.0, weekly=41.0))
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)

    server.replies[TOKEN_B] = http_error(401)
    returned = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 5 * MIN)[ACCT_B.id]

    stored = monitor.load_cache()[ACCT_B.id]
    assert (stored.ok, stored.error, stored.windows) == (False, "auth_expired", ())  # the good numbers are gone
    assert returned == stored
    status = monitor.cache_status(ACCT_B.id, now=T0 + 6 * MIN)
    assert status["snapshot"] == stored
    assert status["age_seconds"] is None  # no numbers, so no age
    assert (status["last_error"], status["last_error_at"], status["stale"]) == ("auth_expired", T0 + 5 * MIN, True)


def test_a_live_account_waiting_for_claude_code_keeps_its_numbers_overnight(world):
    """A is live. Overnight Claude Code is idle, so the live token expires: token_stale on every poll, numbers stay."""
    server = FakeUsageServer(usage_payload(session=30.0, weekly=55.0))
    monitor.refresh_all([ACCT_A], opener=server, delay_between=0, now=T0)

    put_live(world, make_login(ACCOUNT_A, "live-a", EXPIRED_MS))
    for hour in range(1, 9):
        monitor.refresh_all([ACCT_A], opener=server, delay_between=0, now=T0 + timedelta(hours=hour))

    status = monitor.cache_status(ACCT_A.id, now=T0 + timedelta(hours=8))
    assert status["snapshot"].ok and status["snapshot"].weekly.utilization == 55.0
    assert status["age_seconds"] == 8 * 3600
    assert (status["last_error"], status["stale"]) == ("token_stale", True)
    assert status["last_error_at"] == T0 + timedelta(hours=8)  # the *latest* failed attempt

    # Claude Code woke up and refreshed the live login: the next poll succeeds and everything clears.
    put_live(world, make_login(ACCOUNT_A, "live-a", FAR_FUTURE_MS))
    monitor.refresh_all([ACCT_A], opener=server, delay_between=0, now=T0 + timedelta(hours=9))
    assert monitor.cache_status(ACCT_A.id, now=T0 + timedelta(hours=9))["stale"] is False


# --------------------------------------------------------------------------- which failures keep the numbers


@pytest.mark.parametrize("code", TRANSIENT_CODES)
def test_transient_failures_keep_the_last_good_snapshot(code):
    good = good_snap()
    monitor.save_cache({ACCT_B.id: good})

    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + 5 * MIN, ok=False, error=code)})

    assert monitor.load_cache() == {ACCT_B.id: good}
    status = monitor.cache_status(ACCT_B.id, now=T0 + 5 * MIN)
    assert (status["stale"], status["last_error"], status["age_seconds"]) == (True, code, 300.0)


@pytest.mark.parametrize("code", PERMANENT_CODES)
def test_permanent_failures_replace_the_snapshot(code):
    monitor.save_cache({ACCT_B.id: good_snap()})

    failed = UsageSnapshot(ACCT_B.id, T0 + 5 * MIN, ok=False, error=code)
    monitor.save_cache({ACCT_B.id: failed})

    assert monitor.load_cache() == {ACCT_B.id: failed}
    status = monitor.cache_status(ACCT_B.id, now=T0 + 6 * MIN)
    assert (status["stale"], status["last_error"], status["age_seconds"]) == (True, code, None)


@pytest.mark.parametrize("code", ["bad_response", "http_404", "http_400", "something_new"])
def test_unlisted_codes_do_not_count_as_a_dead_login_so_they_keep_the_numbers(code):
    good = good_snap()
    monitor.save_cache({ACCT_B.id: good})
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error=code)})
    assert monitor.load_cache()[ACCT_B.id] == good


def test_the_permanent_set_is_exactly_the_two_login_errors():
    assert monitor.PERMANENT_ERRORS == {"auth_expired", "no_credentials"}


@pytest.mark.parametrize("code", TRANSIENT_CODES + PERMANENT_CODES)
def test_with_no_good_snapshot_the_failure_itself_is_stored_so_the_account_shows_why(code):
    failed = UsageSnapshot(ACCT_B.id, T0, ok=False, error=code)
    monitor.save_cache({ACCT_B.id: failed})

    assert monitor.load_cache() == {ACCT_B.id: failed}
    status = monitor.cache_status(ACCT_B.id, now=T0 + MIN)
    assert (status["snapshot"], status["age_seconds"], status["last_error"], status["stale"]) == (failed, None, code, True)


def test_failures_keep_replacing_each_other_when_there_is_nothing_good_to_protect():
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0, ok=False, error="auth_expired")})
    later = UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error="network")
    monitor.save_cache({ACCT_B.id: later})
    assert monitor.load_cache()[ACCT_B.id] == later  # the newest reason wins; the dead numbers do not come back


def test_a_good_snapshot_survives_many_failures_in_a_row():
    good = good_snap()
    monitor.save_cache({ACCT_B.id: good})
    for minute, code in enumerate(["network", "http_500", "rate_limited", "busy"], start=1):
        monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + minute * MIN, ok=False, error=code)})
    assert monitor.load_cache()[ACCT_B.id] == good
    assert monitor.cache_status(ACCT_B.id, now=T0 + 5 * MIN)["last_error"] == "busy"


# --------------------------------------------------------------------------- recording and clearing failures


def test_a_success_older_than_the_recorded_failure_does_not_clear_it():
    """`save_cache(load_cache())` re-stores the old good snapshot; that is not a new success."""
    monitor.save_cache({ACCT_B.id: good_snap(when=T0)})
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + 10 * MIN, ok=False, error="network")})

    monitor.save_cache(monitor.load_cache())

    status = monitor.cache_status(ACCT_B.id, now=T0 + 11 * MIN)
    assert (status["stale"], status["last_error"]) == (True, "network")


def test_a_success_at_or_after_the_failure_clears_it():
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0, ok=False, error="network")})
    monitor.save_cache({ACCT_B.id: good_snap(when=T0)})  # same instant counts
    assert monitor.cache_status(ACCT_B.id)["stale"] is False


def test_errors_are_tracked_per_account():
    monitor.save_cache({ACCT_A.id: good_snap(ACCT_A), ACCT_B.id: good_snap(ACCT_B)})
    monitor.save_cache(
        {ACCT_A.id: good_snap(ACCT_A, when=T0 + MIN), ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error="network")}
    )
    assert monitor.cache_status(ACCT_A.id)["stale"] is False
    assert monitor.cache_status(ACCT_B.id)["stale"] is True


def test_accounts_missing_from_save_cache_lose_their_snapshot_and_error_record():
    monitor.save_cache({ACCT_A.id: good_snap(ACCT_A), ACCT_B.id: good_snap(ACCT_B)})
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error="network")})
    assert set(read_cache_file()["errors"]) == {ACCT_B.id}

    monitor.save_cache({ACCT_A.id: good_snap(ACCT_A)})

    assert read_cache_file()["errors"] == {} and list(monitor.load_cache()) == [ACCT_A.id]


def test_full_refresh_forgets_error_records_of_removed_accounts(world):
    server = FakeUsageServer()
    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = urllib.error.URLError("down")
    monitor.refresh_all(opener=server, delay_between=0, now=T0 + MIN)
    assert set(read_cache_file()["errors"]) == {ACCT_B.id}
    store.remove_account(ACCT_B.id)

    monitor.refresh_all(opener=server, delay_between=0, now=T0 + 2 * MIN)

    assert read_cache_file()["errors"] == {}


def test_a_backed_off_cycle_leaves_the_failure_record_alone(world):
    """While an account waits out its 429 it is not fetched, and returning its (old) good snapshot is not a success."""
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = http_error(429, {"Retry-After": "600"})
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 5 * MIN)
    row = errors_row()

    skipped = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 8 * MIN)

    assert skipped[ACCT_B.id].ok and skipped[ACCT_B.id].fetched_at == T0
    assert errors_row() == row
    assert monitor.cache_status(ACCT_B.id, now=T0 + 8 * MIN)["stale"] is True
    assert len(server.calls) == 2  # no request was made in the skipped cycle


def test_one_account_failing_does_not_disturb_the_other(world):
    server = FakeUsageServer()
    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = urllib.error.URLError("down")

    results = monitor.refresh_all(opener=server, delay_between=0, now=T0 + 10 * MIN)

    assert list(results) == [ACCT_A.id, ACCT_B.id]  # registry order
    assert results[ACCT_A.id].fetched_at == T0 + 10 * MIN
    assert results[ACCT_B.id].fetched_at == T0  # B shows its last good numbers
    assert monitor.cache_status(ACCT_A.id)["stale"] is False and monitor.cache_status(ACCT_B.id)["stale"] is True


def test_refresh_all_answers_like_the_cache_even_if_persisting_fails(world, monkeypatch):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    good = monitor.load_cache()[ACCT_B.id]
    server.replies[TOKEN_B] = urllib.error.URLError("down")

    def broken(path, data, mode=0o600):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil, "atomic_write_json", broken)
    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 10 * MIN)

    assert results[ACCT_B.id] == good  # still the last good numbers, not the failed attempt


def test_refresh_all_answers_like_the_cache_when_the_persist_lock_cannot_be_taken(world, monkeypatch):
    """Nothing could be merged under the lock, so the answer is computed in memory from the previous cache."""
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    good = monitor.load_cache()[ACCT_B.id]
    cache_before = paths.usage_cache_file().read_bytes()
    server.replies[TOKEN_B] = urllib.error.URLError("down")

    real, calls = fsutil.app_lock, {"n": 0}

    def flaky(timeout=30.0):
        calls["n"] += 1
        if calls["n"] > 1:  # the token lookup (first lock) works; the final persist does not
            raise fsutil.LockTimeout("busy")
        return real(timeout)

    monkeypatch.setattr(fsutil, "app_lock", flaky)
    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 10 * MIN)

    assert results[ACCT_B.id] == good  # last good numbers, not the failed attempt
    assert paths.usage_cache_file().read_bytes() == cache_before  # and, as logged, nothing was written


def test_refresh_account_still_returns_the_raw_failed_attempt_and_stores_nothing_about_it(world):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    stored_before = read_cache_file()
    server.replies[TOKEN_B] = http_error(500)

    attempt = monitor.refresh_account(ACCT_B, opener=server, now=T0 + MIN)

    assert (attempt.ok, attempt.error) == (False, "http_500")
    after = read_cache_file()
    assert after["snapshots"] == stored_before["snapshots"] and after["errors"] == {}


# --------------------------------------------------------------------------- history stays clean


def test_history_gets_a_line_only_for_successful_refreshes(world):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = urllib.error.URLError("down")
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + MIN)
    server.replies[TOKEN_B] = http_error(401)
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 2 * MIN)

    assert [(row["id"], row["t"], row["ok"]) for row in history_lines()] == [(ACCT_B.id, T0.isoformat(), True)]


# --------------------------------------------------------------------------- cache_status


def test_cache_status_shape_and_types(world):
    monitor.refresh_all([ACCT_B], opener=FakeUsageServer(), delay_between=0, now=T0)
    status = monitor.cache_status(ACCT_B.id, now=T0 + 90 * MIN)

    assert list(status) == ["snapshot", "age_seconds", "last_error", "last_error_at", "stale"]
    assert isinstance(status["snapshot"], UsageSnapshot) and status["snapshot"].ok
    assert isinstance(status["age_seconds"], float) and status["age_seconds"] == 5400.0
    assert (status["last_error"], status["last_error_at"], status["stale"]) == (None, None, False)


def test_cache_status_for_an_unknown_account_or_an_empty_cache():
    assert monitor.cache_status("nobody") == {
        "snapshot": None, "age_seconds": None, "last_error": None, "last_error_at": None, "stale": False,
    }


def test_cache_status_age_defaults_to_the_wall_clock_and_never_goes_negative():
    monitor.save_cache({ACCT_B.id: good_snap(when=datetime.now(timezone.utc) - timedelta(seconds=90))})
    assert 89 <= monitor.cache_status(ACCT_B.id)["age_seconds"] < 120

    monitor.save_cache({ACCT_B.id: good_snap(when=T0 + timedelta(hours=1))})  # fetched "in the future"
    assert monitor.cache_status(ACCT_B.id, now=T0)["age_seconds"] == 0.0


def test_cache_status_reads_caches_written_before_failures_were_recorded_separately():
    fsutil.ensure_private_dir(paths.app_dir())
    old_failure = monitor.snapshot_to_dict(UsageSnapshot(ACCT_B.id, T0, ok=False, error="network"))
    paths.usage_cache_file().write_text(json.dumps({"version": 1, "snapshots": {ACCT_B.id: old_failure}}))

    status = monitor.cache_status(ACCT_B.id, now=T0 + MIN)

    assert (status["last_error"], status["last_error_at"], status["stale"], status["age_seconds"]) == ("network", T0, True, None)


@pytest.mark.parametrize("junk", ["x", 5, [], {"last_error": 5}, {"last_error": ""}, {"last_error_at": "x"}])
def test_a_damaged_error_record_is_ignored_rather_than_crashing(junk):
    monitor.save_cache({ACCT_B.id: good_snap()})
    state = read_cache_file()
    state["errors"] = {ACCT_B.id: junk}
    paths.usage_cache_file().write_text(json.dumps(state))

    status = monitor.cache_status(ACCT_B.id)

    assert status["snapshot"].ok and status["stale"] is False  # nothing usable recorded, so nothing to report
    monitor.save_cache(monitor.load_cache())  # and writing over it works


def test_a_damaged_errors_section_reads_as_empty():
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"errors": "nope", "snapshots": {}}))
    assert monitor.cache_status(ACCT_B.id)["stale"] is False


def test_cache_status_needs_no_lock_and_writes_nothing(world):
    monitor.save_cache({ACCT_B.id: good_snap()})
    before = paths.usage_cache_file().read_bytes()
    with fsutil.app_lock(timeout=1):  # a refresh in progress must not block the menu from drawing
        status = monitor.cache_status(ACCT_B.id)
    assert status["snapshot"].ok
    assert paths.usage_cache_file().read_bytes() == before


def test_cache_status_snapshot_is_exactly_what_load_cache_returns(world):
    server = FakeUsageServer()
    monitor.refresh_all(opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = http_error(401)
    monitor.refresh_all(opener=server, delay_between=0, now=T0 + MIN)

    cache = monitor.load_cache()
    assert all(monitor.cache_status(account_id)["snapshot"] == snapshot for account_id, snapshot in cache.items())


def test_no_secrets_in_the_recorded_failures(world):
    server = FakeUsageServer()
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    server.replies[TOKEN_B] = http_error(429, {"Retry-After": "60"})
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + MIN)
    assert "sk-ant" not in paths.usage_cache_file().read_text()


# =========================================================================== synthetic example responses

from pathlib import Path  # noqa: E402

from cc_switcher.models import BreakdownRow  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_AT = datetime(2025, 2, 1, 4, 2, 11, tzinfo=timezone.utc)


def example_usage() -> dict:
    return json.loads((FIXTURES / "usage_example.json").read_text())


def example_resets_block() -> dict:
    return json.loads((FIXTURES / "resets_example_ineligible.json").read_text())["cedar_ember"]


def example_snapshot(account=ACCT_A, with_resets=True) -> UsageSnapshot:
    snap = usage.parse_usage(example_usage(), account.id, EXAMPLE_AT)
    if with_resets:
        snap = UsageSnapshot(**{**snap.__dict__, "resets": usage.parse_resets(example_resets_block())})
    return snap


# --------------------------------------------------------------------------- serialization


def test_example_response_round_trips_through_the_cache_unchanged():
    snap = example_snapshot()
    wire = json.loads(json.dumps(monitor.snapshot_to_dict(snap)))

    back = monitor.snapshot_from_dict(wire)

    assert back == snap
    assert back.extra_usage.spend_limit_reached is True and back.extra_usage.user_disabled is False
    assert back.extra_usage.decimal_places == 2
    assert [r.key for r in back.breakdown] == ["claude_code", "chat", "cowork", "other"]
    assert back.breakdown_as_of == datetime(2025, 2, 1, 4, 2, 10, 345678, tzinfo=timezone.utc)
    assert back.resets.eligible is False and back.resets.ineligible_reason == "surface" and back.resets.grants == ()


def test_example_response_snapshot_dict_carries_the_new_fields():
    wire = monitor.snapshot_to_dict(example_snapshot())

    assert wire["extra_usage"] == {
        "enabled": False,
        "monthly_limit": 5000.0,
        "used_credits": 6125.0,
        "utilization": 100.0,
        "currency": "USD",
        "disabled_reason": "org_level_disabled_until",
        "spend_limit_reached": True,
        "user_disabled": False,
        "decimal_places": 2,
    }
    assert wire["breakdown"][0] == {"key": "claude_code", "display_name": "Claude Code", "percent": 100.0, "percent_of_limit": 31.0}
    assert len(wire["breakdown"]) == 4
    assert wire["breakdown_as_of"] == "2025-02-01T04:02:10.345678+00:00"
    assert [w["key"] for w in wire["windows"]] == ["five_hour", "seven_day"]
    assert [w["severity"] for w in wire["windows"]] == ["normal", "normal"]
    assert wire["resets"] == example_resets_block()


def test_example_response_through_the_cache_file(world):
    monitor.save_cache({ACCT_A.id: example_snapshot()})
    assert monitor.load_cache() == {ACCT_A.id: example_snapshot()}
    assert monitor.cache_status(ACCT_A.id, now=EXAMPLE_AT + 12 * MIN)["age_seconds"] == 720.0


def test_a_snapshot_without_breakdown_serialises_empty_and_round_trips():
    snap = usage.parse_usage(usage_payload(), ACCT_A.id, fetched_at=T0)
    wire = monitor.snapshot_to_dict(snap)
    assert wire["breakdown"] == [] and wire["breakdown_as_of"] is None
    assert monitor.snapshot_from_dict(wire) == snap


def test_cache_rows_written_before_these_fields_existed_still_load():
    old = monitor.snapshot_to_dict(example_snapshot())
    for name in ("spend_limit_reached", "user_disabled", "decimal_places"):
        del old["extra_usage"][name]
    del old["breakdown"], old["breakdown_as_of"]

    snap = monitor.snapshot_from_dict(old)

    assert snap.ok and [w.key for w in snap.windows] == ["five_hour", "seven_day"]
    assert (snap.extra_usage.spend_limit_reached, snap.extra_usage.user_disabled, snap.extra_usage.decimal_places) == (None, None, None)
    assert snap.extra_usage.used_credits == 6125.0
    assert (snap.breakdown, snap.breakdown_as_of) == ((), None)

    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"version": 1, "snapshots": {ACCT_A.id: old}}))
    assert monitor.load_cache()[ACCT_A.id] == snap  # and through the real cache path


@pytest.mark.parametrize("junk", [None, "x", 5, [None, 5, "x", [], {}, {"key": ""}, {"key": 7}, {"percent": 3}]])
def test_damaged_breakdown_in_the_cache_is_dropped_not_fatal(junk):
    wire = monitor.snapshot_to_dict(example_snapshot())
    wire["breakdown"] = junk
    snap = monitor.snapshot_from_dict(wire)
    assert snap.ok and snap.breakdown == () and snap.extra_usage is not None


@pytest.mark.parametrize("section", ["windows", "promos", "breakdown"])
@pytest.mark.parametrize("junk", [5, "x", {"a": 1}, True])
def test_a_list_section_holding_something_else_does_not_cost_the_snapshot(section, junk):
    """snapshot_from_dict promises ValueError only for a missing id/time; a damaged section just reads as empty."""
    wire = monitor.snapshot_to_dict(example_snapshot())
    wire[section] = junk
    snap = monitor.snapshot_from_dict(wire)
    assert snap.ok and snap.account_id == ACCT_A.id
    assert getattr(snap, section) == ()


def test_damaged_breakdown_rows_are_skipped_individually_and_numbers_are_checked():
    wire = monitor.snapshot_to_dict(example_snapshot())
    wire["breakdown"] = [None, {"key": "claude_code", "display_name": None, "percent": "x", "percent_of_limit": 26}, {"key": "chat", "percent": 1}]
    wire["breakdown_as_of"] = "never"
    snap = monitor.snapshot_from_dict(wire)
    assert snap.breakdown == (BreakdownRow("claude_code", "Claude Code", None, 26.0), BreakdownRow("chat", "Chat", 1.0, None))
    assert snap.breakdown_as_of is None


@pytest.mark.parametrize("field, junk", [("spend_limit_reached", "yes"), ("user_disabled", 0), ("decimal_places", "2"), ("decimal_places", 99), ("decimal_places", -1)])
def test_damaged_new_extra_usage_fields_in_the_cache_become_none(field, junk):
    wire = monitor.snapshot_to_dict(example_snapshot())
    wire["extra_usage"][field] = junk
    assert getattr(monitor.snapshot_from_dict(wire).extra_usage, field) is None


# --------------------------------------------------------------------------- end to end from synthetic example responses


def test_an_example_response_and_example_resets_block_through_refresh_all(world):
    server = FakeUsageServer(default=example_usage(), cedar=example_resets_block())

    results = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=EXAMPLE_AT)

    snap = results[ACCT_B.id]
    assert snap.ok and [(w.key, w.utilization) for w in snap.windows] == [("five_hour", 7.0), ("seven_day", 31.0)]
    assert snap.extra_usage.spend_limit_reached is True and snap.extra_usage.decimal_places == 2
    assert len(snap.breakdown) == 4 and snap.breakdown_as_of is not None
    assert snap.resets is not None and snap.resets.eligible is False and snap.resets.grants == ()
    assert monitor.load_cache()[ACCT_B.id] == snap  # exactly what was stored
    assert "sk-ant" not in paths.usage_cache_file().read_text()


def test_a_failed_refresh_keeps_the_example_breakdown_and_extra_usage_too(world):
    server = FakeUsageServer(default=example_usage(), cedar=example_resets_block())
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=EXAMPLE_AT)
    server.replies[TOKEN_B] = urllib.error.URLError("down")

    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=EXAMPLE_AT + 10 * MIN)

    kept = monitor.load_cache()[ACCT_B.id]
    assert kept.ok and len(kept.breakdown) == 4 and kept.extra_usage.used_credits == 6125.0


def test_the_example_response_makes_one_history_line_with_both_windows(world):
    server = FakeUsageServer(default=example_usage(), cedar=example_resets_block())
    monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=EXAMPLE_AT)
    (line,) = history_lines()
    assert line["u"] == {"five_hour": 7.0, "seven_day": 31.0}
    # extra usage is switched off on this account but its $61.25 of $50.00 is still recorded (minor units)
    assert line["x"] == {"used": 6125.0, "limit": 5000.0, "cur": "USD"}


# --------------------------------------------------------------------------- alerts must stay quiet


def test_the_example_account_raises_no_alerts_now_or_on_repeat():
    snaps = {ACCT_A.id: example_snapshot()}
    assert alerts_for(snaps, now=EXAMPLE_AT) == []
    assert alerts_for(snaps, now=EXAMPLE_AT + timedelta(hours=1)) == []
    assert read_cache_file()["alerts"]["fired"] == {}


def test_an_ineligible_resets_status_alone_is_silent():
    # 7% / 31% are far below every threshold, so any alert here would have to come from the resets block
    assert alerts_for({ACCT_A.id: example_snapshot(with_resets=True)}) == []
    assert read_cache_file()["alerts"]["resets_left"] == {ACCT_A.id: 0}


def test_disabled_extra_usage_at_100_percent_stays_silent():
    """Example: extra usage off, $61.25 of $50.00 recorded. Nothing is being spent, so no alert."""
    snap = example_snapshot(with_resets=False)
    assert snap.extra_usage.enabled is False and snap.extra_usage.utilization == 100.0
    assert alerts_for({ACCT_A.id: snap}, now=EXAMPLE_AT) == []
    assert alerts_for({ACCT_A.id: snap}, now=EXAMPLE_AT + timedelta(days=3)) == []
    assert read_cache_file()["alerts"]["extra_usage"] == {}


def test_example_window_numbers_still_alert_normally_when_they_climb():
    """The same example shape at 96% weekly: the quiet result above is down to the numbers, not to a blind spot."""
    raw = example_usage()
    raw["seven_day"]["utilization"] = 96.0
    snap = usage.parse_usage(raw, ACCT_A.id, EXAMPLE_AT)
    found = alerts_for({ACCT_A.id: snap}, now=EXAMPLE_AT)
    assert [(a.kind, a.window_key, a.threshold) for a in found] == [("threshold", "seven_day", 95)]


# =========================================================================== history records extra usage whether or not it is enabled


def history_snap(extra, when=T0) -> UsageSnapshot:
    return UsageSnapshot(ACCT_A.id, when, ok=True, windows=(win("seven_day", 10.0),), extra_usage=extra)


def test_history_logs_used_and_limit_for_extra_usage_that_is_switched_off():
    monitor.append_history({ACCT_A.id: history_snap(ExtraUsage(False, 1000.0, 6125.0, 100.0, "USD", "org_level_disabled_until", True, False, 2))})
    (line,) = history_lines()
    assert line["x"] == {"used": 6125.0, "limit": 1000.0, "cur": "USD"}  # $61.25 of $10.00, in minor units


def test_history_logs_enabled_extra_usage_the_same_way():
    monitor.append_history({ACCT_A.id: history_snap(ExtraUsage(True, 5000.0, 1234.0, 24.7, "EUR"))})
    assert history_lines()[0]["x"] == {"used": 1234.0, "limit": 5000.0, "cur": "EUR"}


@pytest.mark.parametrize(
    "extra",
    [None, ExtraUsage(False, None, None, None), ExtraUsage(True, None, None, None, "USD")],
)
def test_history_skips_extra_usage_with_no_amounts_at_all(extra):
    monitor.append_history({ACCT_A.id: history_snap(extra)})
    assert "x" not in history_lines()[0]


def test_history_logs_a_limit_even_when_nothing_is_used_yet():
    monitor.append_history({ACCT_A.id: history_snap(ExtraUsage(False, 1000.0, None, None, "USD"))})
    assert history_lines()[0]["x"] == {"used": None, "limit": 1000.0, "cur": "USD"}


def test_history_entry_stays_compact():
    monitor.append_history({ACCT_A.id: history_snap(ExtraUsage(False, 1000.0, 6125.0, 100.0, "USD", "org_level_disabled_until", True, False, 2))})
    text = paths.usage_history_file().read_text()
    assert text.count("\n") == 1 and ", " not in text and "org_level" not in text and "spend_limit" not in text


# =========================================================================== extra-usage alerts


def extra_snap(account, utilization, *, used=None, limit=1000.0, enabled=True, when=T0, currency="USD", places=2) -> UsageSnapshot:
    return UsageSnapshot(
        account.id, when, ok=True, windows=(win("seven_day", 10.0),),
        extra_usage=ExtraUsage(enabled, limit, used, utilization, currency, None, None, None, places),
    )


def extra_alerts(snapshots, now=T0, accounts=(ACCT_A, ACCT_B)):
    return [a for a in alerts_for(snapshots, accounts=accounts, now=now) if a.kind == "extra_usage"]


def test_the_alert_text_matches_the_spec():
    (alert,) = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 81.2, used=812.0)})
    assert alert == monitor.Alert(
        ACCT_A.id, "extra_usage", None, 80, "work: extra usage 80% of $10.00", "$8.12 used, $1.88 left this month"
    )


@pytest.mark.parametrize(
    "percent, used, expected_threshold",
    [
        (0.0, 0.0, None),
        (79.0, 790.0, None),
        (79.99, 799.9, None),
        (80.0, 800.0, 80),
        (99.9, 999.0, 80),
        (100.0, 1000.0, 100),
        (113.5, 6125.0, 100),  # over the cap: still the 100% alert, not a new threshold
    ],
)
def test_thresholds_79_80_100_and_over(percent, used, expected_threshold):
    found = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, percent, used=used)})
    assert [a.threshold for a in found] == ([expected_threshold] if expected_threshold else [])


def test_the_100_percent_alert_states_nothing_left():
    (alert,) = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=6125.0)})
    assert alert.title == "work: extra usage 100% of $10.00"
    assert alert.message == "$61.25 used, $0.00 left this month"  # never a negative amount


def test_80_then_100_are_two_separate_announcements_each_once():
    at_80 = {ACCT_A.id: extra_snap(ACCT_A, 80.0, used=800.0)}
    at_100 = {ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0)}

    assert [a.threshold for a in extra_alerts(at_80)] == [80]
    assert extra_alerts(at_80) == []
    assert [a.threshold for a in extra_alerts(at_100)] == [100]  # 80 was already announced
    assert extra_alerts(at_100) == [] and extra_alerts(at_80) == []


def test_jumping_straight_to_100_reports_once_and_consumes_the_80():
    assert [a.threshold for a in extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0)})] == [100]
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0)}) == []  # no late "80%" afterwards


def test_a_restart_does_not_renotify():
    snaps = {ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0)}
    assert len(extra_alerts(snaps)) == 1
    # "Restart": nothing in memory; the cache file is the only state.
    assert read_cache_file()["alerts"]["extra_usage"] == {ACCT_A.id: {"month": "2026-11", "fired": [80, 100]}}
    assert extra_alerts(snaps) == [] and extra_alerts(snaps, now=T0 + timedelta(hours=6)) == []


def test_a_new_month_re_arms_both_thresholds():
    november = {ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, when=T0)}
    assert [a.threshold for a in extra_alerts(november)] == [100]

    dec = T0 + timedelta(days=10)  # 2026-12-02
    december = {ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=dec)}
    assert [a.threshold for a in extra_alerts(december, now=dec)] == [80]
    assert extra_alerts(december, now=dec) == []
    assert read_cache_file()["alerts"]["extra_usage"][ACCT_A.id] == {"month": "2026-12", "fired": [80]}

    december_full = {ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, when=dec + MIN)}
    assert [a.threshold for a in extra_alerts(december_full, now=dec)] == [100]


def test_the_year_rollover_re_arms_too():
    alerts_for({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, when=datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc))})
    found = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, when=datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc))})
    assert [a.threshold for a in found] == [100]


def test_the_month_is_the_utc_month_of_the_snapshot():
    late_nov_utc = datetime(2026, 12, 1, 5, 0, tzinfo=timezone(timedelta(hours=10)))  # = 2026-11-30 19:00 UTC
    extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=late_nov_utc)})
    assert read_cache_file()["alerts"]["extra_usage"][ACCT_A.id]["month"] == "2026-11"


def test_the_month_comes_from_fetched_at_not_from_the_clock_of_the_call():
    extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=T0)}, now=T0 + timedelta(days=40))
    assert read_cache_file()["alerts"]["extra_usage"][ACCT_A.id]["month"] == "2026-11"


def test_a_threshold_is_not_announced_again_later_in_the_same_month():
    """The window alerts' markers are pruned after 14 days; this one must not be."""
    start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    assert len(extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=start)}, now=start)) == 1

    later = start + timedelta(days=28)  # well past FIRED_RETENTION, still November
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=later)}, now=later) == []


def test_data_from_an_earlier_month_than_the_record_is_ignored():
    dec = T0 + timedelta(days=10)
    extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0, when=dec)}, now=dec)  # record: December

    stale_november = {ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, when=T0)}
    assert extra_alerts(stale_november, now=dec) == []
    assert read_cache_file()["alerts"]["extra_usage"][ACCT_A.id] == {"month": "2026-12", "fired": [80]}  # untouched


@pytest.mark.parametrize("percent", [80.0, 100.0, 250.0])
def test_disabled_extra_usage_is_silent_at_any_level(percent):
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, percent, used=percent * 10, enabled=False)}) == []
    assert read_cache_file()["alerts"]["extra_usage"] == {}


def test_enabling_extra_usage_later_in_the_month_starts_the_alerts():
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, enabled=False)}) == []
    assert [a.threshold for a in extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 100.0, used=1000.0, enabled=True, when=T0 + MIN)})] == [100]


def test_the_percentage_is_derived_from_used_and_limit_when_utilization_is_missing():
    assert [a.threshold for a in extra_alerts({ACCT_A.id: extra_snap(ACCT_A, None, used=800.0, limit=1000.0)})] == [80]
    assert extra_alerts({ACCT_B.id: extra_snap(ACCT_B, None, used=799.0, limit=1000.0)}) == []


def test_the_servers_utilization_is_trusted_over_used_and_limit():
    (alert,) = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=100.0, limit=1000.0)})
    assert (alert.threshold, alert.message) == (80, "$1.00 used, $9.00 left this month")


@pytest.mark.parametrize(
    "utilization, used, limit",
    [(None, None, 1000.0), (None, 800.0, None), (None, 800.0, 0.0), (None, 800.0, -5.0), (None, None, None)],
)
def test_no_alert_when_the_percentage_cannot_be_known(utilization, used, limit):
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, utilization, used=used, limit=limit)}) == []


def test_message_and_title_degrade_gracefully_when_amounts_are_missing():
    (no_used,) = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 90.0, used=None, limit=1000.0)})
    assert (no_used.title, no_used.message) == ("work: extra usage 80% of $10.00", "Extra usage this month")

    (no_limit,) = extra_alerts({ACCT_B.id: extra_snap(ACCT_B, 90.0, used=900.0, limit=None)})
    assert (no_limit.title, no_limit.message) == ("home: extra usage 80%", "$9.00 used this month")


def test_money_uses_the_servers_currency_and_decimal_places():
    (yen,) = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 80.0, used=8000.0, limit=10000.0, currency="JPY", places=0)})
    assert (yen.title, yen.message) == ("work: extra usage 80% of 10,000 JPY", "8,000 JPY used, 2,000 JPY left this month")

    (dinar,) = extra_alerts({ACCT_B.id: extra_snap(ACCT_B, 80.0, used=8000.0, limit=10000.0, currency="KWD", places=3)})
    assert dinar.title == "home: extra usage 80% of 10.000 KWD"


def test_accounts_are_tracked_independently_and_alongside_window_alerts():
    snap_a = UsageSnapshot(
        ACCT_A.id, T0, ok=True, windows=(win("seven_day", 96.0),),
        extra_usage=ExtraUsage(True, 1000.0, 850.0, 85.0, "USD", decimal_places=2),
    )
    snaps = {ACCT_A.id: snap_a, ACCT_B.id: extra_snap(ACCT_B, 85.0, used=850.0)}

    found = alerts_for(snaps)

    assert sorted((a.account_id, a.kind) for a in found) == sorted(
        [(ACCT_A.id, "threshold"), (ACCT_A.id, "extra_usage"), (ACCT_B.id, "extra_usage")]
    )
    assert alerts_for(snaps) == []


def test_failed_snapshots_raise_no_extra_usage_alert():
    failed = UsageSnapshot(ACCT_A.id, T0, ok=False, error="network", extra_usage=ExtraUsage(True, 1000.0, 1000.0, 100.0, "USD"))
    assert extra_alerts({ACCT_A.id: failed}) == []


def test_snapshots_without_extra_usage_are_ignored():
    assert extra_alerts({ACCT_A.id: ok_snap(ACCT_A, win("seven_day", 10.0))}) == []


def test_removed_accounts_lose_their_extra_usage_record():
    extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0), ACCT_B.id: extra_snap(ACCT_B, 85.0, used=850.0)})
    assert set(read_cache_file()["alerts"]["extra_usage"]) == {ACCT_A.id, ACCT_B.id}

    alerts_for({}, accounts=[ACCT_A])

    assert set(read_cache_file()["alerts"]["extra_usage"]) == {ACCT_A.id}


@pytest.mark.parametrize(
    "record",
    ["junk", 5, [], {}, {"month": 5, "fired": [80]}, {"month": "2026-11", "fired": "80"}, {"month": "2026-11", "fired": [True, "80", None]}, {"fired": [80]}],
)
def test_a_damaged_extra_usage_record_is_survived_and_repaired(record):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"alerts": {"extra_usage": {ACCT_A.id: record}}}))

    found = extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0)})

    assert [a.threshold for a in found] == [80]  # nothing usable was recorded, so it is announced once
    assert read_cache_file()["alerts"]["extra_usage"][ACCT_A.id] == {"month": "2026-11", "fired": [80]}
    assert extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0)}) == []


def test_alert_state_written_before_extra_usage_alerts_existed_still_works():
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_text(json.dumps({"alerts": {"fired": {}, "auth_alerted": [], "high": {}, "resets_left": {}}}))
    assert len(extra_alerts({ACCT_A.id: extra_snap(ACCT_A, 85.0, used=850.0)})) == 1


def test_extra_usage_alerts_follow_the_example_fixture_when_it_is_switched_on():
    """The example response's numbers (61.25 of 50.00) with is_enabled true would be the 100% alert."""
    raw = example_usage()
    raw["extra_usage"]["is_enabled"] = True
    snap = usage.parse_usage(raw, ACCT_A.id, EXAMPLE_AT)

    (alert,) = extra_alerts({ACCT_A.id: snap}, now=EXAMPLE_AT)

    assert (alert.threshold, alert.title, alert.message) == (100, "work: extra usage 100% of $50.00", "$61.25 used, $0.00 left this month")


def test_a_stale_last_good_snapshot_does_not_reannounce_through_refresh_all(world):
    """refresh_all hands detect_alerts the last good snapshot after a failure; that must stay silent."""
    server = FakeUsageServer(default={**usage_payload(), "extra_usage": {"is_enabled": True, "monthly_limit": 1000, "used_credits": 850,
                                                                         "utilization": 85.0, "currency": "USD", "decimal_places": 2}})
    snaps = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0)
    assert [a.threshold for a in monitor.detect_alerts(snaps, [ACCT_B], [], now=T0) if a.kind == "extra_usage"] == [80]

    server.replies[TOKEN_B] = urllib.error.URLError("down")
    snaps = monitor.refresh_all([ACCT_B], opener=server, delay_between=0, now=T0 + 10 * MIN)  # last good, stale
    assert [a for a in monitor.detect_alerts(snaps, [ACCT_B], [], now=T0 + 10 * MIN) if a.kind == "extra_usage"] == []


def test_cache_rows_missing_a_label_fall_back_to_a_readable_one_not_the_raw_key():
    wire = monitor.snapshot_to_dict(example_snapshot())
    wire["windows"] = [
        {"key": "cinder_cove", "utilization": 5.0},
        {"key": "seven_day_cowork", "label": None, "utilization": 6.0},
        {"key": "seven_day_omelette", "label": "", "utilization": 7.0},
        {"key": "juniper_tide", "label": "Kept as stored", "utilization": 8.0},
    ]
    wire["promos"] = [{"key": "wattle_ember", "limit_dollars": 5}]

    snap = monitor.snapshot_from_dict(wire)

    assert [(w.key, w.label) for w in snap.windows] == [
        ("cinder_cove", "Cinder Cove"),
        ("seven_day_cowork", "Weekly · Cowork"),
        ("seven_day_omelette", "Weekly · Omelette"),
        ("juniper_tide", "Kept as stored"),
    ]
    assert snap.promos[0].label == "Wattle Ember"


@pytest.mark.parametrize("older_error", [None, "network", "auth_expired"])
def test_older_poll_cannot_replace_newer_usage_or_mark_it_failed(older_error):
    newer = good_snap(session=90, when=T0 + 2 * MIN)
    monitor.save_cache({ACCT_B.id: newer})
    older = (UsageSnapshot(ACCT_B.id, T0, ok=False, error=older_error)
             if older_error else good_snap(session=10, when=T0))
    monitor.save_cache({ACCT_B.id: older})
    assert monitor.load_cache()[ACCT_B.id] == newer
    assert monitor.cache_status(ACCT_B.id)["last_error"] is None


def test_older_failure_does_not_replace_a_newer_failure_reason():
    monitor.save_cache({ACCT_B.id: good_snap()})
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + 2 * MIN, ok=False, error="rate_limited")})
    monitor.save_cache({ACCT_B.id: UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error="network")})
    assert monitor.cache_status(ACCT_B.id)["last_error"] == "rate_limited"


@pytest.mark.parametrize("older_error", [None, "rate_limited"])
def test_overlapping_poll_does_not_erase_a_newer_retry_deadline(world, monkeypatch, older_error):
    new_snapshot = UsageSnapshot(ACCT_B.id, T0 + MIN, ok=False, error="rate_limited")
    deadline = T0 + timedelta(hours=1)

    def fetch(*args, **kwargs):
        state = monitor._read_state()
        monitor._merge_attempt(state, ACCT_B.id, new_snapshot)
        state["backoff"][ACCT_B.id] = {"failures": 1, "endpoint": "usage", "next_allowed_at": deadline.isoformat()}
        state["resets_cache"][ACCT_B.id] = {"fetched_at": (T0 + MIN).isoformat(), "next_check_at": deadline.isoformat(), "status": None}
        monitor._write_state(state)
        older = (UsageSnapshot(ACCT_B.id, T0, ok=False, error=older_error)
                 if older_error else good_snap(when=T0))
        row = {"fetched_at": T0.isoformat(), "next_check_at": (T0 + 30 * MIN).isoformat(), "status": None}
        return monitor._Outcome(older, 60 if older_error else None, row, "usage" if older_error else None)

    monkeypatch.setattr(monitor, "_refresh", fetch)
    monitor.refresh_all([ACCT_B], delay_between=0, now=T0)
    state = read_cache_file()
    assert state["backoff"][ACCT_B.id]["next_allowed_at"] == deadline.isoformat()
    assert state["resets_cache"][ACCT_B.id]["next_check_at"] == deadline.isoformat()
    assert state["errors"][ACCT_B.id]["last_error"] == "rate_limited"


@pytest.mark.parametrize("failures", ["broken", [], -10, 1000])
def test_malformed_backoff_count_cannot_abort_a_rate_limited_poll(world, failures):
    state = monitor._empty_state()
    state["backoff"][ACCT_B.id] = {"failures": failures}
    monitor._write_state(state)
    result = monitor.refresh_all([ACCT_B], opener=rate_limited_server(), delay_between=0, now=T0)
    assert result[ACCT_B.id].error == "rate_limited"
    assert 60 <= (datetime.fromisoformat(backoff_row()["next_allowed_at"]) - T0).total_seconds() <= 3600


def test_rate_limit_is_published_before_a_later_account_can_delay_the_poll(world):
    server = rate_limited_server(3600)

    def check_deadline_before_account_a():
        if server.calls == [TOKEN_B]:
            assert backoff_row()["next_allowed_at"] == (T0 + 60 * MIN).isoformat()

    server.on_call = check_deadline_before_account_a
    result = monitor.refresh_all([ACCT_B, ACCT_A], opener=server, delay_between=0, now=T0)
    assert result[ACCT_A.id].ok


def test_deadline_written_during_inter_account_delay_is_honored(world):
    server = FakeUsageServer()

    def delay(_seconds):
        state = monitor._read_state()
        state["backoff"][ACCT_B.id] = {"failures": 1, "next_allowed_at": (T0 + 60 * MIN).isoformat()}
        monitor._write_state(state)

    monitor.refresh_all([ACCT_A, ACCT_B], opener=server, delay_between=1, sleep=delay, now=T0)
    assert server.calls == [TOKEN_A]


def test_later_accounts_respect_a_retry_deadline_written_during_this_poll(world):
    server = FakeUsageServer()

    def another_poll_was_limited():
        state = monitor._read_state()
        state["backoff"][ACCT_B.id] = {"failures": 1, "next_allowed_at": (T0 + 60 * MIN).isoformat()}
        monitor._write_state(state)

    server.on_call = another_poll_was_limited
    result = monitor.refresh_all([ACCT_A, ACCT_B], opener=server, delay_between=0, now=T0)
    assert server.calls == [TOKEN_A]
    assert result[ACCT_B.id].error == "rate_limited"


def test_oversized_numeric_retry_metadata_does_not_break_cache_status():
    state = monitor._empty_state()
    monitor._merge_attempt(state, ACCT_B.id, UsageSnapshot(ACCT_B.id, T0, ok=False, error="rate_limited"))
    state["backoff"][ACCT_B.id] = {"endpoint": "usage", "retry_after_seconds": 10**400}
    monitor._write_state(state)
    assert monitor.cache_status(ACCT_B.id)["rate_limit"]["retry_after_seconds"] is None


def test_backoff_exponent_is_capped_before_power_allocation():
    class CheckedExponent(int):
        def __rpow__(self, base):
            assert self <= 6, "unbounded exponent allocation happens before the 1-hour cap"
            return base ** int(self)

    class CheckedCount(int):
        def __sub__(self, other):
            return CheckedExponent(int(self) - other)

    assert monitor._backoff_seconds(None, CheckedCount(1000)) == 3600


@pytest.mark.parametrize("content", [b"\xff", b"[" * 20000])
def test_unreadable_usage_cache_does_not_abort_monitoring(world, content):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.usage_cache_file().write_bytes(content)
    result = monitor.refresh_all([ACCT_B], opener=FakeUsageServer(), delay_between=0, now=T0)
    assert result[ACCT_B.id].ok


def test_full_poll_does_not_restore_cache_for_an_account_removed_in_flight(world, monkeypatch):
    def fetch(account, **kwargs):
        if account.id == ACCT_A.id:
            store.remove_account(ACCT_B.id)
        return monitor._Outcome(good_snap(account))

    monkeypatch.setattr(monitor, "_refresh", fetch)
    monitor.refresh_all(delay_between=0, now=T0)
    assert ACCT_B.id not in read_cache_file()["snapshots"]
