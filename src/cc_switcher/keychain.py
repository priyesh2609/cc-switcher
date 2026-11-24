"""Thin wrapper over macOS ``/usr/bin/security`` for generic-password items.

Why the ``security`` CLI and not the Security framework: Claude Code reads its
credentials through ``security`` too. Items created/updated by ``security`` keep
``security`` in their access list, so Claude Code can keep reading them without
a Keychain prompt.

Secrets never go on the command line when avoidable: like Claude Code, writes go
through ``security -i`` on stdin with the payload hex-encoded (``-X``). Only a
payload too large for one ``security -i`` line (4032 chars, Claude Code's own
limit) falls back to argv, exactly as Claude Code does.
"""

from __future__ import annotations

import re
import subprocess

from cc_switcher import paths

SECURITY = "/usr/bin/security"
TIMEOUT_SECONDS = 10
STDIN_COMMAND_LIMIT = 4032  # same limit Claude Code uses for `security -i`

_SAFE_ATTR = re.compile(r"^[A-Za-z0-9 ._:@+-]+$")


class KeychainError(RuntimeError):
    pass


def _check_attr(value: str, what: str) -> str:
    # Values are placed inside double quotes on a `security -i` line; refuse anything
    # that could break out of the quoting.
    if not value or not _SAFE_ATTR.match(value) or len(value) > 255:
        raise KeychainError(f"Unsafe Keychain {what}: {value!r}")
    return value


def _run(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [SECURITY, *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainError(
            "Keychain did not answer in time. Is the login keychain locked or a prompt waiting?"
        ) from exc


def _scope(service: str) -> list[str]:
    """``-a <account>`` for Claude Code's live item, nothing for our own items.

    Claude Code reads and writes its login with ``-a <$USER> -s <service>``. If two items ever
    shared that service name under different accounts, an unscoped lookup could return the one
    Claude Code is *not* using, and a switch would write one item while the verify step read the
    other. Pinning the account makes cc-switcher see exactly what Claude Code sees. Our own
    ``cc-switcher:<id>`` services are unique, so they stay unscoped.
    """
    if service == paths.claude_keychain_service():
        return ["-a", paths.keychain_account_name()]
    return []


def read(service: str) -> str | None:
    """Return the secret stored for ``service``, or None when there is no such item."""
    result = _run(["find-generic-password", *_scope(service), "-s", service, "-w"])
    if result.returncode != 0:
        if result.returncode == 44 or "could not be found" in result.stderr:
            return None
        raise KeychainError(f"Keychain read failed for {service!r}: {result.stderr.strip()}")
    value = result.stdout.rstrip("\n")
    return _decode_hex_output(value) or None


_HEX = re.compile(r"\A(?:[0-9a-fA-F]{2})+\Z")


def _decode_hex_output(value: str) -> str:
    """``security -w`` prints the data as hex when it isn't printable text: undo that.

    Only an all-hex, even-length value that decodes to UTF-8 text of a JSON object is
    decoded (every item cc-switcher reads is one); anything else is returned unchanged.
    """
    if value.startswith("{") or not _HEX.match(value):
        return value
    try:
        decoded = bytes.fromhex(value).decode("utf-8")
    except UnicodeDecodeError:
        return value
    return decoded if decoded.lstrip().startswith("{") else value


def read_account_attr(service: str) -> str | None:
    """Return the ``acct`` attribute of the item, or None when there is no such item."""
    result = _run(["find-generic-password", *_scope(service), "-s", service])
    if result.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', result.stdout)
    return match.group(1) if match else None


def write(service: str, account: str, secret: str) -> None:
    """Create or update (``-U``) the item. Updating in place keeps the item's access list."""
    _check_attr(service, "service")
    _check_attr(account, "account")
    hex_payload = secret.encode("utf-8").hex()
    command = f'add-generic-password -U -a "{account}" -s "{service}" -X "{hex_payload}"\n'
    if len(command) <= STDIN_COMMAND_LIMIT:
        result = _run(["-i"], input_text=command)
    else:
        result = _run(["add-generic-password", "-U", "-a", account, "-s", service, "-X", hex_payload])
    if result.returncode != 0 or "error" in (result.stderr or "").lower():
        raise KeychainError(f"Keychain write failed for {service!r}: {(result.stderr or '').strip()}")


def delete(service: str) -> bool:
    """Delete one item. Returns False when there was nothing to delete."""
    result = _run(["delete-generic-password", *_scope(service), "-s", service])
    return result.returncode == 0
