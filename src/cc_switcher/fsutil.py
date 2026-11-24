"""Small filesystem helpers: atomic private JSON writes and a cross-process lock."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from cc_switcher import paths


class LockTimeout(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + fsync + rename in the same directory, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    # ensure_ascii=True: always encodable. Data can come from Claude Code's profile cache
    # (display names) and may hold a lone surrogate that UTF-8 refuses to encode.
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=True) + "\n", mode)


@contextmanager
def app_lock(timeout: float = 30.0) -> Iterator[None]:
    """Exclusive cross-process lock (CLI, menu bar app, background refresh all take it).

    Held around every read-modify-write of Keychain items or app files. Not re-entrant:
    take it once at the top of a public operation, never inside helpers.
    """
    ensure_private_dir(paths.app_dir())
    lock_path = paths.lock_file()
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        "Another cc-switcher operation is still running. Try again in a moment."
                    ) from exc
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
