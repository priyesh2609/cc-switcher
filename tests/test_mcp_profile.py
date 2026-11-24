"""WP-C: portable MCP profile. Everything runs against tmp files, the FakeKeychain and a fake runner."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess

import pytest

from cc_switcher import creds, fsutil, keychain, paths
from cc_switcher import mcp_profile as mp

PAT = "ghp_FAKE0123456789abcdefFAKE0123456789ab"


# --------------------------------------------------------------------------- helpers


def write_claude_json(servers: dict, **extra) -> None:
    data = {"numStartups": 7, **extra, "mcpServers": servers}
    paths.claude_json().write_text(json.dumps(data, indent=2))


class FakeRunner:
    """Records argv; answers with scripted (rc, stdout, stderr) in order, default success."""

    def __init__(self, *results):
        self.calls: list[list[str]] = []
        self._results = list(results)

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self._results.pop(0) if self._results else (0, "ok", "")


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(mp, "find_claude", lambda: "/fake/bin/claude")


SENTRY = {"type": "http", "url": "https://mcp.sentry.dev/mcp"}
ATLASSIAN = {"type": "http", "url": "https://mcp.atlassian.com/v1/mcp"}
SLACK = {"type": "http", "url": "https://mcp.slack.com/mcp"}
GITHUB_PROFILE = {
    "type": "http",
    "url": "https://api.githubcopilot.com/mcp/",
    "headers": {"Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}"},
}


# --------------------------------------------------------------------------- server_key


def test_server_key_matches_two_real_keys():
    assert mp.server_key("sentry", SENTRY) == "sentry|800cb29a3ac61727"
    assert mp.server_key("atlassian", ATLASSIAN) == "atlassian|1eb778bd626fb68d"


def test_server_key_missing_headers_hash_as_empty_object():
    assert mp.server_key("sentry", {**SENTRY, "headers": {}}) == mp.server_key("sentry", SENTRY)
    assert mp.server_key("sentry", {**SENTRY, "headers": None}) == mp.server_key("sentry", SENTRY)


def test_server_key_depends_on_name_url_and_headers():
    base = mp.server_key("sentry", SENTRY)
    assert mp.server_key("sentry-prod", SENTRY).split("|")[1] == base.split("|")[1]  # name is only the prefix
    assert mp.server_key("sentry-prod", SENTRY) != base
    assert mp.server_key("sentry", {**SENTRY, "url": "https://mcp.sentry.dev/other"}) != base
    assert mp.server_key("sentry", {**SENTRY, "headers": {"X-Org": "a"}}) != base


def test_server_key_keeps_non_ascii_unescaped():
    cfg = {"type": "http", "url": "https://例え.jp/mcp"}
    payload = '{"type":"http","url":"https://例え.jp/mcp","headers":{}}'
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    assert mp.server_key("x", cfg) == f"x|{expected}"


# --------------------------------------------------------------------------- catalog


def test_catalog_contents():
    assert set(mp.CATALOG) == {
        "slack", "notion", "linear", "atlassian", "figma", "supabase", "vercel", "sentry", "asana", "github",
    }
    expected_urls = {
        "slack": "https://mcp.slack.com/mcp",
        "notion": "https://mcp.notion.com/mcp",
        "linear": "https://mcp.linear.app/mcp",
        "atlassian": "https://mcp.atlassian.com/v1/mcp",
        "figma": "https://mcp.figma.com/mcp",
        "supabase": "https://mcp.supabase.com/mcp",
        "vercel": "https://mcp.vercel.com",
        "sentry": "https://mcp.sentry.dev/mcp",
        "asana": "https://mcp.asana.com/v2/mcp",
        "github": "https://api.githubcopilot.com/mcp/",
    }
    for name, entry in mp.CATALOG.items():
        assert entry["type"] == "http"
        assert entry["url"] == expected_urls[name]
        assert entry["note"]


def test_catalog_github_needs_pat_header():
    github = mp.CATALOG["github"]
    assert github["headers"] == {"Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}"}
    assert "token" in github["note"].lower() and "CCS_GITHUB_AUTHORIZATION" in github["note"]


# --------------------------------------------------------------------------- list_user_servers


def test_list_user_servers_reads_only_user_scope_dicts():
    write_claude_json(
        {"sentry": SENTRY, "bogus": "not a dict", "local": {"command": "node", "args": ["x.js"]}},
        projects={"/some/proj": {"mcpServers": {"proj-only": SLACK}}},
    )
    assert mp.list_user_servers() == {"sentry": SENTRY, "local": {"command": "node", "args": ["x.js"]}}


def test_list_user_servers_missing_or_odd_file():
    assert mp.list_user_servers() == {}
    paths.claude_json().write_text("{not json")
    assert mp.list_user_servers() == {}
    paths.claude_json().write_text(json.dumps({"mcpServers": []}))
    assert mp.list_user_servers() == {}


def test_list_user_servers_never_modifies_claude_json():
    write_claude_json({"sentry": SENTRY})
    before = paths.claude_json().read_bytes()
    mtime = paths.claude_json().stat().st_mtime_ns
    mp.list_user_servers()
    mp.status()
    assert paths.claude_json().read_bytes() == before
    assert paths.claude_json().stat().st_mtime_ns == mtime


def test_list_user_servers_honours_claude_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "alt"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"sentry": SENTRY}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert mp.list_user_servers() == {"sentry": SENTRY}


# --------------------------------------------------------------------------- auth_state


def test_auth_state_with_sample_blob(samples):
    blob = {"mcpOAuth": samples.SAMPLE_MCP_OAUTH}
    assert mp.auth_state("sentry", SENTRY, blob) == "signed_in"
    assert mp.auth_state("atlassian", ATLASSIAN, blob) == "needs_sign_in"  # empty accessToken stub


def test_auth_state_refresh_token_alone_counts_as_signed_in():
    key = mp.server_key("sentry", SENTRY)
    blob = {"mcpOAuth": {key: {"accessToken": "", "refreshToken": "r"}}}
    assert mp.auth_state("sentry", SENTRY, blob) == "signed_in"


@pytest.mark.parametrize("blob", [{}, {"mcpOAuth": {}}, {"mcpOAuth": "garbage"}, {"mcpOAuth": {"other|1": {"accessToken": "x"}}}])
def test_auth_state_needs_sign_in_when_entry_missing(blob):
    assert mp.auth_state("sentry", SENTRY, blob) == "needs_sign_in"


def test_auth_state_token_belongs_to_exact_key(samples):
    # Same name, different URL -> different key -> the sentry token does not apply.
    moved = {"type": "http", "url": "https://mcp.sentry.dev/elsewhere"}
    assert mp.auth_state("sentry", moved, {"mcpOAuth": samples.SAMPLE_MCP_OAUTH}) == "needs_sign_in"


def test_auth_state_header_auth_and_stdio():
    blob = {"mcpOAuth": {}}
    assert mp.auth_state("github", GITHUB_PROFILE, blob) == "header_auth"
    assert mp.auth_state("x", {**SENTRY, "headers": {"authorization": "Bearer abc"}}, blob) == "header_auth"
    assert mp.auth_state("x", {**SENTRY, "headers": {"X-Api-Key": "abc"}}, blob) == "header_auth"
    assert mp.auth_state("x", {**SENTRY, "headers": {"X-Org": "acme"}}, blob) == "needs_sign_in"
    assert mp.auth_state("local", {"command": "node"}, blob) == "not_oauth"
    assert mp.auth_state("local", {"type": "stdio", "command": "node"}, blob) == "not_oauth"


# --------------------------------------------------------------------------- redaction


def test_placeholder_var_naming():
    assert mp.placeholder_var("github", "Authorization") == "CCS_GITHUB_AUTHORIZATION"
    assert mp.placeholder_var("my-server", "X-Api-Key") == "CCS_MY_SERVER_X_API_KEY"
    assert mp.placeholder_var("a.b", "k_1") == "CCS_A_B_K_1"


def test_redact_header_keeps_bearer_scheme_and_other_headers():
    cfg = {
        "type": "http",
        "url": "https://api.example.com/mcp",
        "headers": {"Authorization": f"Bearer {PAT}", "X-Api-Key": "k-123", "Cookie": "s=1", "X-Org": "acme"},
    }
    out = mp.redact_config("example", cfg)
    assert out["headers"] == {
        "Authorization": "Bearer ${CCS_EXAMPLE_AUTHORIZATION}",
        "X-Api-Key": "${CCS_EXAMPLE_X_API_KEY}",
        "Cookie": "${CCS_EXAMPLE_COOKIE}",
        "X-Org": "acme",
    }


def test_redact_is_idempotent_and_keeps_references():
    cfg = {
        "type": "http",
        "url": "https://x.dev/mcp?token=${CCS_X_TOKEN}",
        "headers": {"Authorization": "Bearer ${GITHUB_PAT}", "X-Api-Key": "${CCS_X_X_API_KEY}"},
    }
    once = mp.redact_config("x", cfg)
    assert once == cfg
    assert mp.redact_config("x", once) == once


def test_redact_stdio_env_args_and_url():
    stdio = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@acme/mcp", "--api-key=abc123", "--token", "zzz", "--verbose", "SLACK_TOKEN=xoxb-1", "--port", "8080"],
        "env": {"GITHUB_TOKEN": "ghp_x", "NODE_ENV": "production", "FROM_SHELL": "${HOME_TOKEN}"},
    }
    out = mp.redact_config("acme", stdio)
    assert out["env"] == {"GITHUB_TOKEN": "${CCS_ACME_GITHUB_TOKEN}", "NODE_ENV": "production", "FROM_SHELL": "${HOME_TOKEN}"}
    assert out["args"] == [
        "-y", "@acme/mcp", "--api-key=${CCS_ACME_API_KEY}", "--token", "${CCS_ACME_TOKEN}", "--verbose",
        "SLACK_TOKEN=${CCS_ACME_SLACK_TOKEN}", "--port", "8080",
    ]
    url = mp.redact_config("z", {"type": "http", "url": "https://user:hunter2@z.dev/mcp?org=acme&api_key=abc&x=1#frag"})
    assert url["url"] == "https://user:${CCS_Z_PASSWORD}@z.dev/mcp?org=acme&api_key=${CCS_Z_API_KEY}&x=1#frag"
    text = json.dumps([out, url])
    for secret in ("abc123", "zzz", "xoxb-1", "ghp_x", "hunter2", "api_key=abc"):
        assert secret not in text


def test_redact_does_not_mutate_input():
    cfg = {"type": "http", "url": "https://x.dev/mcp", "headers": {"Authorization": "Bearer abc"}}
    snapshot = json.loads(json.dumps(cfg))
    mp.redact_config("x", cfg)
    assert cfg == snapshot


# --------------------------------------------------------------------------- profile files


def test_export_writes_profile_without_secrets():
    write_claude_json(
        {
            "sentry": SENTRY,
            "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": f"Bearer {PAT}"}},
            "local": {"command": "node", "args": ["s.js"], "env": {"API_SECRET": "s3cr3t", "DEBUG": "1"}},
        }
    )
    out = mp.export_profile()
    assert out == paths.mcp_profile_file()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    text = out.read_text()
    assert PAT not in text and "s3cr3t" not in text
    data = json.loads(text)
    assert data["version"] == 1
    assert data["servers"]["sentry"] == SENTRY
    assert data["servers"]["github"]["headers"] == {"Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}"}
    assert data["servers"]["local"] == {
        "type": "stdio", "command": "node", "args": ["s.js"],
        "env": {"API_SECRET": "${CCS_LOCAL_API_SECRET}", "DEBUG": "1"},
    }


def test_export_to_explicit_path_with_names(tmp_path):
    write_claude_json({"sentry": SENTRY, "atlassian": ATLASSIAN})
    target = tmp_path / "out" / "p.json"
    assert mp.export_profile(target, names=["atlassian"]) == target
    assert json.loads(target.read_text())["servers"] == {"atlassian": ATLASSIAN}
    assert not paths.mcp_profile_file().exists()


def test_export_errors():
    with pytest.raises(mp.McpProfileError, match="No user-scope MCP servers"):
        mp.export_profile()
    write_claude_json({"sentry": SENTRY})
    with pytest.raises(mp.McpProfileError, match="nope"):
        mp.export_profile(names=["nope"])


def test_load_profile_errors_and_missing_ok(tmp_path):
    missing = tmp_path / "none.json"
    with pytest.raises(mp.McpProfileError, match="No MCP profile"):
        mp.load_profile(missing)
    assert mp.load_profile(missing, missing_ok=True) == {"version": 1, "servers": {}}

    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(mp.McpProfileError, match="not valid JSON"):
        mp.load_profile(bad)
    bad.write_text(json.dumps({"version": 2, "servers": {}}))
    with pytest.raises(mp.McpProfileError, match="version"):
        mp.load_profile(bad)
    bad.write_text(json.dumps({"version": 1, "servers": {"-rf": SLACK}}))
    with pytest.raises(mp.McpProfileError, match="Invalid MCP server name"):
        mp.load_profile(bad)
    bad.write_text(json.dumps({"version": 1, "servers": {"s": {"type": "http"}}}))
    with pytest.raises(mp.McpProfileError, match="needs a 'url'"):
        mp.load_profile(bad)


def test_save_then_load_round_trip(tmp_path):
    target = tmp_path / "p.json"
    profile = {"version": 1, "servers": {"slack": SLACK, "github": GITHUB_PROFILE}}
    assert mp.save_profile(profile, target) == target
    assert mp.load_profile(target) == profile


def test_save_profile_refuses_literal_secret(tmp_path):
    leaky = {"version": 1, "servers": {"gh": {**GITHUB_PROFILE, "headers": {"Authorization": f"Bearer {PAT}"}}}}
    target = tmp_path / "p.json"
    with pytest.raises(mp.McpProfileError, match="literal secret"):
        mp.save_profile(leaky, target)
    assert not target.exists()


def test_add_to_profile_from_catalog(tmp_path):
    target = tmp_path / "p.json"
    profile = mp.add_to_profile(["slack", "github"], target)
    assert profile["servers"]["slack"] == SLACK  # "note" is not part of the server config
    assert profile["servers"]["github"] == GITHUB_PROFILE
    assert json.loads(target.read_text()) == profile
    # merges with what is already there
    again = mp.add_to_profile(["sentry"], target)
    assert set(again["servers"]) == {"slack", "github", "sentry"}


def test_add_to_profile_unknown_name(tmp_path):
    target = tmp_path / "p.json"
    with pytest.raises(mp.McpProfileError, match="mystery"):
        mp.add_to_profile(["slack", "mystery"], target)
    assert not target.exists()


def test_add_to_profile_default_path():
    mp.add_to_profile(["linear"])
    assert "linear" in json.loads(paths.mcp_profile_file().read_text())["servers"]


# --------------------------------------------------------------------------- plan_apply


def profile_of(**servers):
    return {"version": 1, "servers": servers}


def test_plan_add_update_unchanged_and_prune():
    profile = profile_of(slack=SLACK, sentry=SENTRY, atlassian=ATLASSIAN)
    current = {
        "sentry": SENTRY,
        "atlassian": {"type": "http", "url": "https://mcp.atlassian.com/old"},
        "stray": {"command": "node"},
    }
    plan = {c.name: c for c in mp.plan_apply(profile, current)}
    assert plan["slack"].action == "add" and plan["slack"].config == SLACK
    assert plan["sentry"].action == "unchanged"
    assert plan["atlassian"].action == "update"
    assert "sign in again" in plan["atlassian"].reason  # URL changed -> new token key
    assert "stray" not in plan  # no prune -> never removed

    pruned = {c.name: c for c in mp.plan_apply(profile, current, prune=True)}
    assert pruned["stray"].action == "remove" and pruned["stray"].config is None


def test_plan_update_without_key_change_does_not_warn_about_sign_in():
    current = {"srv": {"type": "http", "url": "https://x.dev/mcp", "oauth": {"callbackPort": 1}}}
    plan = mp.plan_apply(profile_of(srv={"type": "http", "url": "https://x.dev/mcp"}), current)
    assert plan[0].action == "update"
    assert plan[0].reason == "config differs; existing sign-in is kept"
    assert "sign in again" not in plan[0].reason


def test_plan_compares_normalized_configs():
    current = {
        "sentry": {"type": "http", "url": SENTRY["url"], "headers": {}},
        "local": {"command": "node", "args": [], "env": {}},
    }
    profile = profile_of(sentry=SENTRY, local={"type": "stdio", "command": "node"})
    assert [c.action for c in mp.plan_apply(profile, current)] == ["unchanged", "unchanged"]


def test_plan_skips_unresolved_placeholder(monkeypatch):
    monkeypatch.delenv("CCS_GITHUB_AUTHORIZATION", raising=False)
    (change,) = mp.plan_apply(profile_of(github=GITHUB_PROFILE), {})
    assert change.action == "skip"
    assert change.reason == "set $CCS_GITHUB_AUTHORIZATION"
    monkeypatch.setenv("CCS_GITHUB_AUTHORIZATION", "")  # empty counts as unset
    assert mp.plan_apply(profile_of(github=GITHUB_PROFILE), {})[0].action == "skip"


def test_plan_resolves_placeholder_before_comparing(monkeypatch):
    monkeypatch.setenv("CCS_GITHUB_AUTHORIZATION", PAT)
    live = {"github": {**GITHUB_PROFILE, "headers": {"Authorization": f"Bearer {PAT}"}}}
    (change,) = mp.plan_apply(profile_of(github=GITHUB_PROFILE), live)
    assert change.action == "unchanged"
    assert PAT not in json.dumps(change.config)  # Change keeps the placeholder form

    live["github"]["headers"]["Authorization"] = "Bearer ghp_OLD"
    assert mp.plan_apply(profile_of(github=GITHUB_PROFILE), live)[0].action == "update"


def test_plan_leaves_foreign_references_for_claude_code(monkeypatch):
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    cfg = {**GITHUB_PROFILE, "headers": {"Authorization": "Bearer ${GITHUB_PAT}"}}
    assert mp.plan_apply(profile_of(github=cfg), {})[0].action == "add"  # not a CCS_ placeholder


# --------------------------------------------------------------------------- apply_profile


def test_apply_add_uses_claude_cli_with_user_scope(fake_claude):
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(slack=SLACK), runner=runner)
    assert [c.action for c in result] == ["add"]
    assert runner.calls == [
        ["/fake/bin/claude", "mcp", "add-json", "--scope", "user", "slack", json.dumps(SLACK, separators=(",", ":"))]
    ]


def test_apply_never_writes_claude_json(fake_claude):
    write_claude_json({"stray": SENTRY})
    before = paths.claude_json().read_bytes()
    mp.apply_profile(profile_of(slack=SLACK), prune=True, runner=FakeRunner())
    assert paths.claude_json().read_bytes() == before


def test_apply_update_is_remove_then_add(fake_claude):
    write_claude_json({"atlassian": {"type": "http", "url": "https://mcp.atlassian.com/old"}})
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(atlassian=ATLASSIAN), runner=runner)
    assert [c.action for c in result] == ["update"]
    assert [c[1:3] for c in runner.calls] == [["mcp", "remove"], ["mcp", "add-json"]]
    assert runner.calls[0] == ["/fake/bin/claude", "mcp", "remove", "--scope", "user", "atlassian"]


def test_apply_prune_removes_only_unlisted(fake_claude):
    write_claude_json({"sentry": SENTRY, "stray": ATLASSIAN})
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(sentry=SENTRY), prune=True, runner=runner)
    assert [(c.action, c.name) for c in result] == [("unchanged", "sentry"), ("remove", "stray")]
    assert runner.calls == [["/fake/bin/claude", "mcp", "remove", "--scope", "user", "stray"]]


def test_apply_without_prune_keeps_extra_servers(fake_claude):
    write_claude_json({"sentry": SENTRY, "stray": ATLASSIAN})
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(sentry=SENTRY), runner=runner)
    assert [c.action for c in result] == ["unchanged"]
    assert runner.calls == []


def test_apply_dry_run_runs_nothing(monkeypatch):
    write_claude_json({"stray": SENTRY})
    monkeypatch.setattr(mp, "find_claude", lambda: pytest.fail("dry run must not look for claude"))
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(slack=SLACK), prune=True, dry_run=True, runner=runner)
    assert [(c.action, c.name) for c in result] == [("add", "slack"), ("remove", "stray")]
    assert runner.calls == []


def test_apply_resolves_placeholder_into_argv_but_not_into_result(fake_claude, monkeypatch):
    monkeypatch.setenv("CCS_GITHUB_AUTHORIZATION", PAT)
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(github=GITHUB_PROFILE), runner=runner)
    sent = json.loads(runner.calls[0][-1])
    assert sent["headers"] == {"Authorization": f"Bearer {PAT}"}
    assert result[0].action == "add"
    assert PAT not in json.dumps([c.__dict__ for c in result])


def test_apply_skips_unresolved_placeholder_and_still_applies_the_rest(fake_claude, monkeypatch):
    monkeypatch.delenv("CCS_GITHUB_AUTHORIZATION", raising=False)
    runner = FakeRunner()
    result = mp.apply_profile(profile_of(github=GITHUB_PROFILE, slack=SLACK), runner=runner)
    assert [(c.action, c.name) for c in result] == [("skip", "github"), ("add", "slack")]
    assert result[0].reason == "set $CCS_GITHUB_AUTHORIZATION"
    assert len(runner.calls) == 1 and runner.calls[0][5] == "slack"


def test_apply_reports_cli_failure_and_redacts_secret(fake_claude, monkeypatch):
    monkeypatch.setenv("CCS_GITHUB_AUTHORIZATION", PAT)
    runner = FakeRunner((1, "", f"Error: bad header Bearer {PAT}\nsecond line"))
    result = mp.apply_profile(profile_of(github=GITHUB_PROFILE), runner=runner)
    assert result[0].action == "skip"
    assert result[0].reason.startswith(mp.FAILED_PREFIX)
    assert PAT not in result[0].reason and "***" in result[0].reason
    assert mp.has_failures(result)


def test_apply_failure_without_output_reports_exit_code(fake_claude):
    result = mp.apply_profile(profile_of(slack=SLACK), runner=FakeRunner((3, "", "")))
    assert result[0].reason == "failed: exit 3"


def test_apply_update_restores_previous_config_when_readd_fails(fake_claude):
    old = {"type": "http", "url": "https://mcp.atlassian.com/old"}
    write_claude_json({"atlassian": old})
    runner = FakeRunner((0, "", ""), (1, "", "boom"), (0, "", ""))  # remove ok, add fails, restore ok
    result = mp.apply_profile(profile_of(atlassian=ATLASSIAN), runner=runner)
    assert result[0].action == "skip" and "previous config restored" in result[0].reason
    assert [c[1:3] for c in runner.calls] == [["mcp", "remove"], ["mcp", "add-json"], ["mcp", "add-json"]]
    assert json.loads(runner.calls[2][-1]) == old


def test_apply_update_stops_when_remove_fails(fake_claude):
    write_claude_json({"atlassian": {"type": "http", "url": "https://mcp.atlassian.com/old"}})
    runner = FakeRunner((1, "", "cannot remove"))
    result = mp.apply_profile(profile_of(atlassian=ATLASSIAN), runner=runner)
    assert result[0].action == "skip" and "cannot remove" in result[0].reason
    assert len(runner.calls) == 1  # nothing was added on top


def test_apply_loads_default_profile_file(fake_claude):
    mp.save_profile(profile_of(slack=SLACK))
    runner = FakeRunner()
    assert [c.action for c in mp.apply_profile(runner=runner)] == ["add"]


def test_apply_with_missing_profile_file_is_an_error(fake_claude, tmp_path):
    with pytest.raises(mp.McpProfileError, match="No MCP profile"):
        mp.apply_profile(mp.load_profile(tmp_path / "typo.json"), runner=FakeRunner())
    with pytest.raises(mp.McpProfileError, match="No MCP profile"):
        mp.apply_profile(runner=FakeRunner())


def test_apply_refuses_to_prune_everything_with_empty_profile(fake_claude):
    write_claude_json({"sentry": SENTRY})
    runner = FakeRunner()
    with pytest.raises(mp.McpProfileError, match="Refusing --prune"):
        mp.apply_profile(profile_of(), prune=True, runner=runner)
    assert runner.calls == []


def test_apply_without_claude_binary_is_an_error(monkeypatch):
    monkeypatch.setattr(mp, "find_claude", lambda: None)
    with pytest.raises(mp.McpProfileError, match="claude"):
        mp.apply_profile(profile_of(slack=SLACK))  # no runner injected -> needs a real binary
    # nothing to do -> no binary needed
    write_claude_json({"sentry": SENTRY})
    assert mp.apply_profile(profile_of(sentry=SENTRY))[0].action == "unchanged"


def test_default_runner_wraps_subprocess(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 2, stdout="out", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mp._default_runner(["claude", "mcp", "list"]) == (2, "out", "err")
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL and seen["kwargs"]["capture_output"] is True

    def missing(argv, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", missing)
    assert mp._default_runner(["claude"])[0] == 127


# --------------------------------------------------------------------------- find_claude


def test_mcp_uses_the_same_explicit_claude_binary(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CC_SWITCHER_CLAUDE_PATH", str(binary))
    monkeypatch.setattr(mp.shutil, "which", lambda name: "/older/bin/claude")
    assert mp.find_claude() == str(binary)


def test_find_claude_prefers_path_then_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(mp.shutil, "which", lambda name: "/on/path/claude")
    assert mp.find_claude() == "/on/path/claude"

    monkeypatch.setattr(mp.shutil, "which", lambda name: None)
    local = paths.home() / ".local" / "bin" / "claude"
    monkeypatch.setattr(mp, "_claude_candidates", lambda: [local, tmp_path / "nope"])
    assert mp.find_claude() is None
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert mp.find_claude() is None  # not executable
    local.chmod(0o755)
    assert mp.find_claude() == str(local)


def test_claude_candidates_order():
    cands = [str(p) for p in mp._claude_candidates()]
    assert cands == [str(paths.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]


# --------------------------------------------------------------------------- status


def test_status_combines_profile_config_and_keychain(fake_keychain, samples, monkeypatch):
    fake_keychain.put_json(paths.claude_keychain_service(), {"mcpOAuth": samples.SAMPLE_MCP_OAUTH})
    write_claude_json(
        {
            "sentry": SENTRY,  # signed in
            "atlassian": ATLASSIAN,  # empty stub
            "github": {**GITHUB_PROFILE, "headers": {"Authorization": f"Bearer {PAT}"}},
            "local": {"command": "node"},
            "extra": {"type": "http", "url": "https://x.dev/mcp"},  # configured, not in profile
        }
    )
    mp.save_profile(profile_of(sentry=SENTRY, atlassian=ATLASSIAN, github=GITHUB_PROFILE, slack=SLACK, local={"command": "node"}))
    rows = {r.name: r for r in mp.status()}
    assert list(r.name for r in mp.status()) == sorted(rows, key=str.lower)

    assert rows["sentry"] == mp.ServerStatus("sentry", "http", SENTRY["url"], True, True, "signed_in")
    assert rows["atlassian"].auth == "needs_sign_in"
    assert rows["github"].auth == "header_auth"
    assert rows["local"] == mp.ServerStatus("local", "stdio", None, True, True, "not_oauth")
    assert rows["slack"] == mp.ServerStatus("slack", "http", SLACK["url"], True, False, "not_configured")
    assert rows["extra"].in_profile is False and rows["extra"].configured is True
    assert rows["extra"].auth == "needs_sign_in"
    assert PAT not in repr(list(rows.values()))


def test_status_with_explicit_profile_path_and_no_profile(tmp_path, fake_keychain):
    write_claude_json({"sentry": SENTRY})
    # no live login item at all -> every OAuth server needs sign-in here
    (row,) = mp.status()
    assert (row.in_profile, row.configured, row.auth) == (False, True, "needs_sign_in")

    other = tmp_path / "other.json"
    mp.save_profile(profile_of(slack=SLACK), other)
    assert {r.name: r.in_profile for r in mp.status(other)} == {"sentry": False, "slack": True}


def test_status_follows_claude_config_dir_keychain_service(fake_keychain, tmp_path, monkeypatch):
    cfg = tmp_path / "alt"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    (cfg / ".claude.json").write_text(json.dumps({"mcpServers": {"sentry": SENTRY}}))
    key = mp.server_key("sentry", SENTRY)
    fake_keychain.put_json(paths.claude_keychain_service(), {"mcpOAuth": {key: {"accessToken": "t"}}})
    assert os.environ["CLAUDE_CONFIG_DIR"] and paths.claude_keychain_service() != "Claude Code-credentials"
    assert mp.status()[0].auth == "signed_in"


# --------------------------------------------------------------------------- sign-in survives `claude mcp remove`
#
# Real `claude mcp remove` deletes that server's entries from the Keychain blob. These tests stand in
# for it with CleaningRunner, so the fake keychain is wiped exactly where Claude Code would wipe it.

SENTRY_KEY = "sentry|800cb29a3ac61727"
SENTRY_FILE_CONFIG = {"type": "http", "url": "https://mcp.sentry.dev/mcp"}


class CleaningRunner(FakeRunner):
    """FakeRunner whose ``remove`` deletes ``<name>|*`` from mcpOAuth/mcpOAuthClientConfig, like Claude Code."""

    def __init__(self, fake, *results, on_add=None):
        super().__init__(*results)
        self.fake = fake
        self.on_add = on_add
        self.lock_free_during_calls: list[bool] = []

    def __call__(self, argv):
        # apply_profile must not hold the app lock across a `claude` call (the lock is not re-entrant).
        try:
            with fsutil.app_lock(timeout=0.5):
                self.lock_free_during_calls.append(True)
        except fsutil.LockTimeout:
            self.lock_free_during_calls.append(False)
        rc, out, err = super().__call__(argv)
        service = paths.claude_keychain_service()
        if argv[1:3] == ["mcp", "remove"] and rc == 0:
            blob = self.fake.get_json(service)
            for store in ("mcpOAuth", "mcpOAuthClientConfig"):
                for key in [k for k in blob.get(store, {}) if k.startswith(argv[-1] + "|")]:
                    del blob[store][key]
            self.fake.put_json(service, blob)
        if argv[1:3] == ["mcp", "add-json"] and rc == 0 and self.on_add:
            blob = self.fake.get_json(service)
            self.on_add(blob)
            self.fake.put_json(service, blob)
        return rc, out, err


@pytest.fixture
def signed_in(fake_keychain, samples):
    """Live blob with a login, a signed-in sentry, an unsigned atlassian stub, client config and an unknown key."""
    blob = {
        "claudeAiOauth": samples.make_login(samples.ACCOUNT_A),
        "mcpOAuth": copy.deepcopy(samples.SAMPLE_MCP_OAUTH),
        "mcpOAuthClientConfig": {SENTRY_KEY: {"clientId": "cid", "clientSecret": "fake-client-secret"}},
        "futureKey": {"x": 1},
    }
    fake_keychain.put_json(paths.claude_keychain_service(), blob)
    write_claude_json({"sentry": SENTRY_FILE_CONFIG})
    return blob


def live(fake_keychain) -> dict:
    return fake_keychain.get_json(paths.claude_keychain_service())


def sentry_with(**extra) -> dict:
    return profile_of(sentry={**SENTRY_FILE_CONFIG, **extra})


def test_same_key_update_keeps_the_sign_in_byte_for_byte(fake_claude, fake_keychain, signed_in):
    # oauth.callbackPort is not part of the token key, so sentry|800cb29a3ac61727 stays valid.
    profile = sentry_with(oauth={"callbackPort": 8080})
    assert mp.plan_apply(profile, mp.list_user_servers())[0].reason == "config differs; existing sign-in is kept"

    runner = CleaningRunner(fake_keychain)
    result = mp.apply_profile(profile, runner=runner)

    assert [c.action for c in result] == ["update"]
    assert [c[1:3] for c in runner.calls] == [["mcp", "remove"], ["mcp", "add-json"]]
    after = live(fake_keychain)
    # remove wiped the entries; they are back, identical, and nothing else moved
    assert after == signed_in
    assert json.dumps(after["mcpOAuth"][SENTRY_KEY], sort_keys=True) == json.dumps(
        signed_in["mcpOAuth"][SENTRY_KEY], sort_keys=True
    )
    assert creds.non_login_parts(after) == creds.non_login_parts(signed_in)
    assert after["claudeAiOauth"] == signed_in["claudeAiOauth"]
    assert mp.auth_state("sentry", {**SENTRY_FILE_CONFIG, "oauth": {}}, after) == "signed_in"
    # one Keychain write, to the live service, with the existing acct attribute (put_json default "tester")
    assert fake_keychain.writes == [paths.claude_keychain_service()]
    assert fake_keychain.items[paths.claude_keychain_service()][0] == "tester"
    assert runner.lock_free_during_calls == [True, True]


def test_same_key_update_writes_nothing_when_the_entry_survived(fake_claude, fake_keychain, signed_in):
    class KeepingRunner(FakeRunner):  # a remove that leaves the Keychain alone
        pass

    runner = KeepingRunner()
    mp.apply_profile(sentry_with(oauth={"callbackPort": 8080}), runner=runner)
    assert fake_keychain.writes == []
    assert live(fake_keychain) == signed_in


def test_changed_url_is_not_restored_and_says_sign_in_again(fake_claude, fake_keychain, signed_in):
    # A different URL gives sentry a new key. The old token is invalid for it, so it stays deleted.
    moved = {**SENTRY_FILE_CONFIG, "url": "https://sentry.example.com/mcp"}
    runner = CleaningRunner(fake_keychain)
    result = mp.apply_profile(profile_of(sentry=moved), runner=runner)

    assert result[0].action == "update" and "sign in again" in result[0].reason
    after = live(fake_keychain)
    assert SENTRY_KEY not in after["mcpOAuth"] and SENTRY_KEY not in after["mcpOAuthClientConfig"]
    assert mp.server_key("sentry", moved) not in after["mcpOAuth"]
    assert mp.auth_state("sentry", moved, after) == "needs_sign_in"
    # unrelated entries, the login and unknown keys are untouched; no Keychain write happened
    assert after["mcpOAuth"]["atlassian|1eb778bd626fb68d"] == signed_in["mcpOAuth"]["atlassian|1eb778bd626fb68d"]
    assert after["claudeAiOauth"] == signed_in["claudeAiOauth"] and after["futureKey"] == {"x": 1}
    assert fake_keychain.writes == []


def test_changed_headers_is_also_a_key_change(fake_claude, fake_keychain, signed_in, monkeypatch):
    monkeypatch.setenv("CCS_SENTRY_X_ORG_KEY", "org-1")
    cfg = {**SENTRY_FILE_CONFIG, "headers": {"X-Org-Key": "${CCS_SENTRY_X_ORG_KEY}"}}
    result = mp.apply_profile(profile_of(sentry=cfg), runner=CleaningRunner(fake_keychain))
    assert "sign in again" in result[0].reason
    assert SENTRY_KEY not in live(fake_keychain)["mcpOAuth"]
    assert fake_keychain.writes == []


def test_restore_keeps_what_changed_in_the_keychain_meanwhile(fake_claude, fake_keychain, signed_in):
    # While `add-json` runs, Claude Code refreshes the atlassian token. The restore reads the blob fresh
    # (I1), so that newer value must survive alongside the restored sentry entry.
    def refresh_atlassian(blob):
        blob["mcpOAuth"]["atlassian|1eb778bd626fb68d"]["accessToken"] = "fake-atlassian-newer"

    runner = CleaningRunner(fake_keychain, on_add=refresh_atlassian)
    mp.apply_profile(sentry_with(oauth={"callbackPort": 1}), runner=runner)
    after = live(fake_keychain)
    assert after["mcpOAuth"][SENTRY_KEY] == signed_in["mcpOAuth"][SENTRY_KEY]
    assert after["mcpOAuth"]["atlassian|1eb778bd626fb68d"]["accessToken"] == "fake-atlassian-newer"


def test_restore_replaces_an_empty_stub_that_would_shadow_the_tokens(fake_claude, fake_keychain, signed_in):
    def write_stub(blob):
        blob["mcpOAuth"][SENTRY_KEY] = {"serverName": "sentry", "accessToken": ""}

    runner = CleaningRunner(fake_keychain, on_add=write_stub)
    mp.apply_profile(sentry_with(oauth={"callbackPort": 1}), runner=runner)
    assert live(fake_keychain)["mcpOAuth"][SENTRY_KEY] == signed_in["mcpOAuth"][SENTRY_KEY]


def test_restore_failure_is_reported_not_hidden(fake_claude, fake_keychain, signed_in):
    fake_keychain.fail_writes_for.add(paths.claude_keychain_service())
    runner = CleaningRunner(fake_keychain)
    result = mp.apply_profile(sentry_with(oauth={"callbackPort": 1}), runner=runner)

    assert result[0].action == "skip"
    assert result[0].reason.startswith("re-sign-in needed: sentry")
    assert mp.has_failures(result)
    for secret in ("fake-sentry-access", "fake-sentry-refresh", "fake-client-secret"):
        assert secret not in repr(result)


def test_unreadable_keychain_blocks_the_update_before_anything_changes(fake_claude, signed_in, monkeypatch):
    def boom():
        raise keychain.KeychainError("Keychain read failed for 'Claude Code-credentials': locked")

    monkeypatch.setattr(mp, "_live_blob", boom)
    runner = FakeRunner()
    result = mp.apply_profile(sentry_with(oauth={"callbackPort": 1}), runner=runner)
    assert result[0].action == "skip" and result[0].reason.startswith(mp.FAILED_PREFIX)
    assert "nothing changed" in result[0].reason
    assert runner.calls == []  # the server was neither removed nor re-added


def test_rollback_after_failed_readd_also_restores_the_sign_in(fake_claude, fake_keychain, signed_in):
    # New URL, but the add fails: the old config goes back, and so must its token (old key again valid).
    moved = {**SENTRY_FILE_CONFIG, "url": "https://sentry.example.com/mcp"}
    runner = CleaningRunner(fake_keychain, (0, "", ""), (1, "", "boom"), (0, "", ""))
    result = mp.apply_profile(profile_of(sentry=moved), runner=runner)

    assert result[0].action == "skip" and "previous config restored" in result[0].reason
    assert "re-sign-in" not in result[0].reason
    assert live(fake_keychain) == signed_in


def test_rollback_reports_resign_when_token_restore_fails(fake_claude, fake_keychain, signed_in):
    fake_keychain.fail_writes_for.add(paths.claude_keychain_service())
    moved = {**SENTRY_FILE_CONFIG, "url": "https://sentry.example.com/mcp"}
    runner = CleaningRunner(fake_keychain, (0, "", ""), (1, "", "boom"), (0, "", ""))
    result = mp.apply_profile(profile_of(sentry=moved), runner=runner)
    assert "previous config restored" in result[0].reason and "re-sign-in needed: sentry" in result[0].reason
    assert mp.has_failures(result)


def test_prune_loses_the_removed_servers_token_by_design(fake_claude, fake_keychain, signed_in):
    write_claude_json({"sentry": SENTRY_FILE_CONFIG, "atlassian": ATLASSIAN})  # atlassian is not in the profile
    runner = CleaningRunner(fake_keychain)
    result = mp.apply_profile(profile_of(sentry=SENTRY_FILE_CONFIG), prune=True, runner=runner)
    assert [(c.action, c.name) for c in result] == [("unchanged", "sentry"), ("remove", "atlassian")]
    after = live(fake_keychain)
    assert "atlassian|1eb778bd626fb68d" not in after["mcpOAuth"]  # Claude Code deleted it with the server
    assert after["mcpOAuth"][SENTRY_KEY] == signed_in["mcpOAuth"][SENTRY_KEY]  # kept servers keep theirs
    assert fake_keychain.writes == []  # nothing is restored for a removal


def test_add_and_unchanged_never_touch_the_keychain(fake_claude, fake_keychain, signed_in, monkeypatch):
    monkeypatch.setattr(mp, "_live_blob", lambda: pytest.fail("no Keychain access expected"))
    result = mp.apply_profile(profile_of(sentry=SENTRY_FILE_CONFIG, slack=SLACK), runner=FakeRunner())
    assert [c.action for c in result] == ["unchanged", "add"]
    assert fake_keychain.writes == []
