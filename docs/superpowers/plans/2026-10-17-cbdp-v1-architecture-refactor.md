# CBDP v1 Architecture Refactor Implementation Plan

> **Execution mode:** inline implementation on local `master`.

## Goal

Refactor CodexBridge around the CBDP v1 event spine while preserving every
existing device feature. The legacy JSON line protocol remains available
through a compatibility gateway and does not own a second copy of business
state.

## Global Constraints

- Follow `docs/CBDP-V1-PROTOCOL.md` as the wire-format authority.
- Keep the management page and its feature refactor out of scope.
- Preserve existing pairing, capabilities, messages, read state, queue,
  speech, companion, and tray behavior.
- Do not commit user-created untracked files or unrelated page changes.
- Run the complete unit-test suite before committing.

## Task 1: Codec and capability primitives

- Fixed header, TLV, CRC, HMAC, capability bitmap, and profile budgets.
- Verification: `python -m unittest tests.test_cbdp_codec tests.test_cbdp_auth_capabilities -v`.

## Task 2: Event spine and projection

- Persist events in SQLite WAL with monotonic global, topic, and revision
  sequences.
- Project threads and messages from events and rebuild from the log after
  restart.
- Verification: projection and event tests.

## Task 3: Subscriptions and writer leases

- Stable topic IDs, cursor recovery, pending replay, cumulative ACKs, bounded
  device queues.
- Generation-checked writer leases with expiry, force takeover, and release.
- Verification: subscription and lease tests.

## Task 4: CBDP session, commands, and transactions

- HELLO, WELCOME, AUTH, AUTH_OK, PING, RESULT/ERROR framing.
- Capability gates, idempotency, delivery acknowledgements, text/audio
  transaction assembly, and resync.
- Adapter to the existing `CommandDispatcher`; no duplicated business logic.
- Verification: session and command gateway tests.

## Task 5: Legacy gateway and transport routing

- Detect JSON lines versus CBDP fixed headers on the same TCP port.
- Map legacy messages and events to the new command/event path without
  introducing a second state store.
- Verification: legacy gateway tests plus existing manager/session tests.

## Task 6: Composition root and lifecycle

- Construct EventSpine, projection, subscription manager, lease manager, and
  CBDP session support in `__main__.py`.
- Close persistent resources during shutdown.
- Verify existing startup and lifecycle tests.

## Task 7: Documentation and final verification

- Update README, legacy protocol navigation, and CBDP protocol notes.
- Record required implementation-time protocol corrections and their effective
  rules for client implementers.
- Run `python -m unittest discover -s tests -v`, `git diff --check`, and review
  the staged file list.
- Commit locally on `master`; do not push or publish.
