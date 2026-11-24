from __future__ import annotations

import json

import pytest

from cc_switcher import creds, fsutil, keychain, oauth, paths, tokens, usage
from cc_switcher.models import Account, make_account_id

from conftest import ACCOUNT_A, ACCOUNT_B, SAMPLE_MCP_OAUTH, make_login

NOW_MS = 1_800_000_000_000
FUTURE = NOW_MS + 8 * 3600 * 1000
PAST = NOW_MS - 60_000


@pytest.fixture(autouse=True)
def _no_real_version_probe(monkeypatch):
    monkeypatch.setattr(usage, "_version_cache", "2.1.330")


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


class FakePost:
    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[dict] = []

    def __call__(self, url, payload, *, headers, timeout):
        self.calls.append(payload)
        if self.raises:
            raise self.raises
        return self.status, self.body


def refreshed_body(**overrides):
    body = {"access_token": "sk-ant-oat01-REFRESHED", "refresh_token": "sk-ant-ort01-ROTATED", "expires_in": 28800}
    body.update(overrides)
    return body


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


# --------------------------------------------------------------------------- I3: the live account


def test_live_account_uses_the_live_access_token_without_any_http(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=4_102_444_800_000))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved-older", expires_at_ms=PAST))

    token = tokens.access_token_for(ACCT_A, http_post=ForbiddenPost())

    assert token == "sk-ant-oat01-fake-live"  # the live token, not the stale saved copy
    assert fake_keychain.writes == []


def test_expired_live_login_is_never_refreshed(fake_keychain):
    live = make_login(ACCOUNT_A, "live", expires_at_ms=PAST)
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved"))
    before = fake_keychain.items[live_service()]

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)

    assert fake_keychain.items[live_service()] == before
    assert fake_keychain.writes == []


def test_live_login_inside_the_short_skew_is_stale_but_outside_it_is_usable(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=NOW_MS + 10_000))
    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)

    put_live(fake_keychain, make_login(ACCOUNT_A, "live", expires_at_ms=NOW_MS + 90_000))
    # A live token with 90 s left is fine; it is NOT subject to the 5 minute refresh skew.
    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-live"


def test_live_login_without_an_access_token_is_stale_not_refreshed(fake_keychain):
    live = make_login(ACCOUNT_A, "live")
    live["accessToken"] = ""
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost())


def test_live_identity_is_decided_by_account_and_org_together(fake_keychain):
    same_user_other_org = (ACCOUNT_A[0], ACCOUNT_B[1])
    put_live(fake_keychain, make_login(same_user_other_org, "other-org", expires_at_ms=PAST))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    post = FakePost(body=refreshed_body())

    # The live login is the same person but a different organization: ACCT_A is NOT live, so it refreshes.
    assert tokens.access_token_for(ACCT_A, http_post=post, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"
    assert len(post.calls) == 1


def test_shared_refresh_token_counts_as_live_even_without_identity_fields(fake_keychain):
    """Defence in depth: never rotate a refresh token a running Claude Code session may be using."""
    live = make_login(ACCOUNT_A, "same", expires_at_ms=PAST)
    del live["accountUuid"], live["organizationUuid"]
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "same", expires_at_ms=PAST))

    with pytest.raises(tokens.TokenStale):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)
    assert fake_keychain.writes == []


def test_shared_refresh_token_with_a_valid_live_token_returns_the_live_token(fake_keychain):
    live = make_login(ACCOUNT_A, "same", expires_at_ms=FUTURE)
    del live["accountUuid"]
    put_live(fake_keychain, live)
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "same", expires_at_ms=PAST))

    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-same"


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
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)  # ForbiddenPost: never called

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

    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-alt-cfg"


# --------------------------------------------------------------------------- saved accounts


def test_valid_saved_token_is_returned_without_refreshing(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"
    assert fake_keychain.writes == []


def test_expired_saved_token_is_refreshed_and_written_back(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b", expires_at_ms=FUTURE))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    live_before = fake_keychain.items[live_service()]
    post = FakePost(body=refreshed_body())

    token = tokens.access_token_for(ACCT_A, http_post=post, now_ms=NOW_MS)

    assert token == "sk-ant-oat01-REFRESHED"
    assert [c["refresh_token"] for c in post.calls] == ["sk-ant-ort01-fake-saved"]
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


def test_token_inside_the_five_minute_skew_is_refreshed(fake_keychain):
    put_live(fake_keychain, make_login(ACCOUNT_B, "live-b"))
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=NOW_MS + 240_000))
    post = FakePost(body=refreshed_body())

    assert tokens.access_token_for(ACCT_A, http_post=post, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"


def test_saved_refresh_works_when_nobody_is_logged_in(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    post = FakePost(body=refreshed_body())

    assert tokens.access_token_for(ACCT_A, http_post=post, now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"


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
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)  # ForbiddenPost: never called

    assert isinstance(info.value, keychain.KeychainError)  # what monitor maps to "keychain_error"
    assert "Can't tell which login is live" in str(info.value)
    assert "sk-ant" not in str(info.value)
    assert fake_keychain.writes == []
    assert fake_keychain.items[ACCT_A.keychain_service][1] == saved_before


def test_unreadable_live_item_still_serves_an_unexpired_saved_token(fake_keychain):
    """Using a still-valid access token rotates nothing, so it stays available."""
    fake_keychain.items[live_service()] = ("tester", "{not json")
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"


def test_missing_live_item_means_nobody_is_live(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    assert tokens.access_token_for(ACCT_A, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)


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


def test_unrotated_refresh_token_is_kept_in_the_saved_item(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    body = refreshed_body()
    del body["refresh_token"]

    tokens.access_token_for(ACCT_A, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert saved_login(fake_keychain, ACCT_A)["refreshToken"] == "sk-ant-ort01-fake-saved"
    assert saved_login(fake_keychain, ACCT_A)["accessToken"] == "sk-ant-oat01-REFRESHED"


def test_refresh_rejection_surfaces_and_leaves_the_saved_item_alone(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    before = fake_keychain.items[ACCT_A.keychain_service]

    with pytest.raises(oauth.RefreshRejected):
        tokens.access_token_for(ACCT_A, http_post=FakePost(status=400, body={"error": "invalid_grant"}), now_ms=NOW_MS)

    assert fake_keychain.items[ACCT_A.keychain_service] == before


def test_transient_refresh_failure_leaves_the_saved_item_alone(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    before = fake_keychain.items[ACCT_A.keychain_service]

    with pytest.raises(oauth.OAuthError) as exc:
        tokens.access_token_for(ACCT_A, http_post=FakePost(raises=TimeoutError("slow")), now_ms=NOW_MS)

    assert not isinstance(exc.value, oauth.RefreshRejected)
    assert fake_keychain.items[ACCT_A.keychain_service] == before


# --------------------------------------------------------------------------- no usable credentials


def test_missing_saved_item(fake_keychain):
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost())


@pytest.mark.parametrize("secret", ["{not json", "[]", json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}), "{}"])
def test_unusable_saved_item(fake_keychain, secret):
    fake_keychain.items[ACCT_A.keychain_service] = ("tester", secret)
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost())


def test_saved_item_for_a_different_identity_is_not_used(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_B, "wrong-person", expires_at_ms=FUTURE))
    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)


def test_saved_login_without_identity_fields_is_still_usable(fake_keychain):
    login = make_login(ACCOUNT_A, "saved", expires_at_ms=FUTURE)
    del login["accountUuid"], login["organizationUuid"]
    put_saved(fake_keychain, ACCT_A, login)
    assert tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-saved"


def test_saved_login_with_only_a_refresh_token_is_refreshed(fake_keychain):
    login = make_login(ACCOUNT_A, "saved")
    login["accessToken"] = ""
    put_saved(fake_keychain, ACCT_A, login)

    assert tokens.access_token_for(ACCT_A, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)


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

    class LockCheckingPost(FakePost):
        def __call__(self, url, payload, *, headers, timeout):
            held["http"] = lock_is_held()
            return super().__call__(url, payload, headers=headers, timeout=timeout)

    tokens.access_token_for(ACCT_A, http_post=LockCheckingPost(body=refreshed_body()), now_ms=NOW_MS)

    assert held == {
        f"read:{live_service()}": True,
        f"read:{ACCT_A.keychain_service}": True,
        "http": True,
        f"write:{ACCT_A.keychain_service}": True,
    }
    assert not lock_is_held(), "the lock must be released afterwards"


def test_lock_is_released_after_failures(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    with pytest.raises(oauth.RefreshRejected):
        tokens.access_token_for(ACCT_A, http_post=FakePost(status=401, body={}), now_ms=NOW_MS)
    assert not lock_is_held()


def test_a_held_lock_times_out_instead_of_racing(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    real = fsutil.app_lock
    monkeypatch.setattr(fsutil, "app_lock", lambda timeout=30.0: real(timeout=0.05))

    with fsutil.app_lock(timeout=1):
        with pytest.raises(fsutil.LockTimeout):
            tokens.access_token_for(ACCT_A, http_post=ForbiddenPost(), now_ms=NOW_MS)


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

    token = tokens.access_token_for(ACCT_A, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)

    assert token == "sk-ant-oat01-REFRESHED"
    assert len(attempts) == 2
    assert saved_login(fake_keychain, ACCT_A)["refreshToken"] == "sk-ant-ort01-ROTATED"


def test_persistent_write_failure_is_reported(fake_keychain):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    fake_keychain.fail_writes_for.add(ACCT_A.keychain_service)

    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_A, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)


def test_tokens_never_appear_in_log_output(fake_keychain, caplog):
    put_saved(fake_keychain, ACCT_A, make_login(ACCOUNT_A, "saved", expires_at_ms=PAST))
    with caplog.at_level("DEBUG"):
        tokens.access_token_for(ACCT_A, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)
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
    post = FakePost(body=refreshed_body())

    token = tokens.access_token_for(ACCT_B, http_post=post, now_ms=NOW_MS)

    assert token == "sk-ant-oat01-REFRESHED"
    assert state["attempts"] == 2 and len(post.calls) == 1
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-ROTATED"


def test_unrotated_refresh_is_read_back_too(fake_keychain, monkeypatch):
    """No new refresh token in the reply: the access token is what a dropped write would lose."""
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=1)
    body = refreshed_body()
    del body["refresh_token"]

    tokens.access_token_for(ACCT_B, http_post=FakePost(body=body), now_ms=NOW_MS)

    assert state["attempts"] == 2
    assert saved_login(fake_keychain, ACCT_B)["accessToken"] == "sk-ant-oat01-REFRESHED"


def test_write_back_that_never_sticks_raises_and_keeps_the_login_for_the_next_call(fake_keychain, monkeypatch):
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    post = FakePost(body=refreshed_body())

    with pytest.raises(keychain.KeychainError) as info:
        tokens.access_token_for(ACCT_B, http_post=post, now_ms=NOW_MS)

    message = str(info.value)
    assert "could not be saved" in message and "ccs add" in message
    assert "sk-ant" not in message and "REFRESHED" not in message and "ROTATED" not in message
    assert state["attempts"] == 3 and len(post.calls) == 1
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-fake-B1"  # still the revoked one

    # The Keychain works again (e.g. unlocked): the next call saves the rotated login it kept in
    # memory, without refreshing again (that would need the revoked B1).
    state["left"] = 0
    token = tokens.access_token_for(ACCT_B, http_post=ForbiddenPost(), now_ms=NOW_MS)
    assert token == "sk-ant-oat01-REFRESHED"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-ROTATED"


def test_kept_login_is_dropped_when_the_saved_item_changed_meanwhile(fake_keychain, monkeypatch):
    """After the failed save the user re-added B (`ccs add`): the kept login must not overwrite that."""
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)

    state["left"] = 0
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "re-added", expires_at_ms=FUTURE))

    assert tokens.access_token_for(ACCT_B, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-fake-re-added"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-fake-re-added"


# --------------------------------------------------------------------------- R3: a removed account's kept login is dropped


def test_kept_login_of_a_removed_account_is_dropped_not_resaved(fake_keychain, monkeypatch):
    from cc_switcher import store

    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)
    state["left"] = 0
    fake_keychain.delete(ACCT_B.keychain_service)  # `ccs remove` meanwhile: item and registry row gone
    assert store.get_account(ACCT_B.id) is None

    with pytest.raises(tokens.NoCredentials):
        tokens.access_token_for(ACCT_B, http_post=ForbiddenPost(), now_ms=NOW_MS)  # a stale Account object

    assert fake_keychain.read(ACCT_B.keychain_service) is None  # not recreated
    assert ACCT_B.id not in tokens._UNSAVED


def test_kept_login_is_saved_when_the_item_is_gone_but_the_account_is_registered(fake_keychain, monkeypatch):
    from cc_switcher import store

    store.upsert_account(ACCT_B)
    put_saved(fake_keychain, ACCT_B, make_login(ACCOUNT_B, "B1", expires_at_ms=PAST))
    state = _drop_saved_writes(fake_keychain, monkeypatch, ACCT_B, times=99)
    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(ACCT_B, http_post=FakePost(body=refreshed_body()), now_ms=NOW_MS)
    state["left"] = 0
    fake_keychain.delete(ACCT_B.keychain_service)  # the item vanished, the account is still saved

    assert tokens.access_token_for(ACCT_B, http_post=ForbiddenPost(), now_ms=NOW_MS) == "sk-ant-oat01-REFRESHED"
    assert saved_login(fake_keychain, ACCT_B)["refreshToken"] == "sk-ant-ort01-ROTATED"
