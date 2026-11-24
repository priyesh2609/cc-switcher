# Remove HTTP User-Agent Implementation Plan

## Goal

Experiment with sending CCS token-refresh, usage and reset-status HTTP requests without a
`User-Agent` header or an explicit Python-version header. This does not bypass provider
rate limits and does not guarantee that the server cannot identify the client by other means.

## Existing behavior and approach

The existing CCS code sends `User-Agent: claude-code/<version>`. Merely deleting that
header leaves urllib's default `User-Agent: Python-urllib/<version>`. Use a fresh urllib
opener with its default headers disabled, and remove any caller-supplied User-Agent. Keep
normal proxy, TLS, redirects, authorization headers and request bodies unchanged.

## Tasks

1. Add local-loopback wire tests for token refresh, usage, reset status and redirects, using
   synthetic credentials. Verify the tests fail against the old implementation.
2. Block default and custom urllib openers in the test harness; permit only the explicit
   loopback test to restore the stdlib opener.
3. Route CCS's default HTTP requests through the no-User-Agent transport without altering
   token-refresh scheduling, Retry-After or failure handling.
4. Run the full suite and build the package. Obtain explicit approval before replacing the
   installed menu-bar process or performing a real account-specific token refresh.
5. For the live experiment, respect server-provided Retry-After and the global three-minute
   token-refresh spacing. Record only redacted status and endpoint details; never log tokens.

## Limits

The application sends no User-Agent header, but a proxy could alter traffic and a server may
infer client properties from TLS or other metadata. A 200 or 429 on one test does not isolate
the User-Agent as the cause because timing and server state can differ.
