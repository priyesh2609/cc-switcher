from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

import pytest

from cc_switcher import cli, creds, fsutil, keychain, paths, store
from cc_switcher.models import Account, make_account_id
from conftest import ACCOUNT_A, ACCOUNT_B, SAMPLE_MCP_OAUTH, make_login


@pytest.fixture
def importer():
    from cc_switcher import credential_import
    return credential_import


@pytest.fixture
def source(tmp_path):
    def create(data, *, mode=0o600):
        file = tmp_path / "credentials.txt"
        file.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        file.chmod(mode)
        return file
    return create


def test_file_import_entrypoint_exists_without_using_live_import(source, monkeypatch, capsys):
    from cc_switcher import switcher
    monkeypatch.setattr(switcher, "import_current", lambda **kw: pytest.fail("live import was invoked"))
    code = cli.main(["import", "--file", str(source(make_login(ACCOUNT_B))), "--label", "work"])
    assert code == 0
    out = capsys.readouterr().out
    assert "work" in out and "not activated" in out.lower()
    assert "not" in out.lower() and "online" in out.lower()


@pytest.mark.parametrize("form", ["blob", "login", "hex", "hex-prefix", "bom"])
def test_private_file_import_saves_only_login_and_preserves_live_mcp(importer, source, fake_keychain, form):
    login = make_login(ACCOUNT_B, "imported", expires_at_ms=1)
    blob = {"claudeAiOauth": login, "mcpOAuth": {"foreign": {"accessToken": "foreign-secret"}}}
    data = login if form == "login" else blob
    if form.startswith("hex"):
        data = ("0x" if form == "hex-prefix" else "") + json.dumps(blob).encode().hex()
    if form == "bom":
        data = "﻿" + json.dumps(blob)
    file = source(data)
    original_file = file.read_bytes()
    live = {"claudeAiOauth": make_login(ACCOUNT_A, "live"), "mcpOAuth": SAMPLE_MCP_OAUTH,
            "mcpOAuthClientConfig": {"vendor": {"clientSecret": "local-mcp-secret"}}}
    fake_keychain.put_json(paths.claude_keychain_service(), live)
    original_live = fake_keychain.items[paths.claude_keychain_service()]

    account = importer.import_file(file, label="work")

    assert account.id == make_account_id(*ACCOUNT_B) and account.label == "work"
    assert fake_keychain.get_json(account.keychain_service) == creds.saved_item(login)
    assert fake_keychain.items[paths.claude_keychain_service()] == original_live
    assert fake_keychain.writes == [account.keychain_service]
    assert store.get_account(account.id) == account
    assert file.read_bytes() == original_file
    metadata = paths.accounts_file().read_text()
    assert "sk-ant-" not in metadata and "foreign-secret" not in metadata and "local-mcp-secret" not in metadata


@pytest.mark.parametrize("existing", ["registry", "keychain", "empty-keychain", "live"])
def test_import_refuses_existing_or_active_account_without_overwriting(importer, source, fake_keychain, existing):
    login = make_login(ACCOUNT_B, "stale")
    account = Account(make_account_id(*ACCOUNT_B), "b@example.test", *ACCOUNT_B)
    if existing == "registry":
        store.upsert_account(account)
    elif existing == "keychain":
        fake_keychain.put_json(account.keychain_service, creds.saved_item(make_login(ACCOUNT_B, "newer")))
    elif existing == "empty-keychain":
        fake_keychain.items[account.keychain_service] = ("tester", "")
    else:
        fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(make_login(ACCOUNT_B, "current")))
    before = copy.deepcopy(fake_keychain.items)
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(login), label="old-copy")
    assert fake_keychain.items == before and not fake_keychain.writes


def test_duplicate_label_is_rejected_before_keychain_write(importer, source, fake_keychain):
    store.upsert_account(Account(make_account_id(*ACCOUNT_A), "a@example.test", *ACCOUNT_A, label="Work"))
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(make_login(ACCOUNT_B)), label="work")
    assert not fake_keychain.writes


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink", "too-large", "public"])
def test_unsafe_input_is_rejected_without_blocking_or_writing(importer, source, tmp_path, fake_keychain, kind):
    file = source(make_login(ACCOUNT_B))
    if kind == "fifo":
        file.unlink()
        os.mkfifo(file, 0o600)
    elif kind == "directory":
        file = tmp_path
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(file)
        file = link
    elif kind == "too-large":
        file.write_bytes(b"x" * (1_048_576 + 1))
    else:
        file.chmod(0o644)
    started = time.monotonic()
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(file)
    assert time.monotonic() - started < 2
    assert not fake_keychain.writes


@pytest.mark.parametrize("field,value", [
    ("accountUuid", None), ("organizationUuid", "bad:identity"), ("accountUuid", ["not-string"]),
    ("accessToken", 42), ("refreshToken", {"nested": "secret"}), ("refreshToken", "line\nbreak"),
    ("expiresAt", True), ("expiresAt", "bad"), ("expiresAt", float("inf")),
    ("refreshTokenExpiresAt", -1), ("scopes", [42]),
])
def test_invalid_login_schema_is_rejected_privately(importer, source, fake_keychain, field, value):
    login = make_login(ACCOUNT_B)
    login[field] = value
    with pytest.raises(importer.CredentialImportError) as caught:
        importer.import_file(source(login))
    assert "sk-ant-" not in str(caught.value)
    assert not fake_keychain.writes and store.load_accounts() == []


@pytest.mark.parametrize("data", ["", "sk-ant-oat01-DO-NOT-SHOW", "[]", '{"claudeAiOauth":null}', '{"accessToken":"x","accessToken":"y"}'])
def test_malformed_file_never_echoes_credentials(importer, source, fake_keychain, data):
    with pytest.raises(importer.CredentialImportError) as caught:
        importer.import_file(source(data))
    assert "DO-NOT-SHOW" not in str(caught.value) and "sk-ant-" not in str(caught.value)
    assert not fake_keychain.writes


def test_unknown_login_fields_are_not_persisted_as_metadata_or_mcp(importer, source, fake_keychain):
    login = make_login(ACCOUNT_B)
    login["email"] = "secret-in-a-metadata-field"
    login["mcpOAuth"] = {"foreign": {"accessToken": "connector-secret"}}
    account = importer.import_file(source({"claudeAiOauth": login}), label="work")
    saved = fake_keychain.get_json(account.keychain_service)["claudeAiOauth"]
    assert "email" not in saved and "mcpOAuth" not in saved
    assert "secret-in-a-metadata-field" not in paths.accounts_file().read_text()
    assert account.email == ""


def test_silent_partial_keychain_write_never_registers_account(importer, source, fake_keychain, monkeypatch):
    old_write = keychain.write
    def partial(service, attr, secret):
        data = json.loads(secret)
        data["claudeAiOauth"]["accessToken"] = "incorrect-but-same-refresh-token"
        old_write(service, attr, json.dumps(data))
    monkeypatch.setattr(keychain, "write", partial)
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(make_login(ACCOUNT_B)))
    assert store.load_accounts() == []


def test_recovery_and_login_markers_block_import(importer, source, fake_keychain):
    from cc_switcher import claude_refresh
    file = source(make_login(ACCOUNT_B))
    with fsutil.app_lock():
        nonce = claude_refresh.begin_login_locked()
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(file)
    with fsutil.app_lock():
        claude_refresh.end_login_locked(nonce)
        claude_refresh._durable_json(claude_refresh._journal(), {"nonce": "old-operation"})
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(file)
    assert not fake_keychain.writes


def test_corrupt_registry_is_not_replaced(importer, source, fake_keychain):
    fsutil.ensure_private_dir(paths.app_dir())
    paths.accounts_file().write_text("{damaged registry")
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(make_login(ACCOUNT_B)))
    assert paths.accounts_file().read_text() == "{damaged registry"
    assert not fake_keychain.writes


@pytest.mark.parametrize("shared", ["accessToken", "refreshToken"])
def test_shared_saved_grant_is_refused_for_different_account_ids(importer, source, fake_keychain, shared):
    existing = Account(make_account_id(*ACCOUNT_A), "a@example.test", *ACCOUNT_A, label="personal")
    store.upsert_account(existing)
    saved = make_login(ACCOUNT_A, "original")
    incoming = make_login(ACCOUNT_B, "copied")
    incoming[shared] = saved[shared]
    fake_keychain.put_json(existing.keychain_service, creds.saved_item(saved))
    before = copy.deepcopy(fake_keychain.items)
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(incoming), label="work")
    assert fake_keychain.items == before and not fake_keychain.writes
    assert store.load_accounts() == [existing]


def test_unreadable_saved_account_blocks_duplicate_grant_check(importer, source, fake_keychain):
    existing = Account(make_account_id(*ACCOUNT_A), "a@example.test", *ACCOUNT_A)
    store.upsert_account(existing)
    fake_keychain.items[existing.keychain_service] = ("tester", "malformed")
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(make_login(ACCOUNT_B)))
    assert not fake_keychain.writes and store.load_accounts() == [existing]


def test_distinct_existing_saved_grant_does_not_block_new_account(importer, source, fake_keychain):
    existing = Account(make_account_id(*ACCOUNT_A), "a@example.test", *ACCOUNT_A)
    store.upsert_account(existing)
    fake_keychain.put_json(existing.keychain_service, creds.saved_item(make_login(ACCOUNT_A, "original")))
    account = importer.import_file(source(make_login(ACCOUNT_B, "distinct")))
    assert len(store.load_accounts()) == 2
    assert account.id == make_account_id(*ACCOUNT_B)


def test_active_fallback_login_is_refused_without_keychain_entry(importer, source, fake_keychain, tmp_path, monkeypatch):
    secure = tmp_path / "claude-secure"
    secure.mkdir(mode=0o700)
    login = make_login(ACCOUNT_B, "live-file")
    fsutil.atomic_write_json(secure / ".credentials.json", creds.saved_item(login))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure))
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(login))
    assert not fake_keychain.writes


def test_shared_live_grant_is_refused_even_when_account_ids_differ(importer, source, fake_keychain):
    login = make_login(ACCOUNT_B, "copied")
    live = make_login(ACCOUNT_A, "live")
    live["refreshToken"] = login["refreshToken"]
    fake_keychain.put_json(paths.claude_keychain_service(), creds.saved_item(live))
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(login))
    assert not fake_keychain.writes


def test_oversized_numeric_value_fails_without_exposing_file(importer, source, fake_keychain):
    login = make_login(ACCOUNT_B)
    login["expiresAt"] = 10**400
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(login))
    assert not fake_keychain.writes


def test_failed_registration_retains_source_and_saved_copy(importer, source, fake_keychain, monkeypatch):
    login = make_login(ACCOUNT_B)
    file = source(login)
    original = file.read_bytes()
    monkeypatch.setattr(store, "save_accounts", lambda value: (_ for _ in ()).throw(OSError("DO-NOT-SHOW")))
    with pytest.raises(importer.CredentialImportError) as caught:
        importer.import_file(file)
    assert "DO-NOT-SHOW" not in str(caught.value)
    assert "saved to Keychain" in str(caught.value)
    assert file.read_bytes() == original
    assert fake_keychain.get_json(paths.saved_login_service(make_account_id(*ACCOUNT_B))) == creds.saved_item(login)


def test_invalid_file_error_is_redacted_by_cli(source, capsys):
    file = source("sk-ant-ort01-PRIVATE")
    assert cli.main(["import", "--file", str(file)]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.err + captured.out
    assert "valid JSON" in captured.err


def test_large_credential_cannot_reach_keychain_argv_fallback(importer, source, fake_keychain):
    login = make_login(ACCOUNT_B)
    login["refreshToken"] = "secret" * 1000
    with pytest.raises(importer.CredentialImportError):
        importer.import_file(source(login))
    assert not fake_keychain.writes
