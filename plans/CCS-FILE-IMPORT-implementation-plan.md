# Credential File Import Implementation Plan

**Goal:** Add the user-approved `ccs import --file PATH --label work` workflow for private JSON/text copies of Claude Keychain credentials.

**Design:** Parse a private local regular file containing either a full Keychain JSON object or a raw `claudeAiOauth` login. Accept a hex-encoded JSON value from `security -w` as well. Validate UUID identities and credential field types offline. Store only the login in a new CCS Keychain entry; register metadata without credential values. Preserve the active login and MCPs. Existing registry rows or Keychain entries are never overwritten. No login, usage check, subprocess Claude invocation or token renewal is part of import.

**Constraints:** Do not read real credential files or write the live Keychain while implementing/testing. Protect against special files, symlinks, oversized input and secret-bearing error text. Refuse group/world-accessible source files; advise chmod 600. Honor refresh recovery and login-in-progress guards under the existing app lock. Do not commit/install/push without new approval. The ongoing review owns token/usage implementation changes; file import owns only the new module/tests, CLI import entrypoint and documentation.

## Tasks

1. Write failing offline tests for accepted full/raw/hex JSON, private file handling, live/MCP preservation, duplicate account/item/label refusal, invalid UUID/token/expiry/scope inputs, no-network behavior and complete Keychain readback.
2. Implement `credential_import.import_file(path, *, label='') -> Account`, with bounded nonblocking no-follow file reading, redacted errors, UUID and token schema validation, locked duplicate/active-account guards, safe stdin-only Keychain writes and full readback before registry insertion. Preserve uncertain writes rather than delete potential credentials automatically.
3. Connect `--file` to `ccs import` while leaving plain `ccs import` unchanged. The success message explicitly says the account was not activated and credential validity was not checked online.
4. Add README and installation examples, including private file permissions and cross-device shared-refresh-grant caveat. Run targeted/full test suite and build the wheel. The user subsequently authorized committing, installing and verifying this feature, then pushing it as a one-time exception to their usual no-push rule.

## Verification

- 46 synthetic/offline importer tests passed, including special-file rejection, current/saved shared-grant refusal and full readback before registry insertion.
- Full isolated release suite: **1,449 passed**, with four threaded-fork deprecation warnings.
- Independent narrow review found duplicate saved grants were not checked; regression tests failed first, then passed after the comparison was added. Existing unreadable saved entries now fail closed.
- The 0.2.1 wheel matched every Python source module. All installed modules matched the verified wheel. CLI help exposes `--file`; the LaunchAgent is running.
- Local doctor: no problems, one pre-existing MCP server needs sign-in. No real credential file was imported or login initiated.
- Separate token/usage review fixes remain uncommitted in the main checkout and are excluded from this release.

## Verification focus

- Saved account A1 with a newer grant must not be overwritten by imported A0; refuse even if the caller uses another label.
- Active login A and its MCP stores remain byte-for-byte unchanged after importing inactive B.
- A FIFO or symlink must be rejected without blocking or following it; input above 1 MiB is rejected without unlimited reading.
- A Keychain write that reports success but changes only half the login must not register the account.
- A file that contains malformed JSON or a token in an unexpected field must not leak its contents into errors, stdout, metadata or diagnostics.
