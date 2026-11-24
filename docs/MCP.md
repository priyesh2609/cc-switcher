# MCP servers across accounts and Macs

Goal: the same MCP servers on every Claude account and every Mac you use, with as few
sign-ins as possible.

## Why claude.ai connectors can't be pinned

The connectors you see as `claude.ai Slack`, `claude.ai Notion` and so on are not stored on
your Mac. Claude Code asks Anthropic for the list (`/v1/mcp_servers`) using the token of the
account you are logged in as, and the sign-in to Slack itself lives on claude.ai, per account.

Example: your work account has Slack and Notion connected on claude.ai, and your personal
account has only Notion. You run `ccs switch personal`. The next `claude` session lists
`claude.ai Notion` as connected and no Slack at all. Nothing on your Mac can change that, because the
list comes from the account.

cc-switcher therefore leaves connectors alone. Treat them as per-account extras.

## How local MCP sign-ins stay fixed

A server you add yourself (user scope, stored in `~/.claude.json` under `mcpServers`) is local to
the Mac. Claude Code keeps its OAuth tokens in the same Keychain item as your Claude login
(`Claude Code-credentials`), in a separate part called `mcpOAuth`:

```
Claude Code-credentials
  claudeAiOauth         <- your Claude login   (the only part cc-switcher swaps)
  mcpOAuth              <- sign-ins to local MCP servers (never touched)
  mcpOAuthClientConfig  <- MCP client settings (never touched)
```

Example: you are signed in to `sentry` locally (token stored under `sentry|800cb29a3ac61727`).
`ccs switch work` replaces `claudeAiOauth` and leaves `mcpOAuth` byte-for-byte as it was, so
`sentry` is still signed in as the other account.

The token key is `name|hash`, where the hash covers the server's type, URL and headers. A token only
matches while all of them stay the same:

- Rename `sentry` to `sentry-prod`: the key becomes `sentry-prod|800cb29a3ac61727`, so you sign in again.
- Change the URL: the hash changes, so you sign in again.

## The profile: one file, every Mac

The MCP profile is a JSON file (default `~/.config/cc-switcher/mcp-profile.json`) that lists
your user-scope servers. It holds no secrets, so you can copy it between Macs.

Tokens are per device, so each Mac still needs one browser sign-in per server. That is a one-off
step per Mac.

1. On the Mac that already has your servers: `ccs mcp export`. The path it writes is printed.
   Or build a profile from known vendors without exporting: `ccs mcp catalog`, then
   `ccs mcp add slack notion linear`.
2. Copy the file to the other Mac (AirDrop, a private repo, a dotfiles folder).
3. On the new Mac: `ccs mcp apply <file> --dry-run` to preview, then `ccs mcp apply <file>`.
4. Run `claude`, open `/mcp`, and sign in to each server once (a browser tab opens per server).
5. `ccs mcp status` shows what is signed in on this Mac.

Status values:

| auth | Meaning |
|---|---|
| `signed_in` | A token for this exact name, URL and headers is in the Keychain. |
| `needs_sign_in` | Configured, but no token yet. Open `/mcp` in `claude` and sign in. |
| `header_auth` | The server authenticates with a header (for example a GitHub PAT), not OAuth. |
| `not_oauth` | A local command (stdio) server. There is nothing to sign in to. |
| `not_configured` | In the profile, but not on this Mac yet. Run `ccs mcp apply`. |

### What `apply` does

`apply` never edits `~/.claude.json`. It runs `claude mcp add-json --scope user ...` and
`claude mcp remove --scope user ...`, so Claude Code does the writing. The only thing `apply` writes
itself is a Keychain entry it puts back to keep a sign-in (next section).

| Action | When |
|---|---|
| add | The server is in the profile and not on this Mac. |
| update | The server exists but differs. This is a remove followed by an add. If the add fails, the previous config is put back. |
| unchanged | The server matches the profile. |
| remove | Only with `--prune`: a server on this Mac that the profile doesn't list. |
| skip | A secret placeholder has no value (see below), the `claude` call failed (`failed: ...`), or the sign-in could not be kept (`re-sign-in needed: <name>`). The reason says which. |

Without `--prune`, servers that are not in the profile are left alone.

### When apply keeps your sign-in, and when it doesn't

`claude mcp remove` deletes that server's tokens from the Keychain along with its config. Because an
update is a remove plus an add, `apply` saves the server's token entry first and puts it back after the
add whenever the old token is still valid for the new config. The token key is `name|hash(type, url,
headers)`, so what matters is whether those three changed.

- **Same key: sign-in kept.** `sentry` is signed in (`sentry|800cb29a3ac61727`). Your profile now adds
  `"oauth": {"callbackPort": 8080}` to it. `ccs mcp apply --dry-run` shows `update`,
  "config differs; existing sign-in is kept". After the real run the key is still
  `sentry|800cb29a3ac61727`, the token is back, and `ccs mcp status` still says `signed_in`.
- **Key changes: one-time re-sign-in.** Your profile moves `sentry` to `https://sentry.example.com/mcp`.
  The key becomes `sentry|` plus a different hash, and the old token belongs to the old key, so it is not
  restored. `apply` shows `update` with "token key changes, sign in again after applying". Open `/mcp`
  and sign in once.
- **`--prune`: one-time re-sign-in if you add it back.** `atlassian` is signed in on this Mac but not in
  the profile. `ccs mcp apply --prune` removes it, and Claude Code deletes its token with it. If you add
  `atlassian` back later, you sign in again.
- **`unchanged` servers are never touched.**

If the token could not be put back after a same-key update (for example the Keychain is locked), the
config is updated but the server shows `skip` with `re-sign-in needed: sentry`. Sign in again in `/mcp`.
If the Keychain can't be read before an update, that server is skipped with `failed: ...` and nothing is
changed for it.

`--prune` with an empty profile is refused, so a mistyped file can never wipe your servers.

### Secrets and placeholders

Most servers use browser sign-in and need no secret. Servers that take a token in a header
(GitHub's endpoint takes a personal access token) would have to put it in the profile, so the profile
holds a placeholder instead:

```json
"github": {
  "type": "http",
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": { "Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}" }
}
```

The variable name is `CCS_<SERVER>_<HEADER>`, upper-case, with every character that is not a letter
or digit turned into `_`. On the Mac you apply it on:

```
export CCS_GITHUB_AUTHORIZATION=ghp_yourtoken
ccs mcp apply
```

If the variable is not set, that one server shows `skip` with the reason `set $CCS_GITHUB_AUTHORIZATION`, and
everything else still applies. A second `apply` without the variable also shows `skip`, even though
the server is already there.

What gets replaced when exporting: header values whose header name contains `auth`, `token`, `key`,
`secret` or `cookie`; `env` values of stdio servers by the same rule; URL query parameters, a
`user:password@` part, and `--flag=value` or `--flag value` arguments, again by name. Values already
written as `${VAR}` are kept. The scan works by name only, so it cannot see a secret in a URL path or a
plain positional argument. Skim the exported file before you copy it somewhere shared.

Two things to know about the secret after `apply`: Claude Code stores the value in `~/.claude.json`
in plain text, and it is visible in the process list while `claude mcp add-json` runs, because that
command takes the JSON as an argument.

Servers added at local (per-project) scope or from a project `.mcp.json` are not part of the profile.

## Plugin servers, user servers and connectors side by side

Three different things can all offer "Slack":

| Where it comes from | Shown as | Tools look like |
|---|---|---|
| A server you added (user scope, this profile) | `slack` | `mcp__slack__...` |
| A Claude Code plugin | `plugin:slack:slack` | `mcp__plugin_slack_slack__...` |
| A claude.ai connector | `claude.ai Slack` | `mcp__claude_ai_Slack__...` |

They are separate servers with separate sign-ins. If you add `slack` to the profile and also keep the
Slack connector (and a Slack plugin), `/mcp` lists all three, and Claude sees three sets of Slack tools.
It costs context, and it can be unclear which one is signed in.

To get one set per service, turn off the ones you don't want:

- In `/mcp`, disable the individual server you don't want.
- Plugin servers: disable or uninstall the plugin.
- Connectors: disconnect them on claude.ai, or turn them all off locally (next section).

Plugin servers are defined by the plugin, not by `~/.claude.json`, so they are not in the profile.
`ccs mcp status` only lists user-scope servers.

## Turning claude.ai connectors off

If you decide to rely only on the profile, turn the connectors off on this Mac: set
`disableClaudeAiConnectors: true` in `~/.claude/settings.json`, or export
`ENABLE_CLAUDEAI_MCP_SERVERS=false`.

```json
{ "disableClaudeAiConnectors": true }
```

```
ENABLE_CLAUDEAI_MCP_SERVERS=false claude
```

Not tested on a live Mac. Both names were taken from Claude Code 2.1.330 (the settings key sits next to
`enabledMcpjsonServers` in its settings key list). If the connectors still show after a restart, check
`/mcp` and your Claude Code version's docs.
