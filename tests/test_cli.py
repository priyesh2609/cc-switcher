"""CLI parsing, dispatch, exit codes, error formatting and table rendering.

The WP-A/B/C modules the CLI imports lazily (switcher, monitor, usage, autoswitch, mcp_profile,
launchagent, menubar) are replaced by scriptable fakes, so these tests exercise only the CLI.
The foundation modules (store, models, fsutil, paths, creds, keychain) are real and run against
the tmp home and fake Keychain that conftest sets up.
"""

from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import cc_switcher
from cc_switcher import cli, creds, fsutil, keychain, menubar, models, paths, store
from cc_switcher import monitor as real_monitor  # the fixture below replaces the module cli imports; this is the real one
from cc_switcher import usage as real_usage

FAR = datetime(2030, 1, 1, tzinfo=timezone.utc)  # the fake format_reset turns any future time into "3h 05m"
PAST = datetime(1999, 1, 1, tzinfo=timezone.utc)

ID_A = "aaaaaaaa-0000-0000-0000-000000000001:0000000a-0000-0000-0000-00000000000a"
ID_B = "bbbbbbbb-0000-0000-0000-000000000002:0000000b-0000-0000-0000-00000000000b"


# --------------------------------------------------------------------------- builders


def make_account(account_id=ID_A, email="alice@acme.com", label="work", org="Acme", plan="max") -> models.Account:
    uuid, org_uuid = account_id.split(":")
    return models.Account(
        id=account_id,
        email=email,
        account_uuid=uuid,
        organization_uuid=org_uuid,
        organization_name=org,
        subscription_type=plan,
        label=label,
        added_at="2026-01-01T00:00:00Z",
    )


def make_snapshot(
    account_id=ID_A,
    *,
    ok=True,
    error=None,
    session=42.0,
    weekly=18.0,
    reset=FAR,
    fetched_ago=None,
    extra=True,
    promo=True,
    extra_windows=(),
    resets=None,
) -> models.UsageSnapshot:
    fetched_at = models.utcnow() - (fetched_ago or timedelta(0))
    if not ok:
        return models.UsageSnapshot(account_id, fetched_at, False, error=error)
    windows = [
        models.UsageWindow("five_hour", "Session (5h)", session, reset),
        models.UsageWindow("seven_day", "Weekly", weekly, reset),
        *extra_windows,
    ]
    return models.UsageSnapshot(
        account_id,
        fetched_at,
        True,
        windows=tuple(windows),
        extra_usage=models.ExtraUsage(True, 10000, 1250, 12.5, "USD") if extra else None,
        promos=(models.PromoCredit("wattle", "Launch promo", 20.0, 15.0, 5.0, 75.0, None, ends_at=FAR),) if promo else (),
        resets=resets,
    )


USE_BY = datetime(2030, 12, 3, 12, 0, tzinfo=timezone.utc)
USE_BY_TEXT = USE_BY.astimezone().strftime("%a %d %b")  # what the CLI prints, in the local timezone


def make_grant(left=1, type_="weekly", usable_now=False, requires_limit=True, ends=USE_BY, paused=False, total=2):
    return models.ResetGrant(
        id=f"g-{type_}-{left}", label="Weekly reset", type=type_, resets_total=total, resets_left=left,
        usable_now=usable_now, use_requires_limit=requires_limit, paused=paused, ends_at=ends,
    )


def make_resets(*grants, at_limit=False, eligible=True):
    return models.ResetStatus(eligible=eligible, at_limit=at_limit, grants=tuple(grants) or (make_grant(),))


class World:
    """Scriptable stand-ins for the modules the CLI imports lazily, plus a call log."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.live_id: str | None = None
        self.snapshots: dict[str, models.UsageSnapshot] = {}
        self.fresh: dict[str, models.UsageSnapshot] | None = None
        self.claude: str | None = "/usr/local/bin/claude"
        self.target: models.Account | None = None
        self.switch_error: Exception | None = None
        self.add_result: models.Account | None = None
        self.import_result: models.Account | None = None
        self.agent_installed = True
        self.agent_running = True
        self.menubar_rc = 0
        self.mcp_status: list = []
        self.mcp_changes: list = []
        self.catalog = {
            "slack": {"type": "http", "url": "https://mcp.slack.com/mcp", "note": "Browser sign-in (OAuth) via /mcp."},
            "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/", "note": "Needs a personal access token header."},
        }
        self.exported_profile_text = '{"servers": {"gh": {"headers": {"Authorization": "Bearer ${CCS_GH_AUTHORIZATION}"}}}}'
        self.refresh_snapshot: models.UsageSnapshot | None = None
        # account id -> (error code, when) for accounts whose most recent refresh failed (monitor.cache_status "stale")
        self.failed_refresh: dict[str, tuple[str, datetime]] = {}

    def log(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def args_of(self, name: str) -> tuple:
        return next(c[1] for c in self.calls if c[0] == name)

    # ---- module factories

    def build(self) -> dict[str, types.ModuleType]:
        w = self

        class SwitchError(RuntimeError):
            pass

        class McpProfileError(RuntimeError):
            pass

        switcher = types.ModuleType("cc_switcher.switcher")
        switcher.SwitchError = SwitchError

        def status():
            accounts = store.load_accounts()
            active = next((a for a in accounts if a.id == w.live_id), None)
            return SimpleNamespace(
                live_account_id=w.live_id,
                active=active,
                unregistered_live_login=w.live_id is not None and active is None,
                accounts=accounts,
            )

        def switch_to(account_id):
            w.log("switch_to", account_id)
            if w.switch_error:
                raise w.switch_error
            return store.get_account(account_id)

        def add_account(label="", runner=None):
            w.log("add_account", label)
            return w.add_result

        def import_current(label=""):
            w.log("import_current", label)
            return w.import_result

        def set_label(account_id, label):
            w.log("set_label", account_id, label)
            account = store.get_account(account_id)
            account.label = label
            return account

        switcher.status = status
        switcher.live_account_id = lambda: w.live_id
        switcher.active_account = lambda: status().active
        switcher.find_claude = lambda: w.claude
        switcher.save_active_login = lambda: w.log("save_active_login")
        switcher.switch_to = switch_to
        switcher.add_account = add_account
        switcher.import_current = import_current
        switcher.remove_account = lambda account_id, force=False: w.log("remove_account", account_id, force)
        switcher.set_label = set_label

        monitor = types.ModuleType("cc_switcher.monitor")
        monitor.load_cache = lambda: dict(w.snapshots)

        def refresh_all(accounts=None, **kwargs):
            w.log("refresh_all", [a.id for a in accounts or []])
            if w.fresh is not None:  # the real refresh_all writes the cache; cache_status reads it back
                w.snapshots = dict(w.fresh)
            return dict(w.snapshots)

        def cache_status(account_id, *, now=None):
            snapshot = w.snapshots.get(account_id)
            failed = w.failed_refresh.get(account_id)
            age = None
            if snapshot is not None and snapshot.ok:
                age = max(((now or models.utcnow()) - snapshot.fetched_at).total_seconds(), 0.0)
            return {
                "snapshot": snapshot,
                "age_seconds": age,
                "last_error": failed[0] if failed else None,
                "last_error_at": failed[1] if failed else None,
                "stale": failed is not None,
            }

        def refresh_account(account, **kwargs):
            w.log("refresh_account", account.id)
            return w.refresh_snapshot or make_snapshot(account.id)

        monitor.refresh_all = refresh_all
        monitor.cache_status = cache_status
        monitor.refresh_account = refresh_account
        monitor.snapshot_to_dict = lambda s: {"account_id": s.account_id, "ok": s.ok, "error": s.error}

        usage = types.ModuleType("cc_switcher.usage")
        usage.format_percent = lambda v: "—" if v is None else f"{v:.0f}%"
        usage.format_reset = lambda dt: "—" if dt is None else ("now" if dt < datetime(2000, 1, 1, tzinfo=timezone.utc) else "3h 05m")
        usage.format_money = lambda minor, cur: "—" if minor is None else (f"${minor / 100:,.2f}" if (cur or "USD") == "USD" else f"{minor / 100:.2f} {cur}")

        autoswitch = types.ModuleType("cc_switcher.autoswitch")

        def choose_target(active_id, accounts, snapshots, settings):
            w.log("choose_target", active_id, [a.id for a in accounts], dict(snapshots), settings)
            return w.target

        autoswitch.choose_target = choose_target
        autoswitch.should_switch = lambda active, settings: False

        mcp = types.ModuleType("cc_switcher.mcp_profile")
        mcp.McpProfileError = McpProfileError
        mcp.CATALOG = w.catalog
        mcp.status = lambda profile_path=None: list(w.mcp_status)

        def export_profile(path=None, names=None):
            w.log("export_profile", path, names)
            target = Path(path) if path else paths.mcp_profile_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(w.exported_profile_text)
            return target

        mcp.export_profile = export_profile
        mcp.add_to_profile = lambda names, path=None: w.log("add_to_profile", list(names), path) or {"servers": {}}
        mcp.load_profile = lambda path=None, **kw: w.log("load_profile", path) or {"servers": {"slack": {}}}

        def apply_profile(profile=None, *, prune=False, dry_run=False, runner=None):
            w.log("apply_profile", profile, prune, dry_run)
            return list(w.mcp_changes)

        mcp.apply_profile = apply_profile
        mcp.FAILED_PREFIX = "failed: "
        mcp.RESIGN_PREFIX = "re-sign-in needed: "
        mcp.has_failures = lambda changes: any(
            c.action == "skip" and c.reason.startswith((mcp.FAILED_PREFIX, mcp.RESIGN_PREFIX)) for c in changes
        )

        launchagent = types.ModuleType("cc_switcher.launchagent")
        launchagent.LaunchAgentError = type("LaunchAgentError", (RuntimeError,), {})
        launchagent.install = lambda *a, **k: w.log("agent_install")
        launchagent.uninstall = lambda *a, **k: w.log("agent_uninstall") or True
        launchagent.is_installed = lambda: w.agent_installed
        launchagent.is_running = lambda *a, **k: w.agent_running

        menubar_mod = types.ModuleType("cc_switcher.menubar")
        menubar_mod.main = lambda: w.log("menubar_main") or w.menubar_rc

        return {
            "switcher": switcher,
            "monitor": monitor,
            "usage": usage,
            "autoswitch": autoswitch,
            "mcp_profile": mcp,
            "launchagent": launchagent,
            "menubar": menubar_mod,
        }


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    for name, module in w.build().items():
        # sys.modules covers importlib.import_module (what cli uses); the package attribute covers
        # `from cc_switcher import usage` (what menubar uses).
        monkeypatch.setitem(sys.modules, f"cc_switcher.{name}", module)
        monkeypatch.setattr(cc_switcher, name, module, raising=False)
    return w


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def seed(*accounts):
    store.save_accounts(list(accounts))


def cells(line: str) -> list[str]:
    return re.split(r"\s{2,}", line.strip())


# --------------------------------------------------------------------------- parsing and exit codes


def test_version_and_help_work_even_if_every_module_is_broken(monkeypatch, capsys):
    for name in ("switcher", "monitor", "usage", "autoswitch", "mcp_profile", "launchagent", "menubar", "store", "keychain"):
        monkeypatch.setitem(sys.modules, f"cc_switcher.{name}", None)  # import raises ImportError
    code, out, _ = run(["--version"], capsys)
    assert code == 0 and out.strip() == f"ccs {cc_switcher.__version__}"
    code, out, _ = run(["--help"], capsys)
    assert code == 0 and "switch" in out and "doctor" in out


@pytest.mark.parametrize(
    "argv",
    [["frobnicate"], ["switch"], ["label", "only-one-arg"], ["mcp"], ["agent"], ["mcp", "add"], ["usage", "a", "b"], ["list", "--nope"]],
)
def test_bad_usage_exits_2(argv, world, capsys):
    code, out, err = run(argv, capsys)
    assert code == 2
    assert err and "Traceback" not in err


def test_no_command_means_list(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    code, out, _ = run([], capsys)
    assert code == 0
    assert "alice@acme.com" in out


# --------------------------------------------------------------------------- list


def test_list_table_rows_and_columns(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="", org="", plan="pro"))
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot(ID_A), ID_B: make_snapshot(ID_B, ok=False, error="auth_expired")}
    code, out, _ = run(["list"], capsys)
    lines = out.splitlines()

    assert code == 0
    assert cells(lines[0]) == ["LABEL", "EMAIL", "ORG", "PLAN", "SESSION", "WEEKLY", "NEXT RESET", "CREDITS", "LIMIT RESETS", "UPDATED"]
    assert cells(lines[1]) == ["◉", "work", "alice@acme.com", "Acme", "Max", "42%", "18%", "3h 05m", "$87.50 extra + $5.00 promo", "—", "just now"]
    assert cells(lines[2]) == ["○", "—", "bob@example.org", "—", "Pro", "—", "—", "sign-in expired", "—", "—", "—"]
    assert "Usage from cache (updated just now)" in out
    assert "ccs list --refresh" in out


def test_list_marks_the_account_matching_the_live_login_not_a_stored_flag(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_B
    _, out, _ = run(["list"], capsys)
    rows = {cells(line)[1]: cells(line)[0] for line in out.splitlines()[1:3]}
    assert rows == {"work": "○", "home": "◉"}


def test_list_without_data_says_so(world, capsys):
    seed(make_account())
    _, out, _ = run(["list"], capsys)
    assert "no data yet" in out
    assert "No usage data yet" in out


def test_list_hints_when_the_live_login_is_not_saved(world, capsys):
    seed(make_account())
    world.live_id = ID_B
    _, out, _ = run(["list"], capsys)
    assert "run `ccs import`" in out


def test_list_with_no_accounts_explains_how_to_start(world, capsys):
    code, out, _ = run(["list"], capsys)
    assert code == 0
    assert "No saved accounts yet" in out and "ccs import" in out and "ccs add" in out


def test_list_refresh_saves_the_live_login_then_fetches(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.fresh = {ID_A: make_snapshot(session=77.0)}
    code, out, _ = run(["list", "--refresh"], capsys)
    assert code == 0
    assert world.names() == ["save_active_login", "refresh_all"]
    assert world.args_of("refresh_all") == ([ID_A],)
    assert "77%" in out and "fetched just now" in out


def test_list_json_uses_snapshot_to_dict(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot(ID_A)}
    code, out, _ = run(["list", "--json"], capsys)
    data = json.loads(out)
    assert code == 0
    assert [row["account"]["email"] for row in data] == ["alice@acme.com", "bob@example.org"]
    assert [row["account"]["active"] for row in data] == [True, False]
    assert data[0]["usage"] == {"account_id": ID_A, "ok": True, "error": None}
    assert data[1]["usage"] is None
    assert "oauth_account" not in data[0]["account"]


def test_render_table_pads_columns_and_trims_trailing_space():
    text = cli.render_table(["A", "BB"], [["xxx", "y"], ["z", ""]])
    assert text.splitlines() == ["A    BB", "xxx  y", "z"]


# --------------------------------------------------------------------------- status and usage


def test_status_shows_every_window_credits_and_promo(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {
        ID_A: make_snapshot(extra_windows=(models.UsageWindow("seven_day_opus", "Weekly · Opus", 3.0, FAR),)),
    }
    code, out, _ = run(["status"], capsys)

    assert code == 0
    assert "Active account" in out
    assert "◉ work · alice@acme.com · Acme · Max" in out
    assert re.search(r"Session \(5h\)\s+42%\s+\S+\s+resets in 3h 05m \(", out)
    assert re.search(r"Weekly\s+18%\s+\S+\s+resets in 3h 05m \(", out)
    assert re.search(r"Weekly · Opus\s+3%", out)
    assert "Extra usage: $12.50 of $100.00 used · $87.50 left" in out
    assert "Promo credit (Launch promo): $5.00 remaining of $20.00 (ends in 3h 05m)" in out
    assert "Saved accounts: 1" in out


def test_status_states_without_an_active_account(world, capsys):
    seed(make_account())
    _, out, _ = run(["status"], capsys)
    assert "Not signed in to Claude Code" in out
    world.live_id = ID_B
    _, out, _ = run(["status"], capsys)
    assert "not saved in cc-switcher yet" in out and "ccs import" in out


def test_status_json(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot()}
    code, out, _ = run(["status", "--json"], capsys)
    data = json.loads(out)
    assert code == 0
    assert data["live_account_id"] == ID_A and data["signed_in"] is True
    assert data["active"]["email"] == "alice@acme.com"
    assert data["unregistered_live_login"] is False
    assert data["usage"]["account_id"] == ID_A


@pytest.mark.parametrize(
    ("code", "words"),
    [
        ("auth_expired", "sign-in expired — run `ccs add` again for this account"),
        ("no_credentials", "run `ccs add`"),
        ("token_stale", "Claude Code renews it"),
        ("rate_limited", "rate limited"),
        ("network", "check your internet connection"),
        ("bad_response", "cc-switcher update"),
        ("http_503", "HTTP 503"),
        ("keychain_error", "unlock your login keychain"),
        ("something_new", "something_new"),
    ],
)
def test_usage_errors_are_plain_words(world, capsys, code, words):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot(ok=False, error=code)}
    exit_code, out, _ = run(["usage"], capsys)
    assert exit_code == 0
    assert words in out
    assert "last tried" in out


def test_usage_block_without_a_snapshot(world, capsys):
    seed(make_account())
    _, out, _ = run(["usage"], capsys)
    assert "no usage data yet" in out and "ccs usage --refresh" in out


def test_usage_extra_credits_off_or_unlimited(world, capsys):
    seed(make_account())
    off = make_snapshot(extra=False)
    off = models.UsageSnapshot(
        off.account_id, off.fetched_at, True, windows=off.windows,
        extra_usage=models.ExtraUsage(False, None, None, None, "USD", "not_eligible"),
    )
    world.snapshots = {ID_A: off}
    _, out, _ = run(["usage"], capsys)
    assert "Extra usage: off (not eligible)" in out
    unlimited = models.UsageSnapshot(
        ID_A, models.utcnow(), True, windows=off.windows, extra_usage=models.ExtraUsage(True, None, 500, None, "USD"),
    )
    world.snapshots = {ID_A: unlimited}
    _, out, _ = run(["usage"], capsys)
    assert "Extra usage: on, $5.00 used, no monthly limit" in out


def test_usage_for_one_account_resolves_label_email_or_prefix(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.snapshots = {ID_A: make_snapshot(ID_A), ID_B: make_snapshot(ID_B, session=5.0)}
    code, out, _ = run(["usage", "home"], capsys)
    assert code == 0
    assert "bob@example.org" in out and "alice@acme.com" not in out
    assert "5%" in out
    _, out, _ = run(["usage", "alice"], capsys)
    assert "alice@acme.com" in out and "bob@example.org" not in out


def test_usage_refresh_all_and_json(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.fresh = {ID_A: make_snapshot(ID_A), ID_B: make_snapshot(ID_B, ok=False, error="network")}
    code, out, _ = run(["usage", "--refresh", "--json"], capsys)
    data = json.loads(out)
    assert code == 0
    assert world.names() == ["save_active_login", "refresh_all"]
    assert [row["usage"]["error"] for row in data] == [None, "network"]


def test_usage_unknown_account_is_a_one_line_error(world, capsys):
    seed(make_account())
    code, out, err = run(["usage", "nobody"], capsys)
    assert code == 1 and out == ""
    assert err.strip() == "ccs: No saved account matches 'nobody'. Run `ccs list`."


def test_ambiguous_account_is_a_one_line_error(world, capsys):
    seed(make_account(), make_account(ID_B, "alina@example.org", label="home"))
    code, _, err = run(["switch", "al"], capsys)
    assert code == 1
    assert err.startswith("ccs: 'al' matches several accounts") and err.count("\n") == 1
    assert "switch_to" not in world.names()


# --------------------------------------------------------------------------- switch / next


def test_switch_switches_and_explains_when_it_takes_effect(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_A
    code, out, _ = run(["switch", "home"], capsys)
    assert code == 0
    assert world.calls == [("switch_to", (ID_B,), {})]
    assert "Switched to home (bob@example.org)." in out
    assert "New `claude` sessions use this account immediately; running sessions pick it up shortly." in out


def test_switch_to_the_active_account_does_nothing(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    code, out, _ = run(["switch", "work"], capsys)
    assert code == 0 and "already the active account" in out
    assert world.calls == []


def test_next_refreshes_then_switches_to_the_chosen_target(world, capsys):
    other = make_account(ID_B, "bob@example.org", label="home")
    seed(make_account(), other)
    world.live_id = ID_A
    world.target = other
    world.fresh = {ID_A: make_snapshot(ID_A, session=99.0), ID_B: make_snapshot(ID_B, session=5.0, weekly=9.0)}
    code, out, _ = run(["next"], capsys)

    assert code == 0
    assert world.names() == ["save_active_login", "refresh_all", "choose_target", "switch_to"]
    active_id, account_ids, snapshots, settings = world.args_of("choose_target")
    assert active_id == ID_A and sorted(account_ids) == sorted([ID_A, ID_B])
    assert snapshots[ID_B].session.utilization == 5.0
    assert isinstance(settings, models.Settings)
    assert world.args_of("switch_to") == (ID_B,)
    assert "Switched to home (bob@example.org) (session 5%, weekly 9%)." in out
    assert "running sessions pick it up shortly" in out


def test_next_cached_skips_the_network(world, capsys):
    other = make_account(ID_B, "bob@example.org", label="home")
    seed(make_account(), other)
    world.live_id = ID_A
    world.target = other
    world.snapshots = {ID_B: make_snapshot(ID_B)}
    code, _, _ = run(["next", "--cached"], capsys)
    assert code == 0
    assert "refresh_all" not in world.names() and "save_active_login" not in world.names()


def test_next_with_no_candidate_fails_with_a_reason(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_A
    world.target = None
    code, out, err = run(["next"], capsys)
    assert code == 1 and out == ""
    assert "No other account has room right now" in err and "98%" in err
    assert "switch_to" not in world.names()


def test_next_needs_a_second_account(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    code, _, err = run(["next"], capsys)
    assert code == 1 and "no other saved account" in err
    assert "choose_target" not in world.names()


# --------------------------------------------------------------------------- add / import / remove / label


def test_add_runs_the_login_and_reports_the_new_account(world, capsys):
    world.add_result = make_account(ID_B, "bob@example.org", label="home")
    code, out, _ = run(["add", "--label", "home"], capsys)
    assert code == 0
    assert world.calls == [("add_account", ("home",), {})]
    assert "Saved home (bob@example.org). It is now the active account." in out
    assert "running sessions pick it up shortly" in out


def test_add_cancelled_login_exits_1_and_says_nothing_changed(world, capsys):
    world.add_result = None
    code, out, _ = run(["add"], capsys)
    assert code == 1
    assert "previous login is unchanged" in out


def test_add_without_claude_code_gives_the_install_hint(world, capsys):
    world.claude = None
    code, _, err = run(["add"], capsys)
    assert code == 1
    assert "Claude Code is not installed" in err and "claude.ai/install.sh" in err
    assert world.calls == []


def test_import_passes_the_label(world, capsys):
    world.import_result = make_account()
    code, out, _ = run(["import", "--label", "work"], capsys)
    assert code == 0
    assert world.calls == [("import_current", ("work",), {})]
    assert "Saved work (alice@acme.com) in Acme, Max." in out


def test_remove_forwards_force_and_never_claims_to_touch_the_claude_account(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    code, out, _ = run(["remove", "home"], capsys)
    assert code == 0 and world.calls == [("remove_account", (ID_B, False), {})]
    assert "The Claude account itself is untouched" in out
    world.calls.clear()
    run(["remove", "home", "--force"], capsys)
    assert world.calls == [("remove_account", (ID_B, True), {})]


def test_label_sets_and_clears(world, capsys):
    seed(make_account())
    code, out, _ = run(["label", "alice@acme.com", "day job"], capsys)
    assert code == 0 and world.calls == [("set_label", (ID_A, "day job"), {})]
    assert "alice@acme.com is now labelled 'day job'." in out
    _, out, _ = run(["label", "alice@acme.com", ""], capsys)
    assert "Cleared the label on alice@acme.com." in out


# --------------------------------------------------------------------------- error formatting


def test_known_errors_are_one_line_with_exit_1(world, capsys):
    seed(make_account())
    cases = [
        world_error(world, "switcher", "SwitchError", "Saved login for work is missing. Run `ccs add` again."),
        fsutil.LockTimeout("Another cc-switcher operation is still running. Try again in a moment."),
        keychain.KeychainError("Keychain did not answer in time."),
        creds.CredentialsError("Credentials item is not valid JSON"),
    ]
    for exc in cases:
        world.switch_error = exc
        code, out, err = run(["switch", "work"], capsys)
        assert code == 1 and out == ""
        assert err == f"ccs: {exc}\n"


def world_error(world, module, name, message):
    return getattr(sys.modules[f"cc_switcher.{module}"], name)(message)


def test_unexpected_errors_are_still_one_line(world, capsys):
    seed(make_account())
    world.switch_error = ZeroDivisionError("boom")
    code, _, err = run(["switch", "work"], capsys)
    assert code == 1
    assert err.startswith("ccs: unexpected ZeroDivisionError: boom") and "CCS_DEBUG=1" in err
    assert "Traceback" not in err


def test_ccs_debug_lets_the_traceback_through(world, monkeypatch):
    seed(make_account())
    monkeypatch.setenv("CCS_DEBUG", "1")
    world.switch_error = keychain.KeychainError("nope")
    with pytest.raises(keychain.KeychainError):
        cli.main(["switch", "work"])


def test_keyboard_interrupt_exits_130(world, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_list", lambda args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main([]) == 130


# --------------------------------------------------------------------------- settings


def test_settings_lists_shows_and_sets(world, capsys):
    code, out, _ = run(["settings"], capsys)
    assert code == 0
    assert "poll_interval_seconds = 300" in out
    assert "alert_thresholds      = 80,95,100" in out
    assert "auto_switch_enabled   = false" in out

    assert run(["settings", "menubar_title"], capsys)[1].strip() == "session"

    code, out, _ = run(["settings", "poll_interval_seconds", "600"], capsys)
    assert code == 0 and "poll_interval_seconds = 600" in out
    run(["settings", "auto_switch_enabled", "on"], capsys)
    run(["settings", "alert_thresholds", "95", "80", "100", "80"], capsys)
    run(["settings", "auto_switch_threshold", "90"], capsys)
    run(["settings", "menubar_title", "both"], capsys)
    run(["settings", "auto_switch_windows", "five_hour,seven_day_opus"], capsys)

    saved = store.load_settings()
    assert saved.poll_interval_seconds == 600
    assert saved.auto_switch_enabled is True
    assert saved.alert_thresholds == [80, 95, 100]
    assert saved.auto_switch_threshold == 90.0
    assert saved.menubar_title == "both"
    assert saved.auto_switch_windows == ["five_hour", "seven_day_opus"]
    assert saved.notifications_enabled is True  # untouched


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["nosuch", "1"], "unknown setting 'nosuch'"),
        (["poll_interval_seconds", "10"], "between 60 and 86400"),
        (["poll_interval_seconds", "soon"], "must be a number"),
        (["auto_switch_enabled", "maybe"], "true or false"),
        (["auto_switch_threshold", "150"], "between 1 and 100"),
        (["alert_thresholds", "0,50"], "between 1 and 100"),
        (["alert_thresholds", "high"], "whole numbers"),
        (["menubar_title", "huge"], "one of: none, session, weekly, both"),
        (["auto_switch_windows", "five_hour,bogus"], "usage windows from"),
        (["menubar_title", "weekly", "both"], "exactly one value"),
    ],
)
def test_invalid_settings_exit_2_and_change_nothing(world, capsys, argv, message):
    code, out, err = run(["settings", *argv], capsys)
    assert code == 2 and out == ""
    assert message in err
    assert store.load_settings() == models.Settings()


# --------------------------------------------------------------------------- mcp


def row(name, auth, configured=True, in_profile=True, url="https://mcp.example.com/mcp", type_="http"):
    return SimpleNamespace(name=name, type=type_, url=url, in_profile=in_profile, configured=configured, auth=auth)


def test_mcp_status_table_and_hints(world, capsys):
    world.mcp_status = [
        row("atlassian", "needs_sign_in"),
        row("sentry", "signed_in"),
        row("slack", "not_configured", configured=False),
        row("local-tool", "not_oauth", in_profile=False, url=None, type_="stdio"),
    ]
    code, out, _ = run(["mcp", "status"], capsys)
    lines = out.splitlines()
    assert code == 0
    assert cells(lines[0]) == ["SERVER", "TYPE", "IN PROFILE", "INSTALLED", "SIGN-IN", "URL"]
    assert cells(lines[1])[:5] == ["atlassian", "http", "yes", "yes", "needs sign-in"]
    assert cells(lines[2])[4] == "signed in"
    assert cells(lines[3])[2:5] == ["yes", "no", "not installed here"]
    assert cells(lines[4])[:5] == ["local-tool", "stdio", "no", "yes", "local (no sign-in)"]
    assert "type /mcp" in out and "ccs mcp apply" in out and "ccs mcp export" in out


def test_mcp_status_empty(world, capsys):
    code, out, _ = run(["mcp", "status"], capsys)
    assert code == 0 and "No MCP servers yet" in out


def test_mcp_catalog_lists_names_urls_and_notes(world, capsys):
    code, out, _ = run(["mcp", "catalog"], capsys)
    assert code == 0
    assert "slack  https://mcp.slack.com/mcp" in out
    assert "Needs a personal access token header." in out
    assert "ccs mcp add <names>" in out


def test_mcp_export_default_and_explicit_path(world, capsys, tmp_path):
    code, out, _ = run(["mcp", "export"], capsys)
    assert code == 0
    assert world.args_of("export_profile") == (None, None)
    assert str(paths.mcp_profile_file()) in out
    assert "CCS_GH_AUTHORIZATION" in out and "placeholders" in out

    target = tmp_path / "share" / "profile.json"
    run(["mcp", "export", str(target)], capsys)
    assert world.calls[-1] == ("export_profile", (target, None), {})
    assert target.exists()


def test_mcp_add_validates_names_against_the_catalog(world, capsys):
    code, _, err = run(["mcp", "add", "slack", "nope"], capsys)
    assert code == 2 and "unknown catalog server(s): nope" in err and "Known: slack, github" in err
    assert "add_to_profile" not in world.names()

    code, out, _ = run(["mcp", "add", "slack", "github"], capsys)
    assert code == 0
    assert world.args_of("add_to_profile") == (["slack", "github"], None)
    assert "Added to your MCP profile: slack, github" in out
    assert "github: Needs a personal access token header." in out  # unusual sign-in is called out
    assert "slack: Browser" not in out  # plain OAuth is not
    assert "ccs mcp apply" in out


def test_mcp_apply_forwards_flags_and_summarises(world, capsys):
    Change = SimpleNamespace
    world.mcp_changes = [
        Change(action="add", name="slack", config={"type": "http", "url": "https://mcp.slack.com/mcp"}, reason=""),
        Change(action="unchanged", name="sentry", config={"type": "http", "url": "https://mcp.sentry.dev/mcp"}, reason=""),
        Change(action="remove", name="old", config=None, reason="not in profile"),
    ]
    code, out, _ = run(["mcp", "apply", "--prune", "--dry-run"], capsys)
    assert code == 0
    assert world.args_of("apply_profile") == (None, True, True)
    assert "Dry run — nothing was changed" in out
    assert "+ add" in out and "https://mcp.slack.com/mcp" in out
    assert "- remove" in out and "not in profile" in out
    assert "1 add, 1 unchanged, 1 remove" in out
    assert "Next: run `claude`" not in out  # nothing was really applied

    world.calls.clear()
    code, out, _ = run(["mcp", "apply"], capsys)
    assert world.args_of("apply_profile") == (None, False, False)
    assert "Next: run `claude`, type /mcp" in out


def test_mcp_apply_with_a_path_loads_that_profile(world, capsys, tmp_path):
    profile_file = tmp_path / "p.json"
    code, _, _ = run(["mcp", "apply", str(profile_file)], capsys)
    assert code == 0
    assert world.calls[0] == ("load_profile", (profile_file,), {})
    assert world.args_of("apply_profile")[0] == {"servers": {"slack": {}}}


def test_mcp_apply_reports_skips_and_fails_on_real_failures(world, capsys):
    Change = SimpleNamespace
    world.mcp_changes = [Change(action="skip", name="github", config={}, reason="set $CCS_GITHUB_AUTHORIZATION")]
    code, out, _ = run(["mcp", "apply"], capsys)
    assert code == 0 and "need their ${CCS_...} variable" in out

    world.mcp_changes = [Change(action="skip", name="sentry", config={}, reason="re-sign-in needed: sentry")]
    code, out, _ = run(["mcp", "apply"], capsys)
    assert code == 1 and "sign in to sentry again" in out and "variable" not in out
    assert "re-sign-in needed: sentry" in out

    world.mcp_changes = [Change(action="skip", name="slack", config={}, reason="failed: claude exited 1")]
    code, out, _ = run(["mcp", "apply"], capsys)
    assert code == 1 and "failed: claude exited 1" in out


# --------------------------------------------------------------------------- menubar / agent


def test_menubar_returns_the_apps_exit_code(world, capsys):
    world.menubar_rc = 3
    assert cli.main(["menubar"]) == 3
    assert world.names() == ["menubar_main"]


def test_agent_commands(world, capsys):
    code, out, _ = run(["agent", "install"], capsys)
    assert code == 0 and world.names() == ["agent_install"]
    assert str(paths.launch_agent_plist()) in out and str(paths.log_dir()) in out

    code, out, _ = run(["agent", "uninstall"], capsys)
    assert code == 0 and "no longer starts at login" in out

    world.agent_installed, world.agent_running = True, False
    _, out, _ = run(["agent", "status"], capsys)
    assert "Start at login: on" in out and "not running" in out
    world.agent_installed = False
    _, out, _ = run(["agent", "status"], capsys)
    assert "Start at login: off" in out and "ccs agent install" in out


# --------------------------------------------------------------------------- doctor


@dataclass
class Env:
    world: World
    keychain: object
    samples: object


@pytest.fixture
def healthy(world, fake_keychain, samples, monkeypatch) -> Env:
    """A Mac where everything is fine; tests break one thing at a time."""
    login = samples.make_login(samples.ACCOUNT_A)
    fake_keychain.put_json(paths.claude_keychain_service(), {"claudeAiOauth": login, "mcpOAuth": {}})
    account = make_account(ID_A)
    seed(account)
    fake_keychain.put_json(account.keychain_service, {"claudeAiOauth": login})
    world.live_id = ID_A
    world.mcp_status = [row("sentry", "signed_in")]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.platform, "mac_ver", lambda: ("15.1", ("", "", ""), ""))
    monkeypatch.setattr(cli, "_run_command", lambda argv, timeout=15: SimpleNamespace(returncode=0, stdout="2.1.330 (Claude Code)\n", stderr=""))
    real_find_spec = cli.importlib.util.find_spec
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name, *a: object() if name == "rumps" else real_find_spec(name, *a))
    return Env(world, fake_keychain, samples)


def test_doctor_all_green(healthy, capsys):
    code, out, _ = run(["doctor"], capsys)
    assert code == 0
    assert "✓ macOS 15.1" in out
    assert "✓ Claude Code 2.1.330 (Claude Code) (/usr/local/bin/claude)" in out
    assert "✓ live Claude login found in the Keychain item 'Claude Code-credentials' (max)" in out
    assert "✓ live login is saved as work" in out
    assert "✓ saved logins present for all 1 account(s)" in out
    assert "is private (0700, files 0600)" in out
    assert "✓ menu bar app is installed and running" in out
    assert "✓ MCP sentry: signed in" in out
    assert "All checks passed." in out
    assert "✗" not in out and "fix:" not in out
    assert "refresh_account" not in healthy.world.names()  # offline by default


def test_doctor_without_a_keychain_login_fails_with_a_fix(healthy, capsys):
    healthy.keychain.items.pop(paths.claude_keychain_service())
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "✗ no Claude Code login in the Keychain ('Claude Code-credentials')" in out
    assert "fix: Run `claude` and sign in" in out
    assert "1 problem(s)" in out


def test_doctor_signed_out_item_and_unreadable_item(healthy, capsys):
    service = paths.claude_keychain_service()
    healthy.keychain.put_json(service, {"mcpOAuth": {}})
    _, out, _ = run(["doctor"], capsys)
    assert "has no Claude login (signed out?)" in out
    healthy.keychain.items[service] = ("tester", "not json")
    _, out, _ = run(["doctor"], capsys)
    assert "is unreadable" in out


def test_doctor_keychain_error_is_reported_not_raised(healthy, monkeypatch, capsys):
    def locked(service):
        raise keychain.KeychainError("Keychain did not answer in time.")

    monkeypatch.setattr(keychain, "read", locked)
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "cannot read Keychain item 'Claude Code-credentials': Keychain did not answer in time." in out
    assert "Unlock your login keychain" in out


def test_doctor_unsaved_live_login_is_a_warning(healthy, samples, capsys):
    other = samples.make_login(samples.ACCOUNT_B, token_suffix="b")
    healthy.keychain.put_json(paths.claude_keychain_service(), {"claudeAiOauth": other})
    code, out, _ = run(["doctor"], capsys)
    assert code == 0  # warnings don't fail the run
    assert "! the live login is not saved in cc-switcher yet" in out
    assert "fix: Run `ccs import` to save it" in out
    assert "No problems, 1 warning(s)." in out


def test_doctor_missing_saved_item_names_the_account(healthy, capsys):
    healthy.keychain.items.pop(make_account(ID_A).keychain_service)
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "✗ work: no saved login in the Keychain" in out
    assert "fix: Run `ccs add` and sign in to this account again" in out


def test_doctor_saved_item_for_the_wrong_account(healthy, samples, capsys):
    wrong = samples.make_login(samples.ACCOUNT_B, token_suffix="b")
    healthy.keychain.put_json(make_account(ID_A).keychain_service, {"claudeAiOauth": wrong})
    _, out, _ = run(["doctor"], capsys)
    assert "work: the saved login belongs to a different account" in out


def test_doctor_flags_loose_permissions_with_a_chmod_fix(healthy, capsys):
    paths.app_dir().chmod(0o755)
    paths.accounts_file().chmod(0o644)
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "too-open permissions" in out and str(paths.accounts_file()) in out
    assert f"chmod 700 {paths.app_dir()} && chmod 600 {paths.app_dir()}/*" in out


def test_doctor_agent_states_are_warnings(healthy, capsys):
    healthy.world.agent_installed = False
    code, out, _ = run(["doctor"], capsys)
    assert code == 0 and "! start at login is off" in out and "fix: Run `ccs agent install`" in out
    healthy.world.agent_installed, healthy.world.agent_running = True, False
    _, out, _ = run(["doctor"], capsys)
    assert "! the LaunchAgent is installed but the menu bar app is not running" in out
    assert "menubar.err.log" in out


def test_doctor_mcp_needing_sign_in_is_a_warning(healthy, capsys):
    healthy.world.mcp_status = [row("atlassian", "needs_sign_in"), row("idle", "not_configured", configured=False)]
    code, out, _ = run(["doctor"], capsys)
    assert code == 0
    assert "! MCP atlassian: needs sign-in" in out and "type /mcp" in out
    assert "idle" not in out  # only servers that exist on this Mac are listed


def test_doctor_without_claude_and_off_macos(healthy, monkeypatch, capsys):
    healthy.world.claude = None
    monkeypatch.setattr(sys, "platform", "linux")
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "✗ not macOS (linux)" in out
    assert "✗ Claude Code (`claude`) not found" in out and "claude.ai/install.sh" in out


def test_doctor_claude_version_that_does_not_answer_is_a_warning(healthy, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run_command", lambda argv, timeout=15: SimpleNamespace(returncode=1, stdout="", stderr="x"))
    code, out, _ = run(["doctor"], capsys)
    assert code == 0 and "! found /usr/local/bin/claude but `claude --version` did not answer" in out


def test_doctor_survives_a_crashing_check(healthy, capsys):
    def boom():
        raise RuntimeError("launchctl exploded")

    sys.modules["cc_switcher.launchagent"].is_installed = boom
    code, out, _ = run(["doctor"], capsys)
    assert code == 1
    assert "✗ check check_launchagent crashed: launchctl exploded" in out
    assert "✓ MCP sentry: signed in" in out  # later checks still ran


def test_doctor_online_checks_usage_of_the_active_account(healthy, capsys):
    code, out, _ = run(["doctor", "--online"], capsys)
    assert code == 0
    assert healthy.world.args_of("refresh_account") == (ID_A,)
    assert "✓ usage for work: session 42%, weekly 18%" in out
    assert "--online" not in out

    healthy.world.refresh_snapshot = make_snapshot(ok=False, error="auth_expired")
    code, out, _ = run(["doctor", "--online"], capsys)
    assert code == 1
    assert "✗ usage for work: sign-in expired — run `ccs add` again for this account" in out


def test_doctor_online_without_an_active_account(healthy, capsys):
    healthy.world.live_id = None
    code, out, _ = run(["doctor", "--online"], capsys)
    assert code == 0 and "! no active saved account, so no usage check" in out


# --------------------------------------------------------------------------- menu bar helpers (no rumps needed)


def test_menubar_module_imports_without_a_gui_and_refuses_to_run(monkeypatch):
    monkeypatch.setattr(menubar, "rumps", None)
    with pytest.raises(menubar.MenubarError, match="rumps"):
        menubar.main()


def test_ccs_menubar_without_rumps_is_a_one_line_error(monkeypatch, capsys):
    monkeypatch.setattr(menubar, "rumps", None)
    code, out, err = run(["menubar"], capsys)
    assert code == 1 and out == ""
    assert err.startswith("ccs: The menu bar app needs the 'rumps' package") and err.count("\n") == 1


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("none", ""), ("session", " 42%"), ("weekly", " 18%"), ("both", " 42% · 18%")],
)
def test_title_text(mode, expected):
    assert menubar.title_text(make_snapshot(), mode) == expected


def test_title_text_for_missing_and_failed_snapshots():
    assert menubar.title_text(None, "session") == ""
    assert menubar.title_text(make_snapshot(ok=False, error="network"), "session") == f" {menubar.WARN}"
    assert menubar.title_text(make_snapshot(ok=False, error="network"), "none") == ""


def test_compact_usage_lines(world):
    lines = menubar.compact_usage_lines(
        make_snapshot(extra_windows=(models.UsageWindow("seven_day_opus", "Weekly · Opus", 3.0, None),))
    )
    assert lines == [
        "Session (5h): 42% · resets in 3h 05m",
        "Weekly: 18% · resets in 3h 05m",
        "Weekly · Opus: 3%",
        "Extra usage: $12.50 of $100.00 used · $87.50 left",
        "Launch promo: $5.00 left",
    ]
    assert menubar.compact_usage_lines(None) == ["Usage not fetched yet"]
    assert "Sign-in expired" in menubar.compact_usage_lines(make_snapshot(ok=False, error="auth_expired"))[0]
    assert "Rate limited" in menubar.compact_usage_lines(make_snapshot(ok=False, error="rate_limited"))[0]


def test_menu_titles_are_made_unique_because_rumps_drops_duplicates():
    unique = menubar._UniqueTitles()
    first, second, third = unique("Session: 0%"), unique("Session: 0%"), unique("Session: 0%")
    assert len({first, second, third}) == 3
    assert second.startswith("Session: 0%") and second.strip("​") == "Session: 0%"


# --------------------------------------------------------------------------- redeemable limit resets (display only)


def test_usage_shows_a_resets_line_when_the_snapshot_has_resets(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot(resets=make_resets(make_grant(left=1)))}
    code, out, _ = run(["usage"], capsys)
    assert code == 0
    assert f"Resets: 1 left (weekly) · use by {USE_BY_TEXT} · usable at a limit" in out


def test_status_shows_the_resets_line_too(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot(resets=make_resets(make_grant(left=2, usable_now=True), at_limit=True))}
    _, out, _ = run(["status"], capsys)
    assert f"Resets: 2 left (weekly) · use by {USE_BY_TEXT} · usable now" in out


def test_resets_none_left(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot(resets=make_resets(make_grant(left=0)))}
    _, out, _ = run(["usage"], capsys)
    assert "Resets: none left" in out and "use by" not in out


def test_no_resets_line_when_the_snapshot_has_no_reset_block(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot(resets=None)}
    for argv in (["usage"], ["status"], ["list"]):
        world.live_id = ID_A
        _, out, _ = run(argv, capsys)
        assert "Resets:" not in out and "usable" not in out
    # a failed snapshot has nothing to show either
    world.snapshots = {ID_A: make_snapshot(ok=False, error="network")}
    _, out, _ = run(["usage"], capsys)
    assert "Resets:" not in out


def test_resets_wording_for_kinds_paused_grants_and_missing_expiry():
    live = make_grant(left=1, type_="weekly", requires_limit=False, ends=None)
    other = make_grant(left=2, type_="session", requires_limit=False, ends=None)
    paused = make_grant(left=5, type_="full", paused=True)
    summary = cli.resets_summary(make_resets(live, other, paused))
    assert summary == "3 left (weekly, session)"  # paused grants are not counted; no expiry, no "use by"
    untyped = models.ResetGrant("x", "Reset", None, 1, 1, use_requires_limit=False)
    assert cli.resets_summary(models.ResetStatus(True, grants=(untyped,))) == "1 left"
    assert cli.resets_summary(make_resets(paused)) == "none left"


def test_resets_use_by_is_the_earliest_expiry():
    soon = make_grant(left=1, ends=datetime(2030, 11, 5, 12, 0, tzinfo=timezone.utc))
    later = make_grant(left=1, type_="session", ends=datetime(2030, 12, 5, 12, 0, tzinfo=timezone.utc))
    expected = datetime(2030, 11, 5, 12, 0, tzinfo=timezone.utc).astimezone().strftime("%a %d %b")
    assert f"use by {expected}" in cli.resets_summary(make_resets(later, soon))


def test_list_has_a_limit_resets_column(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"), make_account("cccccccc-0000-0000-0000-000000000003:0000000c-0000-0000-0000-00000000000c", "carol@x.org", label="spare"))
    world.live_id = ID_A
    world.snapshots = {
        ID_A: make_snapshot(ID_A, session=100.0, resets=make_resets(make_grant(left=2, usable_now=True), at_limit=True)),
        ID_B: make_snapshot(ID_B, resets=make_resets(make_grant(left=0))),
    }
    code, out, _ = run(["list"], capsys)
    rows = {cells(line)[1]: cells(line) for line in out.splitlines()[1:4]}
    assert code == 0
    assert rows["work"][-2] == "2, usable now"  # last column is UPDATED
    assert rows["home"][-2] == "0"
    assert rows["spare"][-2] == "—"  # no snapshot yet


def test_a_reset_is_never_usable_now_unless_the_account_is_at_a_limit(world):
    grant = make_grant(left=1, usable_now=True)
    at_limit = make_snapshot(session=100.0, resets=make_resets(grant))
    below = make_snapshot(session=40.0, weekly=20.0, resets=make_resets(grant))
    flagged = make_snapshot(session=40.0, resets=make_resets(grant, at_limit=True))
    assert cli.resets_usable_now(at_limit) and cli.resets_usable_now(flagged)
    assert not cli.resets_usable_now(below)
    assert not cli.resets_usable_now(make_snapshot(session=100.0, resets=None))


def test_menu_resets_line_and_the_bolt_when_usable_now(world):
    grant = make_grant(left=1)
    plain = menubar.compact_usage_lines(make_snapshot(resets=make_resets(grant)))
    assert plain[-1] == f"Resets: 1 left (weekly) · use by {USE_BY_TEXT} · usable at a limit"

    usable = make_snapshot(session=100.0, resets=make_resets(make_grant(left=1, usable_now=True), at_limit=True))
    line = menubar.compact_usage_lines(usable)[-1]
    assert line == f"⚡ Resets: 1 left (weekly) · use by {USE_BY_TEXT} · usable now"

    none_left = menubar.compact_usage_lines(make_snapshot(resets=make_resets(make_grant(left=0))))
    assert none_left[-1] == "Resets: none left"
    assert not any("Resets" in l for l in menubar.compact_usage_lines(make_snapshot(resets=None)))


def test_cc_switcher_can_never_spend_a_reset_or_record_an_offer():
    """Hard rule: nothing here may POST the reset-spending endpoint or request the impression-recording variant."""
    sources = Path(cc_switcher.__file__).parent
    for name in ("cli.py", "menubar.py", "launchagent.py"):
        text = (sources / name).read_text()
        assert "reset_rate_limits" not in text, name
        assert "at_wall" not in text, name
    # and no command for it
    parser_text = cli._build_parser().format_help().lower()
    assert "redeem" not in parser_text and "spend" not in parser_text


# --------------------------------------------------------------------------- mcp apply contract


def test_mcp_apply_never_holds_the_app_lock(world, capsys):
    """apply_profile takes the lock itself (the lock is not re-entrant), so the CLI must not hold it."""
    acquired = []

    def apply_profile(profile=None, *, prune=False, dry_run=False, runner=None):
        with fsutil.app_lock(timeout=0.3):  # would raise LockTimeout if the caller held the lock
            acquired.append(True)
        return []

    sys.modules["cc_switcher.mcp_profile"].apply_profile = apply_profile
    code, _, err = run(["mcp", "apply"], capsys)
    assert code == 0 and acquired == [True], err


def test_mcp_apply_shows_each_reason_and_what_it_means_for_sign_ins(world, capsys):
    Change = SimpleNamespace
    world.mcp_changes = [
        Change(action="update", name="sentry", config={"type": "http", "url": "https://mcp.sentry.dev/mcp"}, reason="config differs; existing sign-in is kept"),
        Change(action="update", name="atlassian", config={"type": "http", "url": "https://mcp.example.com/mcp"}, reason="token key changes, sign in again after applying"),
        Change(action="add", name="github", config={"type": "http", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}"}}, reason=""),
    ]
    code, out, _ = run(["mcp", "apply"], capsys)
    assert code == 0
    assert "sentry" in out and "existing sign-in is kept" in out
    assert "token key changes, sign in again after applying" in out
    assert "ghp_" not in out and "Bearer" not in out  # placeholders only, and headers are not printed at all


def test_mcp_profile_errors_are_one_line(world, capsys):
    def apply_profile(profile=None, *, prune=False, dry_run=False, runner=None):
        raise sys.modules["cc_switcher.mcp_profile"].McpProfileError("No MCP profile at /x. Create one with `ccs mcp export`.")

    sys.modules["cc_switcher.mcp_profile"].apply_profile = apply_profile
    code, out, err = run(["mcp", "apply"], capsys)
    assert code == 1 and out == ""
    assert err == "ccs: No MCP profile at /x. Create one with `ccs mcp export`.\n"


# --------------------------------------------------------------------------- data age and stale numbers

AGO_12M = timedelta(minutes=12)


def stale_world(world, *, error="rate_limited", ago=AGO_12M, session=62.0, weekly=41.0, live=True):
    """Account A has numbers from 12 minutes ago and its latest refresh failed; account B is fresh."""
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_A if live else None
    world.snapshots = {
        ID_A: make_snapshot(ID_A, session=session, weekly=weekly, fetched_ago=ago),
        ID_B: make_snapshot(ID_B, session=5.0, weekly=9.0, fetched_ago=timedelta(minutes=3)),
    }
    world.failed_refresh = {ID_A: (error, models.utcnow() - timedelta(seconds=30))}


def test_age_text():
    assert [cli.age_text(s) for s in (0, 44, 89, 90, 12 * 60, 3 * 3600 + 20, 2 * 86400 + 5, None)] == [
        "just now", "just now", "just now", "1m ago", "12m ago", "3h ago", "2d ago", "just now",
    ]


def test_usage_shows_fresh_numbers_with_their_age(world, capsys):
    stale_world(world)
    world.failed_refresh = {}
    _, out, _ = run(["usage", "home"], capsys)
    assert "(updated 3m ago)" in out
    assert "stale" not in out and "last refresh" not in out


def test_usage_marks_stale_numbers_before_the_numbers_and_says_why(world, capsys):
    stale_world(world)
    code, out, _ = run(["usage", "work"], capsys)
    lines = out.splitlines()
    assert code == 0
    assert "~ stale: updated 12m ago, last refresh: rate limited" in out
    assert "the last refresh failed: A request was rate limited" in out
    assert lines.index(next(l for l in lines if "stale:" in l)) < lines.index(next(l for l in lines if "Session (5h)" in l))
    assert re.search(r"Session \(5h\)\s+62%", out) and re.search(r"Weekly\s+41%", out)  # the numbers are still there
    assert "(updated" not in out  # one freshness statement, not two


def test_status_marks_stale_numbers_too(world, capsys):
    stale_world(world, error="network")
    _, out, _ = run(["status"], capsys)
    assert "~ stale: updated 12m ago, last refresh: offline" in out
    assert "couldn't reach Anthropic" in out and "62%" in out


def test_list_marks_stale_rows_and_footnotes_them(world, capsys):
    stale_world(world)
    code, out, _ = run(["list"], capsys)
    lines = out.splitlines()
    assert code == 0
    assert cells(lines[0])[-1] == "UPDATED"
    work, home = cells(lines[1]), cells(lines[2])
    assert work[5:7] == ["62%~", "41%~"] and work[-1] == "12m ago (stale)"
    assert home[5:7] == ["5%", "9%"] and home[-1] == "3m ago"
    assert "~ = stale: the last refresh failed, so these are the last good numbers." in out
    assert "work: stale: updated 12m ago, last refresh: rate limited" in out
    assert "home:" not in out
    assert "Usage from cache" not in out  # the footnote replaces the cache line when something is stale


def test_list_without_stale_rows_keeps_the_plain_footer(world, capsys):
    stale_world(world)
    world.failed_refresh = {}
    _, out, _ = run(["list"], capsys)
    assert "~" not in out
    assert "Usage from cache (updated 3m ago)" in out  # the newest numbers


def test_list_refresh_that_fails_does_not_claim_fresh_numbers(world, capsys):
    stale_world(world)
    world.fresh = dict(world.snapshots)  # the refresh "succeeded" for B and kept A's last good numbers
    _, out, _ = run(["list", "--refresh"], capsys)
    assert world.names() == ["save_active_login", "refresh_all"]
    assert "Usage fetched just now" not in out
    assert "work: stale: updated 12m ago, last refresh: rate limited" in out


def test_permanent_errors_still_say_sign_in_expired(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot(ID_A, ok=False, error="auth_expired")}
    world.failed_refresh = {ID_A: ("auth_expired", models.utcnow())}
    _, out, _ = run(["usage"], capsys)
    assert "sign-in expired — run `ccs add` again for this account" in out
    assert "stale" not in out
    _, out, _ = run(["list"], capsys)
    assert "sign-in expired" in out and "~" not in out


def test_json_carries_age_and_stale_fields_beside_usage(world, capsys):
    stale_world(world, error="rate_limited")
    for argv in (["list", "--json"], ["usage", "--json"]):
        code, out, _ = run(argv, capsys)
        work, home = json.loads(out)
        assert code == 0
        assert work["stale"] is True and work["last_error"] == "rate_limited"
        assert 719 <= work["age_seconds"] <= 725
        assert datetime.fromisoformat(work["last_error_at"]) < models.utcnow()
        assert work["usage"] == {"account_id": ID_A, "ok": True, "error": None}
        assert home["stale"] is False and home["last_error"] is None and home["last_error_at"] is None
        assert 179 <= home["age_seconds"] <= 185
        assert work["account"]["email"] == "alice@acme.com"  # existing fields are unchanged


def test_status_json_has_the_same_freshness_fields(world, capsys):
    stale_world(world)
    _, out, _ = run(["status", "--json"], capsys)
    data = json.loads(out)
    assert data["stale"] is True and data["last_error"] == "rate_limited" and 719 <= data["age_seconds"] <= 725
    assert data["usage"]["account_id"] == ID_A
    world.live_id = None
    _, out, _ = run(["status", "--json"], capsys)
    data = json.loads(out)
    assert data["usage"] is None and data["stale"] is False and data["age_seconds"] is None


def test_json_for_an_account_with_no_numbers_yet(world, capsys):
    seed(make_account())
    _, out, _ = run(["usage", "--json"], capsys)
    (row,) = json.loads(out)
    assert row["usage"] is None and row["age_seconds"] is None and row["stale"] is False and row["last_error"] is None


def test_next_explains_that_old_numbers_count_as_unknown(world, capsys):
    seed(make_account(), make_account(ID_B, "bob@example.org", label="home"))
    world.live_id = ID_A
    world.target = None
    sys.modules["cc_switcher.autoswitch"].CANDIDATE_MAX_AGE_SECONDS = 1800
    _, _, err = run(["next", "--cached"], capsys)
    assert "numbers older than 30 minutes count as unknown" in err


def test_cli_and_menu_never_override_the_auto_switch_max_age():
    sources = Path(cc_switcher.__file__).parent
    for name in ("cli.py", "menubar.py"):
        assert "max_age_seconds" not in (sources / name).read_text(), name


# --------------------------------------------------------------------------- menu bar: freshness (no run loop needed)


def entry(snapshot, *, age=None, stale=False, error=None):
    return {"snapshot": snapshot, "age_seconds": age, "last_error": error, "last_error_at": None, "stale": stale}


def test_menubar_collect_reuses_very_recent_successes(world, monkeypatch):
    seed(make_account())
    settings = store.load_settings()
    settings.notifications_enabled = False
    store.save_settings(settings)
    observed = []
    monitor = sys.modules["cc_switcher.monitor"]
    monkeypatch.setattr(monitor, "refresh_all", lambda accounts, **kwargs: observed.append(kwargs))
    app = SimpleNamespace(
        _first_run_import=lambda *_: None,
        _view=menubar._View(),
        _read_mcp=lambda: [],
        _maybe_auto_switch=lambda *_: None,
    )
    menubar.SwitcherApp._collect(app, True)
    assert observed == [{"reuse_fresh_seconds": 60}]


def test_title_gets_a_tilde_only_when_stale():
    snap = make_snapshot(session=62.0, weekly=41.0)
    assert menubar.title_text(snap, "session", stale=True) == " 62%~"
    assert menubar.title_text(snap, "weekly", stale=True) == " 41%~"
    assert menubar.title_text(snap, "both", stale=True) == " 62% · 41%~"
    assert menubar.title_text(snap, "session") == " 62%"
    assert menubar.title_text(snap, "none", stale=True) == ""
    failed = make_snapshot(ok=False, error="auth_expired")
    assert menubar.title_text(failed, "session", stale=True) == f" {menubar.WARN}"  # no numbers, so no tilde


def test_usage_block_explains_which_endpoint_was_limited(world):
    failed = entry(make_snapshot(ok=False, error="rate_limited"), stale=True, error="rate_limited")
    failed["rate_limit"] = {
        "endpoint": "oauth_token", "server_error": "rate_limit_error",
        "next_allowed_at": "2030-01-01T00:00:00+00:00", "retry_after_seconds": 7200,
    }
    rendered = "\n".join(cli.usage_block(make_account(), failed, ID_A))
    assert "token refresh" in rendered.lower()
    assert "rate_limit_error" in rendered
    assert "sk-ant-" not in rendered


@pytest.mark.parametrize("endpoint, error", [([], None), ({}, None), ("oauth_token", []), ("usage", {})])
def test_usage_block_ignores_damaged_rate_limit_details(world, endpoint, error):
    failed = entry(make_snapshot(ok=False, error="rate_limited"), stale=True, error="rate_limited")
    failed["rate_limit"] = {"endpoint": endpoint, "server_error": error}
    rendered = "\n".join(cli.usage_block(make_account(), failed, ID_A))
    assert "rate limited" in rendered.lower()


def test_menu_lines_show_fresh_age(world):
    lines = menubar.entry_lines(entry(make_snapshot(), age=180.0))
    assert lines[-1] == "Updated 3m ago"
    assert "Stale" not in " ".join(lines)
    assert lines[0].startswith("Session (5h): 42%")


def test_menu_lines_keep_stale_numbers_and_say_so(world):
    snap = make_snapshot(session=62.0, weekly=41.0, fetched_ago=AGO_12M)
    lines = menubar.entry_lines(entry(snap, age=720.0, stale=True, error="rate_limited"))
    assert lines[0].startswith("Session (5h): 62%") and lines[1].startswith("Weekly: 41%")
    assert lines[-1] == "~ Stale · updated 12m ago · last refresh: rate limited"
    assert not any(l.startswith(menubar.WARN) for l in lines)  # an error never replaces good numbers


def test_menu_lines_without_numbers_keep_the_plain_error_text(world):
    expired = entry(make_snapshot(ok=False, error="auth_expired"), stale=True, error="auth_expired")
    assert menubar.entry_lines(expired) == ["⚠︎ Sign-in expired — use “Add account…” to sign in again"]
    assert menubar.entry_lines(entry(None)) == ["Usage not fetched yet"]
    assert menubar.entry_lines(None) == ["Usage not fetched yet"]


def test_menu_line_count_is_the_same_fresh_or_stale_so_the_menu_updates_in_place(world):
    snap = make_snapshot()
    fresh = menubar.entry_lines(entry(snap, age=60.0))
    stale = menubar.entry_lines(entry(snap, age=900.0, stale=True, error="network"))
    assert len(fresh) == len(stale)


def test_first_menu_line_explains_the_tilde():
    snap = make_snapshot(session=62.0)
    stale = entry(snap, age=720.0, stale=True, error="rate_limited")
    assert menubar.stale_banner(stale, "session") == "~  Menu bar number is from 12m ago (last refresh: rate limited)"
    assert menubar.stale_banner(entry(snap, age=60.0), "session") is None
    assert menubar.stale_banner(stale, "none") is None  # no number in the title, nothing to explain
    assert menubar.stale_banner(None, "session") is None
    assert menubar.stale_banner(entry(make_snapshot(ok=False, error="auth_expired"), stale=True, error="auth_expired"), "session") is None


def test_view_property_exposes_the_last_good_snapshots(world):
    view = menubar._View(entries={ID_A: entry(make_snapshot(ID_A), age=60.0), ID_B: entry(None)})
    assert list(view.snapshots) == [ID_A]
    assert view.active_entry is None  # no status yet


# --------------------------------------------------------------------------- synthetic account display


def example_pro_snapshot(account_id=ID_A, **overrides) -> models.UsageSnapshot:
    """Synthetic example: limit $50.00, $61.25 used, extra usage off by the
    organization, resets not offered (cedar_ember eligible=false, reason "surface"), 31% of the weekly limit is Claude Code."""
    fields = dict(
        account_id=account_id,
        fetched_at=models.utcnow(),
        ok=True,
        windows=(
            models.UsageWindow("five_hour", "Session (5h)", 7.0, FAR),
            models.UsageWindow("seven_day", "Weekly", 31.0, FAR),
        ),
        extra_usage=models.ExtraUsage(
            enabled=False, monthly_limit=5000, used_credits=6125.0, utilization=100.0, currency="USD",
            disabled_reason="org_level_disabled_until", spend_limit_reached=True, user_disabled=False, decimal_places=2,
        ),
        resets=models.ResetStatus(eligible=False, ineligible_reason="surface"),
        breakdown=(
            models.BreakdownRow("claude_code", "Claude Code", 100, 31),
            models.BreakdownRow("chat", "Chats", 0, 0),
            models.BreakdownRow("cowork", "Cowork", 0, 0),
            models.BreakdownRow("other", "Other", 0, 0),
        ),
    )
    fields.update(overrides)
    return models.UsageSnapshot(**fields)


def extra(**kw) -> models.ExtraUsage:
    base = dict(enabled=True, monthly_limit=1000, used_credits=320.0, utilization=32.0, currency="USD", decimal_places=2)
    base.update(kw)
    return models.ExtraUsage(**base)


EXAMPLE_EXTRA_LINE = "Extra usage: $61.25 of $50.00 used (100%) · limit reached · off (paused by your organization)"


def test_example_account_usage_text(world, capsys):
    seed(make_account(label="", org="", plan="pro"))
    world.live_id = ID_A
    world.snapshots = {ID_A: example_pro_snapshot()}
    code, out, _ = run(["usage"], capsys)
    lines = [l.strip() for l in out.splitlines()]
    assert code == 0
    assert EXAMPLE_EXTRA_LINE in lines
    assert "Resets: not offered on this account" in lines
    assert "none left" not in out and "org_level_disabled_until" not in out
    assert "Weekly use: Claude Code 31% of limit" in lines
    weekly = next(i for i, l in enumerate(lines) if l.startswith("Weekly "))
    assert lines[weekly + 1] == "Weekly use: Claude Code 31% of limit"  # directly under the Weekly row
    assert "Chats" not in out and "Cowork" not in out  # rows at 0% of the limit are left out
    assert "$-" not in out and "-$" not in out  # never a negative amount


def test_example_account_status_text_matches(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: example_pro_snapshot()}
    _, out, _ = run(["status"], capsys)
    assert EXAMPLE_EXTRA_LINE in out and "Resets: not offered on this account" in out and "Weekly use: Claude Code 31% of limit" in out


def test_example_account_list_row(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: example_pro_snapshot()}
    _, out, _ = run(["list"], capsys)
    row = cells(out.splitlines()[1])
    header = cells(out.splitlines()[0])
    assert row[header.index("CREDITS") + 1] == "$0.00 extra"  # remaining is 0, never negative
    assert row[header.index("LIMIT RESETS") + 1] == "—"  # not offered, which is not the same as 0 left


def test_example_account_json(world, capsys):
    seed(make_account())
    world.live_id = ID_A
    world.snapshots = {ID_A: example_pro_snapshot()}
    sys.modules["cc_switcher.monitor"].snapshot_to_dict = real_monitor.snapshot_to_dict  # the real serializer
    code, out, _ = run(["usage", "--json"], capsys)
    (row,) = json.loads(out)
    assert code == 0
    extra_json = row["usage"]["extra_usage"]
    assert extra_json["enabled"] is False and extra_json["monthly_limit"] == 5000 and extra_json["used_credits"] == 6125.0
    assert extra_json["disabled_reason"] == "org_level_disabled_until"
    assert row["usage"]["resets"]["eligible"] is False
    assert row["usage"]["resets"]["grants"] == []
    assert row["stale"] is False and row["age_seconds"] is not None


def test_example_account_menu_lines(world):
    lines = menubar.compact_usage_lines(example_pro_snapshot())
    assert lines == [
        "Session (5h): 7% · resets in 3h 05m",
        "Weekly: 31% · resets in 3h 05m",
        "Weekly use: Claude Code 31%",  # shortened: no "of limit"
        EXAMPLE_EXTRA_LINE,
    ]  # and no Resets line: the server doesn't offer them to this account


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # under the limit, on: amounts, then what is left; no percent
        ({}, "$3.20 of $10.00 used · $6.80 left"),
        # exactly at the limit, server did not flag it: $0.00 left, with the percent
        ({"used_credits": 1000.0, "utilization": 100.0}, "$10.00 of $10.00 used (100%) · $0.00 left"),
        # over the limit without the flag: remaining is clamped at zero, never negative
        ({"used_credits": 6125.0, "utilization": 100.0}, "$61.25 of $10.00 used (100%) · $0.00 left"),
        # an example account
        (
            {"enabled": False, "used_credits": 6125.0, "utilization": 100.0, "spend_limit_reached": True,
             "disabled_reason": "org_level_disabled_until", "user_disabled": False},
            "$61.25 of $10.00 used (100%) · limit reached · off (paused by your organization)",
        ),
        # under the limit but switched off by the user
        ({"enabled": False, "disabled_reason": "user_disabled"}, "$3.20 of $10.00 used · $6.80 left · off (turned off by you)"),
        ({"enabled": False, "user_disabled": True}, "$3.20 of $10.00 used · $6.80 left · off (turned off by you)"),
        # unknown reason code -> words
        ({"enabled": False, "disabled_reason": "billing_hold_pending"}, "$3.20 of $10.00 used · $6.80 left · off (billing hold pending)"),
        # percent computed when the server sent none
        ({"used_credits": 1000.0, "utilization": None}, "$10.00 of $10.00 used (100%) · $0.00 left"),
        # limit known, usage unknown
        ({"used_credits": None, "utilization": None}, "$10.00 monthly limit"),
        # fractional minor units round half up, in the account currency
        ({"used_credits": 1234.5, "utilization": 100.0, "currency": "EUR"}, "€12.35 of €10.00 used (100%) · €0.00 left"),
        ({"currency": "GBP", "used_credits": 250.0, "utilization": 25.0}, "2.50 GBP of 10.00 GBP used · 7.50 GBP left"),
        # decimal_places: 0 (JPY) and 3 (KWD) and missing (-> 2)
        ({"currency": "JPY", "monthly_limit": 5000, "used_credits": 1200.0, "decimal_places": 0}, "1,200 JPY of 5,000 JPY used · 3,800 JPY left"),
        ({"currency": "KWD", "monthly_limit": 10000, "used_credits": 1234.0, "decimal_places": 3}, "1.234 KWD of 10.000 KWD used · 8.766 KWD left"),
        ({"decimal_places": None}, "$3.20 of $10.00 used · $6.80 left"),
    ],
)
def test_extra_usage_text(kwargs, expected):
    assert cli.extra_usage_text(extra(**kwargs)) == expected


@pytest.mark.parametrize(
    ("extra_usage", "expected"),
    [
        (models.ExtraUsage(True, None, 500.0, None, "USD"), "on, $5.00 used, no monthly limit"),
        (models.ExtraUsage(True, None, None, None, "USD"), "on, no monthly limit"),
        (models.ExtraUsage(False, None, None, None, "USD"), "off"),
        (models.ExtraUsage(False, None, None, None, "USD", "org_level_disabled_until"), "off (paused by your organization)"),
        (models.ExtraUsage(False, None, None, None, "USD", user_disabled=True), "off (turned off by you)"),
        (models.ExtraUsage(False, None, 500.0, None, "USD"), "$5.00 used · off"),
    ],
)
def test_extra_usage_text_without_a_limit(extra_usage, expected):
    assert cli.extra_usage_text(extra_usage) == expected


def test_humanize_reason():
    assert cli.humanize_reason("org_level_disabled_until") == "paused by your organization"
    assert cli.humanize_reason("user_disabled") == "turned off by you"
    assert cli.humanize_reason("some_new_reason") == "some new reason"
    assert cli.humanize_reason(None) == "" and cli.humanize_reason("") == ""


def test_credits_cell_is_the_remaining_amount_or_a_dash(world):
    def cell(extra_usage, promos=()):
        return cli._credits_cell(example_pro_snapshot(extra_usage=extra_usage, promos=promos))

    assert cell(extra()) == "$6.80 extra"
    assert cell(extra(used_credits=6125.0)) == "$0.00 extra"
    assert cell(extra(enabled=False)) == "$6.80 extra (off)"  # there is room, but it can't be spent right now
    assert cell(models.ExtraUsage(True, None, 500.0, None, "USD")) == "extra on"  # no limit, so no "remaining"
    assert cell(models.ExtraUsage(False, None, None, None, "USD")) == "—"
    assert cell(None) == "—"
    assert cell(extra(), promos=(models.PromoCredit("p", "Promo", 20.0, 15.0, 5.0, 75.0, None),)) == "$6.80 extra + $5.00 promo"


def test_menu_extra_line_only_when_there_is_something_to_say(world):
    nothing = example_pro_snapshot(extra_usage=models.ExtraUsage(False, None, None, None, "USD"), breakdown=())
    assert not any(l.startswith("Extra usage") for l in menubar.compact_usage_lines(nothing))
    under = menubar.compact_usage_lines(example_pro_snapshot(extra_usage=extra()))
    assert "Extra usage: $3.20 of $10.00 used · $6.80 left" in under


# ---- resets: not offered vs none left


def test_resets_not_offered_is_not_none_left(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot(resets=models.ResetStatus(eligible=False, ineligible_reason="surface"))}
    _, out, _ = run(["usage"], capsys)
    assert "Resets: not offered on this account" in out and "none left" not in out
    _, out, _ = run(["list"], capsys)
    row, header = cells(out.splitlines()[1]), cells(out.splitlines()[0])
    assert row[header.index("LIMIT RESETS") + 1] == "—"


def test_resets_none_left_only_when_eligible(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot(resets=models.ResetStatus(eligible=True, grants=(make_grant(left=0),)))}
    _, out, _ = run(["usage"], capsys)
    assert "Resets: none left" in out
    assert cli.resets_summary(models.ResetStatus(eligible=True)) == "none left"  # eligible, no grants at all
    assert cli.resets_summary(models.ResetStatus(eligible=False)) == "not offered on this account"


def test_menu_omits_the_resets_line_when_not_offered(world):
    off = menubar.compact_usage_lines(make_snapshot(resets=models.ResetStatus(eligible=False, ineligible_reason="surface")))
    assert not any("Resets" in l for l in off)
    eligible = menubar.compact_usage_lines(make_snapshot(resets=models.ResetStatus(eligible=True, grants=(make_grant(left=0),))))
    assert eligible[-1] == "Resets: none left"


# ---- weekly breakdown


def row_(key, name, pct):
    return models.BreakdownRow(key, name, pct, pct)


def test_breakdown_text_lists_only_rows_with_a_share(world):
    one = make_snapshot()
    assert cli.breakdown_text(one) is None  # no rows
    snap = lambda *rows: example_pro_snapshot(breakdown=tuple(rows))
    assert cli.breakdown_text(snap(row_("a", "Claude Code", 31), row_("b", "Chats", 0))) == "Weekly use: Claude Code 31% of limit"
    assert cli.breakdown_text(snap(row_("a", "Claude Code", 20), row_("b", "Chats", 6), row_("c", "Cowork", 0))) == "Weekly use: Claude Code 20% · Chats 6%"
    assert cli.breakdown_text(snap(row_("a", "Claude Code", 31)), short=True) == "Weekly use: Claude Code 31%"
    assert cli.breakdown_text(snap(row_("a", "A", 0), row_("b", "B", 0))) is None  # all zero
    assert cli.breakdown_text(snap(models.BreakdownRow("a", "A", 5, None))) is None  # unknown share
    assert cli.breakdown_text(snap(models.BreakdownRow("other", "", 4, 4))) == "Weekly use: other 4% of limit"  # no display name


def test_breakdown_line_is_skipped_when_there_are_no_rows(world, capsys):
    seed(make_account())
    world.snapshots = {ID_A: make_snapshot()}
    _, out, _ = run(["usage"], capsys)
    assert "Weekly use" not in out
    assert not any("Weekly use" in l for l in menubar.compact_usage_lines(make_snapshot()))


def test_breakdown_line_follows_the_weekly_row_even_with_other_windows(world, capsys):
    seed(make_account())
    snap = example_pro_snapshot(
        windows=(
            models.UsageWindow("five_hour", "Session (5h)", 7.0, FAR),
            models.UsageWindow("seven_day", "Weekly", 31.0, FAR),
            models.UsageWindow("seven_day_opus", "Weekly · Opus", 3.0, FAR),
        )
    )
    world.snapshots = {ID_A: snap}
    _, out, _ = run(["usage"], capsys)
    lines = [l.strip() for l in out.splitlines()]
    weekly = next(i for i, l in enumerate(lines) if l.startswith("Weekly ") and "Opus" not in l)
    assert lines[weekly + 1].startswith("Weekly use:") and lines[weekly + 2].startswith("Weekly · Opus")
    menu = menubar.compact_usage_lines(snap)
    assert menu[1].startswith("Weekly: 31%") and menu[2] == "Weekly use: Claude Code 31%" and menu[3].startswith("Weekly · Opus")


def test_breakdown_without_a_weekly_window_still_shows_once(world):
    snap = example_pro_snapshot(windows=(models.UsageWindow("five_hour", "Session (5h)", 7.0, FAR),))
    lines = menubar.compact_usage_lines(snap)
    assert sum("Weekly use" in l for l in lines) == 1


# ---- accounts with no label and no email ("Saved . It is now the active account.")


def nameless(account_id=ID_A):
    return make_account(account_id, email="", label="", org="", plan="")


def test_who_never_returns_an_empty_name():
    nobody = nameless()
    assert nobody.name == "account aaaaaaaa"
    assert cli._who(nobody) == "account aaaaaaaa"
    assert cli._who(make_account(label="", email="alice@acme.com")) == "alice@acme.com"
    assert cli._who(make_account(label="work", email="alice@acme.com")) == "work (alice@acme.com)"
    assert cli._who(make_account(label="work", email="")) == "work"  # not "work ()"


def test_add_import_switch_remove_never_print_an_empty_name(world, capsys):
    nobody = nameless(ID_B)
    seed(make_account())
    world.live_id = ID_A
    world.add_result = nobody
    _, out, _ = run(["add"], capsys)  # a new account: not registered before the sign-in
    assert "Saved account bbbbbbbb. It is now the active account." in out
    seed(make_account(), nobody)
    world.import_result = nobody
    _, out, _ = run(["import"], capsys)
    assert out.startswith("Saved account bbbbbbbb.")
    code, out, _ = run(["switch", "bbbbbbbb-0000-0000-0000-000000000002:0000000b-0000-0000-0000-00000000000b"], capsys)
    assert code == 0 and "Switched to account bbbbbbbb." in out
    _, out, _ = run(["remove", ID_B], capsys)
    assert "Removed account bbbbbbbb from cc-switcher." in out
    _, out, _ = run(["label", ID_B, "spare"], capsys)
    assert "account bbbbbbbb is now labelled 'spare'." in out


def test_list_and_usage_show_a_nameless_account_by_its_short_id(world, capsys):
    seed(nameless())
    world.live_id = ID_A
    world.snapshots = {ID_A: make_snapshot()}
    _, out, _ = run(["list"], capsys)
    assert "account aaaaaaaa" in out.splitlines()[1]
    _, out, _ = run(["usage"], capsys)
    assert out.splitlines()[0] == "◉ account aaaaaaaa"
    _, out, _ = run(["status"], capsys)
    assert "◉ account aaaaaaaa" in out


def test_menu_titles_for_a_nameless_account(world):
    nobody = nameless()
    assert menubar.display_name(nobody) == "account aaaaaaaa"
    assert menubar.display_name(make_account(label="work", email="")) == "work"
    assert menubar.display_name(make_account(label="work", email="a@b.c")) == "work · a@b.c"


def test_the_example_fixtures_render_correctly_end_to_end(world, capsys):
    """Parse synthetic example responses with the production parser, then render them through the CLI and the menu."""
    import dataclasses

    fixtures = Path(__file__).parent / "fixtures"
    raw = json.loads((fixtures / "usage_example.json").read_text())
    resets = json.loads((fixtures / "resets_example_ineligible.json").read_text())
    snapshot = real_usage.parse_usage(raw, ID_A)
    snapshot = dataclasses.replace(snapshot, resets=real_usage.parse_resets(resets["cedar_ember"]))

    seed(make_account(label="", org="", plan="pro"))
    world.live_id = ID_A
    world.snapshots = {ID_A: snapshot}
    _, out, _ = run(["usage"], capsys)
    lines = [l.strip() for l in out.splitlines()]
    assert EXAMPLE_EXTRA_LINE in lines
    assert "Resets: not offered on this account" in lines
    assert "Weekly use: Claude Code 31% of limit" in lines
    _, out, _ = run(["list"], capsys)
    row, header = cells(out.splitlines()[1]), cells(out.splitlines()[0])
    assert row[header.index("CREDITS") + 1] == "$0.00 extra" and row[header.index("LIMIT RESETS") + 1] == "—"
    menu = menubar.compact_usage_lines(snapshot)
    assert EXAMPLE_EXTRA_LINE in menu and "Weekly use: Claude Code 31%" in menu and not any("Resets" in l for l in menu)
