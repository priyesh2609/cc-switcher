# cc-switcher

Switch between Claude Code accounts from your macOS menu bar or the `ccs` command, without losing your MCP sign-ins. It also shows how much of each account's limits you have used.

## Why it exists

Claude Code keeps your Claude login and every remote MCP sign-in in one Keychain item, `Claude Code-credentials`. An account switcher that saves and restores that whole item makes the MCP sign-ins follow the account.

Here is what that looks like. You are on `work` and sign in to Sentry's MCP server. You switch to `personal`: the switcher restores the item it saved for `personal`, which has no Sentry token, so `/mcp` shows Sentry as needing sign-in. Switch back to `work` and you get the copy saved when you left it. Every account ends up with its own half-signed-in set of MCP servers, and you keep signing in again.

cc-switcher replaces only the `claudeAiOauth` part of that item (the Claude login) and leaves `mcpOAuth` and `mcpOAuthClientConfig` as they were. Sign in to Sentry once on this Mac and it stays signed in whichever account is active.

One thing it cannot fix: claude.ai connectors (the `claude.ai Slack` kind) belong to a Claude account on Anthropic's side, so they change when the account changes. For a set of MCP servers that is the same on every account and every Mac, cc-switcher keeps a portable MCP profile (a JSON file with no secrets) that you apply with `ccs mcp apply`. See [docs/MCP.md](docs/MCP.md).

## Features

- **Switching that keeps MCP sign-ins.** Only the Claude login is swapped; the MCP tokens never move.
- **One-click or one-command.** Click an account in the menu bar, or run `ccs switch work` / `ccs next`.
- **Usage for every saved account.** Session (5h) and weekly percentages, per-model weekly limits, reset times, extra-usage credits remaining out of the monthly limit, and promo credits.
- **Experimental inactive-account renewal, opt-in.** When enabled and an inactive account's access token has expired, cc-switcher invokes a noninteractive Claude Code model request in a separate configuration and Keychain context. The request consumes plan usage and may incur charges. The feature is off by default.
- **Redeemable limit resets.** How many you have left on each account and when they expire, shown next to the usage. cc-switcher only displays them: you spend one inside Claude Code with the `/limit-reset` command, and cc-switcher never spends one for you.
- **Honest about data age.** Usage numbers show their age. If a transient refresh fails (for example, rate limit or offline), the last good numbers stay on screen, marked `~` and "stale"; an unusable login can instead show an error. Numbers that are too old are ignored when choosing an account to switch to.
- **Alerts.** A notification when a window crosses 80%, 95% and 100% (configurable), when a window resets, and when an account needs to sign in again.
- **Optional auto-switch.** Off by default. When on, it moves to the account with the most room once the active one reaches your threshold, with a 10-minute cooldown, and tells you what it did.
- **A fixed MCP set per Mac.** `ccs mcp export` / `apply` carries your user-scope servers between Macs, and `ccs mcp status` shows which are signed in.
- **Keychain-backed logins.** Saved logins are stored in the Keychain. If Keychain fails during isolated renewal, a protected plaintext recovery file may retain credentials; see [Security](#security). cc-switcher does not directly refresh the active login.
- **`ccs doctor`.** Checks the whole setup and prints a fix for anything that is wrong.

## Requirements

macOS 12 or later, a recent Claude Code installation, and an internet connection. The experimental renewal implementation was checked against Claude Code 2.1.330. The installer sets up everything else (uv and Python 3.12).

## Quick start

```bash
git clone --branch main https://github.com/priyesh2609/cc-switcher.git
cd cc-switcher
scripts/install.sh
```

The installer saves the account you are signed in to, starts the menu bar app, and runs `ccs doctor`. Then, for each other account:

```bash
ccs add --label personal     # opens the Claude sign-in in your browser
ccs list                     # all accounts with usage
ccs switch personal          # or click it in the menu bar
```

Experimental CLI-driven renewal is off by default. To enable it on a trusted installation:

```bash
ccs settings claude_refresh_enabled true
```

To disable it again, run `ccs settings claude_refresh_enabled false`. When disabled, an expired inactive login cannot be renewed by cc-switcher; its last good usage may become stale until the login is renewed. Full steps, other Macs, costs and troubleshooting: [docs/INSTALL.md](docs/INSTALL.md).

## Commands

Run `ccs` with no command to list accounts. Exit codes: 0 ok, 1 error, 2 bad usage. Set `CCS_DEBUG=1` to see a traceback instead of a one-line error.

| Command | What it does |
|---|---|
| `ccs list [--refresh] [--json]` | Accounts with plan, session %, weekly %, next reset, credits and limit resets left. Uses the cache unless `--refresh`. |
| `ccs status [--json]` | The active account with every usage window, credits, limit resets and any error in plain words. |
| `ccs usage [account] [--refresh] [--json]` | The same detail for one account or all of them. |
| `ccs switch <account>` | Make an account the active one. Label, email, id or a unique prefix all work. |
| `ccs next [--cached]` | Switch to the best other account that has room, judged by fresh usage. |
| `ccs add [--label NAME]` | Sign in to another account (`claude auth login`) and save it. |
| `ccs import [--label NAME]` | Save the account you are signed in to right now. |
| `ccs import --file PATH [--label NAME]` | Import a private JSON/text credential copy offline as a new saved account, without activating it. Never overwrites an existing account. |
| `ccs remove <account> [--force]` | Forget a saved account. `--force` allows the active one. |
| `ccs label <account> <label>` | Set or clear a nickname. |
| `ccs settings [key [value]]` | Show or change settings (poll interval, alert thresholds, auto-switch, menu bar title). |
| `ccs mcp status` | MCP servers on this Mac and whether each is signed in. |
| `ccs mcp catalog` | Ready-made vendor servers (Slack, Notion, Linear, Atlassian, Figma, ...). |
| `ccs mcp export [path]` | Write your user-scope servers to a profile file. Secrets become `${CCS_...}` placeholders. |
| `ccs mcp add <names...>` | Add catalog servers to the profile. |
| `ccs mcp apply [path] [--prune] [--dry-run]` | Install the profile's servers on this Mac. |
| `ccs menubar` | Run the menu bar app in the foreground. |
| `ccs agent install\|uninstall\|status` | Start the menu bar app at login. |
| `ccs doctor [--online]` | Check the installation. `--online` also fetches usage for the active account. |
| `ccs --version` | Print the version. |

## The menu bar app

The title shows the active account's usage (`menubar_title` setting: `none`, `session`, `weekly` or `both`). The menu has one section per account, the active one marked `◉` and the others `○`, with that account's usage lines indented underneath. Click an account to switch to it.

Limit resets appear as a `Resets: 1 left (weekly) · use by Tue 03 Dec` line. When an account is at a limit and has a reset it can use right now, the line starts with ⚡. To use one, run `/limit-reset` inside Claude Code; cc-switcher does not do it for you.

Below the accounts: refresh, switch to the best available account, add an account (opens Terminal, because the sign-in needs a browser), import the current login, remove an account, the auto-switch and notification toggles, MCP server status, start at login, and the data folder.

## How it works

```
macOS Keychain
├── Claude Code-credentials              the live item Claude Code reads
│   ├── claudeAiOauth                    your Claude login    ← the ONLY part cc-switcher replaces
│   ├── mcpOAuth                         MCP sign-ins         never touched
│   └── mcpOAuthClientConfig             MCP client settings  never touched
├── cc-switcher:<accountUuid>:<orgUuid>  saved login of account 1: { "claudeAiOauth": {...} } only
└── cc-switcher:<accountUuid>:<orgUuid>  saved login of account 2

~/.claude.json
├── oauthAccount                         who you are (email, org); replaced on a switch
└── mcpServers                           user-scope MCP servers; cc-switcher only reads this

~/.config/cc-switcher/                   private app data; see recovery exception below
├── accounts.json                        email, org, plan, label per account
├── settings.json
├── usage-cache.json, usage-history.jsonl
└── mcp-profile.json                     the portable MCP profile (no secrets)
```

A switch from `work` to `personal`, in order:

1. Read the live item. Save its `claudeAiOauth` into `work`'s saved item, so any token Claude Code rotated is kept.
2. Write the live item again as the freshly read blob with only `claudeAiOauth` replaced by `personal`'s login.
3. Read it back, verify the login, and attempt to restore MCP stores that disappeared entirely. Individual concurrent MCP changes are left alone.
4. Replace `oauthAccount` in `~/.claude.json`. If that fails, cc-switcher attempts to restore the previous login.

The Keychain interface has no compare-and-swap shared with Claude Code. If Claude Code writes new tokens between cc-switcher's final read and write, that concurrent update can be lost. Close other Claude Code sessions before adding an account, and avoid switching while another session is refreshing credentials.

"Which account is active" is worked out from the live login's account and organization ids, so signing in with `/login` inside Claude Code is noticed by the next refresh. A new `claude` session reads the current login; a running session may keep its previous login until restarted.

Usage comes from the same endpoint Claude Code currently uses, but cc-switcher makes its own requests. Its usage and reset-status requests omit the User-Agent header and do not explicitly send a Python version. Intermediaries and TLS can still expose client characteristics. These endpoints are undocumented client internals; matching the request format does not guarantee identical backend treatment or future compatibility.

In version 0.2.0, experimental CLI-driven renewal is off by default. When explicitly enabled, only an expired inactive login triggers a noninteractive Claude Code model request in a separate configuration and Keychain context; valid access tokens still serve usage GETs without starting a child. The request asks Haiku, at low effort, `Reply with OK only.` and sets a $0.10 CLI budget limit. It consumes plan usage and may incur charges. cc-switcher makes no direct token-refresh POST; Claude Code handles OAuth internally and may send its own User-Agent.

cc-switcher waits at least three minutes after one invocation completes before another starts; a later poll may add more delay. It cannot control Claude Code's internal retries and cannot guarantee identical rate limits or server treatment. Tools, MCP, and user/project settings and hooks are disabled where supported, but managed policy may override this. This is not an OS sandbox; enable it only for a trusted Claude Code installation and environment.

The active login remains managed by Claude Code, and cc-switcher does not automatically reauthenticate an account. An opaque failure after the child has been reaped and the successor safely saved pauses automatic retries for that account until its refresh grant changes, for example through sign-in or native rotation. If the child or credentials remain ambiguous, recovery material and a journal persist and block credential mutations; do not delete them or retry blindly. Last good usage may remain visible as stale; an unusable login may need sign-in again.

## Security

- Saved logins are stored in the Keychain and tokens are held in process memory while in use. If Keychain fails during isolated renewal, Claude Code may write credentials to a plaintext recovery file with `0600` permissions inside a private `0700` directory. This file may persist to permit recovery; do not copy, share or casually delete it.
- The app data folder is `0700` and its files are `0600`. Ordinary metadata and usage files do not contain tokens; the protected recovery file is an exception and must be treated as secret. A separate marker prevents isolated renewal during `ccs add`; an abandoned marker after a crash may require help.
- Account switching leaves MCP token entries in the live Keychain item; profile updates may temporarily retain and restore an entry locally. `ccs mcp export` writes server addresses and placeholders, never secrets.
- cc-switcher never deletes `Claude Code-credentials`. The uninstaller removes only `cc-switcher:*` items, and only with `--purge`.

More in the security section of [docs/INSTALL.md](docs/INSTALL.md).

## Credits

cc-switcher is modelled on [Symbioose/claude-account-switcher](https://github.com/Symbioose/claude-account-switcher), which is MIT-licensed. Its menu bar approach (rumps, a Keychain backup per account, usage in the menu) is where this started. cc-switcher is a separate implementation that is Claude-only and macOS-only, swaps only the Claude login, and adds the MCP profile and the usage monitoring.

## License

MIT
