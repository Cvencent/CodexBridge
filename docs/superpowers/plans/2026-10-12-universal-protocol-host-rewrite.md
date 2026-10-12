# CodexBridge Universal Protocol Host Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 CodexBridge 宿主从双协议实现重写成唯一设备协议，Dotii 与 AI Passport 使用同一发现、握手、能力协商和消息流程。

**Architecture:** 设备仍是 TCP 8765 服务端，PC 连接后先发送 `bridge_auth`。设备用 `bridge_auth_ok` 返回稳定身份与能力，PC 再用首个 `bridge_ready` 返回生效能力并完成上线；之后同一个 `bridge_ready` 作为心跳。UDP 8764 只发送一个 `CODEXBRIDGE_DISCOVER` 请求。

**Tech Stack:** Python 3.11+、标准库、unittest；托盘仍使用可选依赖 pystray + Pillow。

**Spec:** `docs/superpowers/specs/2026-10-12-unified-device-bridge-design.md`

**Scope:** 本计划只重构 CodexBridge PC 端宿主，使其只实现唯一协议。Dotii 固件、AI Passport 固件、管理中心迁移、Codex 采集和单 EXE 打包分别进入后续计划。

## Global Constraints

- 设备协议中不得出现 `protocol` 版本字段、V1/V2 分支、`hello`、`hello_ok` 或并行握手。
- 发现请求固定为 `CODEXBRIDGE_DISCOVER`；TCP 8765、UDP 8764 不变。
- `bridge_auth_ok` 必须包含稳定 `device_id`、`model`、`name`、`firmware`、`capabilities`。
- 首个 `bridge_ready` 必须包含 `bridge`、`version`、`capabilities`；后续 `bridge_ready` 是心跳。
- CodexBridge 管理接口使用 `/api/admin/...`，不得再使用 `/api/v1/admin/...`。
- 单行 JSON 上限 768 字节；文本按 480 字节分片；令牌至少 16 字符。
- 日志、异常和管理接口不得泄露令牌。
- 对外行为发生变化时必须先写失败测试，再改实现，再提交。

## Review Focus

- 发现应答缺少 `device_id`：必须丢弃，不得生成临时设备。
- `bridge_auth_ok.device_id` 与发现得到的设备不一致：必须拒绝上线。
- 令牌错误：设备拒绝时不得泄露令牌，也不得进入在线状态。
- 心跳：每次都必须使用同一 `bridge_ready` 结构，不能退回另一套心跳消息。
- 已上线会话断开：必须清理注册表中的会话引用，后续发现能够重新连接。

---

### Task 1: 收敛发现协议

**Files:**
- Modify: `codexbridge/devices/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces: `DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER"`
- Produces: `DiscoveryResult(host: str, device: str, device_id: str, name: str | None, port: int, capabilities: list[str])`
- Produces: `parse_reply(payload: bytes, host: str) -> DiscoveryResult | None`
- Produces: `discover(timeout: float = 3.0, targets: Iterable[str] = ("255.255.255.255",)) -> list[DiscoveryResult]`

- [ ] **Step 1: Write failing discovery tests**

Replace protocol-version tests with these assertions:

```python
def test_parse_unified_reply(self):
    result = parse_reply(
        b'{"device":"dotii","device_id":"a1","name":"Desk","port":8765,'
        b'"capabilities":["status","tasks"]}',
        "10.0.0.5",
    )
    self.assertEqual((result.device_id, result.name, result.port), ("a1", "Desk", 8765))
    self.assertEqual(result.capabilities, ["status", "tasks"])

def test_reply_without_device_id_is_ignored(self):
    self.assertIsNone(parse_reply(b'{"device":"dotii","port":8765}', "10.0.0.5"))

def test_discover_sends_only_unified_request(self):
    # Update the existing UDP responder to append each payload to `requests`.
    results = discover(timeout=1.0, targets=("127.0.0.1",))
    self.assertEqual(len(results), 1)
    self.assertEqual(requests, [DISCOVERY_REQUEST])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_discovery -v`

Expected: FAIL because `DiscoveryResult` still contains `protocol` and discovery sends two request constants.

- [ ] **Step 3: Implement the unified discovery contract**

Remove `LEGACY_DISCOVERY_REQUEST` and the `protocol` field. Require non-empty `device` and `device_id`; accept `name` only when it is a non-empty string. Send `DISCOVERY_REQUEST` once per target.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m unittest tests.test_discovery -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/discovery.py tests/test_discovery.py
git commit -m "refactor: use one discovery protocol"
```

### Task 2: 统一单设备握手

**Files:**
- Modify: `codexbridge/devices/session.py`
- Modify: `tests/fake_device.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: the device identity and capabilities from Task 1.
- Produces: `DeviceSession(host, port, token, *, device_id, on_device_info=None, on_event=None, on_online=None, on_offline=None, heartbeat_seconds=5.0, timeout_seconds=15.0)`
- Produces: `DeviceSession.run_once() -> None`
- Produces: `DeviceSession.send(message: dict) -> None`
- Produces: `FakeDevice(host="127.0.0.1", device_id="fake-device")` with no protocol parameter.

- [ ] **Step 1: Write the failing session tests**

Replace the hello/v1 tests with the unified handshake assertions:

```python
def test_unified_handshake_and_heartbeat(self):
    seen = []
    with FakeDevice() as device:
        device.expect_handshake("abcdefghijklmnop")
        session = DeviceSession(
            device.host,
            device.port,
            "abcdefghijklmnop",
            device_id="fake-device",
            on_device_info=lambda info: (seen.append(info) or {"status", "text"}),
            heartbeat_seconds=0.2,
            timeout_seconds=1.0,
        )
        session.run_once()
    self.assertEqual(device.received[0]["type"], "bridge_auth")
    self.assertEqual(device.received[1]["type"], "bridge_ready")
    self.assertEqual(device.received[1]["capabilities"], ["status", "text"])
    self.assertEqual(seen[0]["device_id"], "fake-device")

def test_identity_mismatch_is_rejected(self):
    online = []
    with FakeDevice() as device:
        device.expect_handshake("abcdefghijklmnop")
        session = DeviceSession(
            device.host,
            device.port,
            "abcdefghijklmnop",
            device_id="another-id",
            on_online=lambda: online.append(True),
            heartbeat_seconds=0.2,
            timeout_seconds=1.0,
        )
        session.run_once()
    self.assertEqual(online, [])
```

Keep the existing event callback, stalled device, split-line, and token-validation tests, but update them to use the unified handshake.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_session -v`

Expected: FAIL because `DeviceSession` still uses protocol, `hello`, `hello_ok`, and `on_hello`.

- [ ] **Step 3: Implement the unified session state machine**

In `run_once`, perform exactly:

```python
self.send({"type": "bridge_auth", "token": self.token})
reply, closed = self._next_message(
    sock,
    buffer,
    time.monotonic() + self.timeout_seconds,
)
if closed or reply is None or reply.get("type") != "bridge_auth_ok":
    return
if reply.get("device_id") != self.device_id:
    return
capabilities = set(self.on_device_info(reply)) if self.on_device_info else set()
self.send(self._ready_message(capabilities))
```

Use `_ready_message(capabilities)` for both the first ready message and every heartbeat. Remove the `protocol`, `on_hello`, and constructor `capabilities` parameters. Update `FakeDevice` so it waits for `bridge_auth`, replies with `bridge_auth_ok` containing `_device_info()`, then starts post-handshake callbacks only after receiving `bridge_ready`.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m unittest tests.test_session -v`

Expected: PASS, including identity mismatch, token mismatch, heartbeat, event callback, stall, and split-line cases.

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/session.py tests/fake_device.py tests/test_session.py
git commit -m "refactor: use one device handshake"
```

### Task 3: 对齐设备管理器

**Files:**
- Modify: `codexbridge/devices/manager.py`
- Modify: `codexbridge/__main__.py`
- Test: `tests/test_device_manager.py`

**Interfaces:**
- Consumes: `DiscoveryResult` from Task 1 and `DeviceSession` from Task 2.
- Produces: `DeviceManager._handle_device_info(device_id: str, info: dict) -> set[str]`
- Produces: `DeviceManager.remove(device_id: str) -> None`

- [ ] **Step 1: Write failing manager tests**

Update `FakeSession` to accept the unified constructor and assert the manager passes the device metadata callback:

```python
def test_discovery_starts_session_and_replays_unread(self):
    result = DiscoveryResult(
        host="127.0.0.1",
        device="dotii",
        device_id="d1",
        name="Desk",
        port=8765,
        capabilities=["status", "text", "results", "read_ack"],
    )
    self.manager.sync([result])
    self.assertTrue(FakeSession.online.wait(1.0))
    self.assertNotIn("protocol", FakeSession.created.kwargs)
    self.assertIn("on_device_info", FakeSession.created.kwargs)
    self.assertEqual(self.registry.get("d1").name, "Desk")
```

Extend the fake session so `on_device_info` is called with an info dict before `on_online`, and assert that the registry receives the returned capabilities.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_device_manager -v`

Expected: FAIL because the manager still passes `protocol`, `capabilities`, and `on_hello`.

- [ ] **Step 3: Implement the unified manager flow**

Use the discovery reply to create or update the device record. Pass only `device_id`, `on_device_info`, and the session lifecycle callbacks. Rename `_handle_hello` to `_handle_device_info`; it must update model, name, and supported capabilities from `bridge_auth_ok` before returning `hub.capabilities_for(device_id)`.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m unittest tests.test_device_manager -v`

Expected: PASS, including failed-session cleanup, retry, and remove-session cases.

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/manager.py codexbridge/__main__.py tests/test_device_manager.py
git commit -m "refactor: drive sessions from one handshake"
```

### Task 4: 去掉管理接口版本前缀

**Files:**
- Modify: `codexbridge/admin_api.py`
- Test: `tests/test_admin_api.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `GET /api/admin/status`
- Produces: `GET /api/admin/devices`
- Produces: `GET /api/admin/pairing`
- Produces: `POST /api/admin/devices/remove`
- Produces: `POST /api/admin/devices/capabilities`

- [ ] **Step 1: Write failing API tests**

Change every test request to the unversioned path and add one assertion that the old path is absent:

```python
def test_old_versioned_path_is_not_served(self):
    status, _ = handle_request(
        "GET", "/api/v1/admin/status", {}, b"", "127.0.0.1", self.hub, self.registry
    )
    self.assertEqual(status, 404)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest tests.test_admin_api -v`

Expected: FAIL because the server still registers `/api/v1/admin/...`.

- [ ] **Step 3: Implement the unversioned routes and update the README**

Replace all five route constants. Keep loopback-only access, JSON content-type validation, token stripping, and the `on_device_removed` callback unchanged. Update README examples to use `/api/admin/status`.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python -m unittest tests.test_admin_api -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codexbridge/admin_api.py tests/test_admin_api.py README.md
git commit -m "refactor: remove versioned admin routes"
```

### Task 5: 端到端验证唯一协议并清理旧计划

**Files:**
- Create: `tests/test_unified_protocol.py`
- Delete: `docs/superpowers/plans/2026-10-12-codexbridge-host-core.md`

**Interfaces:**
- Consumes: all interfaces from Tasks 1-4.
- Produces: one integration test proving discovery metadata, unified authentication, capability negotiation, replay, and already-read broadcast use the same session.

- [ ] **Step 1: Write the failing integration test**

Use the real `FakeDevice`, `DeviceSession`, `DeviceManager`, and `BridgeHub`:

```python
def test_unified_device_receives_replay_and_read_state(self):
    registry = DeviceRegistry(Path(temp_dir) / "devices.json")
    registry.upsert(
        "fake-device",
        model="dotii",
        token="abcdefghijklmnop",
        supported=["status", "text", "read_ack"],
    )
    hub = BridgeHub(registry)
    hub.publish(MessageRecord("m1", "t1", "status", "working", "t", "b", 0.0))
    with FakeDevice() as device:
        device.expect_handshake("abcdefghijklmnop")
        manager = DeviceManager(registry, hub)
        manager.sync([DiscoveryResult(
            host=device.host,
            device="dotii",
            device_id="fake-device",
            name="Fake Device",
            port=device.port,
            capabilities=["status", "text", "read_ack"],
        )])
        self.assertTrue(wait_until(lambda: any(
            item.get("type") == "message" and item.get("message_id") == "m1"
            for item in device.received
        )))
        manager.stop()
    self.assertFalse(any(item.get("type") in {"hello", "hello_ok"} for item in device.received))
```

Add this second test for the cross-device read loop:

```python
def test_read_ack_broadcasts_to_other_unified_session(self):
    with FakeDevice(device_id="device-a") as first, FakeDevice(device_id="device-b") as second:
        for device in (first, second):
            device.expect_handshake("abcdefghijklmnop")
            self.registry.upsert(
                device.device_id,
                model="dotii",
                token="abcdefghijklmnop",
                supported=["status", "text", "read_ack"],
            )
            self.manager.sync([self._discovery_for(device)])
        self.assertTrue(self._wait_for(lambda: "m1" in self._received_ids(first)))
        self.assertTrue(self._wait_for(lambda: "m1" in self._received_ids(second)))
        first.send_event({"type": "read_ack", "message_id": "m1"})
        self.assertTrue(self._wait_for(lambda: any(
            item.get("type") == "read_state"
            and "device-a" in item.get("read_by", [])
            for item in second.received
        )))
```

`_discovery_for(device)` returns a `DiscoveryResult` with that device's host, ID, name, port, and capabilities. `_wait_for` polls up to one second. Publish the message once in `setUp`, not once per device.

- [ ] **Step 2: Run the test and verify it fails**

Run: `python -m unittest tests.test_unified_protocol -v`

Expected: FAIL until the session and manager use the unified handshake.

- [ ] **Step 3: Make the integration test pass without adding a compatibility layer**

Fix only the production interfaces used by the test. Do not add `protocol`, `hello`, `hello_ok`, or a second handshake branch.

- [ ] **Step 4: Delete the superseded implementation plan**

```bash
git rm docs/superpowers/plans/2026-10-12-codexbridge-host-core.md
```

- [ ] **Step 5: Run all tests and protocol residue checks**

Run: `python -m unittest discover -s tests -v`

Expected: ALL PASS.

Run:

```powershell
rg -n -i "CODEXBRIDGE_DISCOVER_V2|FOLOOS_DISCOVER_V1|hello_ok|protocol\s*=|/api/v1/admin" codexbridge tests README.md
```

Expected: no matches.

- [ ] **Step 6: Run the host smoke test**

Run: `python -m codexbridge --no-tray`

Expected: the process starts and `GET http://127.0.0.1:8788/api/admin/status` returns `{"ok":true,"version":"0.1.0","device_count":0}`.

- [ ] **Step 7: Commit**

```bash
git add tests/test_unified_protocol.py
git rm docs/superpowers/plans/2026-10-12-codexbridge-host-core.md
git commit -m "test: verify the universal device protocol end to end"
```
