from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cc_switcher import claude_refresh, cli, monitor, tokens, usage
from cc_switcher.models import Account, make_account_id
from conftest import ACCOUNT_A


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("RecoveryRequired", "refresh_recovery_required"),
        ("RefreshFailed", "refresh_paused"),
        ("RetryBlocked", "refresh_paused"),
        ("NeedsLogin", "auth_expired"),
        ("RefreshDisabled", "refresh_disabled"),
    ],
)
def test_cli_refresh_failure_is_classified_without_a_usage_request(monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise getattr(claude_refresh, failure)("safe test error")

    monkeypatch.setattr(tokens, "access_token_for", fail)
    monkeypatch.setattr(usage, "fetch_usage_raw", lambda *a, **k: pytest.fail("usage GET must not run"))
    account = Account(id=make_account_id(*ACCOUNT_A), email="a@example.com",
                      account_uuid=ACCOUNT_A[0], organization_uuid=ACCOUNT_A[1])
    snapshot = monitor.refresh_account(account, now=datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert snapshot.ok is False
    assert snapshot.error == expected


def test_cli_refresh_errors_have_actionable_descriptions():
    recovery = cli.error_text("refresh_recovery_required").lower()
    paused = cli.error_text("refresh_paused").lower()
    disabled = cli.error_text("refresh_disabled").lower()
    assert "recovery" in recovery and "_" not in recovery
    assert "paused" in paused and "_" not in paused
    assert "automatic" in paused
    assert "disabled" in disabled and "switch" in disabled
