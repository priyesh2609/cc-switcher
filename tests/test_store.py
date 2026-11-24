"""Account registry and settings files."""

from __future__ import annotations

import json
import stat

import pytest

from cc_switcher import paths, store
from cc_switcher.models import Account, Settings


def _account(n: int, email: str, label: str = "", org: str = "") -> Account:
    return Account(
        id=f"acct-{n}:org-{n}",
        email=email,
        account_uuid=f"acct-{n}",
        organization_uuid=f"org-{n}",
        organization_name=org,
        label=label,
    )


def test_missing_registry_is_empty():
    assert store.load_accounts() == []
    assert store.get_account("nope") is None


def test_round_trip_and_private_permissions():
    a = _account(1, "a@example.com", label="work")
    a.oauth_account = {"accountUuid": "acct-1", "emailAddress": "a@example.com"}
    store.save_accounts([a])

    assert store.load_accounts() == [a]
    assert stat.S_IMODE(paths.accounts_file().stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.app_dir().stat().st_mode) == 0o700
    data = json.loads(paths.accounts_file().read_text())
    assert data["version"] == store.SCHEMA_VERSION


def test_registry_never_contains_tokens(samples):
    store.upsert_account(_account(1, "a@example.com"))
    assert "sk-ant" not in paths.accounts_file().read_text()


def test_upsert_replaces_by_id_and_keeps_others():
    a, b = _account(1, "a@example.com"), _account(2, "b@example.com")
    store.save_accounts([a, b])
    a2 = _account(1, "a@example.com", label="renamed")
    store.upsert_account(a2)

    accounts = {acc.id: acc for acc in store.load_accounts()}
    assert accounts[a.id].label == "renamed"
    assert accounts[b.id] == b
    assert len(accounts) == 2


def test_remove_account():
    a, b = _account(1, "a@example.com"), _account(2, "b@example.com")
    store.save_accounts([a, b])
    store.remove_account(a.id)
    assert store.load_accounts() == [b]
    store.remove_account("not-there")  # no error
    assert store.load_accounts() == [b]


def test_load_tolerates_corruption_and_unknown_fields():
    paths.app_dir().mkdir(parents=True)
    paths.accounts_file().write_text("{broken")
    assert store.load_accounts() == []

    row = _account(1, "a@example.com").to_dict()
    row["field_from_the_future"] = 1
    paths.accounts_file().write_text(json.dumps({"version": 1, "accounts": [row, "junk", {"id": "x"}]}))
    loaded = store.load_accounts()
    assert [a.email for a in loaded] == ["a@example.com"]  # junk and incomplete rows skipped


def test_find_account_by_id_label_email_case_insensitive():
    a = _account(1, "Alice@Example.com", label="Work")
    b = _account(2, "bob@example.com", label="personal")
    store.save_accounts([a, b])

    assert store.find_account(a.id) == a
    assert store.find_account("work") == a
    assert store.find_account("  WORK ") == a
    assert store.find_account("alice@example.com") == a
    assert store.find_account("pers") == b  # unique label prefix
    assert store.find_account("bob") == b  # unique email prefix


def test_find_account_ambiguous_and_missing():
    # Same email in two orgs (personal plan + team seat).
    a = _account(1, "same@example.com", org="Personal")
    b = _account(2, "same@example.com", org="Team")
    store.save_accounts([a, b])

    with pytest.raises(store.AmbiguousAccount) as info:
        store.find_account("same@example.com")
    assert "Personal" in str(info.value) and "Team" in str(info.value)
    with pytest.raises(store.AmbiguousAccount):
        store.find_account("same")
    with pytest.raises(store.AccountNotFound):
        store.find_account("nobody")


def test_settings_default_and_round_trip():
    assert store.load_settings() == Settings()
    s = Settings(poll_interval_seconds=60, alert_thresholds=[50, 90], menubar_title="both")
    store.save_settings(s)
    assert store.load_settings() == s
    assert stat.S_IMODE(paths.settings_file().stat().st_mode) == 0o600


def test_settings_ignore_unknown_keys():
    paths.app_dir().mkdir(parents=True)
    paths.settings_file().write_text(json.dumps({"poll_interval_seconds": 120, "removed_option": True}))
    assert store.load_settings().poll_interval_seconds == 120
