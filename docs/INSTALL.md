# Installing cc-switcher

cc-switcher gives you the `ccs` command and a menu bar app for switching Claude Code accounts. This guide covers installing on this Mac and on others, adding accounts, carrying your MCP servers over, updating, removing, and fixing problems.

## Requirements

- macOS 12 or later
- A recent Claude Code installation, signed in at least once. Experimental renewal was checked against Claude Code 2.1.330. If Claude Code is missing, the installer tells you how to get it (`curl -fsSL https://claude.ai/install.sh | bash`, or see https://docs.claude.com/en/docs/claude-code/setup).
- An internet connection (for the installer, the Claude sign-in, and usage numbers)

You do not need Python or uv in advance: the installer sets them up (uv through Homebrew if you have it, otherwise through uv's official installer).

## Install on this Mac

First get the repository onto the Mac, or use an existing checkout. Then:

```bash
cd cc-switcher
scripts/install.sh
```

The script is safe to run again. Unless `--no-agent` is used, it starts the menu bar app, which may check usage. Experimental inactive-account renewal is off by default; if it was previously enabled, the app may invoke Claude Code and consume plan usage or incur charges. The script prints each step as it goes:

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
| `CC_SWITCHER_CLAUDE_PATH=<absolute path>` | Choose the Claude Code executable used for login, isolated renewal, MCP setup and version detection. |

Example: `CC_SWITCHER_SOURCE=https://example.com/you/cc-switcher.git CC_SWITCHER_MCP_PROFILE=~/mcp-profile.json scripts/install.sh`. A plain `https://...` or `ssh://...` URL is treated as a git repository.

## Install on another Mac

First push the desired commit to `main` yourself. A commit that exists only on the first Mac cannot be fetched on the second. On the other Mac, install a recent Claude Code and sign in, then clone `main` and install without starting the menu bar agent:

```bash
git clone --branch main https://github.com/priyesh2609/cc-switcher.git
cd cc-switcher
git log -1 --oneline         # check that this is the intended commit
scripts/install.sh --no-agent
```

If you already cloned the repository, fetch the pushed commit and check out the intended revision before running the installer. If the installer says `ccs` is not on PATH, add the printed line to your shell configuration and open a new Terminal window. The installer may import the currently signed-in account; add the others explicitly (next section) and check the installation:

```bash
ccs add --label work         # repeat for each other account, signing in separately
ccs --version                # 0.2.1 includes credential-file import; also check the source commit above
ccs doctor
ccs list
```

To opt in to experimental isolated renewal on this trusted installation, enable it before starting the agent:

```bash
ccs settings claude_refresh_enabled true
ccs agent install
ccs agent status
```

These checks do not prove an inactive-account renewal worked. A usage refresh makes network requests and, when the feature is enabled and an inactive token has expired, can invoke a noninteractive Claude Code model request that consumes plan usage and may incur charges. To leave renewal disabled, skip the settings command; you can still start the agent.

Apply your MCP profile separately (the section after). Saved logins, MCP sign-ins and the account list do not carry over by themselves. Separate sign-ins are recommended for independent use on two Macs; deliberate credential-backup migration has an offline file-import workflow and shared-grant caveats below. Do not copy isolated-renewal recovery directories between Macs. After `ccs mcp apply`, run `claude`, open `/mcp` and sign in to each server once (see [MCP.md](MCP.md)).

## Add your accounts (on each Mac, once per account)

If you were signed in when you ran the installer, that account is already saved. For every other account:

```bash
ccs add --label work
```

This runs `claude auth login`, which opens your browser. Sign in to the account you want to add; if your browser is already signed in to another Claude account, sign out first or use a private window. Back in the terminal, cc-switcher saves the new login and it becomes the active account. Check with:

```bash
ccs list
```

For normal use on two Macs, sign in separately rather than copying saved tokens. Two copies of a refresh token can diverge when either one is rotated; the older copy may then stop working. Offline credential-file import is available for deliberate migration or recovery (below), but it does not create independent logins or guarantee that copied credentials are still valid.

From the menu bar you can also choose **Add account...**. It opens Terminal running `ccs add`, because the sign-in needs a browser and visible prompts, and the menu refreshes when it finishes.

## Import an existing JSON/text credential copy

Use this only for credential files you own. Separate native sign-ins remain the recommended setup for using two Macs regularly. A copied grant is not a new login: for example, if both Macs hold R1 at 10:00 and the original Mac rotates it to R2 at 10:05, R1 on the destination may be rejected with `invalid_grant`. This is a shared-grant limitation with both direct and Claude-driven renewal, not a file-import fix.

The file must be a private regular file owned by your macOS user. Full Keychain JSON containing `claudeAiOauth`, a raw login JSON object, and the hex-encoded JSON value from `security -w` are accepted. Each login must include valid `accountUuid` and `organizationUuid` UUID strings and at least one token; CCS will not guess missing identities or query an account to find them. A file containing only a token string is not sufficient. Expired tokens may be imported, but their online validity is not checked.

To avoid automatic background checks while preparing the import, stop the menu-bar app first (also quit any manually started copy):

```bash
ccs agent uninstall
chmod 600 /private/path/work-credentials.json
ccs import --file /private/path/work-credentials.json --label work
ccs list
```

This command:

- Writes only the account login into a **new** saved CCS Keychain entry. It does not activate the account or overwrite the live Claude login, MCP credentials, an existing saved item, or a duplicate account/label.
- Makes no authentication, usage or token-refresh request. It does not open a browser or run Claude Code. A running menu-bar process may independently check imported accounts on a later poll; importing does not pause it.
- Validates types and the stored Keychain readback before registering the account. Unknown login fields are dropped; email/profile fields are not inferred from arbitrary file contents, so use a label to identify the account.
- Leaves the source file untouched. Keep it private; do not paste it into chat, terminal arguments, logs, a repository, or a shared folder. CCS never automatically deletes this recovery source.

Once ready to resume normal polling, run `ccs agent install`. If experimental renewal is enabled, a later poll can invoke Claude Code for an expired inactive login and consume account usage. Existing-account imports are deliberately refused: do not remove a working saved login merely to make room for an older file. If Keychain import or account registration reports an uncertain result, retain the source file and investigate rather than repeatedly reimporting.

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

## Experimental inactive-account renewal

Version 0.2.0 ships this feature **off by default**. Enable it only if you trust the installed Claude Code executable and its environment:

```bash
ccs settings claude_refresh_enabled true
```

To disable it later, run `ccs settings claude_refresh_enabled false`.

Only an expired inactive login triggers a child when the feature is enabled. For example, `work` remains active while `home` has an expired access token: cc-switcher may invoke Claude Code to renew `home`; a valid saved token still serves usage GETs without a child. With the feature off, cc-switcher does not renew `home`, and its last good usage may become stale until the login is renewed.

The child makes a noninteractive Haiku request at low effort: `Reply with OK only.` It sets a $0.10 CLI budget limit, consumes plan usage and may incur charges. Tools, MCP, and user/project settings and hooks are disabled where supported, but managed policy may override this. The separate configuration and Keychain context are not an OS sandbox. Renewal does not initiate a login, and cc-switcher makes no direct token-refresh POST; Claude Code handles OAuth internally.

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

Before updating, quit all running cc-switcher menu bar instances, including any started manually, so old and new processes do not refresh the same saved login at the same time. Then update the source and reinstall:

```bash
cd cc-switcher
git pull
scripts/install.sh
```

The script reinstalls from the current source and attempts to restart the LaunchAgent with the new code; it leaves your accounts and settings alone. Check `ccs agent status` and `ccs doctor` afterwards. A version number alone does not identify which commit was installed.

## Uninstall

```bash
scripts/uninstall.sh
```

This stops the menu bar app, removes the LaunchAgent, and removes the `ccs` command. Your saved logins and settings are kept, so a later reinstall picks them up.

To delete those too, first resolve any incomplete credential recovery. A recovery file may hold the only usable rotated credentials; do not purge it casually.

```bash
scripts/uninstall.sh --purge
```

`--purge` lists what it will delete and asks you to type `delete` (add `--yes` to skip the question). It removes `~/.config/cc-switcher`, `~/Library/Logs/cc-switcher`, and the saved login of every account in `accounts.json` (Keychain items named `cc-switcher:<id>`). It does not touch Claude Code's own login, so `claude` keeps working as the account that is currently signed in.

## Troubleshooting

Start with `ccs doctor`. Every problem comes with a line starting `fix:`. `ccs doctor --online` also tests fetching usage for the active account.

### macOS asks about the Keychain

cc-switcher reads and writes Keychain items through `/usr/bin/security`, the same tool Claude Code uses, so normally there is no prompt. The first time `security` reads an item that another program created, macOS may ask whether to allow it. Choose **Always Allow** (it asks for your Mac login password). If the prompt returns every time, or you see "Keychain did not answer in time", your login keychain is probably locked: unlock it in Keychain Access, or log out and in again.

### "sign-in expired"

Example: `ccs usage home` reports that the saved sign-in has expired and needs to be added again. cc-switcher does not automatically open a login flow. When you are ready to sign in again, run:

```bash
ccs add --label home
```

and sign in as `home`. The saved login is replaced; the label and the rest of the entry are kept. The other accounts are not affected.

### Isolated renewal or recovery fails

For example, `work` is inactive, its saved access token has expired, and experimental renewal is enabled. cc-switcher invokes Claude Code in a separate configuration and Keychain context; it does not automatically reauthenticate. If the renewal cannot complete, usage may remain stale or show an error. If Claude Code rotated credentials but Keychain failed, the old saved credentials may no longer work.

If the child was reaped and the successor safely saved but its result was an opaque failure, automatic retries for that account pause until its refresh grant changes, for example after sign-in or native rotation. If the child or credentials remain ambiguous, a journal and credential staging persist and block credential mutations. There is no automatic recovery or deletion; stop and seek help rather than retrying or removing files.

Claude Code may use a plaintext recovery file with `0600` permissions in a private `0700` directory when Keychain fails. It can persist to permit recovery and contains secrets. Do not copy, share or delete it casually. A separate marker prevents renewal during `ccs add`; if that process crashes and leaves its marker behind, recovery may also require help.

### Usage shows "stale"

Example: at 09:00 `ccs list` shows `work` at 62% session. At 09:12 Anthropic rate-limits the usage request, so the 09:12 refresh fails. `ccs list` now shows `62%~` with `12m ago (stale)` in the UPDATED column, and the menu bar title reads `62%~` with a first menu line saying the number is from 12m ago (last refresh: rate limited).

cc-switcher keeps the last good numbers when a transient refresh fails (for example, rate limit or offline) and shows their age. It also ignores numbers that are too old when it decides on a switch: auto-switch ignores the active account's numbers after 15 minutes, and `ccs next` and "Switch to best available" ignore another account's numbers after 30 minutes.

If no good numbers were fetched, or the login is reported unusable, usage can show an error instead. `ccs usage <account>` says why the last refresh failed, and `ccs usage --json` has `age_seconds`, `stale`, `last_error` and `last_error_at` for scripts. If sign-in has expired, you may need to add the account again; cc-switcher will not reauthenticate automatically.

### Inactive-account renewal queued

When experimental renewal is enabled and an inactive saved access token has expired, cc-switcher invokes a noninteractive Claude Code model request in a separate configuration and Keychain context. This consumes plan usage and may incur charges. cc-switcher makes no direct token-refresh POST; Claude Code handles OAuth internally. Invocations are spaced globally by at least three minutes from completion, including failed attempts. For example, if one invocation finishes at 10:00:12, the next starts no earlier than 10:03:12; the polling interval can make it later. This does not guarantee three minutes between the child's internal requests, or the same rate limits or server treatment as other Claude Code activity.

While an account waits, its last good usage may remain visible as stale. If scheduling or recovery reports an error, stop and investigate rather than deleting state or repeatedly retrying. cc-switcher does not automatically reauthenticate; an expired sign-in may require adding the account again.

### Usage unavailable or rate limited

- "rate limited": a usage request may have returned HTTP 429. Run `ccs usage <account>` for retry details when available. For usage requests, cc-switcher respects the server's `Retry-After`, or uses exponential backoff if none was provided; the last good numbers remain marked stale. For example, a 429 with `Retry-After: 7200` at 10:00 means the next usage request is not before 12:00. Do not run `ccs list --refresh` in a loop. cc-switcher's usage and reset-status requests omit the User-Agent header, but Claude Code's child process may send its own. Neither approach guarantees identical rate limits or hides all client characteristics.
- The menu bar reuses a successful sample less than 60 seconds old. For example, three refresh actions within nine seconds use one usage response; the old code made three requests. Explicit CLI refreshes still request fresh data, subject to backoff.
- Redacted 429 diagnostics go to `~/Library/Logs/cc-switcher/menubar.err.log`: time, a hashed account reference, endpoint, status, allowlisted error type and retry wait. They never contain tokens, response bodies, or server free text. `ccs usage <account> --json` includes the same account reference when details are available.
- "offline": no connection, or Anthropic could not be reached. It clears by itself once the network is back.
- "token needs refresh": the active account's token has expired. cc-switcher does not directly refresh the live login; use Claude Code with that account, then retry usage. If the login cannot be renewed, sign-in again may be necessary.
- "unexpected reply" or an HTTP error that does not go away: check for a cc-switcher update; Claude Code or Anthropic may have changed the endpoint.

### A running session still shows the old account

A new session reads the current Keychain item. A running session may keep its previous login; if it does, quit it and run `claude --continue`. `ccs status` shows the login in the current Keychain item.

### You use `CLAUDE_CONFIG_DIR`

Claude Code keeps its login for that setup in a different Keychain item (`Claude Code-credentials-` plus the first 8 hex digits of the SHA-256 of the directory path) and keeps `.claude.json` inside the directory. cc-switcher follows the same rules, but only when the variable is set in the environment of the program it is running in:

- Run `ccs` from a shell where `CLAUDE_CONFIG_DIR` is set the way it is when you run `claude`. `ccs doctor` prints the Keychain item it is using.
- The menu bar app started at login does not see your shell. `ccs agent install` copies `CLAUDE_CONFIG_DIR` (and `CLAUDE_SECURESTORAGE_CONFIG_DIR`) into the LaunchAgent if they are set when you run it. If you change the variable later, run `ccs agent install` again.

### Multiple Claude Code installations

The installer records the `claude` executable found in the installing shell, so the login item does not silently select a different version. For example, if the shell finds `/opt/homebrew/bin/claude` but launchd's PATH puts `~/.local/bin` first, the app keeps using the selected Homebrew path. To choose another installation, set `CC_SWITCHER_CLAUDE_PATH` to its absolute executable path before running the installer or `ccs agent install`. Reinstall the login item after changing it. A missing configured path is not silently replaced by another binary.

This selects the executable for login, isolated renewal, MCP setup and version detection. It does not make cc-switcher's own HTTP requests identical to Claude Code's.

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

- **Saved logins use the Keychain.** Your active Claude login is stored by Claude Code in `Claude Code-credentials`. cc-switcher keeps one extra Keychain item per account, `cc-switcher:<accountUuid>:<orgUuid>`, holding that account's login and nothing else. Items are written through `/usr/bin/security` with the payload on stdin, not on the command line (except a payload too large for one line, which Claude Code itself also sends as an argument).
- **Protected recovery exception.** If Keychain fails during isolated renewal, Claude Code may write credentials in plaintext to a `0600` recovery file inside a private `0700` directory. It may persist to permit recovery. Treat it as secret; do not copy, share or casually delete it. Ordinary app metadata and usage files do not contain tokens.
- **Private app data.** `~/.config/cc-switcher` is `0700` and its files `0600`: email, organization, plan, label, usage numbers, settings, and the MCP profile. `ccs doctor` checks the permissions.
- **MCP tokens stay local.** A switch replaces only `claudeAiOauth` and attempts to preserve the other live-item fields. During a profile update, cc-switcher may temporarily retain and restore an MCP entry locally. `ccs mcp export` writes addresses and `${CCS_...}` placeholders, never token values.
- **The active login is managed by Claude Code.** cc-switcher does not directly refresh it. Only when experimental renewal is enabled and an inactive access token has expired does cc-switcher invoke a noninteractive Claude Code model request in a separate configuration and Keychain context; it does not automatically reauthenticate. The request consumes plan usage and may incur charges. This separation is not an OS sandbox, and managed policy may override child restrictions.
- **No secrets in logs, errors or notifications.** Messages contain account names and percentages.
- **Anthropic requests.** cc-switcher makes its own usage and reset-status requests, but no direct token-refresh POST. Claude Code handles OAuth internally and may send its own User-Agent. These are undocumented client endpoints, not a supported compatibility guarantee: request handling, limits and response formats can change.
- **Uninstalling leaves Claude Code alone.** The uninstaller never deletes `Claude Code-credentials`.
