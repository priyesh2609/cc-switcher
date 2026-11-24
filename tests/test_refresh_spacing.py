from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from cc_switcher import creds, fsutil, paths, refresh_schedule, store
from cc_switcher.models import Account, Settings, make_account_id
from conftest import ACCOUNT_A, ACCOUNT_B, make_login


ACCOUNT_C = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")


def account(ident):
    return Account(id=make_account_id(*ident), email="", account_uuid=ident[0], organization_uuid=ident[1])


@pytest.fixture
def clock(monkeypatch):
    now = {"wall": 1_800_000_000.0, "mono": 10_000.0}
    monkeypatch.setattr(time, "time", lambda: now["wall"])
    monkeypatch.setattr(time, "monotonic", lambda: now["mono"])
    return now


def advance(clock, seconds):
    clock["wall"] += seconds
    clock["mono"] += seconds


def reserve(account_id, *, eligible=None):
    with fsutil.app_lock():
        refresh_schedule.reserve(account_id, is_eligible=eligible)


def complete(account_id, response=None):
    with fsutil.app_lock():
        refresh_schedule.complete(account_id, response)


def queued(account_id, *, eligible=None):
    with pytest.raises(refresh_schedule.RefreshQueued):
        reserve(account_id, eligible=eligible)


def test_exact_boundary_and_round_robin(clock):
    a, b, c = (account(i).id for i in (ACCOUNT_A, ACCOUNT_B, ACCOUNT_C))
    reserve(a)
    complete(a)
    queued(b)
    queued(c)
    advance(clock, 179)
    queued(a)
    queued(b)
    advance(clock, 1)
    queued(a)
    reserve(b)
    complete(b)
    advance(clock, 180)
    reserve(c)
    complete(c)
    state = paths.refresh_schedule_file().read_text()
    assert "sk-ant-" not in state
    assert all(ident not in state for ident in (a, b, c))
    assert paths.refresh_schedule_file().stat().st_mode & 0o777 == 0o600


def test_fairness_with_a_30_minute_poll_interval(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    store.save_settings(Settings(poll_interval_seconds=1800))
    reserve(a)
    complete(a)
    queued(b)
    advance(clock, 1800)
    queued(a)
    reserve(b)


def test_ineligible_queue_head_does_not_block_another_account(clock):
    a, b, c = (account(i).id for i in (ACCOUNT_A, ACCOUNT_B, ACCOUNT_C))
    reserve(a)
    complete(a)
    queued(b)
    queued(c)
    advance(clock, 180)
    consulted = []

    def eligible(ref):
        consulted.append(ref)
        return ref != refresh_schedule._reference(b)

    reserve(c, eligible=eligible)
    assert consulted == [refresh_schedule._reference(b)]


def test_clock_rollback_never_grants_early_slot(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    reserve(a)
    complete(a)
    clock["wall"] -= 60
    clock["mono"] += 180
    queued(b)
    clock["wall"] += 240
    clock["mono"] += 240
    reserve(b)


def test_slow_operation_spacing_starts_from_completion(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    reserve(a)
    advance(clock, 12)
    complete(a)
    advance(clock, 168)
    queued(b)
    advance(clock, 12)
    reserve(b)
    assert clock["wall"] == 1_800_000_192.0


def test_scheduler_inflight_crash_waits_from_recovery(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    pid = os.fork()
    if pid == 0:
        reserve(a)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(paths.refresh_schedule_file().read_text())["inflight"] is True
    advance(clock, 300)
    queued(b)
    advance(clock, 179)
    queued(b)
    advance(clock, 1)
    reserve(b)


def test_interval_starts_after_slow_credential_persistence(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    reserve(a)
    advance(clock, 30)  # child and credential reconciliation complete before the slot closes
    complete(a)
    advance(clock, 150)
    queued(b)
    advance(clock, 30)
    reserve(b)


@pytest.mark.parametrize("problem", ["write_failure", "corrupt"])
def test_schedule_storage_failure_does_not_reserve_or_destroy_state(monkeypatch, problem):
    a = account(ACCOUNT_A).id
    path = paths.refresh_schedule_file()
    fsutil.ensure_private_dir(paths.app_dir())
    if problem == "corrupt":
        path.write_text("{damaged schedule", encoding="utf-8")
    else:
        write = fsutil.atomic_write_json

        def fail_schedule(target, data, *args, **kwargs):
            if target == path:
                raise OSError("disk full")
            return write(target, data, *args, **kwargs)

        monkeypatch.setattr(fsutil, "atomic_write_json", fail_schedule)
    with pytest.raises(refresh_schedule.ScheduleError):
        reserve(a)
    if problem == "corrupt":
        assert path.read_text(encoding="utf-8") == "{damaged schedule"
    else:
        assert not path.exists()


def test_corrupt_schedule_with_huge_clock_fails_closed():
    a = account(ACCOUNT_A).id
    fsutil.ensure_private_dir(paths.app_dir())
    original = json.dumps({"version": 1, "last": {"wall": 10**400, "mono": 1},
                           "inflight": False, "queue": [], "retry": {}})
    paths.refresh_schedule_file().write_text(original)
    with pytest.raises(refresh_schedule.ScheduleError):
        reserve(a)
    assert paths.refresh_schedule_file().read_text() == original


def test_reboot_after_completed_operation_starts_new_recovery_interval(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    clock["mono"] = 1_000_000
    reserve(a)
    complete(a)
    clock["wall"] += 60
    clock["mono"] = 120
    queued(b)
    advance(clock, 179)
    queued(b)
    advance(clock, 1)
    reserve(b)


def test_reboot_after_inflight_crash_starts_new_recovery_interval(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    clock["mono"] = 1_000_000
    reserve(a)  # no completion, as if the owner crashed
    clock["wall"] += 60
    clock["mono"] = 120
    queued(b)
    advance(clock, 179)
    queued(b)
    advance(clock, 1)
    reserve(b)


def test_reboot_preserves_scheduler_provider_wait_without_blocking_other_accounts(clock):
    # Synthetic scheduler metadata only: an opaque Claude child does not expose a 429 header.
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    clock["mono"] = 1_000_000
    reserve(a)
    complete(a, (429, {}, {"Retry-After": "900"}))
    clock["wall"] += 60
    clock["mono"] = 120
    queued(a)
    queued(b)
    advance(clock, 180)
    reserve(b)
    complete(b)
    queued(a)
    advance(clock, 720)
    reserve(a)


def test_retry_after_survives_scheduler_spacing(clock):
    a, b = account(ACCOUNT_A).id, account(ACCOUNT_B).id
    reserve(a)
    complete(a, (429, {}, {"Retry-After": "900"}))
    advance(clock, 180)
    queued(a)
    reserve(b)
    complete(b)
    advance(clock, 720)
    reserve(a)


def test_reboot_expires_abandoned_queue_entry(clock):
    a, b, c = (account(i).id for i in (ACCOUNT_A, ACCOUNT_B, ACCOUNT_C))
    clock["mono"] = 1_000_000
    reserve(a)
    complete(a)
    queued(b)
    clock["wall"] += 1800
    clock["mono"] = 120
    queued(c)
    advance(clock, 180)
    reserve(c)


def test_processes_share_reservation_without_waiting_three_minutes():
    accounts = [account(ACCOUNT_A).id, account(ACCOUNT_B).id]
    reader, writer = os.pipe()
    gate_reader, gate_writer = os.pipe()
    children = []
    started = time.monotonic()
    for ident in accounts:
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            os.close(gate_writer)
            try:
                os.read(gate_reader, 1)
                with fsutil.app_lock():
                    try:
                        refresh_schedule.reserve(ident)
                    except refresh_schedule.RefreshQueued:
                        result = "queued"
                    else:
                        refresh_schedule.complete(ident)
                        result = "reserved"
                os.write(writer, (result + "\n").encode())
            finally:
                os._exit(0)
        children.append(pid)
    os.close(writer)
    os.close(gate_reader)
    os.write(gate_writer, b"xx")
    os.close(gate_writer)
    with os.fdopen(reader) as stream:
        outcomes = stream.read().splitlines()
    for pid in children:
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    assert sorted(outcomes) == ["queued", "reserved"]
    assert time.monotonic() - started < 5
    assert paths.refresh_schedule_file().stat().st_mode & 0o777 == 0o600


@pytest.fixture
def cli_engine():
    # The engine is landing separately. Do not substitute the removed HTTP transport.
    return pytest.importorskip("cc_switcher.claude_refresh", reason="isolated CLI engine not yet integrated")


def setup_saved(fake_keychain, *identities):
    accounts = [account(ident) for ident in identities]
    for item, ident in zip(accounts, identities):
        fake_keychain.put_json(item.keychain_service, creds.saved_item(make_login(ident, ident[0], 1000)))
    return accounts


@pytest.fixture
def fake_renewal(cli_engine, monkeypatch, fake_keychain):
    from cc_switcher import usage

    store.save_settings(Settings(claude_refresh_enabled=True))
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    calls = []

    def refresh_locked(item, login):
        calls.append(item.id)
        updated = dict(login, accessToken="fake-renewed-access", refreshToken="fake-renewed-refresh",
                       expiresAt=4_102_444_800_000)
        fake_keychain.put_json(item.keychain_service, creds.saved_item(updated))
        return updated

    monkeypatch.setattr(cli_engine, "refresh_locked", refresh_locked)
    return calls


def fake_usage_opener(request, timeout=None):
    assert request.get_method() == "GET"

    class Response:
        status = 200

        def read(self, size=-1):
            body = json.dumps({"five_hour": {"utilization": 42}}).encode()
            return body if size < 0 else body[:size]

        def close(self):
            pass

    return Response()


def test_valid_token_usage_continues_while_another_account_is_queued(fake_keychain, monkeypatch, clock, fake_renewal):
    from cc_switcher import monitor, usage

    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    a, b = setup_saved(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    c = account(ACCOUNT_C)
    fake_keychain.put_json(c.keychain_service, creds.saved_item(make_login(ACCOUNT_C, "fresh", 4_102_444_800_000)))
    results = monitor.refresh_all([a, b, c], opener=fake_usage_opener, delay_between=0)
    assert fake_renewal == [a.id]
    assert results[a.id].ok
    assert results[b.id].error == "refresh_queued"
    assert results[c.id].ok and results[c.id].session.utilization == 42


@pytest.mark.parametrize("change", ["fresh", "live", "removed"])
def test_ineligible_account_does_not_block_waiting_account(fake_keychain, clock, fake_renewal, change):
    from cc_switcher import monitor

    a, b, c = setup_saved(fake_keychain, ACCOUNT_A, ACCOUNT_B, ACCOUNT_C)
    store.save_accounts([a, b, c])
    assert monitor.refresh_account(a, opener=fake_usage_opener).ok
    assert monitor.refresh_account(b, opener=fake_usage_opener).error == "refresh_queued"
    assert monitor.refresh_account(c, opener=fake_usage_opener).error == "refresh_queued"
    if change == "fresh":
        fake_keychain.put_json(b.keychain_service, creds.saved_item(make_login(ACCOUNT_B, "fresh", 4_102_444_800_000)))
    elif change == "live":
        fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(make_login(ACCOUNT_B, "live", 4_102_444_800_000)))
    else:
        fake_keychain.delete(b.keychain_service)
        store.remove_account(b.id)
    advance(clock, 180)
    assert monitor.refresh_account(c, opener=fake_usage_opener).ok
    assert fake_renewal == [a.id, c.id]


def test_queued_account_keeps_last_good_snapshot_and_clear_text(fake_keychain, monkeypatch, clock, fake_renewal):
    from cc_switcher import cli, menubar, monitor, usage

    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    a, b = setup_saved(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    old = usage.parse_usage({"five_hour": {"utilization": 23}}, b.id, fetched_at=datetime.now(timezone.utc))
    monitor.save_cache({b.id: old})
    monitor.refresh_all([a, b], opener=fake_usage_opener, delay_between=0)
    entry = monitor.cache_status(b.id)
    assert fake_renewal == [a.id]
    assert entry["snapshot"] == old
    assert entry["stale"] and entry["last_error"] == "refresh_queued"
    assert "queued" in cli.error_text("refresh_queued").lower()
    assert "queued" in " ".join(menubar.entry_lines(entry)).lower()


def test_missing_refresh_token_does_not_consume_a_slot(fake_keychain, clock, fake_renewal):
    from cc_switcher import monitor

    a, b = setup_saved(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    login = make_login(ACCOUNT_A, "a", 1000)
    del login["refreshToken"]
    fake_keychain.put_json(a.keychain_service, creds.saved_item(login))
    assert monitor.refresh_account(a, opener=fake_usage_opener).error == "auth_expired"
    assert monitor.refresh_account(b, opener=fake_usage_opener).ok
    assert fake_renewal == [b.id]


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_completion_failure_does_not_discard_successor(fake_keychain, monkeypatch, clock, fake_renewal, error_type):
    from cc_switcher import tokens

    (a,) = setup_saved(fake_keychain, ACCOUNT_A)
    observed = []

    def fail_completion(*args, **kwargs):
        observed.append(fake_keychain.get_json(a.keychain_service)["claudeAiOauth"]["refreshToken"])
        raise error_type("metadata unavailable")

    monkeypatch.setattr(refresh_schedule, "complete", fail_completion)
    assert tokens.access_token_for(a) == "fake-renewed-access"
    assert fake_renewal == [a.id]
    assert observed == ["fake-renewed-refresh"]
    assert fake_keychain.get_json(a.keychain_service)["claudeAiOauth"]["refreshToken"] == "fake-renewed-refresh"
    assert json.loads(paths.refresh_schedule_file().read_text())["inflight"] is True


def test_schedule_failure_keeps_last_good_usage(fake_keychain, monkeypatch, clock, fake_renewal):
    from cc_switcher import cli, menubar, monitor, usage

    (a,) = setup_saved(fake_keychain, ACCOUNT_A)
    old = usage.parse_usage({"five_hour": {"utilization": 23}}, a.id, fetched_at=datetime.now(timezone.utc))
    monitor.save_cache({a.id: old})
    path = paths.refresh_schedule_file()
    path.write_text("{damaged schedule", encoding="utf-8")
    results = monitor.refresh_all([a], opener=fake_usage_opener, delay_between=0)
    entry = monitor.cache_status(a.id)
    assert not fake_renewal
    assert results[a.id] == old and entry["snapshot"] == old
    assert entry["stale"] and entry["last_error"] == "refresh_schedule_error"
    assert "schedule" in cli.error_text(entry["last_error"]).lower()
    assert "disk" in " ".join(menubar.entry_lines(entry)).lower()
    assert path.read_text(encoding="utf-8") == "{damaged schedule"


def test_unknown_cli_failure_blocks_same_grant_but_not_another(fake_keychain, monkeypatch, clock, cli_engine, tmp_path):
    a, b = setup_saved(fake_keychain, ACCOUNT_A, ACCOUNT_B)
    first = make_login(ACCOUNT_A, ACCOUNT_A[0], 1000)
    second = make_login(ACCOUNT_B, ACCOUNT_B[0], 1000)
    calls = []
    directory = tmp_path / "isolated"
    directory.mkdir()

    def prepare(item, login, attr):
        operation = {"version": 1, "nonce": "test-operation", "stage": "prepared"}
        cli_engine._durable_json(cli_engine._journal(), operation)
        return directory, "isolated-test-service", operation

    class Child:
        def wait(self, timeout):
            return 1  # opaque failure; no HTTP status or Retry-After is available

    def launch(*args, **kwargs):
        calls.append(True)
        return Child()

    monkeypatch.setattr(cli_engine, "_prepare", prepare)
    monkeypatch.setattr(cli_engine, "_require_trusted_cli_opt_in", lambda: None)  # fake child only
    monkeypatch.setattr(cli_engine, "_assert_distinct_live", lambda *args: None)
    monkeypatch.setattr(cli_engine, "_find_claude", lambda: "/fake/claude")
    monkeypatch.setattr(cli_engine, "_inventory", lambda *args: [first])
    monkeypatch.setattr(cli_engine, "_group_quiescent", lambda child: True)
    monkeypatch.setattr(cli_engine.subprocess, "Popen", launch)
    with fsutil.app_lock():
        with pytest.raises(cli_engine.RefreshFailed):
            cli_engine.refresh_locked(a, first)
    assert calls == [True]
    assert not cli_engine._journal().exists()
    advance(clock, 900)
    with fsutil.app_lock():
        with pytest.raises(cli_engine.RetryBlocked):
            cli_engine.refresh_locked(a, first)
        cli_engine.guard_retry_locked(b, second)
    assert calls == [True]


def test_crash_journal_blocks_new_operations_even_after_scheduler_wait(clock, cli_engine):
    pid = os.fork()
    if pid == 0:
        with fsutil.app_lock():
            cli_engine._durable_json(cli_engine._journal(), {"version": 1, "nonce": "crashed-test", "stage": "launching"})
            os._exit(0)  # bypass normal cleanup, leaving durable ownership behind
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    advance(clock, 3600)
    with fsutil.app_lock():
        with pytest.raises(cli_engine.RecoveryRequired):
            cli_engine.guard_locked()
        with pytest.raises(cli_engine.RecoveryRequired):
            cli_engine.begin_login_locked()
    assert cli_engine._journal().exists()
