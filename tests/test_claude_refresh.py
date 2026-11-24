"""Offline refresh engine tests; shared fake Keychain and network/process guards stay on."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import unicodedata
from pathlib import Path

import pytest

from cc_switcher import creds, fsutil, keychain, paths
from cc_switcher.models import Account, make_account_id


@pytest.fixture
def setup(monkeypatch, fake_keychain, samples):
    from cc_switcher import claude_refresh as engine

    ident = samples.ACCOUNT_A
    account = Account(make_account_id(*ident), "a@example.test", *ident)
    old = samples.make_login(ident, "old", expires_at_ms=1)
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old))
    fake_keychain.put_json(paths.claude_keychain_service(), {
        "claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "live"),
        "mcpOAuth": samples.SAMPLE_MCP_OAUTH,
    })
    monkeypatch.setattr(engine, "_find_claude", lambda: "/fake/claude")
    # All protocol tests use a contained fake child. Production has no verified
    # host-enforced containment and must refuse real CLI launches.
    monkeypatch.setattr(engine, "_require_trusted_cli_opt_in", lambda: None)
    # Never send a signal to a real process group on this Mac in an offline test.
    monkeypatch.setattr(engine, "_kill_group", lambda child: child.kill())
    monkeypatch.setattr(engine, "_group_quiescent", lambda child: True)

    # The production reader scopes isolated credentials to the intended account attribute.
    def security(args, **kwargs):
        service = args[args.index("-s") + 1]
        item = fake_keychain.items.get(service)
        attr = args[args.index("-a") + 1] if "-a" in args else None
        if item is None or (attr is not None and item[0] != attr):
            return subprocess.CompletedProcess(args, 44, "", "not found")
        return subprocess.CompletedProcess(args, 0, item[1] if "-w" in args else f'"acct"<blob>="{item[0]}"', "")

    monkeypatch.setattr(keychain, "_run", security)
    return engine, account, old


class Child:
    pid = 98765

    def __init__(self, on_wait=None, code=0, timeout=False, unreapable=False):
        self.on_wait = on_wait
        self.code = code
        self.timeout = timeout
        self.unreapable = unreapable
        self.killed = False
        self.returncode = None

    def wait(self, timeout):
        if self.on_wait is not None:
            callback, self.on_wait = self.on_wait, None
            callback()
        if (self.timeout and not self.killed) or self.unreapable:
            raise subprocess.TimeoutExpired("secret-bearing-output", timeout, output="NEVER SHOW")
        self.returncode = -9 if self.killed else self.code
        return self.returncode

    def kill(self):
        self.killed = True


def spawn(monkeypatch, callback=None, **options):
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return Child((lambda: callback(args, kwargs)) if callback else None, **options)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def isolated_service(kwargs):
    value = unicodedata.normalize("NFC", kwargs["env"]["CLAUDE_CONFIG_DIR"])
    return "Claude Code-credentials-" + hashlib.sha256(value.encode()).hexdigest()[:8]


def journal():
    return paths.app_dir() / "claude-refresh-operation.json"


def test_refresh_saves_rotation_without_touching_live_or_mcp(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "rotated")
    live_before = copy.deepcopy(fake_keychain.items[paths.claude_keychain_service()])

    def rotate(args, kw):
        assert json.loads(journal().read_text())["stage"] == "launching"
        assert fake_keychain.get_json(isolated_service(kw)) == {"claudeAiOauth": old}
        assert Path(kw["cwd"]).is_relative_to(paths.app_dir())
        assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
        assert kw["umask"] == 0o077
        fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated))

    calls = spawn(monkeypatch, rotate)
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated
        engine.guard_locked()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": rotated}
    assert fake_keychain.items[paths.claude_keychain_service()] == live_before
    assert len(calls) == 1
    assert not journal().exists()


def test_child_environment_cannot_override_grant(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_USE_BEDROCK", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "NODE_OPTIONS", "PYTHONPATH", "CLAUDE_CODE_DEBUG_LOGS_DIR"):
        monkeypatch.setenv(key, "poison")
    fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(samples.make_login(samples.ACCOUNT_B)))
    calls = spawn(monkeypatch)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    args, kw = calls[0]
    assert kw["env"]["CLAUDE_CONFIG_DIR"] == kw["env"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
    assert "poison" not in kw["env"].values()
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in args
    for flag, value in (("--tools", ""), ("--permission-mode", "dontAsk"), ("--permission-prompts", "none")):
        assert args[args.index(flag) + 1] == value
    assert old["refreshToken"] not in str(args)
    assert old["accessToken"] not in str(args)


@pytest.mark.parametrize("code,timeout", [(1, False), (0, True)])
def test_failure_reconciles_rotation_and_blocks_retry_but_not_relogin(setup, monkeypatch, fake_keychain, samples, code, timeout):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "error-rotation")
    calls = spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)), code=code, timeout=timeout)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": rotated}
    assert not journal().exists()
    with fsutil.app_lock():
        engine.guard_locked()
        with pytest.raises(engine.RetryBlocked):
            engine.refresh_locked(account, rotated)
        nonce = engine.begin_login_locked()
        engine.end_login_locked(nonce)
    assert len(calls) == 1
    assert "sk-ant-" not in (paths.app_dir() / "claude-refresh-retry-blocks.json").read_text()


def test_unchanged_failed_grant_cannot_retry_after_three_minutes(setup, monkeypatch):
    engine, account, old = setup
    calls = spawn(monkeypatch, code=1)
    with fsutil.app_lock():
        with pytest.raises(engine.RefreshFailed):
            engine.refresh_locked(account, old)
        engine.guard_locked()  # no global block: switching and explicit login remain possible
        with pytest.raises(engine.RetryBlocked):
            engine.guard_retry_locked(account, old)
        with pytest.raises(engine.RetryBlocked):
            engine.refresh_locked(account, old)
    assert len(calls) == 1  # unknown 429 Retry-After might be 3600s, never assume 180s suffices


def test_relogin_changed_refresh_grant_removes_retry_block(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    spawn(monkeypatch, code=1)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    new_login = samples.make_login(samples.ACCOUNT_A, "relogin")
    fake_keychain.put_json(account.keychain_service, creds.saved_item(new_login))
    with fsutil.app_lock():
        engine.guard_retry_locked(account, new_login)
        engine.guard_locked()


def test_changed_fallback_beats_unchanged_keychain(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "file-rotation")
    spawn(monkeypatch, lambda args, kw: fsutil.atomic_write_json(Path(kw["env"]["CLAUDE_CONFIG_DIR"]) / ".credentials.json", creds.saved_item(rotated)))
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": rotated}


def test_conflicting_successors_preserve_both_and_block_globally(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    first = samples.make_login(samples.ACCOUNT_A, "first")
    second = samples.make_login(samples.ACCOUNT_A, "second")
    created = []

    def rotate(args, kw):
        service = isolated_service(kw)
        directory = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
        fake_keychain.put_json(service, creds.saved_item(first))
        fsutil.atomic_write_json(directory / ".credentials.json", creds.saved_item(second))
        created.append((service, directory))

    spawn(monkeypatch, rotate)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    service, directory = created[0]
    assert fake_keychain.get_json(service) == {"claudeAiOauth": first}
    assert json.loads((directory / ".credentials.json").read_text()) == {"claudeAiOauth": second}
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.guard_locked()
    assert "sk-ant-" not in journal().read_text()


@pytest.mark.parametrize("error", [OSError("token-must-not-leak"), KeyboardInterrupt()])
def test_ambiguous_launch_or_parent_crash_preserves_marker(setup, monkeypatch, error):
    engine, account, old = setup

    def launch(*args, **kw):
        raise error

    monkeypatch.setattr(subprocess, "Popen", launch)
    expected = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else engine.RecoveryRequired
    with fsutil.app_lock(), pytest.raises(expected):
        engine.refresh_locked(account, old)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.guard_locked()
    assert journal().exists()


def test_unreaped_child_preserves_marker_and_never_promotes(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "surviving")
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)), timeout=True, unreapable=True)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_failed_saved_write_retains_only_successor(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    fake_keychain.fail_writes_for.add(account.keychain_service)
    rotated = samples.make_login(samples.ACCOUNT_A, "only-copy")
    isolated = []

    def rotate(args, kw):
        isolated.append(isolated_service(kw))
        fake_keychain.put_json(isolated[-1], creds.saved_item(rotated))

    spawn(monkeypatch, rotate)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert fake_keychain.get_json(isolated[0]) == {"claudeAiOauth": rotated}
    assert journal().exists()


@pytest.mark.parametrize("case", ["wrong-identity", "stale-saved", "shared-live-grant", "live-unreadable"])
def test_invalid_input_never_spawns_or_overwrites(setup, monkeypatch, fake_keychain, samples, case):
    engine, account, old = setup
    passed = copy.deepcopy(old)
    if case == "wrong-identity":
        passed["accountUuid"] = samples.ACCOUNT_B[0]
    elif case == "stale-saved":
        fake_keychain.put_json(account.keychain_service, creds.saved_item(samples.make_login(samples.ACCOUNT_A, "newer")))
    elif case == "shared-live-grant":
        live = samples.make_login(samples.ACCOUNT_B)
        live["refreshToken"] = old["refreshToken"]
        fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(live))
    else:
        fake_keychain.items[paths.claude_keychain_service()] = ("tester", "not JSON")
    before = copy.deepcopy(fake_keychain.items)
    calls = spawn(monkeypatch)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, passed)
    assert not calls
    assert fake_keychain.items == before


def test_foreign_child_login_is_preserved_not_saved(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(samples.make_login(samples.ACCOUNT_B))))
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_guard_rejects_empty_or_malformed_journal(setup):
    engine, account, old = setup
    fsutil.ensure_private_dir(paths.app_dir())
    journal().write_text("")
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.guard_locked()
    assert journal().exists()


def test_keychain_seed_failure_uses_secure_private_fallback(setup, monkeypatch, samples):
    engine, account, old = setup
    original = keychain.write

    def fail_isolated(service, attr, secret):
        if service.startswith("Claude Code-credentials-"):
            raise keychain.KeychainError("secret-must-not-leak")
        original(service, attr, secret)

    monkeypatch.setattr(keychain, "write", fail_isolated)
    rotated = samples.make_login(samples.ACCOUNT_A, "fallback-seed")

    def rotate(args, kw):
        directory = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
        file = directory / ".credentials.json"
        assert json.loads(file.read_text()) == {"claudeAiOauth": old}
        assert directory.stat().st_mode & 0o777 == 0o700
        assert file.stat().st_mode & 0o777 == 0o600
        fsutil.atomic_write_json(file, creds.saved_item(rotated))

    spawn(monkeypatch, rotate)
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated


def test_oversized_seed_cannot_leak_to_security_argv(setup, monkeypatch, fake_keychain):
    engine, account, old = setup
    old["refreshToken"] = "R" * 5000
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old))
    writes = []
    monkeypatch.setattr(keychain, "write", lambda *args: writes.append(args))
    calls = spawn(monkeypatch)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert not calls
    assert not writes


def test_saved_readback_lie_keeps_global_recovery(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "readback")
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)))
    original = keychain.write

    def lie(service, attr, secret):
        if service != account.keychain_service:
            original(service, attr, secret)

    monkeypatch.setattr(keychain, "write", lie)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()


def test_login_marker_is_owned_durable_and_blocks_refresh(setup, monkeypatch):
    engine, account, old = setup
    calls = spawn(monkeypatch)
    with fsutil.app_lock():
        nonce = engine.begin_login_locked()
        engine.guard_locked()  # owner can finish interactive login
        with pytest.raises(engine.RecoveryRequired):
            engine.guard_no_login_locked()
        with pytest.raises(engine.RecoveryRequired):
            engine.begin_login_locked()
        with pytest.raises(engine.RecoveryRequired):
            engine.end_login_locked("not-the-owner")
        with pytest.raises(engine.RecoveryRequired):
            engine.refresh_locked(account, old)
        engine.end_login_locked(nonce)
        engine.guard_no_login_locked()
    assert not calls


def test_refresh_ambiguity_prevents_beginning_interactive_login(setup):
    engine, account, old = setup
    fsutil.ensure_private_dir(paths.app_dir())
    journal().write_text("{}")
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.begin_login_locked()


def test_last_descendant_write_is_read_after_quiescence(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "last-descendant-write")
    created = []

    def wait_for_exit(child):
        # Model the final descendant write just before its group disappears.
        fake_keychain.put_json(created[0], creds.saved_item(rotated))
        return True

    monkeypatch.setattr(engine, "_group_quiescent", wait_for_exit)
    spawn(monkeypatch, lambda args, kw: created.append(isolated_service(kw)))
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": rotated}


def test_surviving_process_group_prevents_success_promotion(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "descendant")
    monkeypatch.setattr(engine, "_group_quiescent", lambda child: False, raising=False)
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)))
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_timeout_group_kill_failure_keeps_global_guard(setup, monkeypatch):
    engine, account, old = setup

    def fail_kill(child):
        raise PermissionError("group ownership uncertain")

    monkeypatch.setattr(engine, "_kill_group", fail_kill, raising=False)
    spawn(monkeypatch, timeout=True)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()


def test_real_cli_without_proven_containment_cannot_launch(setup, monkeypatch, fake_keychain, samples):
    import importlib
    engine, account, old = setup
    engine = importlib.reload(engine)  # real production containment prerequisite, not fixture override
    monkeypatch.setattr(engine, "_find_claude", lambda: "/fake/claude")
    monkeypatch.setattr(engine, "_group_quiescent", lambda child: True)
    monkeypatch.setattr(engine, "_kill_group", lambda child: child.kill())
    rotated = samples.make_login(samples.ACCOUNT_A, "would-rotate")
    calls = spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)))
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert not calls
    assert not journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_staged_credentials_after_crash_keep_recovery_journal(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "staged-successor")
    created = []

    def stage(args, kw):
        file = Path(kw["cwd"]) / ".credentials.json.tmp.1234abcd"
        fsutil.atomic_write_json(file, creds.saved_item(rotated))
        created.append(file)

    spawn(monkeypatch, stage, code=-9)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == creds.saved_item(old)
    assert json.loads(created[0].read_text()) == creds.saved_item(rotated)


def test_symlinked_operation_directory_never_modifies_outside_fallback(setup, monkeypatch, samples, tmp_path):
    engine, account, old = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    file = outside / ".credentials.json"
    file.write_text(json.dumps(creds.saved_item(samples.make_login(samples.ACCOUNT_B))))
    file.chmod(0o644)
    before = file.read_bytes()
    link = tmp_path / "operation-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(engine.RecoveryRequired):
        engine._inventory(account, link, "fake-isolated", "tester")
    assert file.stat().st_mode & 0o777 == 0o644
    assert file.read_bytes() == before


def test_explicit_cli_opt_in_enables_the_trusted_experiment(monkeypatch):
    import importlib
    from cc_switcher import cli, claude_refresh, store

    assert cli.main(["settings", "claude_refresh_enabled", "true"]) == 0
    assert store.load_settings().claude_refresh_enabled is True
    engine = importlib.reload(claude_refresh)
    engine._require_trusted_cli_opt_in()


def test_disabled_refresh_does_not_reserve_scheduler_slot(fake_keychain, samples):
    from cc_switcher import claude_refresh, tokens

    account = Account(make_account_id(*samples.ACCOUNT_A), "a@example.test", *samples.ACCOUNT_A)
    fake_keychain.put_json(account.keychain_service, creds.saved_item(samples.make_login(samples.ACCOUNT_A, "expired", expires_at_ms=1)))
    fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(samples.make_login(samples.ACCOUNT_B, "live")))
    with pytest.raises(claude_refresh.RefreshFailed):
        tokens.access_token_for(account)
    assert not paths.refresh_schedule_file().exists()


def test_missing_access_token_can_be_renewed_by_cli(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    old.pop("accessToken")
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old))
    rotated = samples.make_login(samples.ACCOUNT_A, "renewed")
    calls = spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated)))
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated
    assert len(calls) == 1
    assert fake_keychain.get_json(account.keychain_service) == creds.saved_item(rotated)


@pytest.mark.parametrize("access", [None, "", "absent"])
def test_future_expiry_without_access_token_is_not_success(setup, monkeypatch, fake_keychain, samples, access):
    engine, account, old = setup
    changed = samples.make_login(samples.ACCOUNT_A, "changed-without-access")
    if access == "absent":
        changed.pop("accessToken")
    else:
        changed["accessToken"] = access
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(changed)))
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert fake_keychain.get_json(account.keychain_service) == creds.saved_item(changed)
    with fsutil.app_lock(), pytest.raises(engine.RetryBlocked):
        engine.guard_retry_locked(account, changed)


def test_live_guard_reads_secure_storage_override(setup, monkeypatch, fake_keychain, tmp_path):
    engine, account, old = setup
    settings_dir = tmp_path / "settings"
    secure_dir = tmp_path / "secure"
    secure_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(settings_dir))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure_dir))
    fsutil.atomic_write_json(secure_dir / ".credentials.json", creds.saved_item(old))
    with pytest.raises(engine.RefreshFailed, match="may be live"):
        engine._assert_distinct_live(account, old)


def test_distinct_grants_without_access_tokens_are_not_treated_as_shared(setup, fake_keychain, samples):
    engine, account, old = setup
    old.pop("accessToken")
    live = samples.make_login(samples.ACCOUNT_B, "distinct")
    live.pop("accessToken")
    fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(live))
    engine._assert_distinct_live(account, old)


def test_missing_refresh_token_is_provably_needs_login(setup, monkeypatch, fake_keychain):
    engine, account, old = setup
    old.pop("refreshToken")
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old))
    calls = spawn(monkeypatch)
    with fsutil.app_lock(), pytest.raises(engine.NeedsLogin):
        engine.refresh_locked(account, old)
    assert not calls


def test_keychain_hash_collision_cannot_overwrite_existing_item(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    collision = "Claude Code-credentials-12345678"
    distinct = "Claude Code-credentials-87654321"
    foreign = creds.saved_item(samples.make_login(samples.ACCOUNT_B, "collision"))
    fake_keychain.put_json(collision, foreign)
    values = iter((collision, distinct))
    monkeypatch.setattr(engine, "_isolated_service", lambda directory: next(values))
    calls = spawn(monkeypatch)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert len(calls) == 1
    assert fake_keychain.get_json(collision) == foreign
    assert collision not in fake_keychain.writes


def test_same_successor_in_both_stores_is_not_ambiguous(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "both")

    def rotate(args, kw):
        fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated))
        fsutil.atomic_write_json(Path(kw["env"]["CLAUDE_CONFIG_DIR"]) / ".credentials.json", creds.saved_item(rotated))

    spawn(monkeypatch, rotate)
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == rotated


def test_malformed_fallback_does_not_discard_keychain_successor(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "valid-keychain")
    created = []

    def rotate(args, kw):
        directory = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
        fake_keychain.put_json(isolated_service(kw), creds.saved_item(rotated))
        fsutil.atomic_write_text(directory / ".credentials.json", "{truncated")
        created.append((isolated_service(kw), directory))

    spawn(monkeypatch, rotate, code=1)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    service, directory = created[0]
    assert fake_keychain.get_json(service) == {"claudeAiOauth": rotated}
    assert (directory / ".credentials.json").read_text() == "{truncated"
    assert journal().exists()


def test_unreadable_keychain_and_changed_file_cannot_be_guessed(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "file-copy")
    original_read = keychain.read
    created = []

    def rotate(args, kw):
        directory = Path(kw["env"]["CLAUDE_CONFIG_DIR"])
        service = isolated_service(kw)
        fsutil.atomic_write_json(directory / ".credentials.json", creds.saved_item(rotated))
        created.append(directory)

        def locked(current):
            if current == service:
                raise keychain.KeychainError("NEVER SHOW THIS SECRET")
            return original_read(current)

        monkeypatch.setattr(keychain, "read", locked)

    spawn(monkeypatch, rotate)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired) as caught:
        engine.refresh_locked(account, old)
    assert "SECRET" not in str(caught.value)
    assert json.loads((created[0] / ".credentials.json").read_text()) == {"claudeAiOauth": rotated}
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_fallback_symlink_never_reads_or_modifies_target(setup, monkeypatch, samples, tmp_path):
    engine, account, old = setup
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(creds.saved_item(samples.make_login(samples.ACCOUNT_B))))
    target.chmod(0o644)
    before = target.read_bytes()

    def link(args, kw):
        (Path(kw["env"]["CLAUDE_CONFIG_DIR"]) / ".credentials.json").symlink_to(target)

    spawn(monkeypatch, link)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert target.read_bytes() == before
    assert target.stat().st_mode & 0o777 == 0o644


def test_retry_gate_survives_module_reload_and_metadata_changes(setup, monkeypatch, fake_keychain):
    import importlib
    engine, account, old = setup
    spawn(monkeypatch, code=1)
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    old["expiresAt"] = 9999999999999
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old))
    engine = importlib.reload(engine)
    with fsutil.app_lock(), pytest.raises(engine.RetryBlocked):
        engine.guard_retry_locked(account, old)


def test_relogin_one_account_preserves_other_accounts_retry_block(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    other = Account(make_account_id(*samples.ACCOUNT_B), "b@example.test", *samples.ACCOUNT_B)
    other_login = samples.make_login(samples.ACCOUNT_B, "other")
    fake_keychain.put_json(other.keychain_service, creds.saved_item(other_login))
    spawn(monkeypatch, code=1)
    with fsutil.app_lock():
        with pytest.raises(engine.RefreshFailed):
            engine.refresh_locked(account, old)
        engine._block_retry(other, other_login)
        changed = samples.make_login(samples.ACCOUNT_A, "explicit-relogin")
        fake_keychain.put_json(account.keychain_service, creds.saved_item(changed))
        engine.guard_retry_locked(account, changed)
        with pytest.raises(engine.RetryBlocked):
            engine.guard_retry_locked(other, other_login)


def test_failed_child_keychain_rotation_is_observed_after_keyboard_interrupt(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    rotated = samples.make_login(samples.ACCOUNT_A, "interrupt")
    created = []

    def interrupt(args, kw):
        service = isolated_service(kw)
        fake_keychain.put_json(service, creds.saved_item(rotated))
        created.append(service)
        raise KeyboardInterrupt

    spawn(monkeypatch, interrupt)
    with fsutil.app_lock(), pytest.raises(KeyboardInterrupt):
        engine.refresh_locked(account, old)
    assert fake_keychain.get_json(created[0]) == {"claudeAiOauth": rotated}
    assert journal().exists()
    assert fake_keychain.get_json(account.keychain_service) == {"claudeAiOauth": old}


def test_oversized_successor_stays_in_isolation_not_ccs_argv(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    huge = samples.make_login(samples.ACCOUNT_A, "huge")
    huge["refreshToken"] = "X" * 5000
    created = []
    original = keychain.write

    def safe_write(service, attr, secret):
        assert "X" * 5000 not in secret
        original(service, attr, secret)

    monkeypatch.setattr(keychain, "write", safe_write)

    def rotate(args, kw):
        created.append(isolated_service(kw))
        fake_keychain.put_json(created[0], creds.saved_item(huge))

    spawn(monkeypatch, rotate)
    with fsutil.app_lock(), pytest.raises(engine.RecoveryRequired):
        engine.refresh_locked(account, old)
    assert fake_keychain.get_json(created[0]) == {"claudeAiOauth": huge}
    assert journal().exists()


def test_private_json_fifo_is_rejected_without_waiting_for_a_writer(setup, monkeypatch, tmp_path):
    import os

    engine, _, _ = setup
    fifo = tmp_path / "credentials.fifo"
    os.mkfifo(fifo, 0o600)
    real_open = os.open

    def no_blocking_fifo(path, flags, *args, **kwargs):
        if Path(path) == fifo:
            assert flags & os.O_NONBLOCK, "opening this FIFO would hang with the global credential lock held"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_blocking_fifo)
    with pytest.raises(engine.RecoveryRequired):
        engine._read_private_json(fifo)


def test_rotation_preserves_the_saved_keychain_account_attribute(setup, monkeypatch, fake_keychain, samples):
    engine, account, old = setup
    fake_keychain.put_json(account.keychain_service, creds.saved_item(old), account="previous-user")
    successor = samples.make_login(samples.ACCOUNT_A, "rotated")
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(successor)))
    with fsutil.app_lock():
        assert engine.refresh_locked(account, old) == successor
    assert fake_keychain.read_account_attr(account.keychain_service) == "previous-user"


@pytest.mark.parametrize("access", [" ", "token\r\nInjected: yes", "non-ascii-é"])
def test_cli_success_with_an_unusable_bearer_is_not_success(setup, monkeypatch, fake_keychain, samples, access):
    engine, account, old = setup
    successor = dict(samples.make_login(samples.ACCOUNT_A, "rotated"), accessToken=access)
    spawn(monkeypatch, lambda args, kw: fake_keychain.put_json(isolated_service(kw), creds.saved_item(successor)))
    with fsutil.app_lock(), pytest.raises(engine.RefreshFailed):
        engine.refresh_locked(account, old)
    assert not journal().exists()
