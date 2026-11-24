"""Read and update Claude Code's global state file (``~/.claude.json``).

cc-switcher only ever changes one key in it, ``oauthAccount`` (the email / org
Claude Code shows for the logged-in account). Claude Code rewrites this file
often and it holds a lot of unrelated state (projects, user-scope MCP servers,
settings), so every write is a careful read-modify-write:

- every other key, the key order and the file mode are preserved;
- the new content goes to a temp file in the same directory, then ``os.replace``;
- right before the replace the file is re-stat'ed; if Claude Code changed it
  since we read it, we start over from a fresh read (at most ``MAX_ATTEMPTS``);
- a file that is not valid JSON is never overwritten.

When ``oauthAccount`` is removed, Claude Code fetches the profile again on its
next start (``populateOAuthAccountInfoIfNeeded`` in 2.1.330), so deleting the
key is the safe choice when we have no snapshot for an account.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from cc_switcher import paths

OAUTH_ACCOUNT_KEY = "oauthAccount"
MAX_ATTEMPTS = 5
_RETRY_SLEEP_SECONDS = 0.05


class ClaudeStateError(RuntimeError):
    pass


def _display(path: Path) -> str:
    """``~/.claude.json`` instead of an absolute path, for short human messages."""
    try:
        return "~/" + str(path.relative_to(paths.home()))
    except ValueError:
        return str(path)


def read_claude_json() -> dict[str, Any]:
    """The parsed file, or ``{}`` when it is missing, unreadable, or not a JSON object."""
    try:
        data = json.loads(paths.claude_json().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_oauth_account() -> dict[str, Any] | None:
    value = read_claude_json().get(OAUTH_ACCOUNT_KEY)
    return value if isinstance(value, dict) else None


def write_oauth_account(oauth_account: dict[str, Any] | None) -> None:
    """Set ``oauthAccount`` (or delete the key when None), leaving everything else as it was."""
    path = paths.claude_json()
    shown = _display(path)
    # Follow a symlink (dotfile managers) so we replace the real file, not the link.
    target = Path(os.path.realpath(path))
    try:
        for attempt in range(MAX_ATTEMPTS):
            if _try_write(target, oauth_account, last_attempt=attempt == MAX_ATTEMPTS - 1, shown=shown):
                return
            time.sleep(_RETRY_SLEEP_SECONDS)
    except OSError as exc:
        raise ClaudeStateError(f"Couldn't update {shown}: {exc.strerror or exc}") from exc
    raise ClaudeStateError(f"{shown} kept changing while cc-switcher tried to update it. Try again.")


def _try_write(target: Path, oauth_account: dict[str, Any] | None, *, last_attempt: bool, shown: str) -> bool:
    """One read-modify-write attempt. True when done (written or nothing to do); False to retry."""
    try:
        fh = open(target, "r", encoding="utf-8")
    except FileNotFoundError:
        if oauth_account is None:
            return True
        return _create(target, {OAUTH_ACCOUNT_KEY: oauth_account})

    with fh:
        before = os.fstat(fh.fileno())
        try:
            text = fh.read()
        except UnicodeDecodeError:
            text = None

    try:
        if text is None:
            raise ValueError("not UTF-8")
        data = json.loads(text)
    except ValueError:
        # Possibly caught mid-write by a non-atomic writer: retry, but never overwrite it.
        if last_attempt:
            raise ClaudeStateError(f"{shown} is not valid JSON, so cc-switcher left it alone.")
        return False
    if not isinstance(data, dict):
        raise ClaudeStateError(f"{shown} is not a JSON object, so cc-switcher left it alone.")

    if oauth_account is None:
        if OAUTH_ACCOUNT_KEY not in data:
            return True
        data.pop(OAUTH_ACCOUNT_KEY)
    else:
        if data.get(OAUTH_ACCOUNT_KEY) == oauth_account:
            return True
        data[OAUTH_ACCOUNT_KEY] = oauth_account  # keeps the key's position when it already exists

    new_text = _dumps(data)
    if text.endswith("\n"):
        new_text += "\n"

    tmp = _write_temp(target, new_text, before.st_mode & 0o7777)
    try:
        try:
            now = os.stat(target)
        except FileNotFoundError:
            return False
        if (now.st_ino, now.st_size, now.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
            return False  # Claude Code wrote it after our read: start over from fresh content
        os.replace(tmp, target)
        tmp = None
        return True
    finally:
        if tmp is not None:
            _unlink_quietly(tmp)


def _create(target: Path, data: dict[str, Any]) -> bool:
    """Create a missing file without clobbering one that appears concurrently."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_temp(target, _dumps(data), 0o600)
    try:
        os.link(tmp, target)  # atomic, and fails if the file now exists
    except FileExistsError:
        return False
    finally:
        _unlink_quietly(tmp)
    return True


def _dumps(data: dict[str, Any]) -> str:
    """Like Claude Code's ``JSON.stringify(x, null, 2)``.

    JSON.stringify writes a split emoji as ``"\\ud83d"``, which Python reads back as a lone
    surrogate that UTF-8 can't encode. Then fall back to ASCII escapes: same JSON value.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(data, indent=2, ensure_ascii=True)
    return text


def _write_temp(target: Path, text: str, mode: int) -> str:
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".ccs-tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return tmp


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
