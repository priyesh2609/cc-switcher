"""Core switching against the fake Keychain. The point of these tests is invariants I1-I8."""

from __future__ import annotations

import contextlib
import copy
import json
import re
import subprocess

import pytest

from cc_switcher import claude_state, cli, creds, fsutil, keychain, paths, store, switcher, tokens
from cc_switcher.models import Account, make_account_id

LIVE = "Claude Code-credentials"
REAL_FIND_CLAUDE = switcher.find_claude  # captured before the autouse stub below replaces it
# The real keychain functions, captured before conftest's FakeKeychain replaces them.
REAL_KEYCHAIN = {n: getattr(keychain, n) for n in ("read", "read_account_attr", "write", "delete")}
CLIENT_CONFIG = {"sentry|800cb29a3ac61727": {"clientId": "cid-ü", "clientSecret": "fake-client-secret"}}


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _no_secrets(exc: BaseException) -> None:
    text = str(exc)
    for marker in ("sk-ant", "fake-sentry", "fake-client-secret"):
        assert marker not in text, text


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def strict_lock(monkeypatch):
    """Fail fast on re-entrant app_lock (the real one would just time out after 30 s)."""
    real = fsutil.app_lock
    state = {"held": False}

    @contextlib.contextmanager
    def checked(timeout: float = 30.0):
        assert not state["held"], "app_lock taken while already held (not re-entrant)"
        with real(timeout=1.0):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

    monkeypatch.setattr(fsutil, "app_lock", checked)
    return state


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    """Tests never depend on whether `claude` is installed on the machine running them."""
    monkeypatch.setattr(switcher, "find_claude", lambda: "/fake/bin/claude")


class World:
    """Helpers over the fake Keychain, the tmp ~/.claude.json and the registry."""

    def __init__(self, fake, samples, home):
        self.fake, self.s, self.home = fake, samples, home
        self.id_a = make_account_id(*samples.ACCOUNT_A)
        self.id_b = make_account_id(*samples.ACCOUNT_B)
        self.oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com", "Org A")
        self.oauth_b = samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com", "Org B")

    # live item
    def live_blob(self, login, *, mcp=True, extra=None) -> dict:
        blob = {"claudeAiOauth": login}
        if mcp:
            blob["mcpOAuth"] = copy.deepcopy(self.s.SAMPLE_MCP_OAUTH)
            blob["mcpOAuthClientConfig"] = copy.deepcopy(CLIENT_CONFIG)
        blob.update(extra or {})
        return blob

    def set_live(self, blob, account="tester") -> None:
        self.fake.put_json(LIVE, blob, account=account)

    def live(self) -> dict | None:
        return self.fake.get_json(LIVE)

    def live_raw(self) -> str | None:
        return self.fake.read(LIVE)

    # saved accounts
    def register(self, ident, email, *, token="1", oauth_account=None, label="") -> Account:
        account_id = make_account_id(*ident)
        account = Account(
            id=account_id,
            email=email,
            account_uuid=ident[0],
            organization_uuid=ident[1],
            label=label,
            oauth_account=oauth_account,
            added_at="2026-01-01T00:00:00+00:00",
        )
        store.upsert_account(account)
        self.fake.put_json(paths.saved_login_service(account_id), {"claudeAiOauth": self.s.make_login(ident, token)})
        return account

    def saved(self, account_id) -> dict | None:
        return self.fake.get_json(paths.saved_login_service(account_id))

    # ~/.claude.json
    def set_claude_json(self, data) -> None:
        paths.claude_json().write_text(json.dumps(data, indent=2))

    def claude_json(self) -> dict:
        return json.loads(paths.claude_json().read_text())


@pytest.fixture
def w(fake_keychain, samples, isolated_env) -> World:
    return World(fake_keychain, samples, isolated_env)


# ----------------------------------------------------------------------------- find_claude


def test_find_claude_prefers_the_explicit_app_binary(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CC_SWITCHER_CLAUDE_PATH", str(binary))
    monkeypatch.setattr(switcher.shutil, "which", lambda name: "/older/bin/claude")
    assert REAL_FIND_CLAUDE() == str(binary)


def test_invalid_explicit_claude_binary_does_not_silently_use_another(monkeypatch):
    monkeypatch.setenv("CC_SWITCHER_CLAUDE_PATH", "/missing/claude")
    monkeypatch.setattr(switcher.shutil, "which", lambda name: "/older/bin/claude")
    assert REAL_FIND_CLAUDE() is None


def test_find_claude_prefers_path(monkeypatch):
    monkeypatch.setattr(switcher.shutil, "which", lambda name: "/somewhere/claude")
    assert REAL_FIND_CLAUDE() == "/somewhere/claude"


def test_find_claude_checks_install_dirs_when_path_is_minimal(monkeypatch, tmp_path, isolated_env):
    monkeypatch.setattr(switcher.shutil, "which", lambda name: None)
    brew = tmp_path / "brew-bin"
    brew.mkdir()
    monkeypatch.setattr(switcher, "SYSTEM_BIN_DIRS", (brew,))
    assert REAL_FIND_CLAUDE() is None

    brew_claude = brew / "claude"
    brew_claude.write_text("#!/bin/sh\n")
    brew_claude.chmod(0o644)  # not executable: ignored
    assert REAL_FIND_CLAUDE() is None
    brew_claude.chmod(0o755)
    assert REAL_FIND_CLAUDE() == str(brew_claude)

    local = isolated_env / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "claude").write_text("#!/bin/sh\n")
    (local / "claude").chmod(0o755)
    assert REAL_FIND_CLAUDE() == str(local / "claude")  # ~/.local/bin (via paths.home()) first


# ----------------------------------------------------------------------------- status


def test_status_active_is_derived_from_the_live_token(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B, "live")))

    st = switcher.status()

    assert st.live_account_id == b.id
    assert st.active == b
    assert st.unregistered_live_login is False
    assert {x.id for x in st.accounts} == {a.id, b.id}
    assert switcher.active_account() == b
    assert switcher.live_account_id() == b.id


def test_status_unregistered_and_logged_out(w):
    w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    st = switcher.status()
    assert st.live_account_id == w.id_a and st.active is None and st.unregistered_live_login is True

    stub = w.s.make_login(w.s.ACCOUNT_A)
    stub["accessToken"] = stub["refreshToken"] = ""
    w.set_live(w.live_blob(stub))
    st = switcher.status()
    assert st.live_account_id is None and st.active is None and st.unregistered_live_login is False


def test_damaged_live_item(w):
    w.fake.items[LIVE] = ("tester", "not json at all")
    assert switcher.status().live_account_id is None
    with pytest.raises(switcher.LiveCredentialsUnreadable) as info:
        switcher.live_blob()
    assert isinstance(info.value, switcher.SwitchError)
    assert isinstance(info.value, creds.CredentialsError)
    _no_secrets(info.value)


# ----------------------------------------------------------------------------- switch_to: I1 / I5


def test_switch_preserves_mcp_state_byte_for_byte(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="a-saved")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", token="b-saved")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "a-live"), extra={"futureKey": {"keep": [1, "ü"]}})
    w.set_live(original)

    result = switcher.switch_to(b.id)

    live = w.live()
    assert result.id == b.id
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_B, "b-saved")
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert _compact(live["mcpOAuthClientConfig"]) == _compact(original["mcpOAuthClientConfig"])
    assert live["futureKey"] == {"keep": [1, "ü"]}
    assert list(live) == list(original)

    # I2: saved items hold the login only, never MCP tokens.
    for account_id in (a.id, b.id):
        assert list(w.saved(account_id)) == ["claudeAiOauth"]

    # And back again: still identical.
    switcher.switch_to(a.id)
    live = w.live()
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_A, "a-live")
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert _compact(live["mcpOAuthClientConfig"]) == _compact(original["mcpOAuthClientConfig"])


def test_switch_saves_outgoing_rotated_tokens(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    # Claude Code refreshed A's login since it was saved.
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "rotated")))

    switcher.switch_to(b.id)

    assert w.saved(a.id) == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "rotated")}


def test_switch_auto_imports_unregistered_live_login(w):
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    w.set_claude_json({"numStartups": 3, "oauthAccount": w.oauth_a})

    switcher.switch_to(b.id)

    imported = store.get_account(w.id_a)
    assert imported is not None
    assert imported.email == "a@example.com"
    assert imported.organization_name == "Org A"
    assert imported.oauth_account == w.oauth_a
    assert imported.subscription_type == "max"
    assert w.saved(w.id_a) == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "live")}
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]


def test_switch_auto_import_with_stale_claude_json_runs_no_subprocess(w):
    """~/.claude.json still describes B while A is live: A is imported without B's details."""
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=w.oauth_b)
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    w.set_claude_json({"oauthAccount": w.oauth_b})

    switcher.switch_to(b.id)  # conftest would raise if a subprocess ran

    imported = store.get_account(w.id_a)
    assert imported.email == "" and imported.organization_name == "" and imported.oauth_account is None
    assert w.saved(w.id_a)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-live"


def test_switch_writes_target_oauth_account_and_keeps_other_keys(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", oauth_account=w.oauth_a)
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=w.oauth_b)
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    fresh_a = dict(w.oauth_a, displayName="Alice (renamed)")
    w.set_claude_json({"numStartups": 7, "oauthAccount": fresh_a, "mcpServers": {"x": {"type": "http"}}})

    switcher.switch_to(b.id)

    data = w.claude_json()
    assert data == {"numStartups": 7, "oauthAccount": w.oauth_b, "mcpServers": {"x": {"type": "http"}}}
    # The outgoing account's snapshot was refreshed from the file before it was replaced.
    assert store.get_account(w.id_a).oauth_account == fresh_a
    assert store.get_account(w.id_a).display_name == "Alice (renamed)"


def test_switch_without_snapshot_removes_oauth_account(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", oauth_account=w.oauth_a)
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=None)
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    w.set_claude_json({"numStartups": 7, "oauthAccount": w.oauth_a})

    switcher.switch_to(b.id)

    assert w.claude_json() == {"numStartups": 7}  # Claude Code refetches the profile


def test_switch_ignores_a_snapshot_for_another_identity(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=w.oauth_a)  # wrong snapshot
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    w.set_claude_json({"oauthAccount": w.oauth_a})

    switcher.switch_to(b.id)

    assert "oauthAccount" not in w.claude_json()


def test_switch_sets_last_used_at(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    result = switcher.switch_to(b.id)
    assert result.last_used_at
    assert store.get_account(b.id).last_used_at == result.last_used_at


def test_switch_from_logged_out_state(w):
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live({"mcpOAuth": copy.deepcopy(w.s.SAMPLE_MCP_OAUTH)})
    switcher.switch_to(b.id)
    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert live["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH


def test_switch_when_no_live_item_exists(w):
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    switcher.switch_to(b.id)
    assert w.live() == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B)}
    assert w.fake.read_account_attr(LIVE) == "tester"  # paths.keychain_account_name() fallback


def test_switch_to_live_account_is_a_noop(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", token="old")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B, "newer")))
    raw = w.live_raw()

    result = switcher.switch_to(b.id)

    assert result.id == b.id
    assert w.live_raw() == raw
    assert LIVE not in w.fake.writes
    assert w.saved(b.id)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-newer"  # still saved (I5)


# ----------------------------------------------------------------------------- switch_to: refusals


def test_switch_unknown_account(w):
    with pytest.raises(switcher.UnknownAccount) as info:
        switcher.switch_to("nope:nope")
    assert isinstance(info.value, switcher.SwitchError)
    assert isinstance(info.value, store.AccountNotFound)


@pytest.mark.parametrize("problem", ["missing", "damaged", "logged_out", "other_identity"])
def test_switch_refuses_bad_saved_login_and_changes_nothing(w, problem):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    service_b = paths.saved_login_service(b.id)
    if problem == "missing":
        del w.fake.items[service_b]
    elif problem == "damaged":
        w.fake.items[service_b] = ("tester", "{broken")
    elif problem == "logged_out":
        w.fake.put_json(service_b, {"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
    else:
        w.fake.put_json(service_b, {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "x")})
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "rotated")))
    raw = w.live_raw()

    with pytest.raises(switcher.SwitchError, match="Re-add") as info:
        switcher.switch_to(b.id)

    _no_secrets(info.value)
    assert w.live_raw() == raw
    # The outgoing login was still saved first (I5).
    assert w.saved(w.id_a)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-rotated"


def test_switch_refuses_damaged_live_item(w):
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.fake.items[LIVE] = ("tester", "garbage")
    with pytest.raises(switcher.LiveCredentialsUnreadable):
        switcher.switch_to(b.id)
    assert w.fake.items[LIVE] == ("tester", "garbage")
    assert w.fake.writes == []


def test_switch_refuses_live_login_without_identity(w):
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    login = w.s.make_login(w.s.ACCOUNT_A)
    del login["accountUuid"]
    w.set_live(w.live_blob(login))
    raw = w.live_raw()
    with pytest.raises(switcher.SwitchError, match="no account id"):
        switcher.switch_to(b.id)
    assert w.live_raw() == raw


def test_switch_aborts_untouched_when_saving_outgoing_fails(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "rotated")))
    raw = w.live_raw()
    w.fake.fail_writes_for.add(paths.saved_login_service(a.id))

    with pytest.raises(switcher.SwitchError, match="nothing was changed"):
        switcher.switch_to(b.id)
    assert w.live_raw() == raw


# ----------------------------------------------------------------------------- switch_to: rollback


def _setup_two(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", oauth_account=w.oauth_a)
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=w.oauth_b)
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live"))
    w.set_live(original)
    w.set_claude_json({"numStartups": 1, "oauthAccount": w.oauth_a})
    return b, original


def _wiping_mcp_write(w, monkeypatch, *, times: int):
    """Live writes that carry B's login lose the whole mcpOAuth store, ``times`` times."""
    real_write = w.fake.write
    state = {"left": times}

    def write(service, account, secret):
        blob = json.loads(secret)
        if service == LIVE and state["left"] > 0 and blob["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]:
            state["left"] -= 1
            blob.pop("mcpOAuth", None)
            secret = json.dumps(blob)
        real_write(service, account, secret)

    monkeypatch.setattr(switcher.keychain, "write", write)


def test_verify_repairs_a_write_that_lost_the_mcp_store(w, monkeypatch):
    b, original = _setup_two(w)
    _wiping_mcp_write(w, monkeypatch, times=1)

    switcher.switch_to(b.id)

    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert live["mcpOAuth"] == original["mcpOAuth"]


def test_verify_failure_when_mcp_state_cannot_be_kept_restores_previous_blob(w, monkeypatch):
    b, original = _setup_two(w)
    _wiping_mcp_write(w, monkeypatch, times=99)

    with pytest.raises(switcher.SwitchError, match="previous login is back") as info:
        switcher.switch_to(b.id)

    _no_secrets(info.value)
    assert w.live() == original
    assert w.claude_json()["oauthAccount"] == w.oauth_a  # never touched
    assert store.get_account(b.id).last_used_at == ""


def test_verify_failure_when_keychain_kept_old_login_restores(w, monkeypatch):
    b, original = _setup_two(w)
    real_write = w.fake.write

    def live_writes_silently_ignored(service, account, secret):
        if service != LIVE:
            real_write(service, account, secret)

    monkeypatch.setattr(switcher.keychain, "write", live_writes_silently_ignored)

    with pytest.raises(switcher.SwitchError, match="didn't keep the new login"):
        switcher.switch_to(b.id)

    assert w.live() == original


def test_live_write_failure_reports_cleanly(w):
    b, original = _setup_two(w)
    raw = w.live_raw()
    w.fake.fail_writes_for.add(LIVE)

    with pytest.raises(switcher.SwitchError, match="previous login is back") as info:
        switcher.switch_to(b.id)

    _no_secrets(info.value)
    assert w.live_raw() == raw  # nothing was written; the restore was a no-op


def test_claude_state_failure_rolls_keychain_back(w, monkeypatch):
    b, original = _setup_two(w)

    def broken(oauth_account):
        raise claude_state.ClaudeStateError("~/.claude.json kept changing while cc-switcher tried to update it")

    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", broken)

    with pytest.raises(switcher.SwitchError, match="kept changing") as info:
        switcher.switch_to(b.id)

    _no_secrets(info.value)
    live = w.live()
    assert live == original
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert store.get_account(b.id).last_used_at == ""


def test_rollback_keeps_mcp_sign_in_made_during_the_switch(w, monkeypatch):
    """Claude Code stored a new MCP token between our write and the failure: keep it (I1)."""
    b, original = _setup_two(w)

    def claude_code_signs_in_then_state_fails(oauth_account):
        blob = w.live()
        blob["mcpOAuth"]["linear|0123456789abcdef"] = {"accessToken": "fake-linear-new"}
        w.set_live(blob)
        raise claude_state.ClaudeStateError("boom")

    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", claude_code_signs_in_then_state_fails)

    with pytest.raises(switcher.SwitchError):
        switcher.switch_to(b.id)

    live = w.live()
    assert live["claudeAiOauth"] == original["claudeAiOauth"]
    assert live["mcpOAuth"]["linear|0123456789abcdef"] == {"accessToken": "fake-linear-new"}
    assert live["mcpOAuth"]["sentry|800cb29a3ac61727"] == original["mcpOAuth"]["sentry|800cb29a3ac61727"]


def test_failed_restore_says_so(w, monkeypatch):
    b, _ = _setup_two(w)
    real_write = w.fake.write
    writes = {"live": 0}

    def second_live_write_fails(service, account, secret):
        if service == LIVE:
            writes["live"] += 1
            if writes["live"] > 1:
                raise switcher.keychain.KeychainError("Keychain write failed for 'Claude Code-credentials'")
        real_write(service, account, secret)

    def broken(oauth_account):
        raise claude_state.ClaudeStateError("boom")

    monkeypatch.setattr(switcher.keychain, "write", second_live_write_fails)
    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", broken)

    with pytest.raises(switcher.SwitchError, match="also failed") as info:
        switcher.switch_to(b.id)
    _no_secrets(info.value)


def test_keyboard_interrupt_mid_switch_restores_and_propagates(w, monkeypatch):
    b, original = _setup_two(w)

    def interrupted(oauth_account):
        raise KeyboardInterrupt

    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", interrupted)
    with pytest.raises(KeyboardInterrupt):
        switcher.switch_to(b.id)
    assert w.live() == original


# ----------------------------------------------------------------------------- save_active_login


def test_save_active_login_updates_saved_item_when_changed(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "rotated")))

    result = switcher.save_active_login()

    assert result.id == a.id
    assert w.saved(a.id) == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "rotated")}
    assert LIVE not in w.fake.writes  # the live item is never written here


def test_save_active_login_skips_identical_and_unregistered(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="same")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "same")))
    w.fake.writes.clear()
    assert switcher.save_active_login().id == a.id
    assert w.fake.writes == []

    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B)))
    assert switcher.save_active_login() is None
    assert w.fake.writes == []
    assert store.get_account(w.id_b) is None  # never auto-imported here


def test_save_active_login_with_no_or_damaged_live_item(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    assert switcher.save_active_login() is None
    w.fake.items[LIVE] = ("tester", "garbage")
    assert switcher.save_active_login() is None


def test_save_active_login_refreshes_plan_and_profile(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    login = w.s.make_login(w.s.ACCOUNT_A, "1")
    login["subscriptionType"] = "pro"
    login["rateLimitTier"] = "default_claude_pro"
    w.set_live(w.live_blob(login))
    w.set_claude_json({"oauthAccount": dict(w.oauth_a, organizationName="Org A Renamed")})

    account = switcher.save_active_login()

    assert account.subscription_type == "pro"
    assert account.rate_limit_tier == "default_claude_pro"
    assert account.organization_name == "Org A Renamed"
    assert store.get_account(w.id_a) == account


def test_saved_item_is_repaired_to_login_only(w):
    """An older saved item that carried MCP tokens is rewritten as login-only (I2)."""
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="1")
    w.fake.put_json(
        paths.saved_login_service(a.id),
        {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "1"), "mcpOAuth": w.s.SAMPLE_MCP_OAUTH},
    )
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "1")))
    switcher.save_active_login()
    assert list(w.saved(a.id)) == ["claudeAiOauth"]


# ----------------------------------------------------------------------------- import_current


class StatusRunner:
    def __init__(self, payload=None, rc=0, side_effect=None):
        self.payload, self.rc, self.side_effect = payload, rc, side_effect
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.side_effect:
            self.side_effect()
        stdout = json.dumps(self.payload) if self.payload is not None else ""
        return subprocess.CompletedProcess(argv, self.rc, stdout, "")


def test_import_requires_a_live_login(w):
    with pytest.raises(switcher.SwitchError, match="isn't logged in"):
        switcher.import_current()


def test_import_uses_matching_oauth_account(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    w.set_claude_json({"oauthAccount": w.oauth_a})
    runner = StatusRunner()

    account = switcher.import_current("work", runner=runner)

    assert runner.calls == []  # email was known: no subprocess
    assert account.id == w.id_a
    assert (account.email, account.organization_name, account.display_name) == ("a@example.com", "Org A", "a")
    assert account.subscription_type == "max" and account.rate_limit_tier == "default_claude_max_20x"
    assert account.label == "work"
    assert account.oauth_account == w.oauth_a
    assert account.added_at
    assert store.get_account(w.id_a) == account
    assert w.saved(w.id_a) == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "live")}  # I2
    assert "sk-ant" not in paths.accounts_file().read_text()  # I7


def test_import_with_stale_claude_json_and_stale_auth_status(w):
    """oauthAccount (and so `claude auth status`) still describe B while A is live."""
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    w.set_claude_json({"oauthAccount": w.oauth_b})
    runner = StatusRunner({"loggedIn": True, "email": "b@example.com", "orgId": w.s.ACCOUNT_B[1], "orgName": "Org B"})

    account = switcher.import_current(runner=runner)

    assert runner.calls == [["/fake/bin/claude", "auth", "status", "--json"]]
    assert account.id == w.id_a
    assert account.email == ""  # B's email was rejected
    assert account.organization_name == "" and account.oauth_account is None
    assert w.saved(w.id_a)["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]


def test_import_picks_up_profile_refreshed_by_auth_status(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    w.set_claude_json({"oauthAccount": w.oauth_b})
    runner = StatusRunner(
        {"loggedIn": True, "email": "a@example.com", "orgId": w.s.ACCOUNT_A[1]},
        side_effect=lambda: w.set_claude_json({"oauthAccount": w.oauth_a}),
    )
    account = switcher.import_current(runner=runner)
    assert account.email == "a@example.com" and account.oauth_account == w.oauth_a


def test_import_uses_auth_status_email_when_file_has_none(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    runner = StatusRunner({"loggedIn": True, "email": "a@example.com", "orgId": w.s.ACCOUNT_A[1], "orgName": "Org A"})
    account = switcher.import_current(runner=runner)
    assert account.email == "a@example.com" and account.organization_name == "Org A"


def test_import_ignores_auth_status_for_another_org(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "live")))
    runner = StatusRunner({"loggedIn": True, "email": "x@example.com", "orgId": "some-other-org"})
    assert switcher.import_current(runner=runner).email == ""


def test_import_same_person_other_org_takes_only_personal_fields(w):
    """Same accountUuid, different org (personal plan vs Team seat)."""
    team = (w.s.ACCOUNT_A[0], "0000000t-0000-0000-0000-00000000000t")
    w.set_live(w.live_blob(w.s.make_login(team, "team")))
    w.set_claude_json({"oauthAccount": w.oauth_a})  # personal org's profile

    account = switcher.import_current(runner=StatusRunner())

    assert account.id == make_account_id(*team)
    assert account.email == "a@example.com" and account.display_name == "a"
    assert account.organization_name == "" and account.oauth_account is None


def test_import_saves_tokens_refreshed_by_auth_status(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "before")))
    runner = StatusRunner(
        {"loggedIn": True},
        side_effect=lambda: w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "refreshed"))),
    )
    switcher.import_current(runner=runner)
    assert w.saved(w.id_a)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-refreshed"


def test_import_fails_if_login_changes_during_auth_status(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "before")))
    runner = StatusRunner(side_effect=lambda: w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B))))
    with pytest.raises(switcher.SwitchError, match="changed while importing"):
        switcher.import_current(runner=runner)
    assert store.load_accounts() == []


def test_reimport_keeps_label_and_added_at(w):
    original = w.register(w.s.ACCOUNT_A, "a@example.com", label="work", token="old")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "new")))

    account = switcher.import_current(runner=StatusRunner())

    assert account.label == "work" and account.added_at == original.added_at
    assert account.email == "a@example.com"  # kept although ~/.claude.json is missing
    assert w.saved(w.id_a)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-new"
    assert switcher.import_current("home", runner=StatusRunner()).label == "home"


def test_import_rejects_label_used_by_another_account(w):
    w.register(w.s.ACCOUNT_B, "b@example.com", label="work")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    w.fake.writes.clear()
    with pytest.raises(switcher.SwitchError, match="already used"):
        switcher.import_current("Work", runner=StatusRunner())
    assert store.get_account(w.id_a) is None and w.fake.writes == []


def test_import_survives_auth_status_failures(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))

    def boom(argv):
        raise FileNotFoundError(2, "No such file")

    assert switcher.import_current(runner=boom).email == ""


# ----------------------------------------------------------------------------- claude_auth_status


def test_claude_auth_status_parsing():
    payload = {"loggedIn": False, "authMethod": "none"}
    assert switcher.claude_auth_status(StatusRunner(payload, rc=1)) == payload  # JSON on exit 1 too
    assert switcher.claude_auth_status(lambda argv: (0, json.dumps({"a": 1}))) == {"a": 1}
    assert switcher.claude_auth_status(lambda argv: (0, "not json")) is None
    assert switcher.claude_auth_status(lambda argv: (0, "[1]")) is None
    assert switcher.claude_auth_status(lambda argv: (0, "")) is None

    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 20)

    assert switcher.claude_auth_status(timeout) is None


def test_claude_auth_status_without_claude(monkeypatch):
    monkeypatch.setattr(switcher, "find_claude", lambda: None)
    assert switcher.claude_auth_status() is None


def test_default_runners(monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 3, '{"loggedIn": true}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert switcher.claude_auth_status() == {"loggedIn": True}
    argv, kwargs = seen[-1]
    assert argv == ["/fake/bin/claude", "auth", "status", "--json"]
    assert kwargs["capture_output"] is True and kwargs["timeout"] == switcher.AUTH_STATUS_TIMEOUT_SECONDS

    assert switcher._login_runner(["/fake/bin/claude", "auth", "login"]) == 3
    argv, kwargs = seen[-1]
    assert kwargs == {}  # stdio inherited: the user interacts with the login


# ----------------------------------------------------------------------------- add_account


class LoginRunner:
    """Simulates `claude auth login`: writes what Claude Code would, returns an exit code."""

    def __init__(
        self, w: World, *, rc=0, new_blob=None, oauth_account=None, raise_exc=None, check_lock=None, on_run=None
    ):
        self.w, self.rc, self.new_blob, self.oauth_account = w, rc, new_blob, oauth_account
        self.raise_exc, self.check_lock, self.on_run = raise_exc, check_lock, on_run
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.check_lock is not None:
            assert not self.check_lock["held"], "app_lock must not be held during `claude auth login`"
            with fsutil.app_lock(timeout=0.5):  # another process could take it
                pass
        if self.on_run is not None:
            self.on_run()  # e.g. a running Claude Code session refreshing its login meanwhile
        if self.new_blob is not None:
            self.w.set_live(self.new_blob)
        if self.oauth_account is not None:
            self.w.set_claude_json({"numStartups": 1, "oauthAccount": self.oauth_account})
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.rc


def test_add_restores_mcp_wiped_by_login_and_imports(w, strict_lock):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="old")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "rotated"), extra={"futureKey": 1})
    w.set_live(original)
    runner = LoginRunner(
        w,
        new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B, "new")},  # login dropped MCP state
        oauth_account=w.oauth_b,
        check_lock=strict_lock,
    )

    account = switcher.add_account("second", runner=runner, status_runner=StatusRunner())

    assert runner.calls == [["/fake/bin/claude", "auth", "login"]]
    assert account.id == w.id_b and account.email == "b@example.com" and account.label == "second"
    assert account.oauth_account == w.oauth_b
    live = w.live()
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_B, "new")
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert _compact(live["mcpOAuthClientConfig"]) == _compact(original["mcpOAuthClientConfig"])
    assert live["futureKey"] == 1
    assert w.saved(w.id_b) == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B, "new")}
    # Phase 1 kept A's rotated tokens before the login replaced them (I5).
    assert w.saved(a.id)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-rotated"


def test_add_success_restores_entry_by_entry_newest_tokens_winning(w):
    """The login rewrote the item, leaving only a rotated sentry entry: every other entry comes
    back, and the newer sentry tokens win (round-3 R1)."""
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    rotated = {"serverName": "sentry", "accessToken": "fake-sentry-rotated", "refreshToken": "r2"}
    runner = LoginRunner(
        w,
        new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B), "mcpOAuth": {"sentry|800cb29a3ac61727": rotated}},
        oauth_account=w.oauth_b,
    )
    switcher.add_account(runner=runner)
    live = w.live()
    assert live["mcpOAuth"] == {
        "sentry|800cb29a3ac61727": rotated,
        "atlassian|1eb778bd626fb68d": w.s.SAMPLE_MCP_OAUTH["atlassian|1eb778bd626fb68d"],
    }
    assert live["mcpOAuthClientConfig"] == CLIENT_CONFIG


def test_add_auto_imports_unregistered_login_first(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "only-copy")))
    w.set_claude_json({"oauthAccount": w.oauth_a})
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B)}, oauth_account=w.oauth_b)

    switcher.add_account(runner=runner)

    assert store.get_account(w.id_a).email == "a@example.com"
    assert w.saved(w.id_a)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-only-copy"


def test_add_cancelled_leaves_everything(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    raw = w.live_raw()
    assert switcher.add_account(runner=LoginRunner(w, rc=1)) is None
    assert w.live_raw() == raw
    assert LIVE not in w.fake.writes


def test_add_rc_zero_without_new_login_returns_none(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    assert switcher.add_account(runner=LoginRunner(w, rc=0)) is None
    assert store.get_account(w.id_b) is None


def test_add_failure_after_wiping_restores_previous_login_and_mcp(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "keep"))
    w.set_live(original)
    runner = LoginRunner(w, rc=1, new_blob={"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})

    assert switcher.add_account(runner=runner) is None
    assert w.live() == original


def test_add_interrupted_restores_and_propagates(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "keep"))
    w.set_live(original)
    runner = LoginRunner(w, new_blob={}, raise_exc=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        switcher.add_account(runner=runner)
    assert w.live() == original


def test_add_runner_os_error_is_a_switch_error(w):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    runner = LoginRunner(w, raise_exc=FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(switcher.SwitchError, match="claude auth login"):
        switcher.add_account(runner=runner)


def test_add_reauthenticates_existing_account(w):
    """The 're-add' path after a rejected refresh: same account, new tokens."""
    a = w.register(w.s.ACCOUNT_A, "a@example.com", label="work", token="dead")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B)))
    w.register(w.s.ACCOUNT_B, "b@example.com")
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "fresh")}, oauth_account=w.oauth_a)

    account = switcher.add_account(runner=runner)

    assert account.id == a.id and account.label == "work"
    assert w.saved(a.id)["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-fake-fresh"
    assert w.live()["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH


def test_add_on_fresh_mac_with_no_live_item(w):
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A)}, oauth_account=w.oauth_a)
    account = switcher.add_account(runner=runner)
    assert account.id == w.id_a
    assert w.live() == {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A)}
    assert LIVE not in w.fake.writes  # nothing to restore, so the live item was not rewritten


def test_add_with_label_taken_still_saves_the_login(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", label="work")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B)}, oauth_account=w.oauth_b)

    with pytest.raises(switcher.SwitchError, match="without a label"):
        switcher.add_account("work", runner=runner)
    assert store.get_account(w.id_b).label == ""
    assert w.saved(w.id_b) is not None


def test_add_without_claude_installed(w, monkeypatch):
    monkeypatch.setattr(switcher, "find_claude", lambda: None)
    with pytest.raises(switcher.SwitchError, match="Claude Code installed"):
        switcher.add_account()


def test_add_with_damaged_live_item_can_recover(w):
    w.fake.items[LIVE] = ("tester", "garbage")
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A)}, oauth_account=w.oauth_a)
    assert switcher.add_account(runner=runner).id == w.id_a


def test_add_cancel_does_not_overwrite_a_damaged_item_with_nothing(w):
    w.fake.items[LIVE] = ("tester", "garbage")
    assert switcher.add_account(runner=LoginRunner(w, rc=1)) is None
    assert w.fake.items[LIVE] == ("tester", "garbage")


# ----------------------------------------------------------------------------- remove / label


def test_remove_refuses_active_unless_forced(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))

    with pytest.raises(switcher.SwitchError, match="active account"):
        switcher.remove_account(a.id)
    assert store.get_account(a.id) is not None and w.saved(a.id) is not None

    switcher.remove_account(a.id, force=True)
    assert store.get_account(a.id) is None and w.saved(a.id) is None
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]  # live login untouched


def test_remove_inactive_account(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    switcher.remove_account(b.id)
    assert store.get_account(b.id) is None and w.saved(b.id) is None
    assert store.get_account(w.id_a) is not None


def test_remove_unknown_and_orphan(w):
    with pytest.raises(switcher.UnknownAccount):
        switcher.remove_account("nope:nope")
    w.fake.put_json(paths.saved_login_service("orphan:org"), {"claudeAiOauth": {}})
    switcher.remove_account("orphan:org")  # cleans the orphaned item
    assert w.fake.read(paths.saved_login_service("orphan:org")) is None


def test_set_label(w):
    a = w.register(w.s.ACCOUNT_A, "a@example.com")
    w.register(w.s.ACCOUNT_B, "b@example.com", label="home")

    assert switcher.set_label(a.id, "  work ").label == "work"
    assert store.get_account(a.id).label == "work"
    with pytest.raises(switcher.SwitchError, match="already used by b@example.com"):
        switcher.set_label(a.id, "HOME")
    assert switcher.set_label(a.id, "").label == ""
    with pytest.raises(switcher.UnknownAccount):
        switcher.set_label("nope:nope", "x")


# ============================================================================= review regressions

SENTRY = "sentry|800cb29a3ac61727"
ATLASSIAN = "atlassian|1eb778bd626fb68d"


def _rt(blob) -> str | None:
    return ((blob or {}).get("claudeAiOauth") or {}).get("refreshToken")


def _tok(suffix: str) -> str:
    return f"sk-ant-ort01-fake-{suffix}"


# ----------------------------------------------------------------------------- 1: add phase 3 never replaces a live login


def _refresh_live(w, ident, suffix, sentry_rt=None):
    """What a running Claude Code session does: rewrite the whole item with a refreshed login."""
    blob = w.live()
    blob["claudeAiOauth"] = w.s.make_login(ident, suffix)
    if sentry_rt is not None:
        blob["mcpOAuth"][SENTRY]["refreshToken"] = sentry_rt
    w.set_live(blob)


@pytest.mark.parametrize("how", ["cancel", "failure", "ctrl_c"])
def test_add_cancel_keeps_a_login_rotated_by_a_running_session(w, how):
    """Live A1 (saved). During the browser login a session refreshes A -> A2 (server kills A1)."""
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    kwargs = {"rc": 1} if how == "cancel" else {"rc": 7} if how == "failure" else {"raise_exc": KeyboardInterrupt()}
    runner = LoginRunner(w, on_run=lambda: _refresh_live(w, w.s.ACCOUNT_A, "A2"), **kwargs)

    if how == "ctrl_c":
        with pytest.raises(KeyboardInterrupt):
            switcher.add_account(runner=runner)
    else:
        assert switcher.add_account(runner=runner) is None

    assert _rt(w.live()) == _tok("A2")  # the live login was not rolled back to the dead A1
    assert _rt(w.saved(a.id)) == _tok("A2")  # and its saved copy caught up
    assert w.live()["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH


def test_add_cancel_keeps_another_account_switched_in_meanwhile(w):
    """The menu bar auto-switched to B during the login, then the user cancelled."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    runner = LoginRunner(w, rc=1, on_run=lambda: _refresh_live(w, w.s.ACCOUNT_B, "B1"))

    assert switcher.add_account(runner=runner) is None

    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert _rt(live) == _tok("B1") and _rt(w.saved(b.id)) == _tok("B1")
    assert live["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH


def test_add_cancel_restores_a_login_emptied_by_claude(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1"))
    w.set_live(original)
    emptied = {"claudeAiOauth": dict(w.s.make_login(w.s.ACCOUNT_A, "A1"), accessToken="", refreshToken="")}
    assert switcher.add_account(runner=LoginRunner(w, rc=1, new_blob=emptied)) is None
    assert w.live() == original


def test_add_same_identity_login_reauthenticates_in_place(w):
    """`claude auth login` comes back with the account that was already live: new tokens, same id."""
    a = w.register(w.s.ACCOUNT_A, "a@example.com", label="work", token="A1")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1"))
    w.set_live(original)
    runner = LoginRunner(
        w, new_blob={"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "A-new")}, oauth_account=w.oauth_a
    )

    account = switcher.add_account(runner=runner)

    assert account.id == a.id and account.label == "work" and account.email == "a@example.com"
    assert _rt(w.saved(a.id)) == _tok("A-new")
    live = w.live()
    assert _rt(live) == _tok("A-new")
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert live["mcpOAuthClientConfig"] == original["mcpOAuthClientConfig"]
    assert [x.id for x in store.load_accounts()] == [a.id]


# ----------------------------------------------------------------------------- 2: switch_to never merges onto a stale read


def _once(predicate, action):
    state = {"done": False}

    def hook(service):
        if not state["done"] and predicate(service):
            state["done"] = True
            action()

    return hook


@pytest.mark.parametrize("trigger", ["after_live_read", "while_reading_target"])
def test_switch_rebases_on_a_refresh_between_read_and_write(w, trigger):
    """Live {A1, sentry S1}. Between our read and our write Claude Code writes {A2, sentry S2}."""
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    claude_code_writes = lambda: _refresh_live(w, w.s.ACCOUNT_A, "A2", sentry_rt="fake-sentry-S2")  # noqa: E731
    if trigger == "after_live_read":
        w.fake.after_read = _once(lambda s: s == LIVE, claude_code_writes)
    else:  # the reviewer's experiment: the change lands while the target's saved login is read
        w.fake.before_read = _once(lambda s: s == paths.saved_login_service(b.id), claude_code_writes)

    switcher.switch_to(b.id)

    live = w.live()
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_B, "B1")
    assert live["mcpOAuth"][SENTRY]["refreshToken"] == "fake-sentry-S2"  # newest MCP token kept
    assert live["mcpOAuth"][ATLASSIAN] == w.s.SAMPLE_MCP_OAUTH[ATLASSIAN]
    assert _rt(w.saved(a.id)) == _tok("A2")  # newest A token saved


def test_switch_gives_up_when_the_live_item_keeps_changing(w):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    counter = {"n": 1}

    def keeps_refreshing(service):
        if service == LIVE:
            counter["n"] += 1
            _refresh_live(w, w.s.ACCOUNT_A, f"A{counter['n']}")

    w.fake.after_read = keeps_refreshing

    with pytest.raises(switcher.SwitchError, match="changed its login while switching") as info:
        switcher.switch_to(b.id)

    _no_secrets(info.value)
    assert LIVE not in w.fake.writes  # B was never written over a moving item
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]


def test_switch_keeps_an_mcp_write_that_lands_right_after_ours(w):
    """Claude Code rewrites MCP state right after our write: its store is kept exactly as written,
    including an entry it dropped (indistinguishable from `claude mcp remove`, round-2 N1)."""
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))

    def claude_code_mcp_write():
        blob = w.live()
        blob["mcpOAuth"][SENTRY]["accessToken"] = "fake-sentry-S3"
        blob["mcpOAuth"]["linear|0123456789abcdef"] = {"accessToken": "fake-linear-new"}
        del blob["mcpOAuth"][ATLASSIAN]
        w.set_live(blob)

    w.fake.before_read = _once(lambda s: s == LIVE and LIVE in w.fake.writes, claude_code_mcp_write)

    switcher.switch_to(b.id)

    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert live["mcpOAuth"][SENTRY]["accessToken"] == "fake-sentry-S3"
    assert live["mcpOAuth"]["linear|0123456789abcdef"] == {"accessToken": "fake-linear-new"}
    assert ATLASSIAN not in live["mcpOAuth"]


def test_rollback_never_overwrites_a_login_someone_else_wrote(w, monkeypatch):
    """The switch fails after the write, but by then the live login is a third account's."""
    b, original = _setup_two(w)
    third = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")

    def user_logs_in_elsewhere_then_state_fails(oauth_account):
        _refresh_live(w, third, "C1")
        raise claude_state.ClaudeStateError("boom")

    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", user_logs_in_elsewhere_then_state_fails)

    with pytest.raises(switcher.SwitchError, match="left as is"):
        switcher.switch_to(b.id)
    assert w.live()["claudeAiOauth"]["accountUuid"] == third[0]
    assert w.live()["mcpOAuth"] == original["mcpOAuth"]


# ----------------------------------------------------------------------------- 5: lone surrogate in ~/.claude.json


def test_switch_with_a_lone_surrogate_in_claude_json(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com", oauth_account=w.oauth_b)
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    paths.claude_json().write_text('{"projects": {"/p": {"lastPrompt": "hi \\ud83d"}}, "oauthAccount": {}}\n')

    switcher.switch_to(b.id)

    data = json.loads(paths.claude_json().read_text())
    assert data["projects"]["/p"]["lastPrompt"] == "hi \ud83d"  # lossless
    assert data["oauthAccount"] == w.oauth_b
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]


# ----------------------------------------------------------------------------- 6: saved-item writes are verified


def _saved_writes_silently_ignored(w, monkeypatch):
    """`security -i` can exit 0 without storing anything: saved-item writes are dropped."""
    real_write = w.fake.write

    def write(service, account, secret):
        if service.startswith(paths.SAVED_LOGIN_SERVICE_PREFIX):
            return
        real_write(service, account, secret)

    monkeypatch.setattr(switcher.keychain, "write", write)


def test_switch_stops_if_the_outgoing_save_did_not_stick(w, monkeypatch):
    a = w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A2")))
    raw = w.live_raw()
    _saved_writes_silently_ignored(w, monkeypatch)

    with pytest.raises(switcher.SwitchError, match="nothing was changed"):
        switcher.switch_to(b.id)
    assert w.live_raw() == raw
    assert _rt(w.saved(a.id)) == _tok("A1")


def test_save_active_login_reports_a_save_that_did_not_stick(w, monkeypatch):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A2")))
    _saved_writes_silently_ignored(w, monkeypatch)
    with pytest.raises(switcher.keychain.KeychainError):
        switcher.save_active_login()


def test_import_reports_a_save_that_did_not_stick(w, monkeypatch):
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    w.set_claude_json({"oauthAccount": w.oauth_a})
    _saved_writes_silently_ignored(w, monkeypatch)
    with pytest.raises(switcher.keychain.KeychainError):
        switcher.import_current()
    assert store.get_account(w.id_a) is None  # registry never points at a missing item


class SecurityEmulator:
    """Just enough of /usr/bin/security for keychain.py: items keyed by (account, service),
    looked up like `security` does (``-a`` narrows the match; without it the first item wins)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        if args == ["-i"]:
            m = re.fullmatch(r'add-generic-password -U -a "([^"]*)" -s "([^"]*)" -X "([0-9a-f]*)"\n', input_text)
            assert m, input_text
            self.items[(m[1], m[2])] = bytes.fromhex(m[3]).decode("utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        command, opts, flags, rest = args[0], {}, set(), list(args[1:])
        while rest:
            token = rest.pop(0)
            if token in ("-a", "-s", "-X"):
                opts[token] = rest.pop(0)
            else:
                flags.add(token)
        if command == "add-generic-password":
            self.items[(opts["-a"], opts["-s"])] = bytes.fromhex(opts["-X"]).decode("utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        matches = [k for k in self.items if k[1] == opts["-s"] and ("-a" not in opts or k[0] == opts["-a"])]
        if not matches:
            return subprocess.CompletedProcess(args, 44, "", "security: The specified item could not be found in the keychain.\n")
        key = matches[0]
        if command == "delete-generic-password":
            del self.items[key]
            return subprocess.CompletedProcess(args, 0, "", "")
        if "-w" in flags:
            return subprocess.CompletedProcess(args, 0, self.items[key] + "\n", "")
        attrs = f'keychain: "/x/login.keychain-db"\nattributes:\n    "acct"<blob>="{key[0]}"\n    "svce"<blob>="{key[1]}"\n'
        return subprocess.CompletedProcess(args, 0, attrs, "")


def test_live_item_is_read_and_written_under_claude_codes_account_only(w, monkeypatch):
    """Real keychain.py over an emulated `security`. Claude Code uses (-a $USER, -s <service>);
    a leftover item with the same service under another account must never be read or written.
    (Replaces a FakeKeychain test that asserted an -a value the scoped real lookup can't return.)"""
    sec = SecurityEmulator()
    monkeypatch.setattr(keychain, "_run", sec)  # first: the real functions below only ever call _run
    for name, fn in REAL_KEYCHAIN.items():
        monkeypatch.setattr(keychain, name, fn)

    decoy = json.dumps({"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B, "decoy")})
    sec.items[("legacy-acct", LIVE)] = decoy  # inserted first: an unscoped lookup would return it
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1"))
    sec.items[("tester", LIVE)] = json.dumps(original)
    for ident, email in ((w.s.ACCOUNT_A, "a@example.com"), (w.s.ACCOUNT_B, "b@example.com")):
        account_id = make_account_id(*ident)
        store.upsert_account(Account(id=account_id, email=email, account_uuid=ident[0], organization_uuid=ident[1]))
        sec.items[("tester", paths.saved_login_service(account_id))] = json.dumps(
            {"claudeAiOauth": w.s.make_login(ident, "saved")}
        )

    switcher.switch_to(w.id_b)

    assert sec.items[("legacy-acct", LIVE)] == decoy
    live = json.loads(sec.items[("tester", LIVE)])
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_B, "saved")
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    live_calls = [(a, i) for a, i in sec.calls if LIVE in a or (i and f'-s "{LIVE}"' in i)]
    assert live_calls, "expected security calls for the live item"
    for args, stdin in live_calls:
        assert (stdin and '-a "tester"' in stdin) or ("-a" in args and args[args.index("-a") + 1] == "tester"), (args, stdin)


# ============================================================================= review round 2

ACCOUNT_C = ("cccccccc-0000-0000-0000-000000000003", "0000000c-0000-0000-0000-00000000000c")
SENTRY_CLEARED = {"serverName": "sentry", "serverUrl": "https://mcp.sentry.dev/mcp", "accessToken": ""}


def _user_signs_out_of_sentry(w, how):
    """`claude mcp remove sentry` deletes the entry; /mcp "Clear authentication" leaves a stub."""
    blob = w.live()
    if how == "mcp_remove":
        del blob["mcpOAuth"][SENTRY]
    else:
        blob["mcpOAuth"][SENTRY] = dict(SENTRY_CLEARED)
    w.set_live(blob)


def _assert_signed_out_of_sentry(w, how):
    mcp = w.live()["mcpOAuth"]
    if how == "mcp_remove":
        assert SENTRY not in mcp
    else:
        assert mcp[SENTRY] == SENTRY_CLEARED
    assert mcp[ATLASSIAN] == w.s.SAMPLE_MCP_OAUTH[ATLASSIAN]


# ----------------------------------------------------------------------------- N1: a sign-out during an add/switch sticks


@pytest.mark.parametrize("how", ["mcp_remove", "clear_auth"])
def test_add_cancelled_keeps_a_sentry_sign_out_made_during_the_login(w, how):
    """Live A with sentry S1. During the browser login the user signs out of sentry, then cancels."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    runner = LoginRunner(w, rc=1, on_run=lambda: _user_signs_out_of_sentry(w, how))

    assert switcher.add_account(runner=runner, status_runner=StatusRunner()) is None
    _assert_signed_out_of_sentry(w, how)


@pytest.mark.parametrize("how", ["mcp_remove", "clear_auth"])
def test_add_success_may_bring_back_a_sign_out_made_during_the_login(w, how):
    """Accepted trade-off (R1): once the login rewrote the item, entries are restored one by one,
    so a sign-out made mid-login comes back. Losing a live sign-in would be worse."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))

    def during_login():
        _user_signs_out_of_sentry(w, how)
        _refresh_live(w, w.s.ACCOUNT_B, "B1")

    switcher.add_account(runner=LoginRunner(w, on_run=during_login, oauth_account=w.oauth_b), status_runner=StatusRunner())

    assert w.live()["mcpOAuth"][SENTRY] == w.s.SAMPLE_MCP_OAUTH[SENTRY]


@pytest.mark.parametrize("wiped", ["missing", "null", "empty"])
def test_add_still_restores_mcp_stores_the_login_wiped(w, wiped):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A))
    w.set_live(original)
    new_blob = {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_B, "B1")}
    if wiped != "missing":
        new_blob["mcpOAuth"] = None if wiped == "null" else {}
        new_blob["mcpOAuthClientConfig"] = None if wiped == "null" else {}

    switcher.add_account(runner=LoginRunner(w, new_blob=new_blob, oauth_account=w.oauth_b))

    live = w.live()
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert live["mcpOAuthClientConfig"] == original["mcpOAuthClientConfig"]


def test_add_cancelled_keeps_a_client_config_re_registered_during_the_login(w):
    """N5, on the cancelled path: an old {clientId, clientSecret} is not restored over a newer
    public-client entry. (On the success path R1's stub rule does restore it: an entry without
    a clientSecret carries nothing Claude Code reads.)"""
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(
        w.live_blob(
            w.s.make_login(w.s.ACCOUNT_A),
            extra={"mcpOAuthClientConfig": {SENTRY: {"clientId": "old-cid", "clientSecret": "old-secret"}}},
        )
    )
    new_config = {SENTRY: {"clientId": "new-public-cid"}}

    def during_login():
        blob = w.live()
        blob["mcpOAuthClientConfig"] = new_config
        w.set_live(blob)

    assert switcher.add_account(runner=LoginRunner(w, rc=1, on_run=during_login)) is None

    assert w.live()["mcpOAuthClientConfig"] == new_config


@pytest.mark.parametrize("how", ["mcp_remove", "clear_auth"])
def test_switch_keeps_a_sentry_sign_out_that_lands_right_after_our_write(w, how):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    w.fake.before_read = _once(lambda s: s == LIVE and LIVE in w.fake.writes, lambda: _user_signs_out_of_sentry(w, how))

    switcher.switch_to(b.id)

    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    _assert_signed_out_of_sentry(w, how)


@pytest.mark.parametrize("wiped", ["missing", "null", "empty"])
def test_switch_still_repairs_a_store_wiped_right_after_our_write(w, wiped):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A))
    w.set_live(original)

    def wipe():
        blob = w.live()
        if wiped == "missing":
            del blob["mcpOAuth"]
        else:
            blob["mcpOAuth"] = None if wiped == "null" else {}
        w.set_live(blob)

    w.fake.before_read = _once(lambda s: s == LIVE and LIVE in w.fake.writes, wipe)

    switcher.switch_to(b.id)

    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert live["mcpOAuth"] == original["mcpOAuth"]


@pytest.mark.parametrize("how", ["mcp_remove", "clear_auth"])
def test_rollback_keeps_a_sentry_sign_out_made_during_the_switch(w, monkeypatch, how):
    b, original = _setup_two(w)

    def sign_out_then_fail(oauth_account):
        _user_signs_out_of_sentry(w, how)
        raise claude_state.ClaudeStateError("boom")

    monkeypatch.setattr(switcher.claude_state, "write_oauth_account", sign_out_then_fail)

    with pytest.raises(switcher.SwitchError, match="previous login is back"):
        switcher.switch_to(b.id)
    assert w.live()["claudeAiOauth"] == original["claudeAiOauth"]
    _assert_signed_out_of_sentry(w, how)


# ----------------------------------------------------------------------------- N3: a new login is saved whatever the exit code


def test_add_saves_a_new_identity_even_when_login_exits_nonzero(w):
    """Live A. `claude auth login` completes for unregistered C but exits 1."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1"))
    w.set_live(original)
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com", "Org C")
    runner = LoginRunner(w, rc=1, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c)

    account = switcher.add_account("third", runner=runner)

    id_c = make_account_id(*ACCOUNT_C)
    assert account is not None and account.id == id_c and account.email == "c@example.com" and account.label == "third"
    assert _rt(w.saved(id_c)) == _tok("C1")
    live = w.live()
    assert live["claudeAiOauth"]["accountUuid"] == ACCOUNT_C[0]
    assert _compact(live["mcpOAuth"]) == _compact(original["mcpOAuth"])  # wiped by the login, restored


def test_add_interrupted_after_a_new_identity_logged_in_still_saves_it(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com")
    runner = LoginRunner(
        w, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c, raise_exc=KeyboardInterrupt()
    )

    with pytest.raises(KeyboardInterrupt):
        switcher.add_account(runner=runner)

    id_c = make_account_id(*ACCOUNT_C)
    assert store.get_account(id_c).email == "c@example.com"
    assert _rt(w.saved(id_c)) == _tok("C1")


def test_add_cancelled_with_no_change_returns_none_and_writes_nothing(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    raw = w.live_raw()
    w.fake.writes.clear()
    assert switcher.add_account(runner=LoginRunner(w, rc=1)) is None
    assert w.live_raw() == raw and w.fake.writes == []
    assert [a.id for a in store.load_accounts()] == [w.id_a]


def test_add_new_login_without_account_id_is_reported_not_hidden(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    nameless = w.s.make_login(ACCOUNT_C, "C1")
    del nameless["accountUuid"]

    with pytest.raises(switcher.SwitchError, match="is now logged in, but that login has no account id") as info:
        switcher.add_account(runner=LoginRunner(w, rc=1, new_blob={"claudeAiOauth": nameless}))
    _no_secrets(info.value)


def _run_cli_add(monkeypatch, capsys, runner) -> tuple[int, str]:
    monkeypatch.setattr(switcher, "_login_runner", runner)
    code = cli.main(["add"])
    return code, capsys.readouterr().out


def test_cli_add_cancel_message_is_true_when_nothing_changed(w, monkeypatch, capsys):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    code, out = _run_cli_add(monkeypatch, capsys, LoginRunner(w, rc=1))
    assert code == 1 and "previous login is unchanged" in out


def test_cli_add_cancel_message_is_true_when_the_login_changed_meanwhile(w, monkeypatch, capsys):
    """The menu bar switched to B during the login, then the user cancelled: not "unchanged"."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    runner = LoginRunner(w, rc=1, on_run=lambda: _refresh_live(w, w.s.ACCOUNT_B, "B1"))

    code, out = _run_cli_add(monkeypatch, capsys, runner)

    assert code == 1
    assert "unchanged" not in out and "b@example.com" in out


def test_cli_add_reports_a_new_identity_from_a_nonzero_login(w, monkeypatch, capsys):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com")
    runner = LoginRunner(w, rc=1, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c)

    code, out = _run_cli_add(monkeypatch, capsys, runner)

    assert code == 0 and "Saved c@example.com" in out


# ----------------------------------------------------------------------------- N4: a mid-switch /login keeps its profile


def test_switch_imports_a_mid_switch_login_with_its_profile(w):
    """The user runs /login C between our read and our write; ~/.claude.json already holds C's profile."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    b = w.register(w.s.ACCOUNT_B, "b@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    w.set_claude_json({"oauthAccount": w.oauth_a})
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com", "Org C")

    def user_logs_in_as_c():
        _refresh_live(w, ACCOUNT_C, "C1")
        w.set_claude_json({"oauthAccount": oauth_c})

    w.fake.after_read = _once(lambda s: s == LIVE, user_logs_in_as_c)

    switcher.switch_to(b.id)

    c = store.get_account(make_account_id(*ACCOUNT_C))
    assert c is not None and c.email == "c@example.com" and c.oauth_account == oauth_c
    assert _rt(w.saved(c.id)) == _tok("C1")
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]


# ============================================================================= review round 3

LINEAR = "linear|0123456789abcdef"
LINEAR_SIGNED_IN = {"serverName": "linear", "accessToken": "fake-linear-AT", "refreshToken": "fake-linear-RT"}


# ----------------------------------------------------------------------------- R1: a live sign-in is never lost on login


@pytest.mark.parametrize("session_writes", ["sentry_refresh", "linear_stub"])
def test_add_keeps_live_sign_ins_when_a_session_writes_during_the_logins_wipe(w, session_writes):
    """before: mcpOAuth {sentry S1, linear {AT, RT}, atlassian}. `claude auth login` wipes the item,
    a running session rewrites it with one MCP entry, then the login writes B on top."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    original = w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1"))
    original["mcpOAuth"][LINEAR] = dict(LINEAR_SIGNED_IN)
    w.set_live(original)

    def login():
        w.fake.delete(LIVE)  # Claude Code's post-login logout
        if session_writes == "sentry_refresh":
            entry = {SENTRY: {"serverName": "sentry", "accessToken": "fake-sentry-S2a", "refreshToken": "fake-sentry-S2"}}
        else:
            entry = {LINEAR: {"serverName": "linear", "accessToken": ""}}
        w.set_live({"mcpOAuth": entry})
        _refresh_live(w, w.s.ACCOUNT_B, "B1")  # the new login lands on top

    switcher.add_account(runner=LoginRunner(w, on_run=login, oauth_account=w.oauth_b), status_runner=StatusRunner())

    live = w.live()
    mcp = live["mcpOAuth"]
    assert live["claudeAiOauth"] == w.s.make_login(w.s.ACCOUNT_B, "B1")
    if session_writes == "sentry_refresh":
        assert mcp[SENTRY]["refreshToken"] == "fake-sentry-S2"  # the newest tokens win
    else:
        assert mcp[SENTRY] == original["mcpOAuth"][SENTRY]
    assert mcp[LINEAR] == LINEAR_SIGNED_IN  # a stub never shadows real tokens
    assert mcp[ATLASSIAN] == original["mcpOAuth"][ATLASSIAN]
    assert live["mcpOAuthClientConfig"] == CLIENT_CONFIG


# ----------------------------------------------------------------------------- R2: a refreshed login kept in memory goes live


def _refresh_but_lose_the_save(w, monkeypatch, ident, old: str, new: str, *, fresh_stash: bool = True) -> dict:
    """The account's saved login ``old`` has expired. tokens refreshes it (the server revokes ``old``
    and issues ``new``), but the Keychain silently drops the write-back: ``new`` exists only in
    ``tokens._UNSAVED``. Returns the switch that makes the Keychain work again."""
    if fresh_stash:
        monkeypatch.setattr(tokens, "_UNSAVED", {})
    account_id = make_account_id(*ident)
    service = paths.saved_login_service(account_id)
    w.fake.put_json(service, {"claudeAiOauth": w.s.make_login(ident, old, expires_at_ms=1)})
    real_write, state = w.fake.write, {"drop": True}

    def write(svc, acct, secret):
        if state["drop"] and svc == service:
            return
        real_write(svc, acct, secret)

    monkeypatch.setattr(keychain, "write", write)

    def post(url, payload, *, headers, timeout):
        return 200, {"access_token": f"sk-ant-oat01-fake-{new}", "refresh_token": _tok(new), "expires_in": 28800}

    with pytest.raises(keychain.KeychainError):
        tokens.access_token_for(store.get_account(account_id), http_post=post)
    assert account_id in tokens._UNSAVED
    return state


def test_switch_to_an_account_whose_refreshed_login_is_only_in_memory_writes_that_login_live(w, monkeypatch):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    state = _refresh_but_lose_the_save(w, monkeypatch, w.s.ACCOUNT_B, "B1", "B2")
    state["drop"] = False  # the Keychain works again

    switcher.switch_to(w.id_b)

    assert _rt(w.live()) == _tok("B2")  # not the revoked B1
    assert _rt(w.saved(w.id_b)) == _tok("B2")
    assert w.id_b not in tokens._UNSAVED


def test_switch_refuses_when_the_targets_kept_login_still_cant_be_saved(w, monkeypatch):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    _refresh_but_lose_the_save(w, monkeypatch, w.s.ACCOUNT_B, "B1", "B2")  # still dropping
    raw = w.live_raw()

    with pytest.raises(switcher.SwitchError, match="nothing was changed") as info:
        switcher.switch_to(w.id_b)

    _no_secrets(info.value)
    assert w.live_raw() == raw  # the revoked B1 was not written live
    assert w.id_b in tokens._UNSAVED


def test_switch_away_keeps_a_kept_login_over_its_revoked_predecessor(w, monkeypatch):
    """A's refresh landed only in memory (A1 revoked, A2 kept). Another process then switched to A
    with the dead A1. Switching away saves A2 and must not overwrite it with the live A1 (I5)."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B, "B1")))
    state = _refresh_but_lose_the_save(w, monkeypatch, w.s.ACCOUNT_A, "A1", "A2")
    state["drop"] = False
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1", expires_at_ms=1)))  # the other process's switch

    switcher.switch_to(w.id_b)

    assert _rt(w.saved(w.id_a)) == _tok("A2")
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_B[0]
    assert w.id_a not in tokens._UNSAVED


# ----------------------------------------------------------------------------- R5: truthful add outcomes


def test_add_interrupted_after_login_honours_the_label_and_says_so(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com")
    runner = LoginRunner(
        w, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c, raise_exc=KeyboardInterrupt()
    )

    with pytest.raises(KeyboardInterrupt) as info:
        switcher.add_account("work", runner=runner)

    assert isinstance(info.value, switcher.LoginInterrupted)
    assert info.value.account.id == make_account_id(*ACCOUNT_C) and info.value.account.label == "work"
    assert store.get_account(make_account_id(*ACCOUNT_C)).label == "work"


def test_add_interrupted_before_any_login_is_a_plain_interrupt(w):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    with pytest.raises(KeyboardInterrupt) as info:
        switcher.add_account(runner=LoginRunner(w, raise_exc=KeyboardInterrupt()))
    assert not isinstance(info.value, switcher.LoginInterrupted)


def test_cli_add_interrupted_after_login_prints_one_true_line_and_exits_130(w, monkeypatch, capsys):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com")
    runner = LoginRunner(
        w, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c, raise_exc=KeyboardInterrupt()
    )
    monkeypatch.setattr(switcher, "_login_runner", runner)

    code = cli.main(["add", "--label", "work"])

    out = capsys.readouterr().out
    assert code == 130
    assert "Login was interrupted, but the new account work (c@example.com) was saved. It is now the active account." in out


def test_cli_add_says_now_using_when_another_registered_account_ended_up_live(w, monkeypatch, capsys):
    """Menu bar auto-switch to B during the login, a session refreshes B, the user cancels."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    runner = LoginRunner(w, rc=1, on_run=lambda: _refresh_live(w, w.s.ACCOUNT_B, "B2"))

    code, out = _run_cli_add(monkeypatch, capsys, runner)

    assert "Claude Code is now using b@example.com." in out
    assert "Saved" not in out


def test_cli_add_of_a_brand_new_account_says_saved(w, monkeypatch, capsys):
    w.register(w.s.ACCOUNT_A, "a@example.com")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A)))
    oauth_c = w.s.make_oauth_account(ACCOUNT_C, "c@example.com")
    runner = LoginRunner(w, new_blob={"claudeAiOauth": w.s.make_login(ACCOUNT_C, "C1")}, oauth_account=oauth_c)

    code, out = _run_cli_add(monkeypatch, capsys, runner)

    assert code == 0 and "Saved c@example.com. It is now the active account." in out


# ============================================================================= review round 4


def _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch):
    """S1 start: this (menu bar) process refreshed B (the server revoked B1 and issued B2) but the
    Keychain dropped the save, so B2 is only in memory. Another process then switched to B with
    the stale saved B1: live = B1 (revoked), saved B = B1."""
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    state = _refresh_but_lose_the_save(w, monkeypatch, w.s.ACCOUNT_B, "B1", "B2")
    state["drop"] = False
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B, "B1", expires_at_ms=1)))


def _then_nothing_clobbers_b2(w, monkeypatch):
    """The next poll's save_active_login, then a switch from another process (nothing in memory)."""
    switcher.save_active_login()
    assert _rt(w.saved(w.id_b)) == _tok("B2")
    monkeypatch.setattr(tokens, "_UNSAVED", {})
    w.fake.put_json(paths.saved_login_service(w.id_a), {"claudeAiOauth": w.s.make_login(w.s.ACCOUNT_A, "A1")})
    w.fake.fail_writes_for.discard(LIVE)
    monkeypatch.setattr(keychain, "write", w.fake.write)
    switcher.switch_to(w.id_a)
    assert _rt(w.saved(w.id_b)) == _tok("B2")
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]


def test_switch_failing_on_a_damaged_target_never_lets_revoked_b1_clobber_b2(w, monkeypatch):
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    w.fake.items[paths.saved_login_service(w.id_a)] = ("tester", "{broken")

    with pytest.raises(switcher.SwitchError, match="Re-add"):
        switcher.switch_to(w.id_a)

    assert _rt(w.live()) == _tok("B2")  # the refreshed login went live first
    assert _rt(w.saved(w.id_b)) == _tok("B2")
    assert w.live()["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH
    _then_nothing_clobbers_b2(w, monkeypatch)


def test_switch_whose_own_live_write_rolls_back_never_lets_revoked_b1_clobber_b2(w, monkeypatch):
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    real_write = keychain.write

    def a_cannot_go_live(service, account, secret):
        if service == LIVE and w.s.ACCOUNT_A[0] in secret:
            raise keychain.KeychainError("simulated live write failure")
        real_write(service, account, secret)

    monkeypatch.setattr(keychain, "write", a_cannot_go_live)

    with pytest.raises(switcher.SwitchError, match="previous login is back"):
        switcher.switch_to(w.id_a)

    assert _rt(w.live()) == _tok("B2")
    assert _rt(w.saved(w.id_b)) == _tok("B2")
    _then_nothing_clobbers_b2(w, monkeypatch)


def test_when_the_refreshed_login_cannot_go_live_it_is_not_saved_either(w, monkeypatch):
    """Every live write fails: B2 is not saved (a later live-to-saved copy of B1 would clobber it);
    it stays in memory, and the next switch that can write puts it live and saved."""
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    w.fake.fail_writes_for.add(LIVE)

    with pytest.raises(switcher.SwitchError):
        switcher.switch_to(w.id_a)

    assert _rt(w.saved(w.id_b)) == _tok("B1") and _rt(w.live()) == _tok("B1")
    assert tokens._UNSAVED[w.id_b][1]["refreshToken"] == _tok("B2")  # not lost
    switcher.save_active_login()
    assert tokens._UNSAVED[w.id_b][1]["refreshToken"] == _tok("B2")

    w.fake.fail_writes_for.discard(LIVE)
    switcher.switch_to(w.id_a)

    assert _rt(w.saved(w.id_b)) == _tok("B2")
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]
    assert w.id_b not in tokens._UNSAVED


def test_switch_to_b_while_b_is_live_with_revoked_b1_puts_b2_live(w, monkeypatch):
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    switcher.switch_to(w.id_b)
    assert _rt(w.live()) == _tok("B2") and _rt(w.saved(w.id_b)) == _tok("B2")
    assert w.live()["mcpOAuth"] == w.s.SAMPLE_MCP_OAUTH
    switcher.save_active_login()
    assert _rt(w.saved(w.id_b)) == _tok("B2")


def test_a_newer_login_in_another_process_still_wins_over_the_kept_one(w, monkeypatch):
    """The user ran /login B (fresh grant B-new) after B2 was kept: B-new stays live and saved."""
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_B, "Bnew")))

    switcher.switch_to(w.id_a)

    assert _rt(w.saved(w.id_b)) == _tok("Bnew")
    assert w.live()["claudeAiOauth"]["accountUuid"] == w.s.ACCOUNT_A[0]


def test_a_login_landing_while_the_kept_one_is_being_put_live_wins(w, monkeypatch):
    _b_live_with_revoked_b1_and_b2_kept(w, monkeypatch)
    live_b_writes = []
    real_write = keychain.write

    def recording(service, account, secret):
        if service == LIVE and w.s.ACCOUNT_B[0] in secret:
            live_b_writes.append(secret)
        real_write(service, account, secret)

    monkeypatch.setattr(keychain, "write", recording)
    w.fake.after_read = _once(lambda s: s == LIVE, lambda: _refresh_live(w, w.s.ACCOUNT_B, "Bnew"))

    switcher.switch_to(w.id_a)

    assert live_b_writes == []  # B2 was never written over the newer login
    assert _rt(w.saved(w.id_b)) == _tok("Bnew")


# ----------------------------------------------------------------------------- S4: only the two accounts a switch touches


def test_switch_saves_only_the_target_and_outgoing_kept_logins(w, monkeypatch):
    w.register(w.s.ACCOUNT_A, "a@example.com", token="A1")
    w.register(w.s.ACCOUNT_B, "b@example.com", token="B1")
    w.register(ACCOUNT_C, "c@example.com", token="C1")
    w.set_live(w.live_blob(w.s.make_login(w.s.ACCOUNT_A, "A1")))
    id_c = make_account_id(*ACCOUNT_C)
    state_c = _refresh_but_lose_the_save(w, monkeypatch, ACCOUNT_C, "C1", "C2")
    state_b = _refresh_but_lose_the_save(w, monkeypatch, w.s.ACCOUNT_B, "B1", "B2", fresh_stash=False)
    state_b["drop"] = state_c["drop"] = False
    reads = []
    real_read = keychain.read
    monkeypatch.setattr(keychain, "read", lambda service: (reads.append(service), real_read(service))[1])

    switcher.switch_to(w.id_b)

    assert _rt(w.live()) == _tok("B2")
    assert id_c in tokens._UNSAVED  # left for the refresh path, which saves it next time
    assert _rt(w.saved(id_c)) == _tok("C1")
    assert paths.saved_login_service(id_c) not in reads  # no Keychain calls for C at all
