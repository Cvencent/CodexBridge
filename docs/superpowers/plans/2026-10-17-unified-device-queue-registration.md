# CodexBridge 统一设备、注册与待发送队列实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把手机、ESP 和管理页统一到一套设备注册协议，并让电脑端 Bridge 持久化、同步和执行跨设备待发送队列。

**Architecture:** `DeviceRegistry` 是统一设备身份与注册状态的权威来源；`QueueService` 持有线程级 FIFO 队列，先原子落盘再广播。手机 HTTP、ESP TCP 与管理页都通过 `CommandDispatcher` 修改同一份状态。手机用配对码即时批准；ESP 用 `pending -> 管理页批准 -> register_offer -> register_ok -> bridge_auth`。

**Tech Stack:** Python 3.13、标准库 HTTP/TCP/UDP、unittest、PowerShell、Kotlin/Compose、Gradle、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-10-17-unified-device-queue-registration-design.md`

## Global Constraints

- 协议改动与 `PROTOCOL.md` 必须在同一提交中更新。
- 单线程最多 100 项，未完成项正文最多 10000 字符，令牌不得出现在公开 API 或日志中。
- 未声明 `queue` 能力的旧客户端继续使用 `turn.start` / `mode=steer`。
- 管理批准、拒绝、撤销接口只允许 `127.0.0.1` 回环访问。
- 不提交 `build/`、`dist/`、`release/`、APK、日志、截图、XML 和现有未跟踪诊断文件。
- 默认测试命令：`python -m unittest discover -s tests -v`。
- 当前版本为 `1.4.6`，本次发布目标版本为 `1.4.7`。

## Review Focus

- 旧 `devices.json`、`companion.json` 和未声明注册字段的旧固件升级后必须继续工作。
- 同一线程并发入队、编辑、删除、派发时只能有一个 `turn.start`，旧 revision 不能覆盖新状态。
- 队列落盘失败或 Bridge 重启时不能报告成功、重复派发或静默丢失正文。
- ESP nonce 必须绑定设备、来源地址和发现时间；过期、复用、错误地址必须失败。
- 手机、ESP、SSE、TCP 和管理页必须对同一队列项收敛到同一状态。

---

### Task 1: 队列核心、持久化与统一命令

**Files:**
- Create: `codexbridge/companion/queue.py`
- Modify: `codexbridge/protocol/capabilities.py`
- Modify: `codexbridge/protocol/commands.py`
- Modify: `codexbridge/companion/store.py`
- Modify: `codexbridge/companion/service.py`
- Test: `tests/test_queue_service.py`
- Test: `tests/test_device_commands.py`
- Test: `tests/test_capabilities.py`

**Interfaces:**
- Produces: `QueueService(path, *, start_turn, steer_turn, thread_exists, is_active, publish)`.
- Produces: `list`, `enqueue`, `edit`, `delete`, `steer`, and `on_turn_finished`.
- Produces: `queue.list|enqueue|edit|delete|steer` commands and `queue_*` events.

- [ ] **Step 1: Write failing persistence, revision, and dispatch tests in `tests/test_queue_service.py`.**
- [ ] **Step 2: Run `python -m unittest tests.test_queue_service -v`; expect import failure for the missing module.**
- [ ] **Step 3: Implement `QueueService` with atomic JSON persistence, serialized per-thread dispatch, revision conflicts, and failed-item pause/resume.**
- [ ] **Step 4: Add failing `queue` capability and command tests in `tests/test_device_commands.py` and `tests/test_capabilities.py`.**
- [ ] **Step 5: Add `queue` to `FEATURES` and `PHONE_CAPABILITIES`, register the five commands, and route them through `CompanionService`.**
- [ ] **Step 6: Add `queue` to `read_thread()`, `queued_count` to `list_threads()`, and queue lifecycle calls to turn completion/start/stop.**
- [ ] **Step 7: Run focused tests, then `python -m unittest discover -s tests -v`; both must pass.**
- [ ] **Step 8: Update `PROTOCOL.md`, stage only Task 1 files, and commit `feat: add bridge-owned cross-device message queue`.**

### Task 2: 手机 HTTP、SSE 与 Hub 队列同步

**Files:**
- Modify: `codexbridge/companion/api.py`
- Modify: `codexbridge/hub.py`
- Test: `tests/test_companion_api.py`
- Test: `tests/test_hub.py`

**Interfaces:**
- Consumes: Task 1 queue commands/events.
- Produces: queue-aware `/api/companion/commands`, legacy `mode=start` mapping, SSE filtering, and Hub replay.

- [ ] **Step 1: Write failing tests for `queue.enqueue` over HTTP and queue-capable legacy start mapping.**
- [ ] **Step 2: Run `python -m unittest tests.test_companion_api -v`; expect failures because mapping and filtering are absent.**
- [ ] **Step 3: Map queue-capable legacy `mode=start` to `queue.enqueue`; keep `mode=steer` immediate and old phones unchanged.**
- [ ] **Step 4: Add failing Hub tests for `events_subscribe` with only `queue` enabled and attach-time `queue_snapshot`.**
- [ ] **Step 5: Classify `queue_*` events under `queue`, permit queue subscriptions, and replay queue snapshots.**
- [ ] **Step 6: Run focused tests, then the full suite; both must pass.**
- [ ] **Step 7: Update `PROTOCOL.md` and commit `feat: sync queue events across phone and device transports`.**

### Task 3: 统一 DeviceRegistry 与手机迁移

**Files:**
- Modify: `codexbridge/devices/registry.py`
- Modify: `codexbridge/companion/store.py`
- Modify: `codexbridge/companion/api.py`
- Modify: `codexbridge/hub.py`
- Modify: `codexbridge/__main__.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_companion_store.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Produces: `DeviceRecord.kind`, `transport`, `registration_state`, `registration_method`, `last_address`, `client_id`, `token_hash`, and public `has_token`.
- Produces: `DeviceRegistry.upsert_phone(...)`, `public(...)`, `public_all(...)`.
- Produces: `CompanionStore(registry=...)` synchronization.

- [ ] **Step 1: Write failing version-1 hardware migration and token-hiding tests.**
- [ ] **Step 2: Run `python -m unittest tests.test_registry -v`; expect missing unified fields.**
- [ ] **Step 3: Extend `DeviceRecord` and persist version 2 while preserving plaintext hardware tokens and hashed phone tokens privately.**
- [ ] **Step 4: Write failing phone-to-registry sync and unified admin-list tests.**
- [ ] **Step 5: Sync phone load/claim/negotiate/touch/remove into the registry; derive admin and compatibility companion views from it.**
- [ ] **Step 6: Run focused tests, then the full suite; both must pass.**
- [ ] **Step 7: Update `PROTOCOL.md` and commit `refactor: unify phone and hardware device registry`.**

### Task 4: ESP 主动注册与注册下发握手

**Files:**
- Modify: `codexbridge/devices/discovery.py`
- Modify: `codexbridge/devices/session.py`
- Modify: `codexbridge/devices/manager.py`
- Modify: `codexbridge/devices/registry.py`
- Modify: `codexbridge/admin_api.py`
- Modify: `codexbridge/__main__.py`
- Test: `tests/test_discovery.py`
- Test: `tests/test_session.py`
- Test: `tests/test_device_manager.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Produces: discovery `registration_state`/`registration_nonce`.
- Produces: `send_register_offer(...)` and `DeviceManager.approve|reject|revoke`.
- Produces: admin `/api/admin/devices/approve|reject|revoke`.

- [ ] **Step 1: Write failing discovery nonce validation and `register_offer`/`register_ok` tests.**
- [ ] **Step 2: Run `python -m unittest tests.test_discovery tests.test_session -v`; expect missing fields/handshake.**
- [ ] **Step 3: Parse optional registration fields and implement the one-shot registration session.**
- [ ] **Step 4: Write failing manager tests for pending-no-session and approve-sends-offer-then-connects.**
- [ ] **Step 5: Implement pending, approved, rejected, and revoked transitions with nonce/address/expiry checks.**
- [ ] **Step 6: Add loopback-only admin approve/reject/revoke routes and typed errors.**
- [ ] **Step 7: Run focused tests, then the full suite; both must pass.**
- [ ] **Step 8: Update `PROTOCOL.md` and commit `feat: require admin approval for hardware registration`.**

### Task 5: 管理页统一设备中心

**Files:**
- Modify: `codexbridge/settings_page.py`
- Test: `tests/test_settings_page.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: Task 3 unified rows and Task 4 actions.
- Produces: `连接与发现` plus one `设备中心` with filters `全部 / 手机 / 硬件 / 待批准`.

- [ ] **Step 1: Write failing page-contract tests for one device center and approve/reject actions.**
- [ ] **Step 2: Run `python -m unittest tests.test_settings_page -v`; expect the old two-panel markup.**
- [ ] **Step 3: Merge phone connection settings into `连接与发现` and make `设备中心` render pending/online/offline rows.**
- [ ] **Step 4: Add filters and state-specific phone/hardware actions.**
- [ ] **Step 5: Run focused tests, then the full suite; both must pass.**
- [ ] **Step 6: Commit `feat: unify device management page`.**

### Task 6: Android 手机队列界面与发送改造

**Files:**
- Modify: `mobile/android/app/src/main/java/com/codexbridge/mobile/data/ApiModels.kt`
- Modify: `mobile/android/app/src/main/java/com/codexbridge/mobile/data/ApiClient.kt`
- Modify: `mobile/android/app/src/main/java/com/codexbridge/mobile/AppViewModel.kt`
- Modify: `mobile/android/app/src/main/java/com/codexbridge/mobile/ui/ChatScreen.kt`
- Modify: `mobile/android/app/src/main/java/com/codexbridge/mobile/MainActivity.kt`
- Test: `mobile/android/app/src/test/java/com/codexbridge/mobile/QueueStateTest.kt`

**Interfaces:**
- Produces: queue models, queue API methods, `UiState.queue`, and `send|steerQueue|editQueue|deleteQueue`.

- [ ] **Step 1: Write failing Kotlin snapshot/update/remove state tests.**
- [ ] **Step 2: Run the focused Gradle unit test; expect missing queue models/events.**
- [ ] **Step 3: Add queue models, API methods, state reduction, reconnect/resync refresh, and queue-only send.**
- [ ] **Step 4: Add non-empty queue rows with `引导`, `编辑`, and confirmed `删除`, plus the `发送后加入队列` placeholder.**
- [ ] **Step 5: Run `gradlew test assembleRelease`; expect BUILD SUCCESSFUL and an APK.**
- [ ] **Step 6: Update `mobile/README.md` and commit `feat: add synced send queue to Android app`.**

### Task 7: 文档与端到端回归

**Files:**
- Modify: `PROTOCOL.md`
- Modify: `README.md`
- Modify: `mobile/README.md`
- Modify: `tests/test_unified_protocol.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Produces: documented unified protocol and integration coverage for two clients, old clients, and ESP approval.

- [ ] **Step 1: Write failing two-client queue revision and unified phone/pending-hardware tests.**
- [ ] **Step 2: Run focused integration tests; expect wiring gaps.**
- [ ] **Step 3: Complete main-process wiring and compatibility paths.**
- [ ] **Step 4: Update README/PROTOCOL/mobile README with the unified device and queue behavior.**
- [ ] **Step 5: Run `python -m unittest discover -s tests -v` and `git diff --check`; both must be clean.**
- [ ] **Step 6: Commit `docs: document unified devices and bridge queue protocol`.**

### Task 8: 完整验证与 v1.4.7 发布

**Files:**
- Modify: `codexbridge/__init__.py`
- Create: `release-notes/v1.4.7.md`
- Build: Windows installer, portable ZIP, Android APK.

**Interfaces:**
- Produces: tested `v1.4.7` source, tag, GitHub Release, and downloadable assets.

- [ ] **Step 1: Run the full Python suite; all tests must pass.**
- [ ] **Step 2: Run `gradlew test assembleRelease`; expect BUILD SUCCESSFUL.**
- [ ] **Step 3: Run `./packaging/build_windows.ps1 -Version 1.4.7 -FirmwareRoot D:\dotii\firmware`; both Windows artifacts must exist.**
- [ ] **Step 4: Set `VERSION = "1.4.7"` and create Chinese `release-notes/v1.4.7.md`.**
- [ ] **Step 5: Run `git diff --check`, verify non-zero artifacts, and commit `release: prepare v1.4.7`.**
- [ ] **Step 6: Verify `gh auth status`, push the branch, create/push `v1.4.7`, and confirm the Release contains both Windows files and the mobile APK.**