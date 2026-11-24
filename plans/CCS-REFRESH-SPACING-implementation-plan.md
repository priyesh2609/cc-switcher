# OAuth Refresh Spacing Implementation Plan

**Goal:** Space saved-account OAuth POST attempts globally by at least 180 seconds without delaying usage GETs.

**Architecture:** Reserve attempts under the existing cross-process app lock immediately before the transport call, storing only private non-secret scheduling metadata. Persist an in-flight marker before the call and measure the next 180-second interval from completion, even for failed POSTs. Save a successfully rotated login, or retain it in `_UNSAVED` on write failure, before completing scheduling metadata in a `finally` block under the same lock; this also records a 429's Retry-After before releasing the lock. If a process dies in flight, start a fresh 180-second recovery interval when another process observes the marker. Deferred accounts retain last-good usage and are retried on later polls. Queue pending accounts in first-seen order with a renewable lease of at least two poll intervals, and preserve provider backoff independently. Under the existing lock, remove queue heads that became live, fresh, missing, or removed before granting a slot. Require both wall and monotonic clocks to advance by the interval; a backwards wall-clock change delays rather than accelerates a refresh. A monotonic reset rebases clocks and starts a fresh 180-second recovery wait, restarting any account-specific provider wait in full. The existing lock continues to protect live-account and token-rotation invariants.

**Files:** `src/cc_switcher/refresh_schedule.py` owns private scheduling state and reservations; `paths.py` exposes its path; `tokens.py` wraps only actual POSTs; `monitor.py`, `cli.py`, and `menubar.py` expose queued outcomes; `tests/test_refresh_spacing.py` exercises scheduling; `tests/test_switcher.py` advances a fake clock between two existing token-rotation scenarios.

**Tech stack:** Python, private atomic JSON files, Keychain abstraction, pytest.

**Reference:** `plans/cc-switcher-implementation-plan.md` and the requested change.

## Constraints and review focus

- Never refresh the live grant or serialize credentials, and preserve Keychain rollback and MCP state.
- Reserve only when a valid refresh-token POST will actually be invoked, including failures and 429 responses.
- Never sleep for the spacing period or hold the lock while waiting for it.
- Treat unavailable or damaged scheduling metadata as an actionable scheduler error, not a network error or ordinary queue; fail closed and never overwrite damaged metadata.
- Preserve server Retry-After independently, including across processes.
- Pending accounts must not be starved by an earlier repeatedly failing account; stale queue members must not block forever.
- Backward clock changes defer rather than allow an early POST; process restarts use persisted state.
- Cover corrupt state, rollback, private permissions, consecutive attempts and the exact boundary.

## Task 1 — Regression tests and coordinator

- [x] Add failing tests using fake Keychain and HTTP for 180-second spacing, failures, fairness, backwards clocks, restart/cross-process coordination, and valid-token GETs.
- [x] Run those tests and verify the missing behavior fails.
- [x] Add the scheduling metadata file and locked reservation/queue logic, immediately before the transport call; do not reserve for missing refresh tokens.
- [x] Ensure token-rotation persistence and live-account checks remain under the existing lock.
- [x] Run coordinator tests, including private-file and process coordination cases.

## Task 2 — Monitoring and presentation

- [x] Add failing tests for queued status and preservation of the last-good cache, CLI and menu text, unaffected usage GETs, and independent 429 backoff.
- [x] Convert deferrals to a non-permanent pending outcome and show the queue in CLI and menu bar without erasing prior usage.
- [x] Run focused tests, then the full suite without installing anything. The assigned worktree has no `.venv`, so use the existing main-checkout interpreter with `PYTHONPATH` pointing to this worktree's `src`.
