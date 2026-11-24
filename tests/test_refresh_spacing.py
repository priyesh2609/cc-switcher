from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import pytest

from cc_switcher import cli, creds, fsutil, keychain, menubar, monitor, paths, store, switcher, tokens, usage
from cc_switcher.models import Account, Settings, make_account_id
from conftest import ACCOUNT_A, ACCOUNT_B, make_login


def account(ident):
    return Account(id=make_account_id(*ident), email="", account_uuid=ident[0], organization_uuid=ident[1])


def test_failed_refresh_post_reserves_slot_for_other_account(fake_keychain, monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    a, b = account(ACCOUNT_A), account(ACCOUNT_B)
    for item, ident in ((a, ACCOUNT_A), (b, ACCOUNT_B)):
        fake_keychain.put_json(item.keychain_service, creds.saved_item(make_login(ident, item.id, 1000)))
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 500, {}

    results = monitor.refresh_all([a, b], http_post=post, delay_between=0)

    assert len(calls) == 1
    assert results[a.id].error == "http_500"
    assert results[b.id].error == "refresh_queued"
    assert monitor.cache_status(b.id)["last_error"] == "refresh_queued"
    schedule = paths.refresh_schedule_file().read_text()
    assert "sk-ant-" not in schedule and a.id not in schedule and b.id not in schedule


@pytest.fixture
def clock(monkeypatch):
    now = {"wall": 1_800_000_000.0, "mono": 10_000.0}
    monkeypatch.setattr(time, "time", lambda: now["wall"])
    monkeypatch.setattr(time, "monotonic", lambda: now["mono"])
    return now


def setup_expired(fake_keychain, *identities):
    accounts = [account(ident) for ident in identities]
    for item, ident in zip(accounts, identities):
        fake_keychain.put_json(item.keychain_service, creds.saved_item(make_login(ident, ident[0], 1000)))
    return accounts


def test_exact_boundary_and_round_robin_after_repeated_errors(fake_keychain, monkeypatch, clock):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    ident_c = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")
    a, b, c = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B, ident_c)
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(payload["refresh_token"])
        return 500, {}

    monitor.refresh_all([a, b, c], http_post=post, delay_between=0)
    clock["wall"] += 179
    clock["mono"] += 179
    monitor.refresh_all([a, b, c], http_post=post, delay_between=0)
    assert len(calls) == 1
    clock["wall"] += 1
    clock["mono"] += 1
    monitor.refresh_all([a, b, c], http_post=post, delay_between=0)
    assert len(calls) == 2 and calls[1].endswith(ACCOUNT_B[0])
    clock["wall"] += 180
    clock["mono"] += 180
    monitor.refresh_all([a, b, c], http_post=post, delay_between=0)
    assert len(calls) == 3 and calls[2].endswith(ident_c[0])


def test_fairness_with_a_30_minute_poll_interval(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    store.save_settings(Settings(poll_interval_seconds=1800))
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(payload["refresh_token"])
        return 500, {}

    monitor.refresh_all([a, b], http_post=post, delay_between=0)
    clock["wall"] += 1800
    clock["mono"] += 1800
    monitor.refresh_all([a, b], http_post=post, delay_between=0)
    assert len(calls) == 2 and calls[1].endswith(ACCOUNT_B[0])


@pytest.mark.parametrize("change", ["fresh", "live", "removed"])
def test_ineligible_queue_head_does_not_block_another_account(fake_keychain, clock, change):
    ident_c = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")
    a, b, c = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B, ident_c)
    store.save_accounts([a, b, c])
    calls = []
    def post(url, payload, *, headers, timeout):
        calls.append(payload["refresh_token"])
        return 500, {}
    assert monitor.refresh_account(a, http_post=post).error == "http_500"
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    assert monitor.refresh_account(c, http_post=post).error == "refresh_queued"
    if change == "fresh":
        fake_keychain.put_json(b.keychain_service, creds.saved_item(make_login(ACCOUNT_B, "fresh", 4_102_444_800_000)))
    elif change == "live":
        fake_keychain.put_json(paths.claude_keychain_service(), {"claudeAiOauth": make_login(ACCOUNT_B, "live", 4_102_444_800_000)})
    else:
        switcher.remove_account(b.id)
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(c, http_post=post).error == "http_500"
    assert len(calls) == 2 and calls[-1].endswith(ident_c[0])


def test_clock_rollback_never_grants_early_slot(fake_keychain, monkeypatch, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 500, {}

    monitor.refresh_account(a, http_post=post)
    clock["wall"] -= 60
    clock["mono"] += 180
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    assert len(calls) == 1
    clock["wall"] += 240
    clock["mono"] += 240
    assert monitor.refresh_account(b, http_post=post).error == "http_500"
    assert len(calls) == 2


def test_valid_tokens_fetch_usage_while_another_account_is_queued(fake_keychain, monkeypatch, clock):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    ident_c = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")
    c = account(ident_c)
    fake_keychain.put_json(c.keychain_service, creds.saved_item(make_login(ident_c, "fresh", 4_102_444_800_000)))
    posts, gets = [], []

    def post(url, payload, *, headers, timeout):
        posts.append(url)
        return 500, {}

    class Response:
        status = 200
        def read(self):
            return json.dumps({"five_hour": {"utilization": 42}}).encode()
        def close(self):
            pass

    def opener(request, timeout=None):
        gets.append(request.get_method())
        return Response()

    result = monitor.refresh_all([a, b, c], http_post=post, opener=opener, delay_between=0)
    assert len(posts) == 1
    assert result[b.id].error == "refresh_queued"
    assert result[c.id].ok and result[c.id].session.utilization == 42
    assert gets and set(gets) == {"GET"}


def test_queue_keeps_last_good_snapshot_and_has_clear_text(fake_keychain, monkeypatch, clock):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    old = usage.parse_usage({"five_hour": {"utilization": 23}}, b.id, fetched_at=datetime.now(timezone.utc))
    monitor.save_cache({b.id: old})
    monitor.refresh_all([a, b], http_post=lambda *args, **kwargs: (500, {}), delay_between=0)
    entry = monitor.cache_status(b.id)
    assert entry["snapshot"] == old
    assert entry["stale"] and entry["last_error"] == "refresh_queued"
    assert "queued" in cli.error_text("refresh_queued").lower()
    assert "queued" in " ".join(menubar.entry_lines(entry)).lower()


def test_slow_post_spacing_starts_from_completion(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    calls = []

    def slow_post(url, payload, *, headers, timeout):
        calls.append(clock["wall"])
        clock["wall"] += 12
        clock["mono"] += 12
        return 500, {}

    monitor.refresh_account(a, http_post=slow_post)
    clock["wall"] += 168
    clock["mono"] += 168
    assert monitor.refresh_account(b, http_post=slow_post).error == "refresh_queued"
    clock["wall"] += 12
    clock["mono"] += 12
    assert monitor.refresh_account(b, http_post=slow_post).error == "http_500"
    assert calls == [1_800_000_000.0, 1_800_000_192.0]


def test_crashed_inflight_post_waits_from_recovery(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    pid = os.fork()
    if pid == 0:
        def crash_post(*args, **kwargs):
            os._exit(0)  # process dies after entering the fake POST, before completion is recorded
        monitor.refresh_account(a, http_post=crash_post)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(paths.refresh_schedule_file().read_text())["inflight"] is True
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 500, {}

    clock["wall"] += 300
    clock["mono"] += 300
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    assert not calls
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(b, http_post=post).error == "http_500"
    assert len(calls) == 1


def test_global_interval_starts_after_slow_keychain_persistence(fake_keychain, monkeypatch, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    persist = tokens._persist
    def slow_persist(account, login):
        clock["wall"] += 30
        clock["mono"] += 30
        persist(account, login)
    monkeypatch.setattr(tokens, "_persist", slow_persist)
    assert tokens.access_token_for(
        a, http_post=lambda *args, **kwargs: (200, {"access_token": "new-access", "refresh_token": "new-refresh"})
    ) == "new-access"
    clock["wall"] += 150
    clock["mono"] += 150
    assert monitor.refresh_account(b, http_post=lambda *args, **kwargs: (500, {})).error == "refresh_queued"
    clock["wall"] += 30
    clock["mono"] += 30
    assert monitor.refresh_account(b, http_post=lambda *args, **kwargs: (500, {})).error == "http_500"


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_completion_failure_happens_after_rotated_login_is_saved(fake_keychain, monkeypatch, clock, error_type):
    from cc_switcher import refresh_schedule

    (a,) = setup_expired(fake_keychain, ACCOUNT_A)
    seen = []
    def fail_completion(*args, **kwargs):
        seen.append(fake_keychain.get_json(a.keychain_service)["claudeAiOauth"]["refreshToken"])
        raise error_type("metadata unavailable")
    monkeypatch.setattr(refresh_schedule, "complete", fail_completion)
    token = tokens.access_token_for(
        a, http_post=lambda *args, **kwargs: (200, {"access_token": "new-access", "refresh_token": "new-refresh"})
    )
    assert seen == ["new-refresh"]
    assert token == "new-access"
    assert fake_keychain.get_json(a.keychain_service)["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert json.loads(paths.refresh_schedule_file().read_text())["inflight"] is True


def test_keychain_and_completion_failure_keep_rotated_login_in_memory(fake_keychain, monkeypatch, clock):
    from cc_switcher import refresh_schedule

    (a,) = setup_expired(fake_keychain, ACCOUNT_A)
    fake_keychain.fail_writes_for.add(a.keychain_service)
    monkeypatch.setattr(tokens, "_UNSAVED", {})
    seen = []
    def fail_completion(*args, **kwargs):
        pending = tokens._UNSAVED.get(a.id)
        seen.append(pending[1]["refreshToken"] if pending else None)
        raise OSError("disk unavailable")
    monkeypatch.setattr(refresh_schedule, "complete", fail_completion)
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(
            a, http_post=lambda *args, **kwargs: (200, {"access_token": "new-access", "refresh_token": "new-refresh"})
        )
    assert seen == ["new-refresh"]
    assert tokens._UNSAVED[a.id][1]["refreshToken"] == "new-refresh"
    assert json.loads(paths.refresh_schedule_file().read_text())["inflight"] is True


@pytest.mark.parametrize("problem", ["write_failure", "corrupt"])
def test_schedule_storage_failure_is_actionable_without_losing_usage(fake_keychain, monkeypatch, clock, problem, capsys):
    (a,) = setup_expired(fake_keychain, ACCOUNT_A)
    store.save_accounts([a])
    old = usage.parse_usage({"five_hour": {"utilization": 23}}, a.id, fetched_at=datetime.now(timezone.utc))
    monitor.save_cache({a.id: old})
    path = paths.refresh_schedule_file()
    if problem == "corrupt":
        path.write_text("{damaged schedule", encoding="utf-8")
    else:
        write = fsutil.atomic_write_json
        def fail_schedule(target, data, *args, **kwargs):
            if target == path:
                raise OSError("disk full")
            return write(target, data, *args, **kwargs)
        monkeypatch.setattr(fsutil, "atomic_write_json", fail_schedule)
    calls = []
    def post(*args, **kwargs):
        calls.append(True)
        return 500, {}
    result = monitor.refresh_all([a], http_post=post, delay_between=0)
    entry = monitor.cache_status(a.id)
    assert not calls
    assert result[a.id] == old and entry["snapshot"] == old
    assert entry["last_error"] == "refresh_schedule_error"
    assert "schedule" in cli.error_text(entry["last_error"]).lower()
    assert "disk" in cli.error_text(entry["last_error"]).lower()
    assert "schedule" in " ".join(menubar.entry_lines(entry)).lower()
    assert "disk" in " ".join(menubar.entry_lines(entry)).lower()
    assert cli.cmd_list(argparse.Namespace(refresh=False, json=False)) == 0
    assert "disk" in capsys.readouterr().out.lower()
    if problem == "corrupt":
        assert path.read_text(encoding="utf-8") == "{damaged schedule"


def test_corrupt_schedule_with_huge_clock_fails_closed(fake_keychain, clock):
    (a,) = setup_expired(fake_keychain, ACCOUNT_A)
    fsutil.ensure_private_dir(paths.app_dir())
    original = json.dumps({"version": 1, "last": {"wall": 10**400, "mono": 1},
                           "inflight": False, "queue": [], "retry": {}})
    paths.refresh_schedule_file().write_text(original)
    calls = []
    result = monitor.refresh_account(a, http_post=lambda *args, **kwargs: calls.append(True))
    assert result.error == "refresh_schedule_error"
    assert not calls
    assert paths.refresh_schedule_file().read_text() == original


def test_transport_oserror_remains_a_network_error(fake_keychain, clock):
    (a,) = setup_expired(fake_keychain, ACCOUNT_A)
    def post(*args, **kwargs):
        raise OSError("connection failed")
    assert monitor.refresh_account(a, http_post=post).error == "network"


def test_missing_refresh_token_does_not_consume_a_slot(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    login = make_login(ACCOUNT_A, "a", 1000)
    del login["refreshToken"]
    fake_keychain.put_json(a.keychain_service, creds.saved_item(login))
    calls = []
    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 500, {}
    assert monitor.refresh_account(a, http_post=post).error == "auth_expired"
    assert monitor.refresh_account(b, http_post=post).error == "http_500"
    assert len(calls) == 1


def test_reboot_after_completed_post_starts_new_recovery_interval(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    clock["mono"] = 1_000_000
    calls = []
    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 500, {}

    assert monitor.refresh_account(a, http_post=post).error == "http_500"
    clock["wall"] += 60
    clock["mono"] = 120
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    clock["wall"] += 179
    clock["mono"] += 179
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    clock["wall"] += 1
    clock["mono"] += 1
    assert monitor.refresh_account(b, http_post=post).error == "http_500"
    assert len(calls) == 2


def test_reboot_after_inflight_crash_starts_new_recovery_interval(fake_keychain, monkeypatch, clock):
    from cc_switcher import refresh_schedule
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    clock["mono"] = 1_000_000
    def crash(*args, **kwargs):
        raise SystemExit(1)
    with monkeypatch.context() as patch:
        patch.setattr(refresh_schedule, "complete", crash)
        with pytest.raises(SystemExit):
            monitor.refresh_account(a, http_post=lambda *args, **kwargs: (500, {}))
    clock["wall"] += 60
    clock["mono"] = 120
    assert monitor.refresh_account(b, http_post=lambda *args, **kwargs: (500, {})).error == "refresh_queued"
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(b, http_post=lambda *args, **kwargs: (500, {})).error == "http_500"


def test_reboot_preserves_provider_wait_without_blocking_other_accounts(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    clock["mono"] = 1_000_000
    calls = []
    def post(url, payload, *, headers, timeout):
        calls.append(payload["refresh_token"])
        return 429, {}, {"Retry-After": "900"}
    assert monitor.refresh_account(a, http_post=post).error == "rate_limited"
    clock["wall"] += 60
    clock["mono"] = 120
    assert monitor.refresh_account(a, http_post=post).error == "refresh_queued"
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(b, http_post=post).error == "rate_limited"
    assert monitor.refresh_account(a, http_post=post).error == "refresh_queued"
    clock["wall"] += 720
    clock["mono"] += 720
    assert monitor.refresh_account(a, http_post=post).error == "rate_limited"
    assert len(calls) == 3


def test_reboot_expires_abandoned_queue_entry_and_keeps_fair_order(fake_keychain, clock):
    ident_c = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")
    a, b, c = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B, ident_c)
    clock["mono"] = 1_000_000
    post = lambda *args, **kwargs: (500, {})
    assert monitor.refresh_account(a, http_post=post).error == "http_500"
    assert monitor.refresh_account(b, http_post=post).error == "refresh_queued"
    clock["wall"] += 1800  # B has been absent for longer than the 900-second lease
    clock["mono"] = 120
    assert monitor.refresh_account(c, http_post=post).error == "refresh_queued"
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(c, http_post=post).error == "http_500"


def test_retry_after_survives_direct_refresh_and_spacing(fake_keychain, clock):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    calls = []

    def post(url, payload, *, headers, timeout):
        calls.append(url)
        return 429, {}, {"Retry-After": "900"}

    assert monitor.refresh_account(a, http_post=post).error == "rate_limited"
    clock["wall"] += 180
    clock["mono"] += 180
    assert monitor.refresh_account(a, http_post=post).error == "refresh_queued"
    assert monitor.refresh_account(b, http_post=post).error == "rate_limited"
    assert len(calls) == 2
    clock["wall"] += 720
    clock["mono"] += 720
    assert monitor.refresh_account(a, http_post=post).error == "rate_limited"
    assert len(calls) == 3


def test_processes_share_reservation_without_waiting_three_minutes(fake_keychain):
    a, b = setup_expired(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    reader, writer = os.pipe()
    children = []
    started = time.monotonic()
    for item in (a, b):
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            try:
                result = monitor.refresh_account(item, http_post=lambda *args, **kwargs: (500, {}))
                os.write(writer, (result.error + "\n").encode())
            finally:
                os._exit(0)
        children.append(pid)
    os.close(writer)
    outcomes = os.read(reader, 100).decode() + os.read(reader, 100).decode()
    os.close(reader)
    for pid in children:
        os.waitpid(pid, 0)
    assert sorted(outcomes.splitlines()) == ["http_500", "refresh_queued"]
    assert time.monotonic() - started < 5
    assert paths.refresh_schedule_file().stat().st_mode & 0o777 == 0o600
