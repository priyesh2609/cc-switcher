"""Test harness. SAFETY FIRST: these tests run on a real Mac that holds real Claude credentials.

Autouse fixtures guarantee no test can:
- read or write the real Keychain           -> every cc_switcher.keychain function is replaced by FakeKeychain
- touch the real ~/.claude.json or ~/.config -> CC_SWITCHER_HOME points at a tmp dir
- spawn real processes (claude, security, launchctl, osascript) -> subprocess is blocked
- reach the network                          -> urllib is blocked

Tests that need a subprocess or HTTP response must inject a fake (runner=/opener=/http_post=
parameters) or monkeypatch the specific module attribute themselves.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request

import pytest

from cc_switcher import keychain


class FakeKeychain:
    def __init__(self) -> None:
        self.items: dict[str, tuple[str, str]] = {}  # service -> (account, secret)
        self.writes: list[str] = []  # services written, in order
        self.fail_writes_for: set[str] = set()
        # Test hooks, called with the service name: simulate another process (Claude Code)
        # changing an item right before a read is answered, or right after.
        self.before_read = None
        self.after_read = None

    # mirrors cc_switcher.keychain API
    def read(self, service: str) -> str | None:
        if self.before_read is not None:
            self.before_read(service)
        item = self.items.get(service)
        value = item[1] if item else None
        if self.after_read is not None:
            self.after_read(service)
        return value

    def read_account_attr(self, service: str) -> str | None:
        item = self.items.get(service)
        return item[0] if item else None

    def write(self, service: str, account: str, secret: str) -> None:
        if service in self.fail_writes_for:
            raise keychain.KeychainError(f"simulated write failure for {service}")
        self.items[service] = (account, secret)
        self.writes.append(service)

    def delete(self, service: str) -> bool:
        return self.items.pop(service, None) is not None

    # helpers for tests
    def put_json(self, service: str, data: dict, account: str = "tester") -> None:
        self.items[service] = (account, json.dumps(data))

    def get_json(self, service: str) -> dict | None:
        item = self.items.get(service)
        return json.loads(item[1]) if item else None


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CC_SWITCHER_HOME", str(home))
    monkeypatch.setenv("USER", "tester")
    for var in ("CC_SWITCHER_DIR", "CC_SWITCHER_CLAUDE_PATH", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(keychain, "read", fake.read)
    monkeypatch.setattr(keychain, "read_account_attr", fake.read_account_attr)
    monkeypatch.setattr(keychain, "write", fake.write)
    monkeypatch.setattr(keychain, "delete", fake.delete)

    def _blocked(*args, **kwargs):
        raise AssertionError("cc_switcher.keychain._run reached a real `security` call in a test")

    monkeypatch.setattr(keychain, "_run", _blocked)
    return fake


@pytest.fixture(autouse=True)
def block_side_effects(monkeypatch):
    def _no_subprocess(*args, **kwargs):
        raise AssertionError(f"Real subprocess blocked in tests: {args[:1]!r}. Inject a fake runner.")

    def _no_network(*args, **kwargs):
        raise AssertionError("Real network blocked in tests. Inject a fake opener/http_post.")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    monkeypatch.setattr(subprocess, "check_output", _no_subprocess)
    monkeypatch.setattr(subprocess, "check_call", _no_subprocess)
    monkeypatch.setattr(subprocess, "call", _no_subprocess)
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)


# ----------------------------------------------------------------------------- sample data

ACCOUNT_A = ("aaaaaaaa-0000-0000-0000-000000000001", "0000000a-0000-0000-0000-00000000000a")
ACCOUNT_B = ("bbbbbbbb-0000-0000-0000-000000000002", "0000000b-0000-0000-0000-00000000000b")


def make_login(ident: tuple[str, str], token_suffix: str = "1", expires_at_ms: int = 4102444800000) -> dict:
    """A claudeAiOauth object shaped like Claude Code 2.1.330 writes it (fake token values)."""
    return {
        "accessToken": f"sk-ant-oat01-fake-{token_suffix}",
        "refreshToken": f"sk-ant-ort01-fake-{token_suffix}",
        "expiresAt": expires_at_ms,
        "refreshTokenExpiresAt": None,
        "scopes": ["user:inference", "user:profile", "user:sessions:claude_code", "user:mcp_servers"],
        "subscriptionType": "max",
        "subscriptionTypeSource": "profile",
        "rateLimitTier": "default_claude_max_20x",
        "accountUuid": ident[0],
        "organizationUuid": ident[1],
    }


def make_oauth_account(ident: tuple[str, str], email: str, org: str = "Org") -> dict:
    """An ~/.claude.json "oauthAccount" object (subset of real fields)."""
    return {
        "accountUuid": ident[0],
        "emailAddress": email,
        "organizationUuid": ident[1],
        "organizationName": org,
        "displayName": email.split("@")[0],
        "billingType": "stripe_subscription",
    }


SAMPLE_MCP_OAUTH = {
    "sentry|800cb29a3ac61727": {
        "serverName": "sentry",
        "serverUrl": "https://mcp.sentry.dev/mcp",
        "accessToken": "fake-sentry-access",
        "refreshToken": "fake-sentry-refresh",
        "expiresAt": 4102444800000,
    },
    "atlassian|1eb778bd626fb68d": {
        "serverName": "atlassian",
        "serverUrl": "https://mcp.atlassian.com/v1/mcp",
        "accessToken": "",
        "discoveryState": {"authorizationServerUrl": "https://cf.mcp.atlassian.com"},
    },
}


@pytest.fixture
def samples():
    """Access sample builders from tests: samples.make_login(samples.ACCOUNT_A) ..."""
    import types

    return types.SimpleNamespace(
        ACCOUNT_A=ACCOUNT_A,
        ACCOUNT_B=ACCOUNT_B,
        make_login=make_login,
        make_oauth_account=make_oauth_account,
        SAMPLE_MCP_OAUTH=SAMPLE_MCP_OAUTH,
    )
