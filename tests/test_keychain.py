"""keychain.py command construction. No real `security` call is ever made.

The autouse ``fake_keychain`` fixture replaces ``keychain.read/write/...`` for every
test, so the real implementations are captured here at import time (before any
fixture runs) and exercised against a recorded ``keychain._run``.
"""

from __future__ import annotations

import subprocess

import pytest

from cc_switcher import keychain

REAL_READ = keychain.read
REAL_READ_ACCOUNT_ATTR = keychain.read_account_attr
REAL_WRITE = keychain.write
REAL_DELETE = keychain.delete
REAL_RUN = keychain._run

SERVICE = "Claude Code-credentials"


class Recorder:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def run(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(keychain, "_run", recorder)
    return recorder


def _command_length(secret: str, account: str = "tester", service: str = SERVICE) -> int:
    hex_payload = secret.encode("utf-8").hex()
    return len(f'add-generic-password -U -a "{account}" -s "{service}" -X "{hex_payload}"\n')


# ----------------------------------------------------------------------------- write


def test_write_goes_through_stdin_hex_encoded(run):
    secret = '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-secret"},"note":"ü"}'
    REAL_WRITE(SERVICE, "tester", secret)

    [(args, stdin)] = run.calls
    assert args == ["-i"]
    hex_payload = secret.encode("utf-8").hex()
    assert stdin == f'add-generic-password -U -a "tester" -s "{SERVICE}" -X "{hex_payload}"\n'
    assert bytes.fromhex(hex_payload).decode("utf-8") == secret
    assert "sk-ant" not in stdin  # only hex on the wire


def test_write_at_the_limit_still_uses_stdin(run):
    # Each payload byte is 2 hex chars, so pick an account name that makes the limit reachable exactly.
    account = "tester" if (keychain.STDIN_COMMAND_LIMIT - _command_length("")) % 2 == 0 else "tester1"
    base = _command_length("", account=account)
    secret = "x" * ((keychain.STDIN_COMMAND_LIMIT - base) // 2)
    assert _command_length(secret, account=account) == keychain.STDIN_COMMAND_LIMIT
    REAL_WRITE(SERVICE, account, secret)
    assert run.calls[0][0] == ["-i"]


def test_write_over_the_limit_falls_back_to_argv(run):
    base = _command_length("")
    secret = "x" * ((keychain.STDIN_COMMAND_LIMIT - base) // 2 + 1)
    assert _command_length(secret) > keychain.STDIN_COMMAND_LIMIT
    REAL_WRITE(SERVICE, "tester", secret)

    [(args, stdin)] = run.calls
    assert stdin is None
    assert args == ["add-generic-password", "-U", "-a", "tester", "-s", SERVICE, "-X", secret.encode().hex()]


def test_write_uses_update_flag_never_delete(run):
    REAL_WRITE(SERVICE, "tester", "{}")
    args, stdin = run.calls[0]
    assert "-U" in stdin
    assert not any("delete" in a for a in args)


@pytest.mark.parametrize("rc,stderr", [(1, "boom"), (0, "security: SecKeychainItemCreateFromContent: error")])
def test_write_failure_raises_without_leaking_secret(monkeypatch, rc, stderr):
    monkeypatch.setattr(keychain, "_run", Recorder(returncode=rc, stderr=stderr))
    with pytest.raises(keychain.KeychainError) as info:
        REAL_WRITE(SERVICE, "tester", '{"accessToken":"sk-ant-oat01-secret"}')
    assert "sk-ant" not in str(info.value)


@pytest.mark.parametrize(
    "service,account",
    [('Claude Code-credentials" -s "evil', "tester"), (SERVICE, 'tester"\ndelete-generic-password'), ("", "tester")],
)
def test_write_refuses_attributes_that_break_quoting(run, service, account):
    with pytest.raises(keychain.KeychainError):
        REAL_WRITE(service, account, "{}")
    assert run.calls == []


def test_saved_login_service_names_are_accepted(run):
    REAL_WRITE("cc-switcher:aaaaaaaa-0000-0000-0000-000000000001:0000000a-0000", "claude-code-user", "{}")
    assert len(run.calls) == 1


# ----------------------------------------------------------------------------- read / attr / delete


def test_read_returns_secret_without_trailing_newline(monkeypatch):
    rec = Recorder(stdout='{"a":1}\n')
    monkeypatch.setattr(keychain, "_run", rec)
    assert REAL_READ(SERVICE) == '{"a":1}'
    # The live item is pinned to Claude Code's account name (USER=tester in tests).
    assert rec.calls[0][0] == ["find-generic-password", "-a", "tester", "-s", SERVICE, "-w"]


def test_reads_of_our_own_items_are_not_account_scoped(monkeypatch):
    rec = Recorder(stdout="secret\n")
    monkeypatch.setattr(keychain, "_run", rec)
    assert REAL_READ("cc-switcher:acct:org") == "secret"
    assert rec.calls[0][0] == ["find-generic-password", "-s", "cc-switcher:acct:org", "-w"]


def test_live_item_scope_follows_claude_config_dir(monkeypatch):
    # With CLAUDE_CONFIG_DIR set Claude Code uses a hashed service name; that one is "live" too.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/alt-claude")
    from cc_switcher import paths

    live = paths.claude_keychain_service()
    assert live != SERVICE
    rec = Recorder(stdout='"acct"<blob>="tester"\n')
    monkeypatch.setattr(keychain, "_run", rec)
    assert REAL_READ_ACCOUNT_ATTR(live) == "tester"
    assert rec.calls[0][0] == ["find-generic-password", "-a", "tester", "-s", live]
    rec2 = Recorder()
    monkeypatch.setattr(keychain, "_run", rec2)
    REAL_READ(SERVICE)  # the default-dir service is no longer the live one -> unscoped
    assert rec2.calls[0][0] == ["find-generic-password", "-s", SERVICE, "-w"]


@pytest.mark.parametrize(
    "rc,stdout,stderr",
    [(44, "", ""), (1, "", "security: The specified item could not be found in the keychain."), (0, "\n", "")],
)
def test_read_missing_or_empty_is_none(monkeypatch, rc, stdout, stderr):
    monkeypatch.setattr(keychain, "_run", Recorder(returncode=rc, stdout=stdout, stderr=stderr))
    assert REAL_READ(SERVICE) is None


def test_read_other_failures_raise(monkeypatch):
    monkeypatch.setattr(keychain, "_run", Recorder(returncode=36, stderr="User interaction is not allowed."))
    with pytest.raises(keychain.KeychainError):
        REAL_READ(SERVICE)


def test_read_account_attr_parses_acct(monkeypatch):
    out = 'keychain: "/Users/x/Library/Keychains/login.keychain-db"\nattributes:\n    "acct"<blob>="tester"\n    "svce"<blob>="Claude Code-credentials"\n'
    monkeypatch.setattr(keychain, "_run", Recorder(stdout=out))
    assert REAL_READ_ACCOUNT_ATTR(SERVICE) == "tester"
    monkeypatch.setattr(keychain, "_run", Recorder(returncode=44))
    assert REAL_READ_ACCOUNT_ATTR(SERVICE) is None


def test_delete_reports_whether_something_was_deleted(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(keychain, "_run", rec)
    assert REAL_DELETE("cc-switcher:x") is True
    assert rec.calls[0][0] == ["delete-generic-password", "-s", "cc-switcher:x"]
    monkeypatch.setattr(keychain, "_run", Recorder(returncode=44))
    assert REAL_DELETE("cc-switcher:x") is False


# ----------------------------------------------------------------------------- _run


def test_run_invokes_security_with_captured_text_and_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    REAL_RUN(["-i"], input_text="cmd\n")
    assert seen["argv"] == [keychain.SECURITY, "-i"]
    assert seen["kwargs"] == {
        "input": "cmd\n",
        "capture_output": True,
        "text": True,
        "timeout": keychain.TIMEOUT_SECONDS,
    }


def test_run_timeout_becomes_keychain_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(keychain.KeychainError, match="did not answer"):
        REAL_RUN(["find-generic-password", "-s", SERVICE, "-w"])


# ----------------------------------------------------------------------------- hex output from `security -w`


def test_read_decodes_hex_output(monkeypatch):
    """`security find-generic-password -w` prints the data as hex when it isn't printable text."""
    secret = '{"mcpOAuth":{"x|0":{"serverName":"café"}}}'
    monkeypatch.setattr(keychain, "_run", Recorder(stdout=secret.encode("utf-8").hex() + "\n"))
    assert REAL_READ(SERVICE) == secret


@pytest.mark.parametrize(
    "stdout",
    [
        '{"a":1}',  # JSON-looking: returned as is
        "7b2261223a317",  # odd length
        "7b22zz",  # not hex
        "deadbeef",  # hex, but not UTF-8 text of a JSON object
        "c328",  # hex, invalid UTF-8
        "616263",  # hex of "abc": not a JSON object, so left alone
    ],
)
def test_read_leaves_everything_else_unchanged(monkeypatch, stdout):
    monkeypatch.setattr(keychain, "_run", Recorder(stdout=stdout + "\n"))
    assert REAL_READ(SERVICE) == stdout
