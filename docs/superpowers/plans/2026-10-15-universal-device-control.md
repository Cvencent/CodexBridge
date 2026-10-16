# Universal Device Control Implementation Plan

**Goal:** Mobile and ESP use one device-independent command and event model.

**Architecture:** Keep existing TCP and HTTP transports compatible. Both call
one command dispatcher; desktop IPC stays an internal execution adapter.
Capabilities are supported intersect enabled, not merely advertised features.

**Constraints:** No desktop restart or installation edits. No automatic retry
of writes after uncertain dispatch. Preserve legacy firmware and pairing.
Do not claim shared execution based only on router connectivity.

## Deliverables

- [x] Journal events with stable IDs, bounded replay and explicit resync.
- [x] Mobile reconnect sends Last-Event-ID and handles resync.
- [x] Shared command registry, validation, permissions and structured results.
- [x] Expose command registry and dispatcher on authenticated HTTP transport.
- [x] Map optional TCP device commands to the same dispatcher.
- [x] Desktop owner-routed replies, steering and interrupt with no double send.
- [x] Desktop follow subscriptions and canonical event conversion.
- [x] Mobile-created thread catalog registration and execution ownership.
- [x] Complete approval response types, not only accept/decline.
- [x] Mobile capabilities, errors, background notifications and integration tests.
- [ ] Protocol examples, full Python suite, Android build and real-device checks.

## Final Software Verification

2026-10-15: Python full suite 218 tests passed (18.728 seconds); compileall
and git diff --check passed. Android testDebugUnitTest and assembleRelease
passed; version 0.3.0/code 8, APK v2 signature verified. PyInstaller and Inno
Setup built Bridge 1.4.0. Authenticated HTTP command read against the real
dedicated desktop test task returned two messages and its actual desktop owner.

Final review fixed oldest-due discovery starvation, desktop event pump exception
survival, required desktop handoff with no independent fallback, pending IPC
request shutdown, live SSE capability revocation and persistent notification
reconciliation watermark. Regression tests reproduced each Python issue before
the fix; the final suite is green. Android watermark test failed to compile
before the new function and passed in the final Android suite.

Deployment remains incomplete: the execution policy rejected replacing the
running Bridge executable. Existing Bridge still reports 1.3.11 at port 8568;
discovery remains 8569. No desktop process or installation was modified. No adb
device is attached, so phone installation, background sound/vibration and device
specific power-management behavior are not verified. Android 15 dataSync service
time limits and disconnected-LAN delivery are not solved by this LAN transport.

## Verification Record

2026-10-15: desktop owner start/steer/interrupt and new-task hydration verified
against installed desktop. Dedicated integration task returned BRIDGE_OK;
desktop completion reached EventStream without a polling thread. SDK installed,
Android unit tests and signed release APK built. No attached adb device exists,
so audible/vibration and Android background delivery remain real-device checks.
No independent retry after uncertain desktop dispatch. TCP supports discovery,
replay subscriptions, capability filtering and bounded incoming fragmentation.

## Verification Focus

Each deliverable gets failing tests before implementation. Replay must be
atomic with live subscription; overflow and process restarts trigger resync.
Commands reject undeclared permissions, invalid types, reused IDs with changed
payload, oversized input and unsupported operations. Uncertain desktop writes
must not fall back to independent execution. New threads must be visible in
the desktop catalog before shared status is advertised. Android notification
delivery must be tested with app backgrounded and after network reconnect.
