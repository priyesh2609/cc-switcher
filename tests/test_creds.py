"""Pure credential-blob functions. These carry invariant I1 (MCP state is never swapped or lost)."""

from __future__ import annotations

import copy
import json

import pytest

from cc_switcher import creds


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def live(samples):
    return {
        "claudeAiOauth": samples.make_login(samples.ACCOUNT_A, "a1"),
        "mcpOAuth": copy.deepcopy(samples.SAMPLE_MCP_OAUTH),
        "mcpOAuthClientConfig": {"sentry|800cb29a3ac61727": {"clientId": "cid-ü", "clientSecret": "fake-secret"}},
        "someFutureKey": {"nested": [1, 2, 3]},
    }


# ----------------------------------------------------------------------------- parse / dump


@pytest.mark.parametrize("text", [None, ""])
def test_parse_blob_empty_is_empty_dict(text):
    assert creds.parse_blob(text) == {}


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"a string"', "42"])
def test_parse_blob_rejects_non_objects(text):
    with pytest.raises(creds.CredentialsError):
        creds.parse_blob(text)


def test_parse_blob_error_does_not_echo_content():
    with pytest.raises(creds.CredentialsError) as info:
        creds.parse_blob('{"accessToken": "sk-ant-oat01-secret"')
    assert "sk-ant" not in str(info.value)


def test_dump_blob_is_compact_ascii_and_round_trips_unicode(live):
    """ASCII only: non-ASCII bytes in an item can make `security -w` print it as hex."""
    text = creds.dump_blob(live)
    assert ": " not in text and ", " not in text
    assert text.isascii()
    assert "cid-\\u00fc" in text
    assert creds.parse_blob(text) == live


def test_dump_blob_escapes_lone_surrogates_losslessly():
    blob = {"mcpOAuth": {"x|0": {"serverName": "split emoji \ud83d"}}}
    text = creds.dump_blob(blob)
    assert text.isascii() and "\\ud83d" in text
    assert creds.parse_blob(text) == blob


# ----------------------------------------------------------------------------- login / identity


def test_get_login(samples, live):
    assert creds.get_login(live) == live["claudeAiOauth"]
    assert creds.get_login({}) is None
    assert creds.get_login({"claudeAiOauth": "nope"}) is None


def test_get_login_treats_empty_token_stub_as_logged_out(samples):
    stub = samples.make_login(samples.ACCOUNT_A)
    stub["accessToken"] = ""
    stub["refreshToken"] = ""
    assert creds.get_login({"claudeAiOauth": stub}) is None
    stub["refreshToken"] = "sk-ant-ort01-only-refresh"
    assert creds.get_login({"claudeAiOauth": stub}) is stub


def test_identity_and_account_id(samples):
    login = samples.make_login(samples.ACCOUNT_A)
    assert creds.identity(login) == samples.ACCOUNT_A
    assert creds.login_account_id(login) == f"{samples.ACCOUNT_A[0]}:{samples.ACCOUNT_A[1]}"
    for missing in ("accountUuid", "organizationUuid"):
        partial = dict(login)
        partial.pop(missing)
        assert creds.identity(partial) is None
        assert creds.login_account_id(partial) is None
    assert creds.identity(None) is None


# ----------------------------------------------------------------------------- saved items (I2)


def test_saved_item_holds_only_the_login_as_a_copy(live):
    item = creds.saved_item(live["claudeAiOauth"])
    assert list(item) == ["claudeAiOauth"]
    item["claudeAiOauth"]["accessToken"] = "changed"
    assert live["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-fake-a1"


# ----------------------------------------------------------------------------- merge_login (I1)


def test_merge_login_replaces_only_the_login(samples, live):
    original = copy.deepcopy(live)
    login_b = samples.make_login(samples.ACCOUNT_B, "b1")

    merged = creds.merge_login(live, login_b)

    assert merged["claudeAiOauth"] == login_b
    assert creds.non_login_parts(merged) == creds.non_login_parts(original)
    # byte-for-byte, key order included
    assert _compact(merged["mcpOAuth"]) == _compact(original["mcpOAuth"])
    assert _compact(merged["mcpOAuthClientConfig"]) == _compact(original["mcpOAuthClientConfig"])
    assert merged["someFutureKey"] == {"nested": [1, 2, 3]}
    assert list(merged) == list(original)  # login keeps its position
    assert live == original  # input untouched


def test_merge_login_result_is_independent_of_inputs(samples, live):
    login_b = samples.make_login(samples.ACCOUNT_B, "b1")
    merged = creds.merge_login(live, login_b)
    merged["mcpOAuth"]["sentry|800cb29a3ac61727"]["accessToken"] = "mutated"
    merged["claudeAiOauth"]["accessToken"] = "mutated"
    assert live["mcpOAuth"]["sentry|800cb29a3ac61727"]["accessToken"] == "fake-sentry-access"
    assert login_b["accessToken"] == "sk-ant-oat01-fake-b1"


def test_non_login_parts(live):
    parts = creds.non_login_parts(live)
    assert "claudeAiOauth" not in parts
    assert set(parts) == {"mcpOAuth", "mcpOAuthClientConfig", "someFutureKey"}


# ----------------------------------------------------------------------------- restore_missing_mcp (I1)


def test_restore_missing_mcp_puts_back_what_login_wiped(samples, live):
    # `claude auth login` rewrote the item with just the new login.
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "b1")}

    fixed = creds.restore_missing_mcp(after, live)

    assert fixed["claudeAiOauth"] == after["claudeAiOauth"]  # the new login is kept
    assert _compact(fixed["mcpOAuth"]) == _compact(live["mcpOAuth"])
    assert fixed["mcpOAuthClientConfig"] == live["mcpOAuthClientConfig"]
    assert fixed["someFutureKey"] == live["someFutureKey"]


def test_restore_missing_mcp_prefers_entries_in_after(samples, live):
    fresh = {"serverName": "sentry", "accessToken": "rotated-access", "refreshToken": "rotated-refresh"}
    after = {
        "claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "b1"),
        "mcpOAuth": {"sentry|800cb29a3ac61727": fresh, "linear|0123456789abcdef": {"accessToken": "new"}},
    }

    fixed = creds.restore_missing_mcp(after, live)

    assert fixed["mcpOAuth"]["sentry|800cb29a3ac61727"] == fresh  # after wins
    assert fixed["mcpOAuth"]["linear|0123456789abcdef"] == {"accessToken": "new"}
    assert fixed["mcpOAuth"]["atlassian|1eb778bd626fb68d"] == live["mcpOAuth"]["atlassian|1eb778bd626fb68d"]


def test_restore_missing_mcp_never_copies_the_old_login(samples, live):
    fixed = creds.restore_missing_mcp({}, live)
    assert "claudeAiOauth" not in fixed
    assert fixed["mcpOAuth"] == live["mcpOAuth"]


def test_restore_missing_mcp_does_not_mutate_inputs(samples, live):
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B), "mcpOAuth": {}}
    after_copy, live_copy = copy.deepcopy(after), copy.deepcopy(live)
    fixed = creds.restore_missing_mcp(after, live)
    fixed["mcpOAuth"]["sentry|800cb29a3ac61727"]["accessToken"] = "mutated"
    assert after == after_copy
    assert live == live_copy


# ----------------------------------------------------------------------------- restore_missing_mcp: review regressions

SENTRY = "sentry|800cb29a3ac61727"
ATLASSIAN = "atlassian|1eb778bd626fb68d"


@pytest.mark.parametrize("store", ["mcpOAuth", "mcpOAuthClientConfig"])
@pytest.mark.parametrize("garbage", [None, "", [], 0, "oops"])
def test_restore_missing_mcp_treats_a_non_object_store_as_missing(samples, live, store, garbage):
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "b1"), store: garbage}

    fixed = creds.restore_missing_mcp(after, live)

    assert fixed[store] == live[store]
    assert fixed["claudeAiOauth"] == after["claudeAiOauth"]


@pytest.mark.parametrize(
    "stub",
    [
        {"serverName": "sentry", "accessToken": ""},  # what Claude Code writes when sign-in is needed
        {"serverName": "sentry"},
        {"serverName": "sentry", "accessToken": "", "refreshToken": ""},
        None,
    ],
)
def test_restore_missing_mcp_does_not_let_a_stub_shadow_real_tokens(samples, live, stub):
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "b1"), "mcpOAuth": {SENTRY: stub}}

    fixed = creds.restore_missing_mcp(after, live)

    assert fixed["mcpOAuth"][SENTRY] == live["mcpOAuth"][SENTRY]
    assert fixed["mcpOAuth"][ATLASSIAN] == live["mcpOAuth"][ATLASSIAN]


@pytest.mark.parametrize(
    "fresh",
    [
        {"serverName": "sentry", "accessToken": "rotated", "refreshToken": "rotated-r"},
        {"serverName": "sentry", "accessToken": "", "refreshToken": "only-refresh"},
        {"serverName": "sentry", "accessToken": "only-access"},
    ],
)
def test_restore_missing_mcp_entry_with_tokens_in_after_still_wins(samples, live, fresh):
    after = {"mcpOAuth": {SENTRY: fresh}}
    assert creds.restore_missing_mcp(after, live)["mcpOAuth"][SENTRY] == fresh


def test_restore_missing_mcp_keeps_after_stub_when_before_had_no_tokens_either(samples, live):
    # atlassian in `before` is itself a stub (accessToken ""), so after's newer stub stands.
    newer_stub = {"serverName": "atlassian", "accessToken": "", "discoveryState": {"v": 2}}
    after = {"mcpOAuth": {ATLASSIAN: newer_stub}}
    assert creds.restore_missing_mcp(after, live)["mcpOAuth"][ATLASSIAN] == newer_stub


def test_restore_missing_mcp_client_config_without_secret_does_not_shadow_one(live):
    # Claude Code only reads `clientSecret` from mcpOAuthClientConfig entries.
    after = {"mcpOAuthClientConfig": {SENTRY: {}}}
    assert creds.restore_missing_mcp(after, live)["mcpOAuthClientConfig"][SENTRY] == live["mcpOAuthClientConfig"][SENTRY]
    newer = {SENTRY: {"clientSecret": "fake-new-secret"}}
    assert creds.restore_missing_mcp({"mcpOAuthClientConfig": newer}, live)["mcpOAuthClientConfig"] == newer


def test_restore_missing_mcp_invents_nothing_for_unknown_keys(live):
    # Unknown keys keep the old rules: present in `after` (even null) wins; dicts are unioned.
    assert creds.restore_missing_mcp({"someFutureKey": None}, live)["someFutureKey"] is None
    fixed = creds.restore_missing_mcp({"someFutureKey": {"other": 1}}, {"someFutureKey": {"nested": {"x": ""}}})
    assert fixed["someFutureKey"] == {"other": 1, "nested": {"x": ""}}
    stub_like = creds.restore_missing_mcp({"futureStore": {"k": {"accessToken": ""}}}, {"futureStore": {"k": {"accessToken": "t"}}})
    assert stub_like["futureStore"]["k"] == {"accessToken": ""}


# ----------------------------------------------------------------------------- restore_wiped_mcp (round 2: whole stores only)


@pytest.mark.parametrize("store", ["mcpOAuth", "mcpOAuthClientConfig"])
@pytest.mark.parametrize("wiped", ["missing", None, {}, [], "oops"])
def test_restore_wiped_mcp_puts_back_a_wiped_store(samples, live, store, wiped):
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B, "b1")}
    if wiped != "missing":
        after[store] = wiped

    fixed = creds.restore_wiped_mcp(after, live)

    assert _compact(fixed[store]) == _compact(live[store])  # byte-for-byte, key order included
    assert fixed["claudeAiOauth"] == after["claudeAiOauth"]  # the login is never touched


@pytest.mark.parametrize(
    "store_after",
    [
        {ATLASSIAN: {"serverName": "atlassian", "accessToken": ""}},  # after `claude mcp remove sentry`
        {SENTRY: {"serverName": "sentry", "accessToken": ""}, ATLASSIAN: {"serverName": "atlassian", "accessToken": ""}},
        {SENTRY: {"serverName": "sentry", "accessToken": "rotated"}},  # anything else someone wrote
    ],
)
def test_restore_wiped_mcp_leaves_a_store_with_entries_exactly_as_is(live, store_after):
    after = {"mcpOAuth": copy.deepcopy(store_after)}
    assert creds.restore_wiped_mcp(after, live)["mcpOAuth"] == store_after


def test_restore_wiped_mcp_keeps_a_re_registered_public_client(live):
    """N5: an old {clientId, clientSecret} must not come back over a newer entry."""
    before = {"mcpOAuthClientConfig": {SENTRY: {"clientId": "old-cid", "clientSecret": "old-secret"}}}
    after = {"mcpOAuthClientConfig": {SENTRY: {"clientId": "new-public-cid"}}}
    assert creds.restore_wiped_mcp(after, before) == after


def test_restore_wiped_mcp_unknown_keys_come_back_only_when_missing(live):
    assert creds.restore_wiped_mcp({}, live)["someFutureKey"] == live["someFutureKey"]
    assert creds.restore_wiped_mcp({"someFutureKey": None}, live)["someFutureKey"] is None
    assert creds.restore_wiped_mcp({"someFutureKey": {"other": 1}}, live)["someFutureKey"] == {"other": 1}


def test_restore_wiped_mcp_with_empty_before_and_no_mutation(samples, live):
    after = {"claudeAiOauth": samples.make_login(samples.ACCOUNT_B), "mcpOAuth": {}}
    assert creds.restore_wiped_mcp(after, {"mcpOAuth": {}}) == after  # nothing to restore from
    after_copy, live_copy = copy.deepcopy(after), copy.deepcopy(live)
    fixed = creds.restore_wiped_mcp(after, live)
    fixed["mcpOAuth"][SENTRY]["accessToken"] = "mutated"
    assert after == after_copy and live == live_copy
