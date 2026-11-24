"""Every filesystem path and Keychain service name cc-switcher touches.

All values are functions (not module constants) so tests can redirect them with
environment variables:

- ``CC_SWITCHER_HOME``  replaces ``~`` for every path below (tests set this to a tmp dir)
- ``CC_SWITCHER_DIR``   replaces the app data directory (default ``~/.config/cc-switcher``)
- ``CC_SWITCHER_CLAUDE_PATH`` pins the executable used for login and version detection
- ``CLAUDE_CONFIG_DIR`` honoured exactly like Claude Code honours it
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

# Claude Code's own constants (verified against Claude Code 2.1.330).
CLAUDE_KEYCHAIN_BASE = "Claude Code"
CLAUDE_KEYCHAIN_SUFFIX = "-credentials"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# cc-switcher's own names.
APP_NAME = "cc-switcher"
SAVED_LOGIN_SERVICE_PREFIX = "cc-switcher:"
LAUNCH_AGENT_LABEL = "dev.cc-switcher.menubar"


def home() -> Path:
    override = os.environ.get("CC_SWITCHER_HOME")
    return Path(override) if override else Path.home()


def configured_claude_binary() -> tuple[bool, str | None]:
    """Return (configured, executable). An invalid explicit path never falls back silently."""
    value = os.environ.get("CC_SWITCHER_CLAUDE_PATH")
    if not value:
        return False, None
    path = Path(value).expanduser()
    if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
        return True, str(path)
    return True, None


def app_dir() -> Path:
    override = os.environ.get("CC_SWITCHER_DIR")
    return Path(override) if override else home() / ".config" / APP_NAME


def accounts_file() -> Path:
    return app_dir() / "accounts.json"


def settings_file() -> Path:
    return app_dir() / "settings.json"


def usage_cache_file() -> Path:
    return app_dir() / "usage-cache.json"


def usage_history_file() -> Path:
    return app_dir() / "usage-history.jsonl"


def refresh_schedule_file() -> Path:
    return app_dir() / "refresh-schedule.json"


def mcp_profile_file() -> Path:
    return app_dir() / "mcp-profile.json"


def lock_file() -> Path:
    return app_dir() / ".lock"


def log_dir() -> Path:
    return home() / "Library" / "Logs" / APP_NAME


def launch_agent_plist() -> Path:
    return home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def claude_config_dir() -> Path:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR or ~/.claude."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else home() / ".claude"


def claude_json() -> Path:
    """Claude Code's global state file (holds ``oauthAccount`` and user-scope ``mcpServers``).

    Claude Code keeps it at ``~/.claude.json`` by default and inside
    ``$CLAUDE_CONFIG_DIR`` when that variable is set.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) / ".claude.json" if override else home() / ".claude.json"


def claude_keychain_service() -> str:
    """The Keychain service Claude Code reads its live credentials from.

    Mirrors Claude Code's ``m8("-credentials")``: no suffix by default; when
    ``CLAUDE_CONFIG_DIR`` is set, ``-`` + first 8 hex chars of sha256(NFC path).
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` (newer Claude Code) takes precedence:
    empty string means "no suffix", any other value is hashed instead.
    """
    secure_dir = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    config_dir = secure_dir if secure_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR")
    suffix = ""
    if config_dir:
        normalized = unicodedata.normalize("NFC", config_dir)
        suffix = "-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{CLAUDE_KEYCHAIN_BASE}{CLAUDE_KEYCHAIN_SUFFIX}{suffix}"


def keychain_account_name() -> str:
    """The ``-a`` attribute Claude Code uses: $USER if it is a safe token, else a fixed name."""
    import re

    user = os.environ.get("USER") or ""
    if not user:
        try:
            import pwd

            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:  # pragma: no cover - defensive
            user = ""
    return user if user and re.fullmatch(r"[a-zA-Z0-9._-]+", user) else "claude-code-user"


def saved_login_service(account_id: str) -> str:
    return f"{SAVED_LOGIN_SERVICE_PREFIX}{account_id}"
