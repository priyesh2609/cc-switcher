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
- **Redeemable limit resets.** How many you have left on each account and when they expire, shown next to the usage. cc-switcher only displays them: you spend one inside Claude Code with the `/limit-reset` command, and cc-switcher never spends one for you.
- **Honest about data age.** Every number shows how old it is. If a refresh fails (rate limit, offline) the last good numbers stay on screen, marked `~` and "stale", instead of turning into an error. Numbers that are too old are ignored when choosing an account to switch to.
- **Alerts.** A notification when a window crosses 80%, 95% and 100% (configurable), when a window resets, and when an account needs to sign in again.
- **Optional auto-switch.** Off by default. When on, it moves to the account with the most room once the active one reaches your threshold, with a 10-minute cooldown, and tells you what it did.
- **A fixed MCP set per Mac.** `ccs mcp export` / `apply` carries your user-scope servers between Macs, and `ccs mcp status` shows which are signed in.
- **Keychain-backed logins.** Saved tokens stay in the Keychain. cc-switcher does not refresh the active login, reducing interference with running Claude Code sessions.
- **`ccs doctor`.** Checks the whole setup and prints a fix for anything that is wrong.

## Requirements

macOS 12 or later, Claude Code, and an internet connection. The installer sets up everything else (uv and Python 3.12).

## Quick start

```bash
git clone <your-repo-url> cc-switcher
cd cc-switcher
scripts/install.sh
```

The installer saves the account you are signed in to, starts the menu bar app, and runs `ccs doctor`. Then, for each other account:

```bash
ccs add --label personal     # opens the Claude sign-in in your browser
ccs list                     # all accounts with usage
ccs switch personal          # or click it in the menu bar
```

Full steps, other Macs, updating and troubleshooting: [docs/INSTALL.md](docs/INSTALL.md).

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

~/.config/cc-switcher/                   0700; files 0600; no tokens
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

Usage comes from the same endpoint Claude Code currently uses, but cc-switcher makes its own requests. These endpoints are undocumented client internals; matching the request format does not guarantee identical backend treatment or future compatibility. For the active account cc-switcher uses the live token without refreshing it. Other accounts' saved logins are refreshed when they expire.

## Security

- Tokens are persisted in the Keychain, written through `/usr/bin/security` like Claude Code does. Tokens are also held in process memory while in use. No token goes in an app data file, log, notification or error message.
- The app folder is `0700`, its files `0600`, and they hold metadata only.
- Account switching leaves MCP token entries in the live Keychain item; profile updates may temporarily retain and restore an entry locally. `ccs mcp export` writes server addresses and placeholders, never secrets.
- cc-switcher never deletes `Claude Code-credentials`. The uninstaller removes only `cc-switcher:*` items, and only with `--purge`.

More in the security section of [docs/INSTALL.md](docs/INSTALL.md).

## Credits

cc-switcher is modelled on [Symbioose/claude-account-switcher](https://github.com/Symbioose/claude-account-switcher), which is MIT-licensed. Its menu bar approach (rumps, a Keychain backup per account, usage in the menu) is where this started. cc-switcher is a separate implementation that is Claude-only and macOS-only, swaps only the Claude login, and adds the MCP profile and the usage monitoring.

## License

MIT
