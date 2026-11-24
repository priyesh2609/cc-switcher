# Claude Code–Driven Refresh Implementation Plan

**Goal:** Replace CCS's direct inactive-account OAuth refresh with a controlled Claude Code non-interactive invocation, while preserving account, MCP and credential safety.

**Architecture:** CCS checks expiry and uses Claude Code only for an inactive, distinct grant. An isolated configuration and Keychain item must preserve any rotated token across child failure; a durable exclusion/recovery mechanism must prevent stale credential overwrites or concurrent operations. Polling, usage GETs and server backoff remain distinct from token renewal.

**Tech stack:** Python 3.12, macOS Keychain, Claude Code CLI, pytest and rumps.

**Global constraints:** No CCS-origin OAuth token POST. Do not copy or overwrite the active login or MCP store for an inactive refresh. Keep at least three minutes between attempts, including across CCS processes and after crashes. Respect existing retry deadlines; do not assume one CLI invocation equals one HTTP request. Never log secrets. Do not create or push remote changes. The user authorized one commit for this change and installation on this Mac; do not make a new interactive login.

## Design gate

The user approved Claude Code's plaintext recovery fallback when Keychain fails: use a 0700 private directory and 0600 credential file, retain uncertain recovery material and document this exception. The user also approved opt-in experimental CLI renewal on trusted installations, disabled by default (`claude_refresh_enabled=false`), with explicit enabling here and on their other Mac. Managed hooks/detached descendants are not OS-contained; 180-second spacing applies to CCS invocations measured from completion, not CLI-internal HTTP requests. Preserve rotated credentials across child failure; fail closed with durable recovery state when process or storage state is ambiguous. Do not install or run the refresh path with real accounts until this is implemented and verified.

## Review focus

- A token R1 becomes R2 and the child exits unsuccessfully: R2 must remain recoverable and never be overwritten by R1.
- Parent crashes while the child is still running: another CCS process must not concurrently switch or refresh the same grant.
- A copied grant matches the current live login or live identity is unreadable: do not invoke the child.
- Claude Code stores credentials outside Keychain: avoid unapproved plaintext credential storage, and never delete the only valid successor.
- Legacy usage and token backoffs, clock changes, and child failure must not cause premature or concurrent retries.

## Tasks

1. Finalize the isolated CLI credential and crash-recovery protocol; identify its security and platform constraints. Record any user decision needed before implementing the protocol.
2. Add failing offline tests for no direct token POST, intended account isolation, token rotation, Keychain write failures, crash recovery, concurrency, retries and child environment sanitization.
3. Implement the safe process runner and integration in `tokens.py` and supporting modules; preserve active-to-saved synchronization and MCP state. Make the smallest necessary changes to scheduler, CLI/menu display and cached-backoff compatibility.
4. Update tests for removed behavior and update README, `docs/INSTALL.md`, and installation scripts if necessary. Explain model usage, authentication isolation, security boundaries and other-Mac setup.
5. Run targeted and full tests, build and inspect the wheel, and perform an independent review. If the safety gate and tests pass, install and restart the authorized local app, validate the running version, make the authorized local commit and fast-forward `main` if safe. Give the user the push command and other-Mac steps; do not push.

## Verification and release record

- Final offline suite: **1,403 passed**, with four threaded-fork deprecation warnings.
- Independent engine and integrated review findings were fixed with RED/GREEN tests: staged successor detection, directory-descriptor fallback reads, duplicate-label login ownership cleanup, successful-renewal access-token validation, nonempty-token live matching, and secure-storage fallback selection.
- Wheel 0.2.0 built offline; every Python module and icon matched source, and every installed Python module matched the wheel.
- The old agent was stopped under the credential lock after checking for unfinished refresh and retained-token warnings. Version 0.2.0 restarted on 2026-11-24 at 22:03 IST with the explicitly authorized experimental setting enabled.
- Local `ccs doctor`: no problems; one MCP server still needs its own sign-in. No new authentication was initiated.
- Live inactive-expired-token renewal remains unverified. The earlier `claude -p hi` used a valid live token and showed no rotation; it is not renewal evidence.
- Accepted limitations: trusted CLI rather than OS sandbox; internal OAuth retries are not spaced by CCS; protected recovery files/Keychain copies can persist; manual recovery is required for ambiguous process/storage state; unrelated external Keychain writers do not honor the CCS lock.
- No push performed. The user must push local `main` before the second Mac can fetch this version.
