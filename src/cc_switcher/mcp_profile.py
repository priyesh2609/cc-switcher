"""Portable MCP profile: the same user-scope remote MCP servers on every Mac.

claude.ai connectors belong to a Claude account on Anthropic's side, so they can't be
pinned. User-scope servers in ``~/.claude.json`` (``mcpServers``) are local, and their
OAuth tokens sit in the Keychain blob under ``mcpOAuth``, which account switching never
touches. This module keeps a *profile* (a JSON file with no secrets) that can be exported
once and applied on each Mac, plus a status view of which servers are signed in here.

Safety rules baked in:

- ``~/.claude.json`` is only ever *read*. Changes go through the ``claude`` CLI
  (``claude mcp add-json`` / ``claude mcp remove``), never by editing the file.
- Profiles written by this module never contain secrets (see :func:`redact_config`).
  Secret-looking values become ``${CCS_<NAME>_<KEY>}`` placeholders, resolved from the
  environment only at apply time.
- Returned ``Change`` objects keep the placeholder form, so printing them is safe.
- ``claude mcp remove`` deletes the server's tokens from the Keychain blob. An update that keeps
  the token key therefore snapshots the entry first and restores it afterwards (the only Keychain
  write in this module, done as ``restore_missing_mcp(<fresh live blob>, snapshot)`` under the
  app lock). Token values are never logged or put into a ``Change``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit, urlunsplit

from cc_switcher import creds, fsutil, keychain, paths

PROFILE_VERSION = 1
# Every failed ``claude mcp`` call is reported as Change("skip", reason="failed: ...").
FAILED_PREFIX = "failed: "
# The config was changed but the server's sign-in could not be kept: Change("skip", reason="re-sign-in needed: <name>").
RESIGN_PREFIX = "re-sign-in needed: "
# Keychain blob sections that hold per-server state keyed by ``server_key``.
_MCP_STORES = ("mcpOAuth", "mcpOAuthClientConfig")

Runner = Callable[[list[str]], "tuple[int, str, str]"]


class McpProfileError(RuntimeError):
    """A problem the user can fix (bad profile, missing claude CLI, unknown catalog name...)."""


# --------------------------------------------------------------------------- catalog


def _oauth(url: str, note: str) -> dict[str, Any]:
    return {"type": "http", "url": url, "note": note}


# Vendor-hosted remote MCP endpoints. ``note`` explains the sign-in; it is not part of the
# server config and is stripped by add_to_profile().
CATALOG: dict[str, dict[str, Any]] = {
    "slack": _oauth("https://mcp.slack.com/mcp", "Browser sign-in (OAuth) via /mcp; pick your Slack workspace."),
    "notion": _oauth("https://mcp.notion.com/mcp", "Browser sign-in (OAuth) via /mcp; choose the workspace pages to share."),
    "linear": _oauth("https://mcp.linear.app/mcp", "Browser sign-in (OAuth) via /mcp with your Linear account."),
    "atlassian": _oauth("https://mcp.atlassian.com/v1/mcp", "Browser sign-in (OAuth) via /mcp; choose the Atlassian site to grant."),
    "figma": _oauth("https://mcp.figma.com/mcp", "Browser sign-in (OAuth) via /mcp with your Figma account."),
    "supabase": _oauth("https://mcp.supabase.com/mcp", "Browser sign-in (OAuth) via /mcp; choose the Supabase organization to grant."),
    "vercel": _oauth("https://mcp.vercel.com", "Browser sign-in (OAuth) via /mcp with your Vercel account."),
    "sentry": _oauth("https://mcp.sentry.dev/mcp", "Browser sign-in (OAuth) via /mcp with your Sentry account."),
    "asana": _oauth("https://mcp.asana.com/v2/mcp", "Browser sign-in (OAuth) via /mcp with your Asana account."),
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${CCS_GITHUB_AUTHORIZATION}"},
        "note": (
            "No browser sign-in: needs a GitHub personal access token (PAT) sent as an "
            "Authorization: Bearer header. Run `export CCS_GITHUB_AUTHORIZATION=<PAT>` before "
            "`ccs mcp apply`."
        ),
    },
}


# --------------------------------------------------------------------------- data types


@dataclass
class ServerStatus:
    name: str
    type: str
    url: str | None
    in_profile: bool
    configured: bool
    # "signed_in" | "needs_sign_in" | "header_auth" | "not_oauth" (stdio) | "not_configured"
    auth: str


@dataclass
class Change:
    action: str  # "add" | "update" | "remove" | "unchanged" | "skip"
    name: str
    config: dict | None  # profile form (placeholders, never resolved secrets); None for "remove"
    reason: str = ""


# --------------------------------------------------------------------------- config helpers

_SECRET_NAME = re.compile(r"auth|token|key|secret|cookie", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\$\{(CCS_[A-Z0-9_]+)\}")
# A value that is only a reference such as "${GITHUB_PAT}" or "Bearer ${CCS_X}" holds no secret.
_REFERENCE_RE = re.compile(r"^(?:(?:Bearer|Basic|Token)\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*\}$", re.IGNORECASE)
_AUTH_SCHEME_RE = re.compile(r"^(Bearer|Basic|Token)\s+\S", re.IGNORECASE)
_ARG_KEY_VALUE = re.compile(r"^(-{0,2}[A-Za-z0-9_.-]+)=(.+)$", re.DOTALL)
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _server_type(config: dict) -> str:
    """Claude Code treats an entry without ``type`` as stdio."""
    declared = config.get("type")
    if isinstance(declared, str) and declared:
        return declared
    return "stdio" if "command" in config else ""


def normalize_config(config: dict) -> dict:
    """Canonical form used to compare two configs: drop nulls and empty headers/env/args,
    and make the implicit stdio type explicit."""
    cfg = {k: copy.deepcopy(v) for k, v in config.items() if v is not None}
    if "type" not in cfg and "command" in cfg:
        cfg["type"] = "stdio"
    for key in ("headers", "env"):
        if cfg.get(key) == {}:
            del cfg[key]
    if cfg.get("args") == []:
        del cfg["args"]
    return cfg


def server_key(name: str, config: dict) -> str:
    """The key Claude Code stores a server's OAuth tokens under in ``mcpOAuth``.

    ``name|sha256(json({type, url, headers or {}}, compact, ensure_ascii=False))[:16]``.
    Verified against two real keys (see tests). Keys whose value is missing are left out of
    the hashed object, like ``JSON.stringify`` does for ``undefined``; that only matters for
    configs without type/url, which never use OAuth.
    """
    payload: dict[str, Any] = {}
    if config.get("type") is not None:
        payload["type"] = config["type"]
    if config.get("url") is not None:
        payload["url"] = config["url"]
    payload["headers"] = config.get("headers") or {}
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"{name}|{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def placeholder_var(server_name: str, key: str) -> str:
    """``CCS_<NAME>_<KEY>``: upper-case, every non-alphanumeric character becomes ``_``."""

    def slug(text: str) -> str:
        return re.sub(r"[^A-Z0-9]", "_", text.upper())

    return f"CCS_{slug(server_name)}_{slug(key)}"


def _placeholder(server_name: str, key: str) -> str:
    return "${" + placeholder_var(server_name, key) + "}"


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(_REFERENCE_RE.match(value.strip()))


def _redact_header(server: str, header: str, value: Any) -> Any:
    if not isinstance(value, str) or not value or _is_reference(value):
        return value
    scheme = _AUTH_SCHEME_RE.match(value)
    prefix = f"{scheme.group(1)} " if scheme else ""  # keep "Bearer " so the env var holds only the token
    return prefix + _placeholder(server, header)


def _redact_url(server: str, url: str) -> str:
    try:
        parts = urlsplit(url)
        parts.password  # noqa: B018 - may raise ValueError on malformed netlocs
    except ValueError:
        return url
    changed = False
    netloc = parts.netloc
    if parts.password and not _is_reference(parts.password):
        host = parts.netloc.rpartition("@")[2]
        netloc = f"{parts.username or ''}:{_placeholder(server, 'password')}@{host}"
        changed = True
    query = parts.query
    if query:
        items = []
        for item in query.split("&"):
            key, sep, value = item.partition("=")
            if sep and value and _SECRET_NAME.search(unquote(key)) and not _is_reference(value):
                item = f"{key}={_placeholder(server, unquote(key))}"
                changed = True
            items.append(item)
        query = "&".join(items)
    if not changed:
        return url
    return urlunsplit(parts._replace(netloc=netloc, query=query))


def _redact_args(server: str, args: list) -> list:
    out: list = []
    i = 0
    while i < len(args):
        arg = args[i]
        if isinstance(arg, str):
            pair = _ARG_KEY_VALUE.match(arg)
            if pair and _SECRET_NAME.search(pair.group(1)) and not _is_reference(pair.group(2)):
                # "--api-key=abc" or "GITHUB_TOKEN=abc"
                out.append(f"{pair.group(1)}={_placeholder(server, pair.group(1).lstrip('-'))}")
                i += 1
                continue
            nxt = args[i + 1] if i + 1 < len(args) else None
            if (
                arg.startswith("-")
                and "=" not in arg
                and _SECRET_NAME.search(arg)
                and isinstance(nxt, str)
                and nxt
                and not nxt.startswith("-")
                and not _is_reference(nxt)
            ):
                # "--token abc"
                out.extend([arg, _placeholder(server, arg.lstrip("-"))])
                i += 2
                continue
        out.append(arg)
        i += 1
    return out


def redact_config(name: str, config: dict) -> dict:
    """Normalized copy of ``config`` with secret-looking values replaced by ``${CCS_...}`` placeholders.

    Covers header values (by header name), ``env`` values (by variable name), URL query
    parameters and ``user:password@`` (by parameter name), and ``--flag=value`` /
    ``--flag value`` style args (by flag name). Names are matched against
    /auth|token|key|secret|cookie/i. Values that are already ``${VAR}`` references are kept.
    Idempotent. It cannot see a secret hidden in a URL path or a positional arg.
    """
    cfg = normalize_config(config)
    if isinstance(cfg.get("headers"), dict):
        cfg["headers"] = {
            h: (_redact_header(name, h, v) if _SECRET_NAME.search(str(h)) else v)
            for h, v in cfg["headers"].items()
        }
    if isinstance(cfg.get("env"), dict):
        cfg["env"] = {
            k: (_placeholder(name, str(k)) if _SECRET_NAME.search(str(k)) and isinstance(v, str) and v and not _is_reference(v) else v)
            for k, v in cfg["env"].items()
        }
    if isinstance(cfg.get("url"), str):
        cfg["url"] = _redact_url(name, cfg["url"])
    if isinstance(cfg.get("args"), list):
        cfg["args"] = _redact_args(name, cfg["args"])
    return cfg


def _find_placeholders(value: Any) -> list[str]:
    """Distinct ``CCS_*`` variable names used anywhere inside a config, in order of appearance."""
    found: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for var in _PLACEHOLDER_RE.findall(item):
                if var not in found:
                    found.append(var)
        elif isinstance(item, dict):
            for sub in item.values():
                walk(sub)
        elif isinstance(item, list):
            for sub in item:
                walk(sub)

    walk(value)
    return found


def _resolve_config(config: dict) -> tuple[dict, list[str], list[str]]:
    """Fill ``${CCS_*}`` placeholders from ``os.environ``.

    Returns (resolved config, secret values that were substituted, unresolved variable names).
    Other ``${VAR}`` references are left for Claude Code to expand itself.
    """
    secrets: list[str] = []
    missing: list[str] = []

    def sub(match: re.Match) -> str:
        var = match.group(1)
        value = os.environ.get(var, "")
        if not value:
            if var not in missing:
                missing.append(var)
            return match.group(0)
        secrets.append(value)
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return _PLACEHOLDER_RE.sub(sub, item)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item

    return walk(copy.deepcopy(config)), secrets, missing


def _validate_server(name: Any, config: Any) -> None:
    if not isinstance(name, str) or not _SERVER_NAME_RE.match(name):
        raise McpProfileError(
            f"Invalid MCP server name {name!r}: use letters, digits, '_', '-' or '.', not starting with '-'."
        )
    if not isinstance(config, dict):
        raise McpProfileError(f"MCP server {name!r}: config must be a JSON object.")
    kind = _server_type(config)
    if kind in ("http", "sse", "ws") and not (isinstance(config.get("url"), str) and config["url"]):
        raise McpProfileError(f"MCP server {name!r}: type {kind!r} needs a 'url'.")
    if kind == "stdio" and not (isinstance(config.get("command"), str) and config["command"]):
        raise McpProfileError(f"MCP server {name!r}: stdio server needs a 'command'.")


def _check_profile(data: Any, where: Any = "profile") -> dict:
    if not isinstance(data, dict):
        raise McpProfileError(f"MCP profile {where} is not a JSON object.")
    version = data.get("version", PROFILE_VERSION)
    if version != PROFILE_VERSION:
        raise McpProfileError(f"MCP profile {where} has unsupported version {version!r} (expected {PROFILE_VERSION}).")
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise McpProfileError(f"MCP profile {where}: 'servers' must be an object.")
    for name, config in servers.items():
        _validate_server(name, config)
    return {"version": PROFILE_VERSION, "servers": copy.deepcopy(servers)}


# --------------------------------------------------------------------------- reading local state


def list_user_servers() -> dict[str, dict]:
    """User-scope servers from Claude Code's state file (``mcpServers``). Read-only."""
    data = fsutil.read_json(paths.claude_json(), {})
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {name: copy.deepcopy(cfg) for name, cfg in servers.items() if isinstance(cfg, dict)}


def _has_header_auth(config: dict) -> bool:
    headers = config.get("headers")
    if isinstance(headers, dict) and any(_SECRET_NAME.search(str(h)) for h in headers):
        return True
    return bool(config.get("headersHelper"))


def auth_state(name: str, config: dict, live_blob: dict) -> str:
    """"signed_in" | "needs_sign_in" | "header_auth" | "not_oauth" for one configured server."""
    kind = _server_type(config)
    if kind not in ("http", "sse"):
        return "not_oauth"
    if _has_header_auth(config):
        return "header_auth"
    store = live_blob.get("mcpOAuth") if isinstance(live_blob, dict) else None
    entry = store.get(server_key(name, config)) if isinstance(store, dict) else None
    if _has_tokens(entry):
        return "signed_in"
    return "needs_sign_in"  # absent, or an empty accessToken stub Claude Code wrote


def _has_tokens(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(entry.get("accessToken") or entry.get("refreshToken"))


def _live_blob() -> dict:
    return creds.parse_blob(keychain.read(paths.claude_keychain_service()))


# --------------------------------------------------------------------------- profile files


def _profile_path(path: Path | str | None) -> Path:
    return Path(path).expanduser() if path else paths.mcp_profile_file()


def load_profile(path: Path | None = None, *, missing_ok: bool = False) -> dict:
    """Read and validate a profile. A missing file is an error unless ``missing_ok``.

    (An empty result from a mistyped path must never reach ``apply --prune``.)
    """
    target = _profile_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return {"version": PROFILE_VERSION, "servers": {}}
        raise McpProfileError(
            f"No MCP profile at {target}. Create one with `ccs mcp export` or `ccs mcp add <names>`."
        ) from None
    except OSError as exc:
        raise McpProfileError(f"Cannot read MCP profile {target}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpProfileError(f"MCP profile {target} is not valid JSON: {exc}") from exc
    return _check_profile(data, target)


def save_profile(profile: dict, path: Path | None = None) -> Path:
    """Validate and atomically write a profile. Refuses a profile holding a literal secret."""
    checked = _check_profile(profile)
    for name, config in checked["servers"].items():
        if redact_config(name, config) != normalize_config(config):
            raise McpProfileError(
                f"MCP server {name!r} holds a literal secret (header, env var, URL or arg). "
                "Use a ${CCS_...} placeholder instead."
            )
    target = _profile_path(path)
    fsutil.atomic_write_json(target, checked)
    return target


def export_profile(path: Path | None = None, names: list[str] | None = None) -> Path:
    """Snapshot the current user-scope servers into a profile file, with secrets replaced by placeholders."""
    current = list_user_servers()
    if names is None:
        selected = list(current)
    else:
        unknown = [n for n in names if n not in current]
        if unknown:
            raise McpProfileError(
                f"Not configured at user scope: {', '.join(unknown)}. Configured: {', '.join(current) or 'none'}."
            )
        selected = list(dict.fromkeys(names))
    if not selected:
        raise McpProfileError(
            f"No user-scope MCP servers found in {paths.claude_json()}. "
            "Add some with `claude mcp add --scope user ...` or `ccs mcp add <names>`."
        )
    servers = {}
    for name in selected:
        _validate_server(name, current[name])
        servers[name] = redact_config(name, current[name])
    return save_profile({"version": PROFILE_VERSION, "servers": servers}, path)


def add_to_profile(names: list[str], path: Path | None = None) -> dict:
    """Add catalog servers to the profile file (created if missing). Returns the saved profile."""
    unknown = [n for n in names if n not in CATALOG]
    if unknown:
        raise McpProfileError(f"Unknown catalog server(s): {', '.join(unknown)}. Known: {', '.join(CATALOG)}.")
    profile = load_profile(path, missing_ok=True)
    for name in names:
        profile["servers"][name] = {k: copy.deepcopy(v) for k, v in CATALOG[name].items() if k != "note"}
    save_profile(profile, path)
    return profile


# --------------------------------------------------------------------------- planning / applying


def plan_apply(profile: dict, current: dict[str, dict], prune: bool = False) -> list[Change]:
    """Decide what ``apply`` would do. Pure apart from reading ``os.environ`` for placeholders.

    - "skip":      a ``${CCS_*}`` placeholder has no value in the environment
    - "add":       not configured yet
    - "update":    configured but different
    - "unchanged": configured and equal (placeholders resolved before comparing)
    - "remove":    configured but not in the profile; only when ``prune``
    """
    servers = profile.get("servers", {})
    changes: list[Change] = []
    for name, config in servers.items():
        resolved, _secrets, missing = _resolve_config(config)
        if missing:
            changes.append(Change("skip", name, config, "set " + ", ".join(f"${v}" for v in missing)))
        elif name not in current:
            changes.append(Change("add", name, config))
        elif normalize_config(current[name]) == normalize_config(resolved):
            changes.append(Change("unchanged", name, config))
        else:
            if server_key(name, current[name]) != server_key(name, resolved):
                # url/headers changed: the old token belongs to the old key and is not valid for the new one
                reason = "config differs; token key changes, sign in again after applying"
            else:
                reason = "config differs; existing sign-in is kept"
            changes.append(Change("update", name, config, reason))
    if prune:
        for name in sorted(current):
            if name not in servers:
                changes.append(Change("remove", name, None, "not in profile"))
    return changes


def _claude_candidates() -> list[Path]:
    return [
        paths.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]


def find_claude() -> str | None:
    configured, selected = paths.configured_claude_binary()
    if configured:
        return selected
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _claude_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _failure(rc: int, stdout: str, stderr: str, secrets: list[str]) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() + (stdout or "").splitlines() if ln.strip()]
    text = lines[0] if lines else f"exit {rc}"
    for secret in secrets:
        text = text.replace(secret, "***")
    return FAILED_PREFIX + text[:200]


def _add_argv(binary: str, name: str, config: dict) -> list[str]:
    payload = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
    return [binary, "mcp", "add-json", "--scope", "user", name, payload]


def _remove_argv(binary: str, name: str) -> list[str]:
    return [binary, "mcp", "remove", "--scope", "user", name]


def apply_profile(
    profile: dict | None = None,
    *,
    prune: bool = False,
    dry_run: bool = False,
    runner: Runner | None = None,
) -> list[Change]:
    """Make this Mac's user-scope servers match the profile, via the ``claude`` CLI.

    ``~/.claude.json`` is never edited directly. An "update" is remove + add (``add-json``
    refuses an existing name); if the re-add fails the old config is put back. Failures are
    returned as ``Change("skip", ..., reason="failed: ...")``. Returned configs keep their
    placeholders. ``profile=None`` loads the default profile file.

    ``claude mcp remove`` also deletes that server's tokens from the Keychain blob, so an update
    whose token key (name + type + url + headers) is unchanged snapshots the entry first and
    puts it back afterwards, keeping the sign-in. An update that changes the key does not (the
    old token is useless for the new key), and ``remove`` (``--prune``) loses it by design. If
    restoring fails the Change is ``skip`` with reason ``"re-sign-in needed: <name>"``.

    Do not call this while holding ``fsutil.app_lock()``: the restore step takes the lock itself
    (briefly, never across a ``claude`` call) and the lock is not re-entrant.
    """
    prof = _check_profile(profile) if profile is not None else load_profile()
    if prune and not prof["servers"]:
        raise McpProfileError("Refusing --prune: the profile has no servers, so every user-scope server would be removed.")
    current = list_user_servers()
    plan = plan_apply(prof, current, prune=prune)
    if dry_run or not any(c.action in ("add", "update", "remove") for c in plan):
        return plan

    binary = find_claude() or ("claude" if runner is not None else None)
    if binary is None:
        raise McpProfileError("Could not find the `claude` CLI (looked on PATH, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin).")
    run = runner or _default_runner

    results: list[Change] = []
    for change in plan:
        if change.action == "remove":
            rc, out, err = run(_remove_argv(binary, change.name))
            results.append(change if rc == 0 else Change("skip", change.name, None, _failure(rc, out, err, [])))
        elif change.action in ("add", "update"):
            results.append(_execute_set(change, current.get(change.name), binary, run))
        else:
            results.append(change)
    return results


def _snapshot_mcp(key: str) -> dict[str, dict]:
    """Copy this server's entries out of the live Keychain blob (read-only). {} when there are none."""
    blob = _live_blob()
    snapshot: dict[str, dict] = {}
    for store in _MCP_STORES:
        entries = blob.get(store)
        if isinstance(entries, dict) and isinstance(entries.get(key), dict):
            snapshot[store] = {key: copy.deepcopy(entries[key])}
    return snapshot


def _restore_mcp(snapshot: dict[str, dict]) -> None:
    """Put a snapshotted server entry back into the live blob if ``claude mcp remove`` wiped it.

    I1: the blob written is always ``restore_missing_mcp(<blob read just now>, snapshot)``, read
    inside the lock, so nothing Claude Code or cc-switcher wrote in the meantime is lost. An entry
    that is still there is left alone, as is any other part of the blob (including the login).
    The lock is held only for this read-modify-write, never across a ``claude`` call.
    Raises on any failure; callers must report it, not hide it.
    """
    if not snapshot:
        return
    service = paths.claude_keychain_service()
    with fsutil.app_lock():
        fresh = _live_blob()
        if not fresh:
            raise RuntimeError("live credentials item is gone")
        base = copy.deepcopy(fresh)
        for store, entries in snapshot.items():
            current = base.get(store)
            for key, old in entries.items():
                # An empty accessToken stub (Claude Code writes one when a server needs sign-in)
                # would shadow the real tokens, so drop it before merging.
                if isinstance(current, dict) and isinstance(current.get(key), dict):
                    if _has_tokens(old) and not _has_tokens(current[key]):
                        del current[key]
        merged = creds.restore_missing_mcp(base, snapshot)
        if merged == fresh:
            return  # entries survived, nothing to write
        account = keychain.read_account_attr(service) or paths.keychain_account_name()
        keychain.write(service, account, creds.dump_blob(merged))
        check = _live_blob()
        for store, entries in snapshot.items():
            for key, old in entries.items():
                found = (check.get(store) or {}).get(key) if isinstance(check.get(store), dict) else None
                if not isinstance(found, dict) or (_has_tokens(old) and not _has_tokens(found)):
                    raise RuntimeError("restored entry is not visible after the write")


def _restore_or_resign(change: Change, snapshot: dict[str, dict]) -> Change | None:
    """Restore the sign-in; on failure return the Change that tells the user to sign in again."""
    try:
        _restore_mcp(snapshot)
    except Exception as exc:  # noqa: BLE001 - any failure must surface as "re-sign-in needed"; class name only, no token text
        return Change("skip", change.name, change.config, f"{RESIGN_PREFIX}{change.name} ({type(exc).__name__})")
    return None


def _execute_set(change: Change, old_config: dict | None, binary: str, run: Runner) -> Change:
    name = change.name
    resolved, secrets, missing = _resolve_config(change.config or {})
    if missing:  # environment changed since planning
        return Change("skip", name, change.config, "set " + ", ".join(f"${v}" for v in missing))

    snapshot: dict[str, dict] = {}
    same_key = False
    if change.action == "update":
        # `claude mcp remove` also deletes the server's Keychain tokens. Snapshot them first so a
        # same-key update (the token is still valid for the new config) keeps its sign-in.
        old_key = server_key(name, old_config or {})
        same_key = old_key == server_key(name, resolved)
        try:
            snapshot = _snapshot_mcp(old_key)
        except (keychain.KeychainError, creds.CredentialsError) as exc:
            return Change(
                "skip", name, change.config,
                f"{FAILED_PREFIX}cannot read the Keychain to keep the sign-in ({type(exc).__name__}); nothing changed",
            )
        rc, out, err = run(_remove_argv(binary, name))
        if rc != 0:
            return Change("skip", name, change.config, _failure(rc, out, err, secrets))

    rc, out, err = run(_add_argv(binary, name, resolved))
    if rc == 0:
        if snapshot and same_key:
            return _restore_or_resign(change, snapshot) or change
        # Key changed (url/headers differ): the old token is not valid for the new key, so it is
        # deliberately not restored. plan_apply already told the user to sign in again.
        return change

    reason = _failure(rc, out, err, secrets)
    if change.action == "update" and old_config is not None:
        back_rc, _o, _e = run(_add_argv(binary, name, old_config))
        if back_rc == 0:
            reason += "; previous config restored"
            if snapshot and _restore_or_resign(change, snapshot) is not None:
                reason += f"; {RESIGN_PREFIX}{name}"  # the old config is back with the old key; its tokens are not
        else:
            reason += "; COULD NOT restore previous config, re-add it manually"
    return Change("skip", name, change.config, reason)


def has_failures(changes: list[Change]) -> bool:
    """True when a ``claude`` call failed or a server's sign-in could not be kept."""
    return any(c.action == "skip" and c.reason.startswith((FAILED_PREFIX, RESIGN_PREFIX)) for c in changes)


# --------------------------------------------------------------------------- status


def status(profile_path: Path | None = None) -> list[ServerStatus]:
    """Every server in the profile or configured at user scope, sorted by name, with sign-in state here."""
    profile = load_profile(profile_path, missing_ok=True)["servers"]
    current = list_user_servers()
    blob = _live_blob()
    rows: list[ServerStatus] = []
    for name in sorted(set(profile) | set(current), key=str.lower):
        configured = name in current
        config = current[name] if configured else profile[name]
        rows.append(
            ServerStatus(
                name=name,
                type=_server_type(config) or "unknown",
                url=config.get("url") if isinstance(config.get("url"), str) else None,
                in_profile=name in profile,
                configured=configured,
                auth=auth_state(name, config, blob) if configured else "not_configured",
            )
        )
    return rows
