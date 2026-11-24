from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import cc_switcher

from cc_switcher import creds, fsutil, keychain, oauth, paths, tokens, usage
from cc_switcher.models import Account, make_account_id

from conftest import ACCOUNT_A, ACCOUNT_B, SAMPLE_MCP_OAUTH, make_login

NOW_MS = 1_800_000_000_000
FUTURE = NOW_MS + 8 * 3600 * 1000
PAST = NOW_MS - 60_000


@pytest.fixture(autouse=True)
def _no_real_version_probe(monkeypatch):
    from cc_switcher import store
    from cc_switcher.models import Settings

    store.save_settings(Settings(claude_refresh_enabled=True))
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")
    from cc_switcher import claude_refresh

    monkeypatch.setattr(claude_refresh, "guard_locked", lambda: None)
    monkeypatch.setattr(claude_refresh, "guard_no_login_locked", lambda: None)
    monkeypatch.setattr(claude_refresh, "guard_retry_locked", lambda account, login: None)
    monkeypatch.setattr(claude_refresh, "refresh_locked", lambda account, login: pytest.fail("unexpected CLI refresh"))


def account_for(ident, email="a@example.com") -> Account:
    return Account(
        id=make_account_id(*ident),
        email=email,
        account_uuid=ident[0],
        organization_uuid=ident[1],
        label=email.split("@")[0],
    )


ACCT_A = account_for(ACCOUNT_A, "work@example.com")
ACCT_B = account_for(ACCOUNT_B, "home@example.com")


def live_service() -> str:
    return paths.claude_keychain_service()


def put_live(fake, login: dict | None, *, with_mcp: bool = True) -> None:
    blob: dict = {}
    if login is not None:
        blob["claudeAiOauth"] = login
    if with_mcp:
        blob["mcpOAuth"] = json.loads(json.dumps(SAMPLE_MCP_OAUTH))
    fake.put_json(live_service(), blob)


def put_saved(fake, account: Account, login: dict) -> None:
    fake.put_json(account.keychain_service, creds.saved_item(login))


def saved_login(fake, account: Account) -> dict:
    return fake.get_json(account.keychain_service)["claudeAiOauth"]


class FakeCLI:
    def __init__(self):
        self.calls = []

    def __call__(self, account, login):
        self.calls.append(login.copy())
        successor = dict(login, accessToken="sk-ant-oat01-REFRESHED",
                         refreshToken="sk-ant-ort01-ROTATED", expiresAt=NOW_MS + 28_800_000)
        keychain.write(account.keychain_service, paths.keychain_account_name(),
                       creds.dump_blob(creds.saved_item(successor)))
        return successor


def use_fake_cli(monkeypatch):
    runner = FakeCLI()
    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", runner)
    return runner


class ForbiddenPost:
    """Fails the test if anything tries to refresh a token."""

    calls = 0

    def __call__(self, *args, **kwargs):
        raise AssertionError("a token refresh was attempted")


def lock_is_held() -> bool:
    try:
        with fsutil.app_lock(timeout=0.05):
            return False
    except fsutil.LockTimeout:
        return True


def test_expired_saved_login_uses_cli_engine_under_lock_and_returns_persisted_successor(fake_keychain, monkeypatch):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b", expires_at_ms=FUTURE))
    predecessor = make_login(ACCOUNT_A, "old", expires_at_ms=PAST)
    put_saved(fake_keychain, ACCT_A, predecessor)
    successor = make_login(ACCOUNT_A, "new", expires_at_ms=FUTURE)
    observed = []

    def guard():
        assert lock_is_held()
        observed.append("guard")

    def refresh(account, login):
        assert lock_is_held()
        assert account == ACCT_A and login == predecessor
        observed.append("refresh")
        put_saved(fake_keychain, account, successor)
        return successor

    monkeypatch.setattr(cc_switcher, "claude_refresh", SimpleNamespace(
        guard_locked=guard, guard_no_login_locked=guard,
        guard_retry_locked=lambda account, login: guard(), refresh_locked=refresh,
    ), raising=False)
    monkeypatch.setattr(oauth, "refresh_login", lambda *a, **k: pytest.fail("direct OAuth refresh"), raising=False)

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-new"
    assert observed == ["guard", "guard", "guard", "refresh"]
    assert saved_login(fake_keychain, ACCT_A) == successor


def test_login_guard_blocks_refresh_before_scheduling(fake_keychain, monkeypatch):
    from cc_switcher import refresh_schedule

    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "old", expires_at_ms=PAST))

    class RecoveryRequired(RuntimeError):
        pass

    def guard_add():
        assert lock_is_held()
        raise RecoveryRequired("login in progress")

    def forbidden(*args, **kwargs):
        pytest.fail("scheduled or launched while login is in progress")

    monkeypatch.setattr(cc_switcher, "claude_refresh", SimpleNamespace(
        guard_locked=lambda: None, guard_no_login_locked=guard_add, refresh_locked=forbidden,
    ), raising=False)
    monkeypatch.setattr(refresh_schedule, "reserve", forbidden)
    monkeypatch.setattr(oauth, "refresh_login", forbidden, raising=False)

    with pytest.raises(RecoveryRequired):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


def test_retry_block_prevents_schedule_reservation(fake_keychain, monkeypatch):
    from cc_switcher import refresh_schedule

    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "old", expires_at_ms=PAST))

    class RetryBlocked(RuntimeError):
        pass

    def blocked(account, login):
        assert lock_is_held()
        raise RetryBlocked("manual recovery required")

    monkeypatch.setattr(cc_switcher.claude_refresh, "guard_retry_locked", blocked)
    monkeypatch.setattr(refresh_schedule, "reserve", lambda *a, **k: pytest.fail("reserved a blocked account"))
    with pytest.raises(RetryBlocked):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


def test_failed_cli_attempt_still_spaces_the_next_child(fake_keychain, monkeypatch):
    from cc_switcher import refresh_schedule

    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "old-a", expires_at_ms=PAST))
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "old-b", expires_at_ms=PAST))
    clock = {"wall": 1000.0, "mono": 1000.0}
    monkeypatch.setattr(refresh_schedule, "_clock", lambda: dict(clock))

    def failed(account, login):
        clock.update(wall=1100.0, mono=1100.0)
        raise RuntimeError("child failed")

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", failed)
    with pytest.raises(RuntimeError, match="child failed"):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    clock.update(wall=1279.0, mono=1279.0)
    with pytest.raises(refresh_schedule.RefreshQueued):
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)


# --------------------------------------------------------------------------- I3: the live account


def test_live_account_uses_the_live_access_token_without_any_http(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=4_102_444_800_000))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved-older", expires_at_ms=PAST))

    token = tokens.access_token_for(ACCT_A)

    assert token == "sk-ant-oat01-fake-live"  # the live token, not the stale saved copy
    assert fake_keychain.writes == []


def test_expired_live_login_is_never_refreshed(fake_keychain):
    live = make_login(ACCOUNT_A, "live", expires_at_ms=PAST)
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved"))
    before = fake_keychain.items[live_service()]

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert fake_keychain.items[live_service()] == before
    assert fake_keychain.writes == []


def test_live_login_inside_the_short_skew_is_stale_but_outside_it_is_usable(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=NOW_MS + 10_000))
    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=NOW_MS + 90_000))
    # A live token with 90 s left is fine; it is NOT subject to the 5 minute refresh skew.
    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-live"


def test_live_login_without_an_access_token_is_stale_not_refreshed(fake_keychain):
    live = make_login(ACCOUNT_A, "live")
    live["accessToken"] = ""
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A)


def test_live_identity_is_decided_by_account_and_org_together(fake_keychain, monkeypatch):
    same_user_other_org = (ACCOUNT_A[0], ACCOUNT_B[1])
    put_live(fake_keychain, make_login(same_user_other_org, "other-org", expires_at_ms=PAST))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    runner = use_fake_cli(monkeypatch)

    # The same person in a different organization is not the selected account.
    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"
    assert len(runner.calls) == 1


def test_shared_refresh_token_counts_as_live_even_without_identity_fields(fake_keychain):
    """Defence in depth: never rotate a refresh token a running Claude Code session may be using."""
    live = make_login(ACCOUNT_A, "same", expires_at_ms=PAST)
    del live["accountUuid"], live["organizationUuid"]
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "same", expires_at_ms=PAST))

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    assert fake_keychain.writes == []


def test_shared_refresh_token_with_a_valid_live_token_returns_the_live_token(fake_keychain):
    live = make_login(ACCOUNT_A, "same", expires_at_ms=FUTURE)
    del live["accountUuid"]
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "same", expires_at_ms=PAST))

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-same"


def _live_read_fails(fake_keychain, monkeypatch):
    real_read = keychain.read

    def read(service):
        if service == live_service():
            raise keychain.KeychainError("keychain locked")
        return real_read(service)

    monkeypatch.setattr(keychain, "read", read)


def test_unreadable_live_keychain_blocks_the_refresh(fake_keychain, monkeypatch):
    """I3: the live item can't be read, so liveness can't be determined: no refresh, no write."""
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    saved_before = fake_keychain.read(ACCT_A.keychain_service)
    _live_read_fails(fake_keychain, monkeypatch)

    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    assert fake_keychain.writes == []
    assert fake_keychain.items[ACCT_A.keychain_service][1] == saved_before


def test_live_service_follows_claude_config_dir(fake_keychain, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/alt-claude")
    assert live_service() != "Claude Code-credentials"
    # The default-service item belongs to a different Claude Code config: it must be ignored ...
    fake_keychain.put_json(
        "Claude Code-credentials",
        {"claudeAiOauth": make_login(ACCOUNT_A, "default-cfg", expires_at_ms=FUTURE)},
    )
    # ... and the hashed one is the live login.
    put_live(fake_keychain, make_login(ACCOUNT_A, "alt-cfg", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-alt-cfg"


# --------------------------------------------------------------------------- saved accounts


def test_valid_saved_token_is_returned_without_refreshing(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"
    assert fake_keychain.writes == []


def test_expired_saved_token_is_refreshed_and_written_back(fake_keychain, monkeypatch):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b", expires_at_ms=FUTURE))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    live_before = fake_keychain.items[live_service()]
    runner = use_fake_cli(monkeypatch)

    token = tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert token == "sk-ant-oat01-REFRESHED"
    assert [c["refreshToken"] for c in runner.calls] == ["sk-ant-ort01-fake-saved"]
    # The saved item now holds the rotated tokens and nothing but the login (I2) ...
    item = fake_keychain.get_json(ACCT_A.keychain_service)
    assert list(item) == ["claudeAiOauth"]
    assert item["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-REFRESHED"
    assert item["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-ROTATED"
    assert item["claudeAiOauth"]["expiresAt"] == NOW_MS + 28_800_000
    assert creds.login_account_id(item["claudeAiOauth"]) == ACCT_A.id
    # ... under the Claude Code account attribute, and the live item was not touched at all.
    assert fake_keychain.items[ACCT_A.keychain_service][0] == paths.keychain_account_name() == "tester"
    assert fake_keychain.items[live_service()] == live_before
    assert fake_keychain.writes == [ACCT_A.keychain_service]


def test_saved_token_with_four_minutes_left_does_not_invoke_refresh(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=NOW_MS + 240_000))

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"
    assert fake_keychain.writes == []


def test_saved_token_inside_30_second_skew_uses_cli_refresh(fake_keychain, monkeypatch):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=NOW_MS + 20_000))
    successor = make_login(ACCOUNT_A, "new", expires_at_ms=FUTURE)

    def refresh(account, login):
        put_saved(fake_keychain, account, successor)
        return successor

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", refresh)
    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-new"


def test_saved_refresh_works_when_nobody_is_logged_in(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    use_fake_cli(monkeypatch)

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"


@pytest.mark.parametrize("secret", ["{not json", "[]", '"a string"'])
def test_corrupt_live_item_does_not_refresh_saved_accounts(fake_keychain, secret):
    """I3 fails closed. Formerly the opposite: a corrupt live item counted as "nobody is live".

    But running sessions hold the live tokens in memory, so A might still be live; refreshing
    A's saved copy would rotate the refresh token they use. Raise (monitor: "keychain_error").
    """
    fake_keychain.items[live_service()] = ("tester", secret)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    saved_before = fake_keychain.read(ACCT_A.keychain_service)

    with pytest.raises(tokens.LiveLoginUnreadable) as info:
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    assert isinstance(info.value, keychain.KeychainError)  # what monitor maps to "keychain_error"
    assert "Can't tell which login is live" in str(info.value)
    assert "sk-ant" not in str(info.value)
    assert fake_keychain.writes == []
    assert fake_keychain.items[ACCT_A.keychain_service][1] == saved_before


def test_unreadable_live_item_still_serves_an_unexpired_saved_token(fake_keychain):
    """Using a still-valid access token rotates nothing, so it stays available."""
    fake_keychain.items[live_service()] = ("tester", "{not json")
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"


def test_missing_live_item_means_nobody_is_live(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    use_fake_cli(monkeypatch)
    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


@pytest.mark.parametrize("failure", ["corrupt_item", "read_error"])
def test_monitor_reports_undeterminable_liveness_as_keychain_error(fake_keychain, monkeypatch, failure):
    from cc_switcher import monitor

    if failure == "corrupt_item":
        fake_keychain.items[live_service()] = ("tester", "{not json")
    else:
        _live_read_fails(fake_keychain, monkeypatch)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=1))  # long expired

    snapshot = monitor.refresh_account(ACCT_A, http_post=ForbiddenPost())

    assert snapshot.ok is False and snapshot.error == "keychain_error"  # not "internal_error"
    assert fake_keychain.writes == []


def test_unrotated_refresh_token_is_kept_in_the_saved_item(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))

    def refresh(account, login):
        successor = dict(login, accessToken="sk-ant-oat01-REFRESHED", expiresAt=FUTURE)
        put_saved(fake_keychain, account, successor)
        return successor

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", refresh)
    tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert saved_login(fake_keychain, ACCT_A)["refreshToken"] == "sk-ant-ort01-fake-saved"
    assert saved_login(fake_keychain, ACCT_A)["accessToken"] == "sk-ant-oat01-REFRESHED"


def test_cli_needs_login_surfaces_and_leaves_the_saved_item_alone(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    before = fake_keychain.items[ACCT_A.keychain_service]

    class NeedsLogin(RuntimeError):
        pass

    def fail(account, login):
        raise NeedsLogin("login required")

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", fail)
    with pytest.raises(NeedsLogin):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert fake_keychain.items[ACCT_A.keychain_service] == before


def test_cli_refresh_failure_leaves_the_saved_item_alone(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    before = fake_keychain.items[ACCT_A.keychain_service]

    class RefreshFailed(RuntimeError):
        pass

    def fail(account, login):
        raise RefreshFailed("refresh failed")

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", fail)
    with pytest.raises(RefreshFailed):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert fake_keychain.items[ACCT_A.keychain_service] == before


# --------------------------------------------------------------------------- no usable credentials


def test_missing_saved_item(fake_keychain):
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A)


@pytest.mark.parametrize("secret", ["{not json", "[]", json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}), "{}"])
def test_unusable_saved_item(fake_keychain, secret):
    fake_keychain.items[ACCT_A.keychain_service] = ("tester", secret)
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A)


def test_saved_item_for_a_different_identity_is_not_used(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_B, "wrong-person", expires_at_ms=FUTURE))
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


def test_saved_login_without_identity_fields_is_still_usable(fake_keychain):
    login = make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE)
    del login["accountUuid"], login["organizationUuid"]
    put_saved(fake_keychain, ACCT_A, login)
    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"


def test_saved_login_with_only_a_refresh_token_is_refreshed(fake_keychain, monkeypatch):
    login = make_login(ACCOUNT_A, "saved")
    login["accessToken"] = ""
    put_saved(fake_keychain, ACCT_A, login)
    use_fake_cli(monkeypatch)

    assert tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


# --------------------------------------------------------------------------- locking and persistence


def test_liveness_check_and_refresh_both_happen_under_the_app_lock(fake_keychain, monkeypatch):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    held = {}

    real_read = keychain.read

    def read(service):
        held[f"read:{service}"] = lock_is_held()
        return real_read(service)

    real_write = keychain.write

    def write(service, account, secret):
        held[f"write:{service}"] = lock_is_held()
        return real_write(service, account, secret)

    monkeypatch.setattr(keychain, "read", read)
    monkeypatch.setattr(keychain, "write", write)

    class LockCheckingCLI(FakeCLI):
        def __call__(self, account, login):
            held["cli"] = lock_is_held()
            return super().__call__(account, login)

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", LockCheckingCLI())
    tokens.access_token_for(ACCT_A, now_ms=NOW_MS)

    assert held == {
        f"read:{live_service()}": True,
        f"read:{ACCT_A.keychain_service}": True,
        "cli": True,
        f"write:{ACCT_A.keychain_service}": True,
    }
    assert not lock_is_held(), "the lock must be released afterwards"


def test_lock_is_released_after_failures(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))

    def fail(account, login):
        raise RuntimeError("child failed")

    monkeypatch.setattr(cc_switcher.claude_refresh, "refresh_locked", fail)
    with pytest.raises(RuntimeError, match="child failed"):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    assert not lock_is_held()


def test_a_held_lock_times_out_instead_of_racing(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    real = fsutil.app_lock
    monkeypatch.setattr(fsutil, "app_lock", lambda timeout=30.0: real(timeout=0.05))

    with fsutil.app_lock(timeout=1):
        with pytest.raises(fsutil.LockTimeout):
            tokens.access_token_for(ACCT_A, now_ms=NOW_MS)


def test_write_back_is_retried_once(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    attempts = []
    real_write = fake_keychain.write

    def flaky(service, account, secret):
        attempts.append(service)
        if len(attempts) == 1:
            raise keychain.KeychainError("transient")
        return real_write(service, account, secret)

    monkeypatch.setattr(keychain, "write", flaky)

    successor = make_login(ACCOUNT_A, "successor", expires_at_ms=FUTURE)
    with fsutil.app_lock():
        tokens._persist(ACCT_A, successor)

    assert len(attempts) == 2
    assert saved_login(fake_keychain, ACCT_A) == successor


def test_persistent_write_failure_is_reported(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    fake_keychain.fail_writes_for.add(ACCT_A.keychain_service)

    with fsutil.app_lock(), pytest.raises(keychain.KeychainError):
        tokens._persist(ACCT_A, make_login(ACCOUNT_A, "successor", expires_at_ms=FUTURE))


def test_tokens_never_appear_in_log_output(fake_keychain, caplog, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    use_fake_cli(monkeypatch)
    with caplog.at_level("DEBUG"):
        tokens.access_token_for(ACCT_A, now_ms=NOW_MS)
    text = caplog.text
    for secret in ("sk-ant-oat01", "sk-ant-ort01"):
        assert secret not in text


# --------------------------------------------------------------------------- N2: the write-back after a refresh is verified


@pytest.fixture(autouse=True)
def _fresh_unsaved_stash(monkeypatch):
    """Each test starts without refreshed-but-unsaved logins left over from another test."""
    monkeypatch.setattr(tokens, "_UNSAVED", {}, raising=False)


def _drop_saved_writes(fake_keychain, monkeypatch, account: Account, *, times: int):
    """`security -i` exits 0 but stores nothing, for the first ``times`` writes to ``account``'s item."""
    real_write = fake_keychain.write
    state = {"left": times, "attempts": 0}  # set "left" to 0 to make writes work again

    def write(service, acct, secret):
        if service == account.keychain_service:
            state["attempts"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                return
        real_write(service, acct, secret)

    monkeypatch.setattr(keychain, "write", write)
    return state


def test_dropped_write_back_is_detected_and_retried(fake_keychain, monkeypatch):
    """Saved B1 expired. The refresh revokes B1 and returns B2; the first write silently stores nothing."""
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=1)
    successor = make_login(ACCOUNT_B, "successor", expires_at_ms=FUTURE)

    with fsutil.app_lock():
        tokens._persist(ACCT_B, successor)

    assert state["attempts"] == 2
    assert saved_login(fake_keychain, ACCT_B) == successor


def test_unrotated_refresh_is_read_back_too(fake_keychain, monkeypatch):
    """No new refresh token in the reply: the access token is what a dropped write would lose."""
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=1)
    successor = make_login(ACCOUNT_B, "B1", expires_at_ms=FUTURE)
    successor["accessToken"] = "sk-ant-oat01-REFRESHED"

    with fsutil.app_lock():
        tokens._persist(ACCT_B, successor)

    assert state["attempts"] == 2
    assert saved_login(fake_keychain, ACCT_B)["accessToken"] == "sk-ant-oat01-REFRESHED"


def test_write_back_that_never_sticks_raises_and_keeps_the_login_for_the_next_call(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    successor = make_login(ACCOUNT_B, "successor", expires_at_ms=FUTURE)
    successor.update(accessToken="sk-ant-oat01-REFRESHED", refreshToken="sk-ant-ort01-ROTATED")
    tokens._UNSAVED[ACCT_B.id] = ("sk-ant-ort01-fake-B1", successor)

    with pytest.raises(keychain.KeychainError) as info:
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)

    message = str(info.value)
    assert "could not be saved" in message and "ccs add" in message
    assert "sk-ant" not in message and "REFRESHED" not in message and "ROTATED" not in message
    assert state["attempts"] == 3
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-fake-B1"  # still the revoked one

    # The Keychain works again (e.g. unlocked): the next call saves the rotated login it kept in
    # memory, without refreshing again (that would need the revoked B1).
    state["left"] = 0
    token = tokens.access_token_for(ACCT_B, now_ms=NOW_MS)
    assert token == "sk-ant-oat01-REFRESHED"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-ROTATED"


def _keep_b_successor():
    successor = make_login(ACCOUNT_B, "successor", expires_at_ms=FUTURE)
    successor.update(accessToken="sk-ant-oat01-REFRESHED", refreshToken="sk-ant-ort01-ROTATED")
    tokens._UNSAVED[ACCT_B.id] = ("sk-ant-ort01-fake-B1", successor)


def test_kept_login_is_dropped_when_the_saved_item_changed_meanwhile(fake_keychain, monkeypatch):
    """After the failed save the user re-added B (`ccs add`): the kept login must not overwrite that."""
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    _keep_b_successor()
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)

    state["left"] = 0
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "re-added", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_B, now_ms=NOW_MS) == "sk-ant-oat01-fake-re-added"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-fake-re-added"


# --------------------------------------------------------------------------- R3: a removed account's kept login is dropped


def test_kept_login_of_a_removed_account_is_dropped_not_resaved(fake_keychain, monkeypatch):
    from cc_switcher import store

    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    _keep_b_successor()
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)
    state["left"] = 0
    fake_keychain.delete(ACCT_B.keychain_service)  # `ccs remove` meanwhile: item and registry row gone
    assert store.get_account(ACCT_B.id) is None

    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)  # a stale Account object

    assert fake_keychain.read(ACCT_B.keychain_service) is None  # not recreated
    assert ACCT_B.id not in tokens._UNSAVED


def test_kept_login_is_saved_when_the_item_is_gone_but_the_account_is_registered(fake_keychain, monkeypatch):
    from cc_switcher import store

    store.upsert_account(ACCT_B)
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    _keep_b_successor()
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, now_ms=NOW_MS)
    state["left"] = 0
    fake_keychain.delete(ACCT_B.keychain_service)  # the item vanished, the account is still saved

    assert tokens.access_token_for(ACCT_B, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-ROTATED"
