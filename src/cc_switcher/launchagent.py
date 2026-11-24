"""Start the menu bar app at login via a per-user LaunchAgent.

Everything that touches the system goes through an injectable ``runner`` (default: a
thin ``subprocess.run`` wrapper around ``launchctl``) so tests never reach launchd.
A runner takes an argv list and returns an object with ``returncode``, ``stdout`` and
``stderr`` (a ``subprocess.CompletedProcess`` or any look-alike).
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from cc_switcher import paths

Runner = Callable[[Sequence[str]], Any]

LAUNCHCTL = "/bin/launchctl"
_PATH_TAIL = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
# Variables that change which Keychain item / config file Claude Code uses. launchd does not
# inherit the shell environment, so they are copied into the plist when set at install time.
_PASS_THROUGH_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")


class LaunchAgentError(RuntimeError):
    pass


def _default_runner(argv: Sequence[str]) -> Any:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{paths.LAUNCH_AGENT_LABEL}"


def resolve_ccs() -> list[str]:
    """argv prefix that runs cc-switcher from launchd or Terminal, without relying on PATH.

    The absolute path of the ``ccs`` executable (PATH first, then ``~/.local/bin/ccs`` where
    ``uv tool install`` puts it). If neither exists, ``python -m cc_switcher`` with the
    interpreter that is running right now.
    """
    found = shutil.which("ccs")
    if found:
        return [os.path.abspath(found)]
    candidate = paths.home() / ".local" / "bin" / "ccs"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return [os.path.abspath(candidate)]
    return [sys.executable, "-m", "cc_switcher"]


def default_program() -> list[str]:
    return [*resolve_ccs(), "menubar"]


def plist_dict(program: list[str]) -> dict[str, Any]:
    log_dir = paths.log_dir()
    path_value = ":".join([str(paths.home() / ".local" / "bin"), *_PATH_TAIL])
    env = {"PATH": path_value}
    # launchd has a different PATH from the installing shell. Keep the executable
    # the shell selected (or its explicit override) so login and version probing agree.
    claude = os.environ.get("CC_SWITCHER_CLAUDE_PATH") or shutil.which("claude")
    if claude:
        env["CC_SWITCHER_CLAUDE_PATH"] = os.path.abspath(claude)
    for name in _PASS_THROUGH_ENV:
        if name in os.environ:
            env[name] = os.environ[name]
    return {
        "Label": paths.LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(part) for part in program],
        "RunAtLoad": True,
        # Restart after a crash, but not after a deliberate Quit (exit status 0).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(log_dir / "menubar.out.log"),
        "StandardErrorPath": str(log_dir / "menubar.err.log"),
        "EnvironmentVariables": env,
    }


def is_installed() -> bool:
    return paths.launch_agent_plist().is_file()


def _is_loaded(run: Runner) -> bool:
    return run([LAUNCHCTL, "print", _target()]).returncode == 0


def is_running(runner: Runner | None = None) -> bool:
    result = (runner or _default_runner)([LAUNCHCTL, "print", _target()])
    if result.returncode != 0:
        return False
    return re.search(r"^\s*state = running\s*$", result.stdout or "", re.MULTILINE) is not None


def _write_plist(path: Path, data: dict[str, Any]) -> None:
    """Atomic write; launchd wants the file owned by the user and not group/world writable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(plistlib.dumps(data))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install(
    program: list[str] | None = None,
    runner: Runner | None = None,
    *,
    reload: bool = True,
) -> Path:
    """Write the plist and load it (``launchctl bootstrap``). Returns the plist path.

    ``reload=True`` (CLI default) unloads a running copy first so a changed program path takes
    effect. The menu bar app passes ``reload=False``: unloading would terminate the very
    process asking for the install.
    """
    run = runner or _default_runner
    plist_path = paths.launch_agent_plist()
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    _write_plist(plist_path, plist_dict(program if program is not None else default_program()))

    loaded = _is_loaded(run)
    if loaded and not reload:
        return plist_path
    if loaded:
        run([LAUNCHCTL, "bootout", _target()])  # ignore failures: it may not be loaded after all
    result = run([LAUNCHCTL, "bootstrap", _domain(), str(plist_path)])
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip() or f"exit status {result.returncode}"
        raise LaunchAgentError(f"launchctl bootstrap failed: {detail}")
    return plist_path


def uninstall(runner: Runner | None = None, *, unload: bool = True) -> bool:
    """Unload the agent and delete the plist. Returns True when something was installed.

    ``unload=False`` only deletes the plist (the menu bar app uses it: unloading would kill
    the caller). The running copy then simply doesn't come back at the next login.
    """
    run = runner or _default_runner
    plist_path = paths.launch_agent_plist()
    existed = plist_path.exists()
    if unload and (existed or _is_loaded(run)):
        run([LAUNCHCTL, "bootout", _target()])  # not loaded -> non-zero, which is fine
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    return existed
