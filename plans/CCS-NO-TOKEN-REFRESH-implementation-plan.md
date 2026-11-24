# Remove CCS Token Refresh Implementation Plan

**Status:** Paused. The user proposed invoking Claude Code non-interactively to refresh inactive accounts instead; investigate that design before executing this plan.

**Goal:** Stop CCS from exchanging OAuth refresh tokens; Claude Code alone refreshes the active login.

**Architecture:** CCS reads the live or saved access token and checks its expiry without performing a token POST. Expired inactive logins are still switchable; usage remains stale until Claude Code refreshes the active account. Continue synchronizing rotated live credentials into the saved Keychain item and preserve MCP stores. Usage GETs and their rate-limit backoffs remain in place.

**Tech stack:** Python 3.12, macOS Keychain, urllib, pytest, rumps.

**Scope and constraints:** Keep the existing no-User-Agent transport for usage and reset-status requests. Do not start a login or make a real OAuth request as part of implementation or tests. Do not install, restart or commit without separate approval. Older running app processes retain their old behavior until replaced.

## Review focus

- An expired inactive login must not cause a token POST, even when it contains a refresh token; its last good usage remains marked stale.
- An expired saved login must remain switchable and a simulated Claude Code rotation must be saved before switching away; preserve MCP entries.
- A valid saved access token remains usable for usage until the same 30-second expiry safety window as a live token.
- A legacy token-endpoint backoff must not suppress a valid usage GET; legacy usage-endpoint and unknown-origin backoffs remain honored.
- A recent cached snapshot followed by token-stale must not make an unavailable account eligible for an automatic switch.

## Tasks

1. Write tests demonstrating expired live and inactive credentials cause no refresh POST and never write a replacement login; valid tokens still work. Add the switch-and-simulated-CLI-rotation regression. Run the new tests against the old implementation and verify the intended failures.
2. Replace inactive refresh in `src/cc_switcher/tokens.py` with read-only token selection and expiry checks. Retain active-to-saved synchronization and the defensive same-refresh-token live identity check. Remove obsolete token POST, in-memory refresh recovery and scheduler code only after updating all call sites and tests; preserve usage transport behavior.
3. Adapt `monitor.py`, CLI and menu-bar messages to distinguish expired inactive and live credentials where practical. Preserve transient stale snapshots and usage-endpoint backoff; ignore only identified legacy token-endpoint backoff. Prevent token-stale cached candidates from automatic selection, with tests.
4. Update tests for removed functionality, retain tests for unrelated Keychain/MCP invariants and run targeted suites. Update README and installation guidance to explain that only Claude Code rotates credentials and inactive usage can become stale.
5. Run the complete isolated test suite, verify no CCS code path sends token POSTs, build and inspect the wheel, and report results and remaining deployment steps. No git commit or live-account request without specific authorization.
