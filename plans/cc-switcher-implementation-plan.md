# cc-switcher — implementation plan

Status: v0.1 build in progress. This file is the **contract** parallel builders code against.
Do not change a signature listed here without updating this file.

## Goal

A macOS-only Claude Code account switcher (menu bar app + `ccs` CLI), modelled on
`ref/claude-account-switcher`, with these differences:

1. **Switching swaps only the Claude login.** Claude Code keeps the login (`claudeAiOauth`) and
   every remote-MCP token (`mcpOAuth`, `mcpOAuthClientConfig`) in ONE Keychain item
   (`Claude Code-credentials`). The reference swapped the whole item, so MCP sign-ins followed the
   account. We replace only `claudeAiOauth`; MCP sign-ins stay fixed across accounts.
2. **Fixed local MCP set per Mac.** claude.ai connectors are per-account on Anthropic's side and
   can't be pinned. Instead the user keeps a portable *MCP profile* (user-scope remote MCP servers,
   no secrets) and applies it on each Mac. Connectors stay enabled as per-account extras.
3. **Production-grade usage monitoring** for every saved account: session (5h) %, weekly %,
   per-model weekly %, reset times, extra-usage credits, promo credits; threshold alerts;
   optional auto-switch; history log.
4. **Claude only** (no Codex). **macOS only.** Install on other Macs via `scripts/install.sh`.

## Facts verified against Claude Code 2.1.330 (binary strings + this Mac's Keychain)

| Fact | Value |
|---|---|
| Live Keychain service | `Claude Code-credentials`; with `CLAUDE_CONFIG_DIR` set: `+ "-" + sha256(dir)[:8]` (`paths.claude_keychain_service()`) |
| Keychain `-a` attribute | `$USER` if `[a-zA-Z0-9._-]+`, else `claude-code-user` (`paths.keychain_account_name()`) |
| How Claude Code writes | `security -i` stdin: `add-generic-password -U -a "<acct>" -s "<svc>" -X "<hex>"`; argv fallback when the line > 4032 chars |
| Blob top-level keys | `claudeAiOauth`, `mcpOAuth`, `mcpOAuthClientConfig` (+ future keys: preserve unknowns) |
| `claudeAiOauth` fields | accessToken, refreshToken, expiresAt (epoch **ms**), refreshTokenExpiresAt, scopes[], subscriptionType, subscriptionTypeSource, rateLimitTier, accountUuid, organizationUuid |
| MCP token key | `f"{name}\|{sha256(json.dumps({'type','url','headers'or{}}, separators=(',',':'), ensure_ascii=False))[:16]}"` — verified on `sentry\|800cb29a3ac61727`, `atlassian\|1eb778bd626fb68d` |
| Empty MCP entries | Claude Code writes `accessToken: ""` stubs when a server needs sign-in. "Signed in" = non-empty accessToken or refreshToken |
| Token refresh | `POST https://platform.claude.com/v1/oauth/token` JSON `{grant_type:"refresh_token", refresh_token, client_id:"9d1c250a-e61b-44d9-88ed-5944d1962f5e", scope:" ".join(scopes)}` → `{access_token, refresh_token?(absent ⇒ keep old), expires_in (s), refresh_token_expires_in?, scope?}` |
| Usage endpoint | `GET https://api.anthropic.com/api/oauth/usage`, headers `Authorization: Bearer <access>`, `anthropic-beta: oauth-2025-04-20`. Claude Code caches it 60 s |
| Usage response | `{five_hour, seven_day, seven_day_oauth_apps, seven_day_opus, seven_day_sonnet, cinder_cove, wattle_ember, extra_usage, limits}` — windows are `{utilization (percent), resets_at (ISO)}` or null; `wattle_ember` adds `label, limit_dollars, used_dollars, remaining_dollars, periods[{starts_at, ends_at}]`; `extra_usage = {is_enabled, monthly_limit, used_credits, utilization, currency, disabled_reason}` (minor units, cents for USD); `limits = [{kind, group, percent, resets_at, severity, is_active, scope:{model:{display_name}, surface:{display_name}}}]`. All objects are passthrough: tolerate unknown keys/codenames |
| Redeemable limit resets | Same endpoint with `?cedar_ember=1&skip_spend=1` (what `/limit-reset` reads). Response block `cedar_ember = {eligible, ineligible_reason, at_limit, exhausted[], grants[{id, label, type: "session"\|"weekly"\|"full", resets_total, resets_left, starts_at, ends_at, clears[], paused, usable_now, use_requires_limit, percent_used{}, blocking[], gives_back_weekly, unavailable_reason}], next_grant_id, weekly_resets_at, cooldown_until, event_props}`. **Never** call `at_wall=1` (records offer impressions) and **never** POST `/api/organizations/{org}/reset_rate_limits` (that spends a reset). Display only |
| Claude Code token refresh is CAS | it only writes a refreshed login if the stored refreshToken still equals the one it used, so a switch is not clobbered by a running session |
| claude.ai connectors | listed from `/v1/mcp_servers` with the logged-in account's token, proxied via `mcp-proxy.anthropic.com`; switchable off with `disableClaudeAiConnectors` setting / `ENABLE_CLAUDEAI_MCP_SERVERS=false` |

## Invariants (every builder must respect)

- **I1 — MCP state is never swapped or lost.** The live item is only ever written as
  `creds.merge_login(<freshly read live blob>, <login>)` or `creds.restore_missing_mcp(...)`.
- **I2 — Saved items hold only the login:** `{"claudeAiOauth": {...}}` (`creds.saved_item`).
- **I3 — Never refresh the live login.** It is shared with running Claude Code sessions; a rotated
  refresh token would log them out. Only refresh *saved* logins of accounts that are not live,
  and decide "not live" while holding `fsutil.app_lock()`.
- **I4 — Identity comes from the token, not from bookkeeping.** "Active account" = the registry
  account whose id equals `make_account_id(*creds.identity(live claudeAiOauth))`. Never trust a
  stored "active" flag (the user can `/login` inside Claude Code at any time).
- **I5 — Before overwriting the live login, save it** into its account's saved item (keeps rotated
  tokens). If the live login belongs to no registered account, auto-import it first.
- **I6 — Locking:** every public operation that reads-then-writes Keychain items or app files takes
  `fsutil.app_lock()` exactly once at the top (it is NOT re-entrant). Never hold it while a
  long-running interactive command runs (`claude auth login`).
- **I7 — No secrets in files, logs, argv (when avoidable), exceptions or notifications.**
  App files are 0600 in a 0700 dir. Usage responses contain no secrets.
- **I8 — Testability:** call `keychain.read(...)`, `paths.x()` etc. via the module attribute
  (`from cc_switcher import keychain`), never `from cc_switcher.keychain import read`, so the
  test fakes apply. Every subprocess / HTTP call takes an injectable `runner=` / `opener=` /
  `http_post=` parameter (default = real implementation).

## Foundation (DONE — read these files, don't rewrite them)

- `paths.py` — all paths/URLs/constants as functions; env overrides for tests.
- `models.py` — `Account`, `Settings`, `UsageWindow`, `ExtraUsage`, `PromoCredit`, `UsageSnapshot`,
  `WINDOW_LABELS`, `make_account_id`, `utcnow`.
- `keychain.py` — `read(service)`, `read_account_attr(service)`, `write(service, account, secret)`,
  `delete(service) -> bool`, `KeychainError`.
- `creds.py` — `parse_blob`, `dump_blob`, `get_login`, `identity`, `login_account_id`, `saved_item`,
  `merge_login`, `non_login_parts`, `restore_missing_mcp`, `LOGIN_KEY`, `CredentialsError`.
- `fsutil.py` — `app_lock(timeout)`, `atomic_write_json`, `atomic_write_text`, `read_json`,
  `ensure_private_dir`, `LockTimeout`.
- `store.py` — `load_accounts`, `save_accounts`, `get_account`, `upsert_account`, `remove_account`,
  `find_account(query)`, `load_settings`, `save_settings`, `AccountNotFound`, `AmbiguousAccount`.
- `tests/conftest.py` — autouse `FakeKeychain`, tmp home, subprocess + network blocked; `samples`
  fixture (`make_login`, `make_oauth_account`, `ACCOUNT_A/B`, `SAMPLE_MCP_OAUTH`).

## Work packages (run in parallel; each owns only its files)

### WP-A Core switching — `claude_state.py`, `switcher.py`, tests for foundation + these

`claude_state.py`
```python
class ClaudeStateError(RuntimeError): ...
def read_claude_json() -> dict                      # {} when missing/invalid
def read_oauth_account() -> dict | None
def write_oauth_account(oauth_account: dict | None) -> None
    # Read-modify-write ~/.claude.json (paths.claude_json()): set "oauthAccount" (delete key if None),
    # preserve every other key, the file mode, and key order. Atomic temp+rename in the same dir.
    # Claude Code rewrites this file often: re-stat right before replace; if mtime_ns/size changed
    # since the read, re-read and retry (max 5). Missing file -> create it with just oauthAccount.
```

`switcher.py`
```python
class SwitchError(RuntimeError): ...
@dataclass
class Status:
    live_account_id: str | None          # identity of the live login, registered or not
    active: Account | None               # registered account matching it
    unregistered_live_login: bool        # live login exists but isn't saved yet
    accounts: list[Account]
def find_claude() -> str | None          # shutil.which + ~/.local/bin, /opt/homebrew/bin, /usr/local/bin
def live_blob() -> dict                  # parse_blob(keychain.read(paths.claude_keychain_service()))
def live_account_id() -> str | None
def status() -> Status
def active_account() -> Account | None
def import_current(label: str = "") -> Account
    # Lock. Requires a live login (SwitchError otherwise). Account fields from the login
    # (uuids, subscriptionType, rateLimitTier) + ~/.claude.json oauthAccount when its accountUuid
    # matches (email, org name, display name). If email still unknown, try
    # `claude auth status --json` via injectable runner. Writes saved item (I2) and upserts the
    # registry; keeps existing label/added_at; sets label if given.
def save_active_login() -> Account | None
    # Lock. If live login is registered and differs from its saved item, overwrite the saved item.
    # Returns the active account (or None). Called before every usage refresh and switch.
def switch_to(account_id: str) -> Account
    # Lock. Steps: read live -> I5 (save/auto-import outgoing) -> read target saved login
    # (SwitchError "re-add" if missing/invalid/identity mismatch) -> no-op if already live ->
    # write merge_login(live, target_login) to the live service with the existing acct attr
    # (fallback paths.keychain_account_name()) -> re-read & verify identity == target and
    # non_login_parts unchanged (else restore previous live blob, raise) -> write_oauth_account
    # (target.oauth_account) (on failure restore previous live blob, raise) -> set last_used_at.
def add_account(label: str = "", runner=None) -> Account | None
    # Phase 1 (lock): save_active_login logic; snapshot `before = live_blob()`.
    # Phase 2 (NO lock): run `claude auth login` with inherited stdio (runner(argv) -> returncode).
    # Phase 3 (lock): after = live_blob(); fixed = restore_missing_mcp(after, before); write it if
    # it differs (I1). If rc != 0 or no new login -> return None (previous login is untouched or
    # restored). Else import the new login (label) and return it.
def remove_account(account_id: str, force: bool = False) -> None
    # Lock. Refuse the active account unless force. Delete saved item + registry row.
def set_label(account_id: str, label: str) -> Account
def claude_auth_status(runner=None) -> dict | None   # `claude auth status --json`
```
Tests (fake keychain): switching preserves `mcpOAuth` byte-for-byte; outgoing rotated tokens are
saved; unregistered live login is auto-imported before switching; verify failure restores the
previous live blob; claude_state failure rolls Keychain back; add flow restores MCP entries wiped by
login; remove refuses active; claude_json write preserves other keys + retries on concurrent change;
keychain.py builds the stdin command with hex and falls back to argv over 4032 chars (test by
monkeypatching `keychain._run` inside the test after the autouse fixture).

### WP-B Usage — `oauth.py`, `tokens.py`, `usage.py`, `monitor.py`, `autoswitch.py` + tests

```python
# oauth.py
class OAuthError(RuntimeError): ...            # network/5xx: transient
class RefreshRejected(OAuthError): ...         # 400/401/invalid_grant: user must re-add the account
def is_expired(login: dict, skew_seconds: int = 300, now_ms: int | None = None) -> bool
def refresh_login(login: dict, *, http_post=None, timeout: float = 15) -> dict   # returns updated login copy

# tokens.py
class NoCredentials(RuntimeError): ...
class TokenStale(RuntimeError): ...            # live login expired; Claude Code will refresh it (I3)
def access_token_for(account: Account, *, http_post=None) -> str
    # Lock. Live account -> live accessToken (TokenStale if expired; never refresh).
    # Otherwise saved item; if expired -> oauth.refresh_login -> write saved item -> return.

# usage.py
class UsageError(RuntimeError):  # .code: "auth_expired"|"rate_limited"|"network"|"http_<n>"|"bad_response"; .retry_after: float|None
def claude_code_version(runner=None) -> str    # cached; parse `claude --version`; fallback "2.1.330"
def fetch_usage_raw(access_token: str, *, opener=None, timeout: float = 10) -> dict
def parse_usage(raw: dict, account_id: str, fetched_at: datetime | None = None) -> UsageSnapshot
    # known windows via models.WINDOW_LABELS (skip nulls); any other top-level dict with
    # utilization/resets_at -> window (label from its "label" or key); a dict that also has
    # *_dollars fields -> PromoCredit; limits[] rows that have a model/surface scope -> extra windows
    # key f"limit:{kind}:{scope}" label "Weekly · <display_name>"; extra_usage -> ExtraUsage.
    # resets_at: ISO-8601 (Z or offset) or epoch seconds. Never raise on unknown shapes.
def fetch_resets_raw(access_token: str, *, opener=None, timeout: float = 10) -> dict   # GET USAGE_URL + "?cedar_ember=1&skip_spend=1"
def parse_resets(block: dict | None) -> ResetStatus | None    # parse raw["cedar_ember"]; None when absent/malformed; skip malformed grants
def format_reset(resets_at: datetime | None, now: datetime | None = None) -> str   # "now", "42m", "3h 05m", "2d 4h"
def format_money(minor_units: float | None, currency: str | None) -> str          # "$12.34", "€5.00", "12.34 GBP"
def format_percent(value: float | None) -> str                                     # "42%" / "—"

# monitor.py
@dataclass(frozen=True)
class Alert: account_id: str; kind: str; window_key: str | None; threshold: int | None; title: str; message: str
def refresh_account(account: Account, *, http_post=None, opener=None) -> UsageSnapshot   # never raises; errors -> ok=False + code
    # Also fetches resets (fetch_resets_raw -> parse_resets -> snapshot.resets) at most every
    # 30 min per account (reuse the cached ResetStatus in between); a resets failure never fails
    # the snapshot (keep previous resets or None).
def refresh_all(accounts: list[Account] | None = None, *, http_post=None, opener=None) -> dict[str, UsageSnapshot]
    # sequential, small delay between accounts; respects a per-account backoff after 429
    # (store next_allowed_at in the cache); writes cache + history; returns snapshots
def load_cache() -> dict[str, UsageSnapshot]
def save_cache(snapshots: dict[str, UsageSnapshot]) -> None
def append_history(snapshots: dict[str, UsageSnapshot]) -> None   # JSONL, trims to ~5 MB
def snapshot_to_dict(s: UsageSnapshot) -> dict ; def snapshot_from_dict(d: dict) -> UsageSnapshot
def detect_alerts(snapshots: dict[str, UsageSnapshot], accounts: list[Account], thresholds: list[int]) -> list[Alert]
    # once per (account, window, threshold, resets_at); dedupe state persisted in the cache file;
    # also "auth_expired" error alert once per account until it recovers; and a "reset" alert when
    # a window that was >= 80% drops below 20% after its resets_at passed.

# autoswitch.py  (pure)
def should_switch(active: UsageSnapshot | None, settings: Settings) -> bool
def choose_target(active_id: str | None, accounts: list[Account], snapshots: dict[str, UsageSnapshot], settings: Settings) -> Account | None
    # candidates: other accounts with ok snapshots below threshold on settings.auto_switch_windows;
    # prefer lowest weekly, then lowest session. Unknown/errored snapshots are never chosen.
```

### WP-C MCP profile — `mcp_profile.py`, `docs/MCP.md` + tests

```python
CATALOG: dict[str, dict]   # vendor remote MCP endpoints, e.g. "slack": {"type":"http","url":"https://mcp.slack.com/mcp"}
                           # include: slack, notion, linear, atlassian, figma, supabase, vercel, sentry, asana, github(note: needs PAT header)
@dataclass
class ServerStatus: name: str; type: str; url: str | None; in_profile: bool; configured: bool; auth: str
    # auth: "signed_in" | "needs_sign_in" | "header_auth" | "not_oauth" (stdio) | "not_configured"
@dataclass
class Change: action: str; name: str; config: dict | None; reason: str = ""   # "add"|"update"|"remove"|"unchanged"|"skip"
def server_key(name: str, config: dict) -> str                 # verified formula (see Facts)
def list_user_servers() -> dict[str, dict]                    # ~/.claude.json "mcpServers" (user scope)
def auth_state(name: str, config: dict, live_blob: dict) -> str
def load_profile(path: Path | None = None) -> dict            # {"version":1,"servers":{name: config}}
def save_profile(profile: dict, path: Path | None = None) -> Path
def export_profile(path: Path | None = None, names: list[str] | None = None) -> Path
    # from current user-scope servers; header values that look secret (auth|token|key|secret|cookie)
    # become "${CCS_<NAME>_<HEADER>}" placeholders; never write secrets
def add_to_profile(names: list[str], path: Path | None = None) -> dict   # from CATALOG
def plan_apply(profile: dict, current: dict[str, dict], prune: bool = False) -> list[Change]
def apply_profile(profile: dict | None = None, *, prune: bool = False, dry_run: bool = False, runner=None) -> list[Change]
    # via `claude mcp add-json --scope user <name> <json>` / `claude mcp remove --scope user <name>`;
    # placeholders resolved from os.environ, unresolved -> Change("skip", reason=...). Never edits ~/.claude.json directly.
def status(profile_path: Path | None = None) -> list[ServerStatus]
```
`docs/MCP.md`: why connectors can't be pinned, how the profile works, step-by-step for a new Mac
(apply profile → open `claude` → `/mcp` → sign in to each server once), plugin-provided servers vs
user-scope servers and duplicates, how to turn connectors off if wanted.

### WP-D App, CLI, packaging, docs — `cli.py`, `menubar.py`, `launchagent.py`, `scripts/*`, `README.md`, `docs/INSTALL.md` + tests

- `cli.py` (`main(argv=None) -> int`, argparse). Commands: `list` (default; table with active
  marker, name, email, org, plan, session %, weekly %, next reset, credits — from cache,
  `--refresh` fetches), `status [--json]`, `usage [account] [--refresh] [--json]`, `switch <query>`,
  `next` (best available via autoswitch.choose_target), `add [--label]`, `import [--label]`,
  `remove <query> [--force]`, `label <query> <label>`, `settings [key [value]]`,
  `mcp status|catalog|export [path]|add <names…>|apply [path] [--prune] [--dry-run]`,
  `menubar`, `agent install|uninstall|status`, `doctor [--online]`, `--version`.
  Exit codes 0 ok / 1 error / 2 bad usage. Friendly one-line errors, no tracebacks unless `CCS_DEBUG=1`.
- `menubar.py` (rumps): title shows active session/weekly % per `settings.menubar_title`; per-account
  rows (click to switch) with usage lines (session, weekly, per-model, resets, credits, errors);
  Refresh, Switch to best available, Add account…, Import current login (highlighted when the live
  login is unregistered), Remove ▸, Auto-switch / Notifications toggles, MCP status ▸, Start at login
  toggle (launchagent), Open data folder, Quit. Background timer every `poll_interval_seconds`:
  `switcher.save_active_login()` → `monitor.refresh_all()` → alerts → auto-switch (10-min cooldown).
  All blocking work off the main thread; UI updates via `NSOperationQueue.mainQueue()`.
  Hide the Dock icon (NSApplicationActivationPolicyAccessory).
- `launchagent.py`: `install(program: list[str] | None = None, runner=None)`, `uninstall(runner=None)`,
  `is_installed() -> bool`, `is_running(runner=None) -> bool`, `plist_dict(program) -> dict`.
  Label `paths.LAUNCH_AGENT_LABEL`; RunAtLoad, KeepAlive {SuccessfulExit: false}, ProcessType
  Interactive, LimitLoadToSessionType Aqua, logs to `paths.log_dir()`, PATH incl. ~/.local/bin,
  /opt/homebrew/bin, /usr/local/bin. `launchctl bootstrap/bootout gui/<uid>`.
- `scripts/install.sh` (idempotent; new Mac): check macOS → install uv if missing → `uv tool install
  --python 3.12 --force <repo path or $CC_SWITCHER_SOURCE>` → `ccs import` if a live login exists →
  optional `ccs mcp apply "$CC_SWITCHER_MCP_PROFILE"` → `ccs agent install`. `scripts/uninstall.sh`
  (keeps saved logins unless `--purge`).
- `README.md`, `docs/INSTALL.md` (fresh Mac, updating, moving accounts between Macs = re-add per Mac
  because tokens are per-device grants, troubleshooting, security model).

## Verification gate (after all WPs land)

1. `pytest` green. 2. `ccs --version`, `ccs doctor` offline. 3. Read-only live checks
(`ccs status`, `ccs mcp status`) — **ask the user before any command that writes Keychain/Claude
state or calls Anthropic endpoints with their tokens.** 4. Independent code review of WP-A.
