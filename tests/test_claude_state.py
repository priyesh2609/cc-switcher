"""~/.claude.json read-modify-write: only oauthAccount changes, nothing else is lost."""

from __future__ import annotations

import json
import os
import stat

import pytest

from cc_switcher import claude_state, paths


def _write(data, *, mode: int = 0o600, newline: bool = False) -> None:
    path = paths.claude_json()
    text = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if newline else "")
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def _read() -> dict:
    return json.loads(paths.claude_json().read_text(encoding="utf-8"))


def _leftover_temp_files() -> list[str]:
    return [p.name for p in paths.claude_json().parent.iterdir() if p.name.endswith(".ccs-tmp")]


@pytest.fixture
def existing(samples):
    data = {
        "numStartups": 42,
        "oauthAccount": samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com", "Org A"),
        "mcpServers": {"sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"}},
        "projects": {"/Users/x/proj": {"allowedTools": [], "history": ["ünïcödé"]}},
        "userID": "abc",
    }
    _write(data, mode=0o644, newline=True)
    return data


# ----------------------------------------------------------------------------- reading


def test_read_missing_or_invalid_is_empty():
    assert claude_state.read_claude_json() == {}
    assert claude_state.read_oauth_account() is None
    paths.claude_json().write_text("{oops")
    assert claude_state.read_claude_json() == {}
    paths.claude_json().write_text("[1, 2]")
    assert claude_state.read_claude_json() == {}


def test_read_oauth_account(existing):
    assert claude_state.read_oauth_account() == existing["oauthAccount"]
    _write({"oauthAccount": "not-an-object"})
    assert claude_state.read_oauth_account() is None


def test_honours_claude_config_dir(monkeypatch, tmp_path, samples):
    config_dir = tmp_path / "alt-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    oauth_b = samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com")
    claude_state.write_oauth_account(oauth_b)
    assert json.loads((config_dir / ".claude.json").read_text()) == {"oauthAccount": oauth_b}


# ----------------------------------------------------------------------------- writing


def test_write_replaces_only_oauth_account(existing, samples):
    oauth_b = samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com", "Org B")

    claude_state.write_oauth_account(oauth_b)

    data = _read()
    assert data["oauthAccount"] == oauth_b
    assert {k: v for k, v in data.items() if k != "oauthAccount"} == {
        k: v for k, v in existing.items() if k != "oauthAccount"
    }
    assert list(data) == list(existing)  # key order, oauthAccount keeps its position
    text = paths.claude_json().read_text(encoding="utf-8")
    assert text.endswith("}\n")  # trailing newline preserved
    assert "ünïcödé" in text  # not \u-escaped
    assert stat.S_IMODE(paths.claude_json().stat().st_mode) == 0o644  # mode preserved
    assert _leftover_temp_files() == []


def test_write_appends_key_when_absent_and_keeps_no_newline(samples):
    _write({"numStartups": 1, "userID": "abc"}, newline=False)
    oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com")
    claude_state.write_oauth_account(oauth_a)
    assert list(_read()) == ["numStartups", "userID", "oauthAccount"]
    assert not paths.claude_json().read_text().endswith("\n")


def test_write_none_deletes_the_key(existing):
    claude_state.write_oauth_account(None)
    data = _read()
    assert "oauthAccount" not in data
    assert data["projects"] == existing["projects"]


def test_write_none_on_missing_file_creates_nothing():
    claude_state.write_oauth_account(None)
    assert not paths.claude_json().exists()


def test_write_creates_missing_file_private(samples):
    oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com")
    claude_state.write_oauth_account(oauth_a)
    assert _read() == {"oauthAccount": oauth_a}
    assert stat.S_IMODE(paths.claude_json().stat().st_mode) == 0o600


def test_write_same_value_does_not_rewrite(existing):
    before = paths.claude_json().stat()
    claude_state.write_oauth_account(existing["oauthAccount"])
    after = paths.claude_json().stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_write_refuses_to_overwrite_invalid_json(monkeypatch, samples):
    monkeypatch.setattr(claude_state, "_RETRY_SLEEP_SECONDS", 0)
    paths.claude_json().write_text('{"numStartups": 1, "trunc')
    with pytest.raises(claude_state.ClaudeStateError, match="not valid JSON"):
        claude_state.write_oauth_account(samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com"))
    assert paths.claude_json().read_text() == '{"numStartups": 1, "trunc'


def test_write_refuses_non_object_json(samples):
    paths.claude_json().write_text("[]")
    with pytest.raises(claude_state.ClaudeStateError):
        claude_state.write_oauth_account(samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com"))
    assert paths.claude_json().read_text() == "[]"


def test_write_follows_symlink(tmp_path, samples):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"keep": True}))
    paths.claude_json().symlink_to(real)

    oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com")
    claude_state.write_oauth_account(oauth_a)

    assert paths.claude_json().is_symlink()
    assert json.loads(real.read_text()) == {"keep": True, "oauthAccount": oauth_a}


# ----------------------------------------------------------------------------- concurrency


def test_concurrent_change_is_retried_and_preserved(monkeypatch, existing, samples):
    """Claude Code rewrites the file between our read and our replace: start over, keep its change."""
    real_write_temp = claude_state._write_temp
    calls = {"n": 0}

    def write_temp_then_claude_code_writes(target, text, mode):
        tmp = real_write_temp(target, text, mode)
        calls["n"] += 1
        if calls["n"] == 1:
            changed = dict(existing)
            changed["numStartups"] = 43
            changed["newKeyFromClaudeCode"] = {"x": 1}
            paths.claude_json().write_text(json.dumps(changed, indent=2) + "\n")
        return tmp

    monkeypatch.setattr(claude_state, "_write_temp", write_temp_then_claude_code_writes)
    oauth_b = samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com")

    claude_state.write_oauth_account(oauth_b)

    data = _read()
    assert calls["n"] == 2
    assert data["oauthAccount"] == oauth_b
    assert data["numStartups"] == 43  # Claude Code's write survived
    assert data["newKeyFromClaudeCode"] == {"x": 1}
    assert _leftover_temp_files() == []


def test_gives_up_after_max_attempts_without_clobbering(monkeypatch, existing, samples):
    monkeypatch.setattr(claude_state, "_RETRY_SLEEP_SECONDS", 0)
    real_write_temp = claude_state._write_temp
    counter = {"n": 0}

    def always_changes(target, text, mode):
        tmp = real_write_temp(target, text, mode)
        counter["n"] += 1
        changed = dict(existing)
        changed["numStartups"] = 1000 + counter["n"]
        paths.claude_json().write_text(json.dumps(changed, indent=2))
        return tmp

    monkeypatch.setattr(claude_state, "_write_temp", always_changes)

    with pytest.raises(claude_state.ClaudeStateError, match="kept changing"):
        claude_state.write_oauth_account(samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com"))

    assert counter["n"] == claude_state.MAX_ATTEMPTS
    data = _read()
    assert data["oauthAccount"] == existing["oauthAccount"]  # never replaced
    assert data["numStartups"] == 1000 + claude_state.MAX_ATTEMPTS
    assert _leftover_temp_files() == []


def test_file_created_concurrently_is_not_clobbered(monkeypatch, samples):
    real_write_temp = claude_state._write_temp
    state = {"first": True}

    def someone_creates_it(target, text, mode):
        tmp = real_write_temp(target, text, mode)
        if state.pop("first", False):
            paths.claude_json().write_text(json.dumps({"createdBy": "claude"}))
        return tmp

    monkeypatch.setattr(claude_state, "_write_temp", someone_creates_it)
    oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com")

    claude_state.write_oauth_account(oauth_a)

    assert _read() == {"createdBy": "claude", "oauthAccount": oauth_a}
    assert _leftover_temp_files() == []


def test_os_errors_become_claude_state_error(monkeypatch, existing, samples):
    def fail(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(claude_state, "_write_temp", fail)
    with pytest.raises(claude_state.ClaudeStateError, match="Permission denied"):
        claude_state.write_oauth_account(samples.make_oauth_account(samples.ACCOUNT_B, "b@example.com"))


# ----------------------------------------------------------------------------- lone surrogates


def test_write_with_a_lone_surrogate_falls_back_to_ascii_losslessly(samples):
    # JSON.stringify writes a split emoji as "\ud83d"; Python reads it as a lone surrogate.
    paths.claude_json().write_text('{"projects": {"/p": {"lastPrompt": "hi \\ud83d"}}, "n": "café"}\n')
    oauth_a = samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com")

    claude_state.write_oauth_account(oauth_a)

    text = paths.claude_json().read_text(encoding="utf-8")
    assert "\\ud83d" in text
    data = json.loads(text)
    assert data == {"projects": {"/p": {"lastPrompt": "hi \ud83d"}}, "n": "café", "oauthAccount": oauth_a}
    assert _leftover_temp_files() == []


def test_create_with_a_lone_surrogate_in_oauth_account(samples):
    oauth_a = dict(samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com"), displayName="x \udc00")
    claude_state.write_oauth_account(oauth_a)
    assert _read() == {"oauthAccount": oauth_a}


def test_undecodable_file_is_left_alone(monkeypatch, samples):
    monkeypatch.setattr(claude_state, "_RETRY_SLEEP_SECONDS", 0)
    paths.claude_json().write_bytes(b'{"n": "\xff\xfe"}')
    with pytest.raises(claude_state.ClaudeStateError):
        claude_state.write_oauth_account(samples.make_oauth_account(samples.ACCOUNT_A, "a@example.com"))
    assert paths.claude_json().read_bytes() == b'{"n": "\xff\xfe"}'
