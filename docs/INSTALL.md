# Installing cc-switcher

cc-switcher gives you the `ccs` command and a menu bar app for switching Claude Code accounts. This guide covers installing on this Mac and on others, adding accounts, carrying your MCP servers over, updating, removing, and fixing problems.

## Requirements

- macOS 12 or later
- Claude Code, installed and signed in at least once. If it is missing, the installer tells you how to get it (`curl -fsSL https://claude.ai/install.sh | bash`, or see https://docs.claude.com/en/docs/claude-code/setup).
- An internet connection (for the installer, the Claude sign-in, and usage numbers)

You do not need Python or uv in advance: the installer sets them up (uv through Homebrew if you have it, otherwise through uv's official installer).

## Install on this Mac

You host the repository yourself, so first get it onto the Mac (clone it from wherever you keep it, or copy the folder). Then:

```bash
cd cc-switcher
scripts/install.sh
```

The script is safe to run again. It prints each step as it goes:

1. Checks that this is macOS 12 or later.
2. Looks for Claude Code and warns, with the install command, if it is missing.
3. Installs uv if it is missing.
4. Installs cc-switcher with `uv tool install --python 3.12`, which puts `ccs` in `~/.local/bin`.
5. Checks that `~/.local/bin` is on your PATH. If it is not, it prints the line to add to `~/.zshrc`; it does not edit the file for you.
6. If `claude auth status` shows you are signed in, runs `ccs import` to save that account.
7. If `CC_SWITCHER_MCP_PROFILE` is set, runs `ccs mcp apply` with that file.
8. Installs and starts the menu bar app at login (skip with `--no-agent`).
9. Runs `ccs doctor`.

Options:

| | |
|---|---|
| `scripts/install.sh --no-agent` | Do not start the menu bar app at login. |
| `CC_SWITCHER_SOURCE=<path or git URL>` | Install from somewhere other than the repo the script sits in. |
| `CC_SWITCHER_MCP_PROFILE=<file>` | Apply this MCP profile after installing. |
| `CC_SWITCHER_CLAUDE_PATH=<absolute path>` | Choose the Claude Code executable used for login, MCP setup and version detection. |

Example: `CC_SWITCHER_SOURCE=https://example.com/you/cc-switcher.git CC_SWITCHER_MCP_PROFILE=~/mcp-profile.json scripts/install.sh`. A plain `https://...` or `ssh://...` URL is treated as a git repository.

## Install on another Mac

Do the same thing there: get the repository onto that Mac (clone or copy), run `scripts/install.sh`, then add its accounts (next section) and apply your MCP profile (the section after).

What does not carry over by itself: saved logins, MCP sign-ins, and the account list. Each Mac has its own. After `ccs mcp apply` on the new Mac, run `claude`, open `/mcp` and sign in to each server once; those tokens are per Mac too (see [MCP.md](MCP.md)).

## Add your accounts (on each Mac, once per account)

If you were signed in when you ran the installer, that account is already saved. For every other account:

```bash
ccs add --label work
```

This runs `claude auth login`, which opens your browser. Sign in to the account you want to add; if your browser is already signed in to another Claude account, sign out first or use a private window. Back in the terminal, cc-switcher saves the new login and it becomes the active account. Check with:

```bash
ccs list
```

For another Mac, sign in separately rather than copying saved tokens. Two copies of a refresh token can diverge when either one is rotated; the older copy may then stop working. cc-switcher does not provide a supported cross-device token-transfer workflow.

From the menu bar you can also choose **Add account...**. It opens Terminal running `ccs add`, because the sign-in needs a browser and visible prompts, and the menu refreshes when it finishes.

## Carry your fixed MCP set to another Mac

Your user-scope MCP servers (the ones you added with `claude mcp add --scope user`) are local to a Mac. A profile file moves the list of them; it holds addresses and no secrets.

On the Mac that has your servers:

```bash
ccs mcp export ~/mcp-profile.json
```

Copy the file to the other Mac (AirDrop, a private repo, a dotfiles folder). There:

```bash
ccs mcp apply ~/mcp-profile.json --dry-run    # preview
ccs mcp apply ~/mcp-profile.json
claude                                         # then type /mcp and sign in to each server once
ccs mcp status                                 # what is signed in on this Mac
```

The sign-in step is once per Mac per server: MCP tokens are per device too. After that, they stay put when you switch accounts, which is the point of cc-switcher. If a server in the profile needs a token in a header (GitHub's endpoint), the profile holds a `${CCS_...}` placeholder; export the variable named in the `ccs mcp export` output before you run `apply`.

Details, including claude.ai connectors (which cannot be pinned) and how to avoid duplicate Slack/Notion entries: [MCP.md](MCP.md).

## Daily use

```bash
ccs list                 # accounts and usage (cached)
ccs list --refresh       # fetch fresh numbers
ccs switch work          # label, email, or a unique prefix
ccs next                 # switch to the best other account with room
ccs status               # active account in detail
```

After `ccs switch`, a new `claude` session reads the new login. A session that is already running may keep the old one; if it does, quit it and run `claude --continue` to resume the conversation.

## Limit resets

Some accounts have redeemable limit resets (the ones Claude Code's `/limit-reset` command spends). `ccs usage` and `ccs status` show a line such as `Resets: 1 left (weekly) · use by Tue 03 Dec`, `ccs list` has a LIMIT RESETS column, and the menu bar shows the same line under the account, with a ⚡ in front when the account is at a limit and a reset can be used right now. "usable at a limit" means you have a reset but it only works once you hit a limit.

cc-switcher only shows how many you have. To use one, run `/limit-reset` inside Claude Code. cc-switcher never spends a reset, and it has no command or button for it.

## Start at login

The installer does this unless you passed `--no-agent`. To change it later:

```bash
ccs agent install      # install and start now
ccs agent uninstall    # stop starting at login
ccs agent status
```

You can also use the **Start at login** item in the menu bar app. Turning it off there stops it from starting at your next login but leaves the current copy running until you quit it.

The menu bar app runs as a LaunchAgent (`~/Library/LaunchAgents/dev.cc-switcher.menubar.plist`). Its logs are in `~/Library/Logs/cc-switcher/` (`menubar.out.log`, `menubar.err.log`). Quitting from the menu bar does not restart it; a crash does, after 30 seconds.

## Update

```bash
cd cc-switcher
git pull
scripts/install.sh
```

The script reinstalls from the current source, restarts the menu bar app with the new code, and leaves your accounts and settings alone.

## Uninstall

```bash
scripts/uninstall.sh
```

This stops the menu bar app, removes the LaunchAgent, and removes the `ccs` command. Your saved logins and settings are kept, so a later reinstall picks them up.

To delete those too:

```bash
scripts/uninstall.sh --purge
```

`--purge` lists what it will delete and asks you to type `delete` (add `--yes` to skip the question). It removes `~/.config/cc-switcher`, `~/Library/Logs/cc-switcher`, and the saved login of every account in `accounts.json` (Keychain items named `cc-switcher:<id>`). It does not touch Claude Code's own login, so `claude` keeps working as the account that is currently signed in.

## Troubleshooting

Start with `ccs doctor`. Every problem comes with a line starting `fix:`. `ccs doctor --online` also tests fetching usage for the active account.

### macOS asks about the Keychain

cc-switcher reads and writes Keychain items through `/usr/bin/security`, the same tool Claude Code uses, so normally there is no prompt. The first time `security` reads an item that another program created, macOS may ask whether to allow it. Choose **Always Allow** (it asks for your Mac login password). If the prompt returns every time, or you see "Keychain did not answer in time", your login keychain is probably locked: unlock it in Keychain Access, or log out and in again.

### "sign-in expired"

Example: you have not used your `home` account for six weeks. Its saved refresh token expired, so `ccs usage home` shows "sign-in expired — run `ccs add` again for this account". Run:

```bash
ccs add --label home
```

and sign in as `home`. The saved login is replaced; the label and the rest of the entry are kept. The other accounts are not affected.

### "The refreshed login could not be saved to Keychain"

Example: `work` is not the account you are using, and its saved token has expired. At 09:00 the menu bar app refreshes it. Anthropic issues a new token and revokes the old one, but at that moment your login keychain is locked, so cc-switcher cannot store the new token. It keeps the new token in memory and shows this message. At 09:05 you unlock the keychain; the next refresh (the menu bar app tries again every few minutes) saves it, and nothing else is needed.

Two ways it goes wrong:

- You quit the menu bar app before the Keychain was available. The new token existed only in that app's memory, so it is gone, and the saved copy is the revoked one. `ccs usage work` then says "sign-in expired". Run `ccs add --label work` once.
- You run `ccs list --refresh` in a terminal while the menu bar app is holding the new token. The terminal process only has the revoked saved copy, so its refresh is rejected and it tells you to run `ccs add`. Wait for the menu bar app to save it, or unlock the keychain first.

cc-switcher never writes tokens to a plain file to avoid this, because a file on disk is easier to leak than the Keychain. The cost is the narrow window above.

### Usage shows "stale"

Example: at 09:00 `ccs list` shows `work` at 62% session. At 09:12 Anthropic rate-limits the usage request, so the 09:12 refresh fails. `ccs list` now shows `62%~` with `12m ago (stale)` in the UPDATED column, and the menu bar title reads `62%~` with a first menu line saying the number is from 12m ago (last refresh: rate limited).

cc-switcher keeps the last good numbers when a refresh fails (rate limit, offline, Claude Code idle overnight) and shows their age, so a short outage does not blank your usage. It also ignores numbers that are too old when it decides on a switch: auto-switch ignores the active account's numbers after 15 minutes, and `ccs next` and "Switch to best available" ignore another account's numbers after 30 minutes.

It stops showing numbers only when the sign-in itself expired (the line then reads "sign-in expired"). Run `ccs add` for that account. `ccs usage <account>` says why the last refresh failed, and `ccs usage --json` has `age_seconds`, `stale`, `last_error` and `last_error_at` for scripts.

### Usage unavailable or rate limited

- "rate limited": either the token-refresh endpoint or the usage endpoint returned HTTP 429. Run `ccs usage <account>` for endpoint and retry details when available. cc-switcher respects the server's `Retry-After`, or uses exponential backoff if none was provided; the last good numbers remain marked stale. For example, a 429 with `Retry-After: 7200` at 10:00 means the next request is not before 12:00. Older cache entries do not identify the endpoint. Do not run `ccs list --refresh` in a loop.
- The menu bar reuses a successful sample less than 60 seconds old. For example, three refresh actions within nine seconds use one usage response; the old code made three requests. Explicit CLI refreshes still request fresh data, subject to backoff.
- Redacted 429 diagnostics go to `~/Library/Logs/cc-switcher/menubar.err.log`: time, a hashed account reference, endpoint, status, allowlisted error type and retry wait. They never contain tokens, response bodies, or server free text. `ccs usage <account> --json` includes the same account reference when details are available.
- "offline": no connection, or Anthropic could not be reached. It clears by itself once the network is back.
- "token needs refresh": the active account's token has expired. cc-switcher never refreshes the live login (that would log out running sessions), so it waits for Claude Code to do it. Run any `claude` command, then `ccs usage --refresh`.
- "unexpected reply" or an HTTP error that does not go away: check for a cc-switcher update; Claude Code or Anthropic may have changed the endpoint.

### A running session still shows the old account

A new session reads the current Keychain item. A running session may keep its previous login; if it does, quit it and run `claude --continue`. `ccs status` shows the login in the current Keychain item.

### You use `CLAUDE_CONFIG_DIR`

Claude Code keeps its login for that setup in a different Keychain item (`Claude Code-credentials-` plus the first 8 hex digits of the SHA-256 of the directory path) and keeps `.claude.json` inside the directory. cc-switcher follows the same rules, but only when the variable is set in the environment of the program it is running in:

- Run `ccs` from a shell where `CLAUDE_CONFIG_DIR` is set the way it is when you run `claude`. `ccs doctor` prints the Keychain item it is using.
- The menu bar app started at login does not see your shell. `ccs agent install` copies `CLAUDE_CONFIG_DIR` (and `CLAUDE_SECURESTORAGE_CONFIG_DIR`) into the LaunchAgent if they are set when you run it. If you change the variable later, run `ccs agent install` again.

### Multiple Claude Code installations

The installer records the `claude` executable found in the installing shell, so the login item does not silently select a different version. For example, if the shell finds `/opt/homebrew/bin/claude` but launchd's PATH puts `~/.local/bin` first, the app keeps using the selected Homebrew path. To choose another installation, set `CC_SWITCHER_CLAUDE_PATH` to its absolute executable path before running the installer or `ccs agent install`. Reinstall the login item after changing it. A missing configured path is not silently replaced by another binary.

This selects the executable for login, MCP setup and version detection; it does not make cc-switcher's own HTTP requests identical to Claude Code's.

### `ccs: command not found`

`~/.local/bin` is not on your PATH. Add this line to `~/.zshrc` and open a new Terminal window:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### The menu bar icon does not appear

Run `ccs agent status` and `ccs doctor`. Look at `~/Library/Logs/cc-switcher/menubar.err.log`. To see errors live, run `ccs menubar` in a terminal. The icon is also hidden when the menu bar is full; quit a few other menu bar apps to check.

### No notifications

Notifications are sent through macOS, and they respect Focus modes and the per-app switches in System Settings > Notifications (they may be listed under Script Editor). Also check the **Notifications** item in the menu is ticked.

### An error that just says "unexpected ..."

Run the same command with `CCS_DEBUG=1 ccs ...` to get the full traceback, and include it when you report the problem. The traceback never contains tokens.

## Security model

- **Tokens live only in the Keychain.** Your Claude login is stored by Claude Code in `Claude Code-credentials`. cc-switcher keeps one extra Keychain item per account, `cc-switcher:<accountUuid>:<orgUuid>`, holding that account's login and nothing else. Items are written through `/usr/bin/security` with the payload on stdin, not on the command line (except a payload too large for one line, which Claude Code itself also sends as an argument).
- **Files hold metadata only.** `~/.config/cc-switcher` is `0700` and its files `0600`: email, organization, plan, label, usage numbers, settings, and the MCP profile. `ccs doctor` checks the permissions.
- **MCP tokens stay local.** A switch replaces only `claudeAiOauth` and attempts to preserve the other live-item fields. During a profile update, cc-switcher may temporarily retain and restore an MCP entry locally. `ccs mcp export` writes addresses and `${CCS_...}` placeholders, never token values.
- **The live login is never refreshed by cc-switcher.** It is shared with your running Claude Code sessions; a refresh rotates the token and would sign them out. Saved logins of accounts that are not live are refreshed when they expire.
- **No secrets in logs, errors or notifications.** Messages contain account names and percentages.
- **Anthropic requests.** Usage and inactive-account token refreshes contact Anthropic's endpoints with the account's own token. These are undocumented client endpoints, not a supported compatibility guarantee: request handling, limits and response formats can change.
- **Uninstalling leaves Claude Code alone.** The uninstaller never deletes `Claude Code-credentials`.
