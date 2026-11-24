"""LaunchAgent plist + install/uninstall command sequences (injected runner; launchd is never reached),
plus static checks of the shell scripts (they are only ever executed by a human on a real Mac)."""

from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_switcher import launchagent, paths

REPO = Path(__file__).resolve().parent.parent
UID = os.getuid()
TARGET = f"gui/{UID}/{paths.LAUNCH_AGENT_LABEL}"


class FakeLaunchctl:
    """Records argv lists; ``loaded`` is what `launchctl print` reports."""

    def __init__(self, loaded: bool = False, state: str = "running", bootstrap_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.loaded = loaded
        self.state = state
        self.bootstrap_rc = bootstrap_rc

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1]
        if verb == "print":
            if not self.loaded:
                return SimpleNamespace(returncode=113, stdout="", stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout=f"{TARGET} = {{\n\tstate = {self.state}\n\tpid = 1\n}}\n", stderr="")
        if verb == "bootstrap":
            if self.bootstrap_rc == 0:
                self.loaded = True
            return SimpleNamespace(returncode=self.bootstrap_rc, stdout="", stderr="Bootstrap failed: 5: Input/output error" if self.bootstrap_rc else "")
        if verb == "bootout":
            was_loaded, self.loaded = self.loaded, False
            return SimpleNamespace(returncode=0 if was_loaded else 3, stdout="", stderr="")
        raise AssertionError(f"unexpected launchctl call {argv}")

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


PROGRAM = ["/Users/tester/.local/bin/ccs", "menubar"]


# --------------------------------------------------------------------------- plist contents


def test_plist_dict_has_the_documented_keys():
    plist = launchagent.plist_dict(PROGRAM)
    assert plist["Label"] == "dev.cc-switcher.menubar" == paths.LAUNCH_AGENT_LABEL
    assert plist["ProgramArguments"] == PROGRAM
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProcessType"] == "Interactive"
    assert plist["LimitLoadToSessionType"] == "Aqua"
    log_dir = paths.log_dir()
    assert Path(plist["StandardOutPath"]).parent == log_dir
    assert Path(plist["StandardErrorPath"]).parent == log_dir
    assert plist["StandardOutPath"] != plist["StandardErrorPath"]


def test_plist_path_covers_the_tool_locations_in_order():
    path = launchagent.plist_dict(PROGRAM)["EnvironmentVariables"]["PATH"].split(":")
    assert path[0] == str(paths.home() / ".local" / "bin")
    assert "/opt/homebrew/bin" in path and "/usr/local/bin" in path
    assert path.index("/opt/homebrew/bin") < path.index("/usr/bin")


def test_plist_passes_claude_config_dir_through_only_when_set(monkeypatch):
    assert "CLAUDE_CONFIG_DIR" not in launchagent.plist_dict(PROGRAM)["EnvironmentVariables"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/tester/.claude-work")
    env = launchagent.plist_dict(PROGRAM)["EnvironmentVariables"]
    assert env["CLAUDE_CONFIG_DIR"] == "/Users/tester/.claude-work"


def test_plist_remembers_the_installing_shells_claude_path(monkeypatch):
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: "/opt/homebrew/bin/claude" if name == "claude" else None)
    env = launchagent.plist_dict(PROGRAM)["EnvironmentVariables"]
    assert env["CC_SWITCHER_CLAUDE_PATH"] == "/opt/homebrew/bin/claude"


def test_plist_respects_an_explicit_claude_path(monkeypatch):
    monkeypatch.setenv("CC_SWITCHER_CLAUDE_PATH", "/custom/bin/claude")
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: "/other/bin/claude")
    env = launchagent.plist_dict(PROGRAM)["EnvironmentVariables"]
    assert env["CC_SWITCHER_CLAUDE_PATH"] == "/custom/bin/claude"


def test_plist_serializes_with_plistlib():
    data = plistlib.loads(plistlib.dumps(launchagent.plist_dict(PROGRAM)))
    assert data["ProgramArguments"] == PROGRAM


# --------------------------------------------------------------------------- default program


def test_resolve_ccs_prefers_path(monkeypatch):
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: "/opt/somewhere/bin/ccs")
    assert launchagent.resolve_ccs() == ["/opt/somewhere/bin/ccs"]
    assert launchagent.default_program() == ["/opt/somewhere/bin/ccs", "menubar"]


def test_resolve_ccs_falls_back_to_local_bin(monkeypatch):
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: None)
    ccs = paths.home() / ".local" / "bin" / "ccs"
    ccs.parent.mkdir(parents=True)
    ccs.write_text("#!/bin/sh\n")
    ccs.chmod(0o755)
    assert launchagent.resolve_ccs() == [str(ccs)]


def test_resolve_ccs_last_resort_is_python_module(monkeypatch):
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: None)
    assert launchagent.resolve_ccs() == [sys.executable, "-m", "cc_switcher"]


# --------------------------------------------------------------------------- install


def test_install_writes_plist_then_bootstraps():
    runner = FakeLaunchctl(loaded=False)
    result = launchagent.install(PROGRAM, runner=runner)

    assert result == paths.launch_agent_plist()
    assert launchagent.is_installed()
    assert plistlib.loads(result.read_bytes())["ProgramArguments"] == PROGRAM
    assert stat.S_IMODE(result.stat().st_mode) == 0o644
    assert paths.log_dir().is_dir()
    assert runner.verbs == ["print", "bootstrap"]
    assert runner.calls[-1] == ["/bin/launchctl", "bootstrap", f"gui/{UID}", str(result)]


def test_install_reloads_a_loaded_agent_by_default():
    runner = FakeLaunchctl(loaded=True)
    launchagent.install(PROGRAM, runner=runner)
    assert runner.verbs == ["print", "bootout", "bootstrap"]
    assert runner.calls[1] == ["/bin/launchctl", "bootout", TARGET]


def test_install_without_reload_leaves_a_loaded_agent_alone():
    # The menu bar app calls it this way: bootout would terminate the process that asked.
    runner = FakeLaunchctl(loaded=True)
    launchagent.install(PROGRAM, runner=runner, reload=False)
    assert runner.verbs == ["print"]
    assert launchagent.is_installed()  # the plist is still (re)written


def test_install_reports_launchctl_failure():
    runner = FakeLaunchctl(loaded=False, bootstrap_rc=5)
    with pytest.raises(launchagent.LaunchAgentError, match="Input/output error"):
        launchagent.install(PROGRAM, runner=runner)


def test_install_defaults_the_program(monkeypatch):
    monkeypatch.setattr(launchagent.shutil, "which", lambda name: "/opt/x/bin/ccs")
    launchagent.install(runner=FakeLaunchctl())
    plist = plistlib.loads(paths.launch_agent_plist().read_bytes())
    assert plist["ProgramArguments"] == ["/opt/x/bin/ccs", "menubar"]


def test_install_is_idempotent():
    runner = FakeLaunchctl()
    launchagent.install(PROGRAM, runner=runner)
    first = paths.launch_agent_plist().read_bytes()
    launchagent.install(PROGRAM, runner=runner)
    assert paths.launch_agent_plist().read_bytes() == first
    assert runner.verbs == ["print", "bootstrap", "print", "bootout", "bootstrap"]


# --------------------------------------------------------------------------- uninstall


def test_uninstall_boots_out_and_deletes_plist():
    runner = FakeLaunchctl()
    launchagent.install(PROGRAM, runner=runner)
    runner.calls.clear()

    assert launchagent.uninstall(runner=runner) is True
    assert runner.verbs == ["bootout"]
    assert runner.calls[0] == ["/bin/launchctl", "bootout", TARGET]
    assert not paths.launch_agent_plist().exists()
    assert not launchagent.is_installed()


def test_uninstall_when_nothing_is_installed_does_nothing():
    runner = FakeLaunchctl(loaded=False)
    assert launchagent.uninstall(runner=runner) is False
    assert runner.verbs == ["print"]


def test_uninstall_boots_out_a_loaded_agent_even_without_a_plist():
    runner = FakeLaunchctl(loaded=True)
    assert launchagent.uninstall(runner=runner) is False
    assert runner.verbs == ["print", "bootout"]


def test_uninstall_without_unload_only_deletes_the_plist():
    runner = FakeLaunchctl()
    launchagent.install(PROGRAM, runner=runner)
    runner.calls.clear()
    assert launchagent.uninstall(runner=runner, unload=False) is True
    assert runner.calls == []
    assert not paths.launch_agent_plist().exists()


# --------------------------------------------------------------------------- running state


@pytest.mark.parametrize(
    ("loaded", "state", "expected"),
    [(True, "running", True), (True, "waiting", False), (False, "running", False)],
)
def test_is_running(loaded, state, expected):
    runner = FakeLaunchctl(loaded=loaded, state=state)
    assert launchagent.is_running(runner=runner) is expected
    assert runner.calls == [["/bin/launchctl", "print", TARGET]]


def test_default_runner_goes_through_subprocess_which_tests_block():
    with pytest.raises(AssertionError, match="Real subprocess blocked"):
        launchagent.is_running()


# --------------------------------------------------------------------------- shell scripts (static)


@pytest.mark.parametrize("name", ["install.sh", "uninstall.sh"])
def test_scripts_are_executable_strict_bash(name):
    path = REPO / "scripts" / name
    text = path.read_text()
    assert path.stat().st_mode & stat.S_IXUSR
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


def test_install_script_follows_the_documented_steps_in_order():
    text = (REPO / "scripts" / "install.sh").read_text()
    markers = [
        'step "Checking macOS"',
        'step "Checking Claude Code"',
        'step "Checking uv',
        'uv tool install --python 3.12 --force',
        '"${CCS}" import',
        '"${CCS}" mcp apply "${CC_SWITCHER_MCP_PROFILE}"',
        '"${CCS}" agent install',
        '"${CCS}" doctor',
    ]
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions)
    assert "CC_SWITCHER_SOURCE" in text and "--no-agent" in text
    # PATH advice is printed, never written into a shell profile
    assert not [line for line in text.splitlines() if "zshrc" in line and (">>" in line or "tee" in line or "sed -i" in line)]


def test_uninstall_script_never_touches_claude_codes_own_item():
    text = (REPO / "scripts" / "uninstall.sh").read_text()
    assert "Claude Code-credentials" not in text
    # the only Keychain deletions target cc-switcher's own services
    deletions = [line for line in text.splitlines() if "delete-generic-password" in line]
    assert deletions and all('"cc-switcher:' in line for line in deletions)
    assert "--purge" in text and "agent uninstall" in text and "uv tool uninstall cc-switcher" in text
