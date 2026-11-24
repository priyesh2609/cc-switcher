"""Menu bar app (rumps).

Threading rules, because Keychain reads and network calls can block for seconds:

- Callbacks and timers run on the main thread. They only read small local files, flip
  flags and start worker threads. They never call the Keychain, the network or a subprocess.
- All of that work happens on daemon worker threads. A worker never touches rumps objects;
  it hands a closure to ``_on_main`` (``NSOperationQueue.mainQueue``) to update the UI.
- At most one refresh cycle runs at a time. A request that arrives meanwhile is coalesced
  into a single follow-up cycle.

The module imports cleanly without rumps / AppKit (tests, non-macOS); only ``main()`` needs them.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cc_switcher import cli, paths

try:  # pragma: no cover - depends on the platform
    import AppKit
    import rumps
    from Foundation import NSOperationQueue
except Exception:  # missing rumps/pyobjc, or not macOS
    AppKit = None
    rumps = None
    NSOperationQueue = None

class MenubarError(RuntimeError):
    """The menu bar app cannot start (missing rumps, ...). Shown as a one-line message by the CLI."""


AUTO_SWITCH_COOLDOWN_SECONDS = 600
TICK_SECONDS = 30  # how often the timer wakes up; the poll interval itself comes from settings
ADD_POLL_SECONDS = 2
ADD_TIMEOUT_SECONDS = 15 * 60
MARKER = "◉"  # active
HOLLOW = "○"  # inactive
WARN = "⚠︎"
TILDE = "~"  # marks numbers from a failed refresh: the last good ones, not current
BOLT = "⚡"  # prefix for a reset that can be used right now
LINE_INDENT = "      "


# --------------------------------------------------------------------------- pure helpers


def _pct(window: Any) -> str:
    if window is None or window.utilization is None:
        return "—"
    return f"{window.utilization:.0f}%"


def title_text(snapshot: Any, mode: str, stale: bool = False) -> str:
    """Text shown next to the menu bar icon: usage of the active account, per ``menubar_title``.

    ``stale`` (the last refresh failed, these are the last good numbers) adds a trailing ``~``
    so the number never looks current; the first menu line explains it.
    """
    if mode == "none" or snapshot is None:
        return ""
    if not snapshot.ok:
        return f" {WARN}"
    session, weekly = _pct(snapshot.session), _pct(snapshot.weekly)
    mark = TILDE if stale else ""
    if mode == "weekly":
        return f" {weekly}{mark}"
    if mode == "both":
        return f" {session} · {weekly}{mark}"
    return f" {session}{mark}"


def stale_banner(entry: dict[str, Any] | None, mode: str) -> str | None:
    """First menu line when the title number is stale; None otherwise."""
    if entry is None or mode == "none" or not cli.entry_is_stale(entry):
        return None
    reason = cli.short_error_text(entry.get("last_error"))
    return f"{TILDE}  Menu bar number is from {cli.age_text(cli.entry_age(entry))} (last refresh: {reason})"


def _menu_error(code: str | None) -> str:
    if code in ("auth_expired", "no_credentials"):
        return f"{WARN} Sign-in expired — use “Add account…” to sign in again"
    if code == "rate_limited":
        return f"{WARN} Rate limited — retrying shortly"
    if code == "refresh_queued":
        return "Token refresh queued — retrying on a later poll"
    if code == "refresh_schedule_error":
        return f"{WARN} Token refresh schedule error — check disk space and app data permissions"
    if code == "network":
        return f"{WARN} Can’t reach Anthropic"
    if code == "token_stale":
        return f"{WARN} Login needs a refresh — use `claude` once"
    return f"{WARN} Usage unavailable ({cli.short_error_text(code)})"


def compact_usage_lines(
    snapshot: Any,
    *,
    age_seconds: float | None = None,
    stale: bool = False,
    last_error: str | None = None,
) -> list[str]:
    """One short line per usage window, plus credits and how fresh they are. Shown under each account.

    Without ``age_seconds`` no freshness line is added. A stale snapshot (the last refresh failed)
    keeps its numbers and gets a ``~ Stale`` line that says why.
    """
    if snapshot is None:
        return ["Usage not fetched yet"]
    if not snapshot.ok:
        return [_menu_error(snapshot.error)]
    from cc_switcher import usage

    lines = []
    breakdown = cli.breakdown_text(snapshot, short=True)
    for window in snapshot.windows:
        reset = ""
        if window.resets_at is not None:
            relative = usage.format_reset(window.resets_at)
            reset = " · resets now" if relative == "now" else f" · resets in {relative}"
        lines.append(f"{window.label}: {usage.format_percent(window.utilization)}{reset}")
        if window.key == "seven_day" and breakdown:
            lines.append(breakdown)
    if not lines:
        lines.append("No usage reported")
    if breakdown and snapshot.window("seven_day") is None:
        lines.append(breakdown)
    extra = snapshot.extra_usage
    if extra is not None and cli.extra_usage_has_data(extra):
        lines.append(f"Extra usage: {cli.extra_usage_text(extra)}")
    for promo in snapshot.promos:
        remaining = promo.remaining_dollars
        if remaining is not None:
            lines.append(f"{promo.label}: {usage.format_money(remaining * 100, 'USD')} left")
    if snapshot.resets is not None and snapshot.resets.eligible:  # nothing to show when the server doesn't offer resets
        # Display only: the reset itself is spent inside Claude Code with /limit-reset.
        text = f"Resets: {cli.resets_summary(snapshot.resets, dot='·')}"
        lines.append(f"{BOLT} {text}" if cli.resets_usable_now(snapshot) else text)
    if age_seconds is not None:
        age = cli.age_text(age_seconds)
        if stale:
            lines.append(f"{TILDE} Stale · updated {age} · last refresh: {cli.short_error_text(last_error)}")
            if last_error == "refresh_schedule_error":
                lines.append("Check disk space and app data permissions; seek help before changing a damaged schedule")
        else:
            lines.append(f"Updated {age}")
    return lines


def entry_lines(entry: dict[str, Any] | None) -> list[str]:
    """``compact_usage_lines`` for one ``monitor.cache_status`` entry."""
    entry = entry or {}
    snapshot = entry.get("snapshot")
    age = cli.entry_age(entry) if snapshot is not None else None
    return compact_usage_lines(
        snapshot,
        age_seconds=age,
        stale=cli.entry_is_stale(entry) if snapshot is not None else False,
        last_error=entry.get("last_error"),
    )


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str, subtitle: str = "", title: str = "cc-switcher") -> None:
    """Post a notification from any thread. Never blocks the caller, never raises.

    ``osascript`` works for a plain Python process (rumps' own notifications need an app
    bundle); rumps is the fallback. Messages hold names and percentages only (no secrets).
    """

    def work() -> None:
        sent = False
        try:
            script = f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
            if subtitle:
                script += f" subtitle {_applescript_string(subtitle)}"
            sent = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15).returncode == 0
        except Exception:
            pass
        if not sent and rumps is not None:

            def fallback() -> None:
                try:
                    rumps.notification(title=title, subtitle=subtitle, message=message)
                except Exception:
                    pass

            _on_main(fallback)

    threading.Thread(target=work, daemon=True).start()


def _on_main(fn: Callable[[], None]) -> None:
    """Run ``fn`` on the main thread (Cocoa's main operation queue)."""
    if NSOperationQueue is None:
        fn()
        return
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def _log(message: str) -> None:
    print(f"cc-switcher: {message}", file=sys.stderr, flush=True)


def _error_message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class _UniqueTitles:
    """rumps keys menu items by title and silently drops a duplicate, so make titles unique."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def __call__(self, title: str) -> str:
        while title in self.seen:
            title += "​"
        self.seen.add(title)
        return title


# --------------------------------------------------------------------------- app state


@dataclass
class _View:
    accounts: list[Any] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)  # account id -> monitor.cache_status()
    status: Any = None  # switcher.Status, or None until the first worker pass
    settings: Any = None
    mcp: list[Any] | None = None  # list[ServerStatus]; None = unavailable
    error: str = ""

    @property
    def snapshots(self) -> dict[str, Any]:
        return {i: e["snapshot"] for i, e in self.entries.items() if e.get("snapshot") is not None}

    @property
    def active_entry(self) -> dict[str, Any] | None:
        return self.entries.get(self.active_id) if self.active_id else None

    @property
    def active_id(self) -> str | None:
        return self.status.active.id if self.status is not None and self.status.active is not None else None

    @property
    def unregistered(self) -> bool:
        return bool(self.status is not None and self.status.unregistered_live_login)


def display_name(account: Any) -> str:
    """'work · me@example.com' for a labelled account, else Account.name (never empty)."""
    return f"{account.label} · {account.email}" if account.label and account.email else account.name


def _sorted_accounts(accounts: list[Any]) -> list[Any]:
    # Registry order changes whenever an account is saved again; sort so the menu doesn't jump around.
    return sorted(accounts, key=lambda a: (a.added_at or "", a.email.lower(), a.id))


_Base = rumps.App if rumps is not None else object


class SwitcherApp(_Base):  # type: ignore[misc, valid-type]
    def __init__(self) -> None:
        icon = Path(__file__).parent / "resources" / "icon.png"
        if icon.is_file():
            super().__init__("", icon=str(icon), template=True, quit_button=None)
        else:
            super().__init__("CC", quit_button=None)
        self._view = _View()
        self._menu_key: Any = None
        self._line_items: dict[tuple[str, int], Any] = {}
        self._banner_item: Any = None
        self._refresh_item: Any = None
        self._state_lock = threading.Lock()
        self._running = False
        self._pending = False
        self._pending_fetch = False
        self._last_fetch_at = float("-inf")
        self._last_auto_attempt: float | None = None
        self._tried_first_import = False
        self._switching = False
        self._adding = False

        try:
            self._view = self._cached_view()
        except Exception as exc:
            _log(f"could not read saved state: {_error_message(exc)}")
            self._view.error = _error_message(exc)
        try:
            self._apply_view(self._view)
        except Exception as exc:
            _log(f"could not build the menu: {_error_message(exc)}")
            self.menu.add(rumps.MenuItem("cc-switcher could not start (see the log)", callback=None))
            self.menu.add(rumps.MenuItem("\u23FB  Quit", callback=rumps.quit_application))
        self._timer = rumps.Timer(self._on_tick, TICK_SECONDS)
        self._timer.start()
        self._request(fetch=True)

    # ------------------------------------------------------------------ views

    def _cached_view(self) -> _View:
        """Registry + cached usage from local files only (cheap, safe on the main thread)."""
        from cc_switcher import store

        accounts = store.load_accounts()
        return _View(
            accounts=accounts,
            entries=cli.cache_entries(accounts),
            settings=store.load_settings(),
            mcp=self._view.mcp,
        )

    def _structure_key(self, view: _View) -> tuple:
        from cc_switcher import launchagent

        settings = view.settings
        return (
            tuple((a.id, a.label, a.email, a.subscription_type) for a in _sorted_accounts(view.accounts)),
            view.active_id,
            view.unregistered,
            bool(settings and settings.auto_switch_enabled),
            bool(settings and settings.notifications_enabled),
            launchagent.is_installed(),
            None if view.mcp is None else tuple((s.name, s.auth, s.configured) for s in view.mcp),
            tuple(len(entry_lines(view.entries.get(a.id))) for a in _sorted_accounts(view.accounts)),
            self._banner(view) is not None,
            view.error,
        )

    def _banner(self, view: _View) -> str | None:
        mode = view.settings.menubar_title if view.settings is not None else "session"
        return stale_banner(view.active_entry, mode)

    def _apply_view(self, view: _View) -> None:
        """Main thread. Update the title, then the menu (in place when its shape is unchanged)."""
        self._view = view
        entry = view.active_entry
        snapshot = entry.get("snapshot") if entry else None
        mode = view.settings.menubar_title if view.settings is not None else "session"
        self.title = title_text(snapshot, mode, stale=bool(entry and cli.entry_is_stale(entry)))
        key = self._structure_key(view)
        if key != self._menu_key:
            self._rebuild_menu(view)
            self._menu_key = key
        else:
            self._update_lines(view)

    def _update_lines(self, view: _View) -> None:
        for account in view.accounts:
            for index, line in enumerate(entry_lines(view.entries.get(account.id))):
                item = self._line_items.get((account.id, index))
                if item is not None:
                    item.title = LINE_INDENT + line
        banner = self._banner(view)
        if banner is not None and self._banner_item is not None:
            self._banner_item.title = banner

    # ------------------------------------------------------------------ menu

    def _account_title(self, account: Any, active: bool, shared_email: bool) -> str:
        name = display_name(account)
        if shared_email and account.organization_name:
            name += f" · {account.organization_name}"
        plan = f" ({account.subscription_type.capitalize()})" if account.subscription_type else ""
        return f"{MARKER if active else HOLLOW}  {name}{plan}"

    def _rebuild_menu(self, view: _View) -> None:
        self.menu.clear()
        self._line_items = {}
        unique = _UniqueTitles()
        accounts = _sorted_accounts(view.accounts)
        emails = [a.email for a in accounts if a.email]

        self._banner_item = None
        banner = self._banner(view)
        if banner is not None:  # first line: explains the ~ after the menu bar number
            self._banner_item = rumps.MenuItem(unique(banner), callback=None)
            self.menu.add(self._banner_item)
            self.menu.add(rumps.separator)
        if not accounts:
            self.menu.add(rumps.MenuItem(unique("No accounts saved yet"), callback=None))
            self.menu.add(rumps.MenuItem(unique(f"{LINE_INDENT}Sign in with `claude`, then choose Import current login"), callback=None))
        for account in accounts:
            active = account.id == view.active_id
            item = rumps.MenuItem(
                unique(self._account_title(account, active, bool(account.email) and emails.count(account.email) > 1)),
                callback=self._on_account_click,
            )
            item._account_id = account.id
            item._account_name = account.name
            self.menu.add(item)
            for index, line in enumerate(entry_lines(view.entries.get(account.id))):
                line_item = rumps.MenuItem(unique(LINE_INDENT + line), callback=None)
                self._line_items[(account.id, index)] = line_item
                self.menu.add(line_item)
        if view.error:
            self.menu.add(rumps.MenuItem(unique(f"{WARN} {view.error[:120]}"), callback=None))

        self.menu.add(rumps.separator)
        self._refresh_item = rumps.MenuItem(unique("↻  Refresh usage"), callback=self._on_refresh)
        self.menu.add(self._refresh_item)
        self.menu.add(rumps.MenuItem(unique("⇄  Switch to best available"), callback=self._on_best))
        self.menu.add(rumps.MenuItem(unique("✚  Add account…"), callback=self._on_add))
        import_title = f"{WARN}  Import current login (not saved yet)" if view.unregistered else "⤓  Import current login"
        self.menu.add(rumps.MenuItem(unique(import_title), callback=self._on_import))
        if accounts:
            remove_menu = rumps.MenuItem(unique("−  Remove account"))
            sub_unique = _UniqueTitles()
            for account in accounts:
                suffix = " (active)" if account.id == view.active_id else ""
                label = display_name(account)
                if account.email and emails.count(account.email) > 1 and account.organization_name:
                    label += f" · {account.organization_name}"
                entry = rumps.MenuItem(sub_unique(label + suffix), callback=self._on_remove_click)
                entry._account_id = account.id
                entry._account_name = account.name
                remove_menu.add(entry)
            self.menu.add(remove_menu)

        self.menu.add(rumps.separator)
        settings = view.settings
        auto = rumps.MenuItem(unique("Auto-switch when a limit is reached"), callback=self._on_toggle)
        auto._setting = "auto_switch_enabled"
        auto.state = 1 if settings is not None and settings.auto_switch_enabled else 0
        self.menu.add(auto)
        notifications = rumps.MenuItem(unique("Notifications"), callback=self._on_toggle)
        notifications._setting = "notifications_enabled"
        notifications.state = 1 if settings is not None and settings.notifications_enabled else 0
        self.menu.add(notifications)
        self.menu.add(self._mcp_menu(view, unique))

        from cc_switcher import launchagent

        login = rumps.MenuItem(unique("Start at login"), callback=self._on_toggle_login)
        login.state = 1 if launchagent.is_installed() else 0
        self.menu.add(login)
        self.menu.add(rumps.MenuItem(unique("Open data folder"), callback=self._on_open_folder))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem(unique("⏻  Quit"), callback=rumps.quit_application))

    def _mcp_menu(self, view: _View, unique: _UniqueTitles) -> Any:
        rows = view.mcp
        needs = rows is not None and any(r.auth == "needs_sign_in" for r in rows)
        parent = rumps.MenuItem(unique(f"MCP servers{' ' + WARN if needs else ''}"))
        sub_unique = _UniqueTitles()
        if rows is None:
            parent.add(rumps.MenuItem(sub_unique("Unavailable right now"), callback=None))
            return parent
        shown = [r for r in rows if r.configured or r.in_profile]
        if not shown:
            parent.add(rumps.MenuItem(sub_unique("No servers configured"), callback=None))
        words = {
            "signed_in": "signed in",
            "needs_sign_in": "needs sign-in (run claude, then /mcp)",
            "header_auth": "header token",
            "not_oauth": "local",
            "not_configured": "not installed (ccs mcp apply)",
        }
        for row in shown:
            mark = WARN if row.auth == "needs_sign_in" else ("✓" if row.configured else HOLLOW)
            parent.add(rumps.MenuItem(sub_unique(f"{mark}  {row.name} — {words.get(row.auth, row.auth)}"), callback=None))
        return parent

    # ------------------------------------------------------------------ cycle (worker threads)

    def _request(self, fetch: bool) -> None:
        """Start a refresh cycle unless one is running; then coalesce into one follow-up."""
        with self._state_lock:
            if self._running:
                self._pending = True
                self._pending_fetch = self._pending_fetch or fetch
                return
            self._running = True
        if fetch:
            self._set_refreshing(True)
        threading.Thread(target=self._run_cycles, args=(fetch,), daemon=True).start()

    def _set_refreshing(self, refreshing: bool) -> None:
        def update() -> None:
            if self._refresh_item is not None:
                self._refresh_item.title = "↻  Refreshing…" if refreshing else "↻  Refresh usage"

        _on_main(update)

    def _run_cycles(self, fetch: bool) -> None:
        while True:
            try:
                view, alerts, auto = self._collect(fetch)
            except Exception as exc:  # keep the app alive whatever a module does
                _log(f"refresh failed: {_error_message(exc)}")
                view, alerts, auto = self._failed_view(_error_message(exc)), [], None
            _on_main(lambda v=view, a=alerts, r=auto: self._finish_cycle(v, a, r))
            with self._state_lock:
                if not self._pending:
                    self._running = False
                    break
                fetch, self._pending, self._pending_fetch = self._pending_fetch, False, False
        self._set_refreshing(False)  # queued after the last _finish_cycle, so the title ends up correct

    def _failed_view(self, message: str) -> _View:
        previous = self._view
        return _View(
            accounts=previous.accounts,
            entries=previous.entries,  # a failed cycle must not wipe or replace the numbers on screen
            status=previous.status,
            settings=previous.settings,
            mcp=previous.mcp,
            error=f"Refresh failed: {message}",
        )

    def _finish_cycle(self, view: _View, alerts: list[Any], auto: dict | None) -> None:
        try:
            self._apply_view(view)
        except Exception as exc:  # a drawing problem must not stop notifications or later cycles
            _log(f"could not update the menu: {_error_message(exc)}")
        for alert in alerts:
            notify(alert.message, title=alert.title)
        if auto is not None:
            self._announce_auto_switch(auto)

    def _collect(self, fetch: bool) -> tuple[_View, list[Any], dict | None]:
        from cc_switcher import monitor, store, switcher

        errors: list[str] = []
        settings = store.load_settings()
        alerts: list[Any] = []
        auto: dict | None = None

        if fetch:
            self._last_fetch_at = time.monotonic()
            self._first_run_import(store, switcher)
            try:
                switcher.save_active_login()
            except Exception as exc:
                errors.append(_error_message(exc))
        accounts = store.load_accounts()
        if fetch and accounts:
            try:
                # Reuse a good sample less than 60s old: several UI actions can otherwise
                # issue identical usage requests seconds apart. Failed/stale samples still retry.
                monitor.refresh_all(accounts, reuse_fresh_seconds=60)
            except Exception as exc:
                errors.append(_error_message(exc))
        entries = cli.cache_entries(accounts)  # numbers + age + whether the last refresh failed
        snapshots = {i: e["snapshot"] for i, e in entries.items() if e.get("snapshot") is not None}
        status = switcher.status()
        mcp = self._view.mcp
        if fetch:
            mcp = self._read_mcp()

        if fetch:
            if settings.notifications_enabled and accounts:
                try:
                    alerts = list(monitor.detect_alerts(snapshots, accounts, settings.alert_thresholds))
                except Exception as exc:
                    errors.append(_error_message(exc))
            auto = self._maybe_auto_switch(settings, status, accounts, snapshots, switcher)
            if auto is not None and auto["status"] == "switched":
                status = switcher.status()
        view = _View(
            accounts=accounts,
            entries=entries,
            status=status,
            settings=settings,
            mcp=mcp,
            error="; ".join(errors),
        )
        return view, alerts, auto

    def _first_run_import(self, store: Any, switcher: Any) -> None:
        if self._tried_first_import:
            return
        self._tried_first_import = True
        if store.load_accounts():
            return
        try:
            account = switcher.import_current()
        except Exception as exc:
            notify(
                f"{_error_message(exc)} Sign in with `claude`, then choose Import current login.",
                subtitle="No account saved yet",
            )
            return
        notify(f"{account.name} ({account.subscription_type or 'plan unknown'})", subtitle="Claude account imported")

    def _read_mcp(self) -> list[Any] | None:
        try:
            from cc_switcher import mcp_profile

            return list(mcp_profile.status())
        except Exception as exc:
            _log(f"MCP status unavailable: {_error_message(exc)}")
            return None

    def _maybe_auto_switch(self, settings: Any, status: Any, accounts: list[Any], snapshots: dict, switcher: Any) -> dict | None:
        if not settings.auto_switch_enabled or self._adding or self._switching or status.active is None:
            return None
        from cc_switcher import autoswitch

        active = status.active
        snapshot = snapshots.get(active.id)
        if snapshot is None or not snapshot.ok or not autoswitch.should_switch(snapshot, settings):
            return None
        now = time.monotonic()
        if self._last_auto_attempt is not None and now - self._last_auto_attempt < AUTO_SWITCH_COOLDOWN_SECONDS:
            return None
        self._last_auto_attempt = now
        level = snapshot.max_utilization(settings.auto_switch_windows)
        reason = f"{active.name} is at {level:.0f}%" if level is not None else f"{active.name} reached its limit"
        target = autoswitch.choose_target(active.id, accounts, snapshots, settings)
        if target is None:
            return {"status": "no_target", "reason": reason}
        try:
            switcher.switch_to(target.id)
        except Exception as exc:
            return {"status": "error", "reason": reason, "message": _error_message(exc)}
        return {"status": "switched", "reason": reason, "target": target.name}

    def _announce_auto_switch(self, auto: dict) -> None:
        if auto["status"] == "switched":
            notify(f"{auto['reason']}, so I switched to {auto['target']}. Running sessions pick it up shortly.", subtitle="Auto-switched account")
        elif auto["status"] == "no_target":
            notify(f"{auto['reason']}, and no other account has room.", subtitle="Limit reached")
        else:
            notify(f"{auto['reason']}, but switching failed: {auto['message']}", subtitle="Auto-switch failed")

    # ------------------------------------------------------------------ actions

    def _run_async(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
        what: str = "That",
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        """Run ``work`` on a worker thread; report failure with an alert, success via ``done``."""

        def target() -> None:
            try:
                result = work()
                error = None
            except Exception as exc:
                result, error = None, _error_message(exc)

            def finish() -> None:
                if cleanup:
                    cleanup()
                if error is not None:
                    rumps.alert(title="cc-switcher", message=f"{what} failed: {error}")
                elif done:
                    done(result)

            _on_main(finish)

        threading.Thread(target=target, daemon=True).start()

    def _on_tick(self, _sender: Any) -> None:
        try:
            from cc_switcher import store

            interval = store.load_settings().poll_interval_seconds
        except Exception:
            interval = 300
        due = time.monotonic() - self._last_fetch_at >= interval
        self._request(fetch=due)  # between polls this only re-checks who is active

    def _on_refresh(self, _sender: Any) -> None:
        self._request(fetch=True)

    def _on_account_click(self, sender: Any) -> None:
        account_id, name = sender._account_id, sender._account_name
        if account_id == self._view.active_id:
            return
        if self._switching:
            notify("Wait for the current switch to finish.", subtitle="Switch already running")
            return
        self._switching = True

        def work() -> None:
            from cc_switcher import switcher

            switcher.switch_to(account_id)

        def done(_result: Any) -> None:
            notify("New claude sessions use it immediately; running sessions pick it up shortly.", subtitle=f"Switched to {name}")
            self._request(fetch=True)

        def cleanup() -> None:
            self._switching = False

        self._run_async(work, done, "Switching", cleanup)

    def _on_best(self, _sender: Any) -> None:
        if self._switching:
            return
        self._switching = True

        def work() -> Any:
            from cc_switcher import autoswitch, monitor, store, switcher

            switcher.save_active_login()
            accounts = store.load_accounts()
            monitor.refresh_all(accounts, reuse_fresh_seconds=60)  # a recent successful sample is still fresh
            status = switcher.status()
            active_id = status.active.id if status.active is not None else None
            target = autoswitch.choose_target(active_id, accounts, monitor.load_cache(), store.load_settings())
            if target is not None:
                switcher.switch_to(target.id)
            return target

        def done(target: Any) -> None:
            if target is None:
                notify("Every other account is at its limit, or its usage is unknown or more than 30 minutes old.", subtitle="No account has room right now")
            else:
                notify("New claude sessions use it immediately; running sessions pick it up shortly.", subtitle=f"Switched to {target.name}")
                self._request(fetch=True)

        def cleanup() -> None:
            self._switching = False

        self._run_async(work, done, "Switching", cleanup)

    def _on_import(self, _sender: Any) -> None:
        def work() -> Any:
            from cc_switcher import switcher

            return switcher.import_current()

        def done(account: Any) -> None:
            notify(account.name, subtitle="Account saved")
            self._request(fetch=True)

        self._run_async(work, done, "Importing")

    def _on_add(self, _sender: Any) -> None:
        if self._adding:
            notify("Finish the sign-in in the Terminal window first.", subtitle="Already adding an account")
            return
        self._adding = True

        def work() -> Any:
            return self._add_in_terminal()

        def done(result: Any) -> None:
            if result == "0":
                notify("The new account is saved and active.", subtitle="Account added")
            self._request(fetch=True)

        def cleanup() -> None:
            self._adding = False

        self._run_async(work, done, "Adding an account", cleanup)

    def _add_in_terminal(self) -> str | None:
        """Open Terminal running `ccs add` (login needs a browser and visible prompts), wait for it.

        Returns the command's exit status as text, or None if we stopped waiting. Even then the
        next timer tick notices the new account in the registry.
        """
        from cc_switcher import fsutil, launchagent

        fsutil.ensure_private_dir(paths.app_dir())
        marker = paths.app_dir() / ".add-finished"
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        command = (
            f"{shlex.join([*launchagent.resolve_ccs(), 'add'])}; echo $? > {shlex.quote(str(marker))}; "
            "echo; echo 'Finished. You can close this window.'"
        )
        script = f'tell application "Terminal"\nactivate\ndo script {_applescript_string(command)}\nend tell'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"could not open Terminal ({(result.stderr or '').strip() or 'osascript failed'})")
        deadline = time.monotonic() + ADD_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if marker.exists():
                try:
                    status = marker.read_text(encoding="utf-8").strip()
                finally:
                    try:
                        marker.unlink()
                    except FileNotFoundError:
                        pass
                return status
            time.sleep(ADD_POLL_SECONDS)
        return None

    def _on_remove_click(self, sender: Any) -> None:
        account_id, name = sender._account_id, sender._account_name
        if account_id == self._view.active_id:
            rumps.alert(
                title="Can’t remove the active account",
                message="Switch to another account first, or run `ccs remove --force` in Terminal.",
            )
            return
        answer = rumps.alert(
            title=f"Remove {name}?",
            message="Its saved sign-in is deleted from cc-switcher. The Claude account itself is untouched; add it again later with Add account….",
            ok="Remove",
            cancel="Cancel",
        )
        if not answer:
            return

        def work() -> None:
            from cc_switcher import switcher

            switcher.remove_account(account_id)

        def done(_result: Any) -> None:
            notify(name, subtitle="Account removed")
            self._request(fetch=False)

        self._run_async(work, done, "Removing")

    def _on_toggle(self, sender: Any) -> None:
        setting = sender._setting

        def work() -> None:
            from cc_switcher import fsutil, store

            with fsutil.app_lock():
                settings = store.load_settings()
                setattr(settings, setting, not getattr(settings, setting))
                store.save_settings(settings)

        self._run_async(work, lambda _r: self._request(fetch=False), "Saving the setting")

    def _on_toggle_login(self, _sender: Any) -> None:
        def work() -> bool:
            from cc_switcher import launchagent

            if launchagent.is_installed():
                launchagent.uninstall(unload=False)  # unloading would terminate this very process
                return False
            launchagent.install(reload=False)
            return True

        def done(enabled: bool) -> None:
            notify("cc-switcher starts when you log in." if enabled else "cc-switcher will not start at your next login.", subtitle="Start at login")
            self._request(fetch=False)

        self._run_async(work, done, "Changing start at login")

    def _on_open_folder(self, _sender: Any) -> None:
        def work() -> None:
            from cc_switcher import fsutil

            fsutil.ensure_private_dir(paths.app_dir())
            subprocess.run(["open", str(paths.app_dir())], capture_output=True, timeout=15)

        self._run_async(work, None, "Opening the folder")


# --------------------------------------------------------------------------- entry point


def _acquire_single_instance() -> Any:
    """Hold an exclusive lock for the life of the process; None if another menu bar app has it."""
    from cc_switcher import fsutil

    fsutil.ensure_private_dir(paths.app_dir())
    fd = os.open(str(paths.app_dir() / ".menubar.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main() -> int:
    if rumps is None or AppKit is None:
        raise MenubarError("The menu bar app needs the 'rumps' package on macOS. Re-run scripts/install.sh.")
    lock = _acquire_single_instance()
    if lock is None:
        print("The cc-switcher menu bar app is already running.")
        return 0
    # Accessory = no Dock icon, no app menu bar; just the status item.
    policy = getattr(AppKit, "NSApplicationActivationPolicyAccessory", 1)
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(policy)
    SwitcherApp().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
