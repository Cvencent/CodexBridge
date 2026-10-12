# Dotii Unified Bridge Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Dotii 固件通过 CodexBridge 的唯一设备协议获取 Codex 状态、任务和结果，同时保留管理中心的其他能力，最终只发布一个 CodexBridge.exe 和一个托盘。

**Architecture:** CodexBridge 继续作为唯一常驻宿主，新增 Codex app-server 采集并把结果归一为带可选结构化 `data` 的通用 `message`。Dotii 固件保留 HTTP 管理中心轮询作为 Bambu、自定义页和显示配置的数据源，同时新增 UDP 发现、TCP 8765 统一握手和消息客户端；两路数据在设备内合并。管理中心的 PC 代码迁入 CodexBridge，管理进程只作为无托盘子进程运行。

**Tech Stack:** Python 3.11+、标准库、unittest、ESP-IDF 5.x、cJSON、FreeRTOS sockets、LVGL 9、PyInstaller。

**Spec:** `docs/superpowers/specs/2026-10-12-unified-device-bridge-design.md`

## Global Constraints

- 设备协议中不得出现 `protocol` 版本字段、`hello`、`hello_ok`、双握手或按代际分支的兼容路径。
- 发现请求固定为 `CODEXBRIDGE_DISCOVER`；TCP 8765、UDP 8764 不变。
- 握手顺序固定为 `bridge_auth` -> `bridge_auth_ok` -> `bridge_ready`；后续 `bridge_ready` 只作为心跳。
- Dotii 稳定 ID 固定为 `dotii-<Wi-Fi STA MAC 的 12 位小写十六进制>`。
- Dotii 支持能力固定为 `status`、`text`、`tasks`、`results`、`read_ack`；默认启用同样这五项。
- 单行 JSON 上限 768 字节；`message.body` 每片最多 480 字节；设备拼接上限 4 KB。
- 每台设备令牌不少于 16 字符；日志、异常、网页响应和测试失败信息不得输出令牌。
- CodexBridge 管理接口只接受回环地址，继续使用 `/api/admin/...`。
- Dotii 的 Bambu、自定义页、显示设置、BLE 配网和烧录继续走管理中心，Codex 数据只从 CodexBridge 获取。
- Dotii 详情页停留满 2000 ms 才发送一次 `read_ack`；翻页或换任务后重新计时。
- 同一个发布包只有一个托盘和一个 EXE：`CodexBridge.exe`；管理中心不得重新创建托盘。

## Review Focus

- 配网时 CodexBridge 尚未启动：设备必须继续保留管理中心信息并明确提示 Codex 配对未完成，不能写入半套配置或崩溃。
- BLE Status 缺少 `device_id`：管理中心必须拒绝领取 CodexBridge 令牌，不能生成临时 ID。
- 令牌领取成功但 BLE 配置失败：设备下一次配置仍使用旧管理中心令牌授权，不能锁死或泄露新令牌。
- CodexBridge 断开后管理中心仍在线：Dotii 必须继续显示 Bambu、自定义页和显示配置，Codex 区域单独标为离线。
- 同一消息正文跨多个 768 字节协议行：设备必须按 `message_id`、`seq`、`total` 重组，并在超过 4 KB 时保留尾部截断提示。
- 两台设备同时在线：任一设备发送 `read_ack` 后，其他设备必须收到同一 `message_id` 的 `read_state`。
- 长时间未连接：PC 每 5 秒发送同一结构的 `bridge_ready`，设备 15 秒未收到任何 PC 行时静默心跳被误判离线。
- 管理进程异常退出：托盘宿主必须重启或关闭它，不能留下无托盘孤儿进程；宿主退出时必须结束管理进程。
- 旧 `%LOCALAPPDATA%\StateDisplay` 配置存在：迁移后 Bambu、自定义页、显示和配网数据必须继续可读，不能要求用户重配。

---

### Task 1: 稳定设备身份与 CodexBridge 配对令牌

**Files:**
- Create: `bridge/codex_bridge_link.py` in `D:/dotii`
- Create: `bridge/tests/test_codex_bridge_link.py` in `D:/dotii`
- Modify: `bridge/bluetooth_bridge.py` in `D:/dotii`
- Modify: `bridge/tests/test_bluetooth_bridge.py` in `D:/dotii`
- Modify: `bridge/codex_bridge.py` in `D:/dotii`
- Create: `main/device_identity.h` in `D:/dotii`
- Create: `main/device_identity.c` in `D:/dotii`
- Modify: `main/device_config.h`, `main/device_config.c` in `D:/dotii`
- Modify: `main/ble_bridge.c`, `main/main.c`, `main/CMakeLists.txt` in `D:/dotii`
- Test: `bridge/tests/test_codex_bridge_link.py`

**Interfaces:**
- Produces: `PairingToken(device_id: str, token: str, port: int)` with `token` excluded from `repr`.
- Produces: `claim_codex_bridge_token(device_id: str, base_url: str = "http://127.0.0.1:8788", timeout: float = 2.0) -> PairingToken`.
- Produces: `_configuration_payload(*, ssid: Any, password: Any, bridge_url: str, bridge_token: str, codex_bridge_token: str, current_token: Any = "") -> bytes`.
- Produces: `BluetoothBridge.start_configure(*, address, ssid, password, bridge_url, bridge_token, codex_bridge_token, current_token="") -> bool`.
- Produces: `device_identity_init() -> esp_err_t` and `device_identity_get() -> const char *`.
- Consumes: CodexBridge `GET /api/admin/pairing?device_id=<id>` returning `ok`, `device_id`, `token`, `port`.

- [ ] **Step 1: Write the failing pairing tests**

Create `bridge/tests/test_codex_bridge_link.py` with these behaviors:

```python
def test_claim_uses_loopback_admin_api_and_hides_token_from_repr(self):
    with mock.patch("codex_bridge_link.urllib.request.urlopen") as urlopen:
        urlopen.return_value = FakeResponse({
            "ok": True,
            "device_id": "dotii-001122334455",
            "token": "0123456789abcdef0123456789abcdef",
            "port": 8765,
        })
        result = claim_codex_bridge_token("dotii-001122334455")

    self.assertEqual(result.port, 8765)
    self.assertNotIn("0123456789abcdef0123456789abcdef", repr(result))
    self.assertEqual(
        urlopen.call_args.args[0].full_url,
        "http://127.0.0.1:8788/api/admin/pairing?device_id=dotii-001122334455",
    )

def test_claim_rejects_missing_device_id(self):
    with self.assertRaisesRegex(ValueError, "device_id"):
        claim_codex_bridge_token("dotii-")

def test_claim_rejects_short_token(self):
    with mock.patch("codex_bridge_link.urllib.request.urlopen") as urlopen:
        urlopen.return_value = FakeResponse({
            "ok": True,
            "device_id": "dotii-001122334455",
            "token": "short",
            "port": 8765,
        })
        with self.assertRaisesRegex(ValueError, "令牌"):
            claim_codex_bridge_token("dotii-001122334455")
```

Extend `bridge/tests/test_bluetooth_bridge.py`:

```python
def test_configuration_includes_codex_bridge_token(self):
    body = _configuration_payload(
        ssid="Studio",
        password="secret",
        bridge_url="http://host/api/v1/snapshot",
        bridge_token="management-token-1234",
        codex_bridge_token="codex-token-1234567890",
    )
    payload = json.loads(body)
    self.assertEqual(payload["codex_bridge_token"], "codex-token-1234567890")

def test_configuration_rejects_short_codex_bridge_token(self):
    with self.assertRaisesRegex(ValueError, "Codex Bridge 令牌"):
        _configuration_payload(
            ssid="Studio",
            password="",
            bridge_url="http://host/api/v1/snapshot",
            bridge_token="management-token-1234",
            codex_bridge_token="short",
        )
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `python -m unittest bridge.tests.test_codex_bridge_link bridge.tests.test_bluetooth_bridge -v`

Expected: FAIL because `codex_bridge_link` does not exist and `_configuration_payload` does not accept `codex_bridge_token`.

- [ ] **Step 3: Implement the loopback pairing client and BLE payload fields**

In `bridge/codex_bridge_link.py`, validate the stable ID with `^dotii-[0-9a-f]{12}$`, use `urllib.request` with a `GET` request, require the response device ID to equal the requested ID, require `16 <= len(token) <= 64`, and require `port == 8765`. Raise user-facing `ValueError` messages without embedding the token.

Add `codex_bridge_token` to the BLE JSON payload with key `codex_bridge_token`; validate it independently with the same 16-64 character bound.

- [ ] **Step 4: Run the Python pairing tests and verify they pass**

Run: `python -m unittest bridge.tests.test_codex_bridge_link bridge.tests.test_bluetooth_bridge -v`

Expected: PASS.

- [ ] **Step 5: Write the failing pairing-route tests**

Add tests proving the management configure route stores `device_status["device_id"]`, refuses configuration when it is absent, calls `claim_codex_bridge_token(device_id)`, and passes both the unchanged management token and the returned Codex Bridge token to `start_configure`.

Run: `python -m unittest bridge.tests.test_bluetooth_bridge -v`

Expected: FAIL because the configure route does not claim a Codex Bridge token.

- [ ] **Step 6: Implement firmware identity and token storage**

Add `device_identity_init` using `esp_read_mac(mac, ESP_MAC_WIFI_STA)` and `snprintf` to produce `dotii-%02x%02x%02x%02x%02x%02x`. Call it in `app_main` before `device_config_init`.

Add `char codex_bridge_token[65]` to `device_config_values_t`. Persist it under NVS key `codex_tok`, load it into `s_config`, and require it during BLE configure with `strlen(next.codex_bridge_token) >= 16`.

Change BLE Status JSON to include the stable ID:

```c
"{\"v\":1,\"device_id\":\"%s\",\"name\":\"Dotii\",\"firmware\":\"%s\","
"\"provisioned\":%s,\"wifi\":%s,\"bridge\":\"%s\"}"
```

- [ ] **Step 7: Run the complete Python suite**

Run: `python -m unittest discover -s bridge/tests -v`

Expected: PASS. The pre-existing macOS Bluetooth dependency-readiness test may fail on this machine when `bluetooth-deps` is absent; record that environment-only failure verbatim if it remains.

- [ ] **Step 8: Compile the firmware**

Run: `idf.py build`

Expected: BUILD SUCCESSFUL. If ESP-IDF is unavailable on the host, record `idf.py not found` as an unverified gate; do not claim the firmware compiled.

- [ ] **Step 9: Commit**

```bash
git add bridge/codex_bridge_link.py bridge/tests/test_codex_bridge_link.py
git add bridge/bluetooth_bridge.py bridge/tests/test_bluetooth_bridge.py bridge/codex_bridge.py
git add main/device_identity.h main/device_identity.c
git add main/device_config.h main/device_config.c main/ble_bridge.c main/main.c main/CMakeLists.txt
git commit -m "feat: add stable Dotii identity and bridge pairing"
```


### Task 2: CodexBridge Codex 采集与结构化消息

**Files:**
- Create: `codexbridge/codex/__init__.py`
- Create: `codexbridge/codex/app_server.py`
- Create: `codexbridge/codex/collector.py`
- Create: `tests/test_codex_collector.py`
- Modify: `codexbridge/messages_store.py`
- Modify: `codexbridge/hub.py`
- Modify: `codexbridge/__main__.py`
- Modify: `codexbridge/admin_api.py`
- Modify: `tests/test_messages_store.py`
- Modify: `tests/test_hub.py`
- Modify: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: the app-server implementation and `thread_snapshot` behavior currently in `D:/dotii/bridge/codex_app_server.py`.
- Produces: `MessageRecord(..., data: dict[str, Any] | None = None)`.
- Produces: `records_from_snapshot(snapshot: dict[str, Any], now: float | None = None) -> list[MessageRecord]`.
- Produces: `run_codex_collector(runtime_folder: Path, cwd: Path, interval: float, stop: threading.Event, publish: Callable[[list[MessageRecord]], None], codex_command: str | None = None, report: Callable[[str, str], None] | None = None, restart: threading.Event | None = None, enabled: Callable[[], bool] | None = None, source_factory: Callable[[], Any] | None = None) -> None`.
- Produces: `GET /api/admin/codex-status` with `enabled`, `collector_state`, `collector_detail`, and `last_snapshot_epoch`.

- [ ] **Step 1: Write failing message-data tests**

Add to `tests/test_messages_store.py`:

```python
def test_message_data_survives_replacement_and_payload(self):
    store = MessageStore()
    store.add(MessageRecord(
        "m1", "t1", "status", "working", "标题", "正文", 1.0,
        data={"codex": {"task": {"status": "working"}}},
    ))
    self.assertEqual(
        store.get("m1").data["codex"]["task"]["status"],
        "working",
    )
```

Add to `tests/test_hub.py`:

```python
def test_structured_data_is_sent_only_to_capable_devices(self):
    record = MessageRecord(
        "m-data", "t1", "status", "working", "标题", "正文", 1.0,
        data={"codex": {"task": {"status": "working"}}},
    )
    hub.publish(record)
    self.assertEqual(capable.fake_session.sent[-1]["data"], record.data)
    self.assertNotIn("data", incapable.fake_session.sent[-1])
```

- [ ] **Step 2: Run the message tests and verify they fail**

Run: `python -m unittest tests.test_messages_store tests.test_hub -v`

Expected: FAIL because `MessageRecord` has no `data` field and hub payloads omit it.

- [ ] **Step 3: Add optional structured data to the unified message**

Add `data` to `MessageRecord`, preserve it when replacing a message with the same ID, and include it in `_message_payload` only when it is a dictionary. `wire_messages` may split the body, but every chunk must carry the same `data` object.

Run: `python -m unittest tests.test_messages_store tests.test_hub tests.test_protocol_messages -v`

Expected: PASS.

- [ ] **Step 4: Write failing snapshot-to-record tests**

Create `tests/test_codex_collector.py` with a representative snapshot containing account usage, one active task, and one completed task. Assert:

```python
def test_snapshot_records_separate_status_tasks_and_results(self):
    records = records_from_snapshot(SNAPSHOT, now=1000.0)
    by_kind = {record.kind: record for record in records}

    self.assertEqual(by_kind["status"].status, "working")
    self.assertEqual(by_kind["status"].data["account"]["weekly_remaining_percent"], 68)
    self.assertNotIn("last_assistant_message", by_kind["status"].data["task"])
    self.assertEqual(by_kind["task"].data["tasks"][0]["thread_id"], "active-thread")
    self.assertEqual(by_kind["result"].data["task"]["thread_id"], "done-thread")

def test_record_ids_are_stable_for_the_same_logical_state(self):
    first = {record.message_id for record in records_from_snapshot(SNAPSHOT, now=1.0)}
    second = {record.message_id for record in records_from_snapshot(SNAPSHOT, now=2.0)}
    self.assertEqual(first, second)
```

The expected IDs are:

```text
status:<primary-thread-id>:<status>
task:<primary-thread-id>
result:<completed-thread-id>:<completed-at-epoch>
```

- [ ] **Step 5: Run the collector tests and verify they fail**

Run: `python -m unittest tests.test_codex_collector -v`

Expected: FAIL because `codexbridge.codex.collector` does not exist.

- [ ] **Step 6: Move app-server code and map snapshots**

Copy the Dotii app-server implementation into `codexbridge/codex/app_server.py`, replacing `schema_version` output with an internal dictionary consumed only by `records_from_snapshot`; do not expose the old snapshot as a second device protocol.

`records_from_snapshot` produces:

- One `status` record whose `data.account` contains quota fields and whose `data.task` contains status metadata without assistant result text.
- One `task` record whose `data.tasks` contains the ordered visible task list.
- One `result` record for the newest completed or failed task, including `last_assistant_message` and `conversation_text`.

Unknown result fields are ignored. All body text is passed to `wire_messages` unmodified so it can be chunked safely.

Run: `python -m unittest tests.test_codex_collector -v`

Expected: PASS.

- [ ] **Step 7: Write the failing collector lifecycle test**

Add:

```python
def test_collector_publishes_records_and_reports_status(self):
    published: list[MessageRecord] = []
    reports: list[tuple[str, str]] = []
    run_codex_collector(
        runtime_folder=Path(temp_dir),
        cwd=Path(temp_dir),
        interval=0.01,
        stop=self.stop,
        publish=published.extend,
        source_factory=lambda: FakeSource(SNAPSHOT),
        report=lambda state, detail: reports.append((state, detail)),
    )
    self.assertEqual({item.kind for item in published}, {"status", "task", "result"})
    self.assertEqual(reports[0], ("online", "Codex App Server 已连接"))
```

- [ ] **Step 8: Run the lifecycle test and verify it fails**

Run: `python -m unittest tests.test_codex_collector.CodexCollectorTest.test_collector_publishes_records_and_reports_status -v`

Expected: FAIL because `run_codex_collector` does not accept `source_factory`.

- [ ] **Step 9: Implement the collector lifecycle and host wiring**

Run the collector in a daemon thread from `codexbridge/__main__.py`. Resolve the working directory from `CODEXBRIDGE_CODEX_CWD` first, then `%USERPROFILE%`, and the CLI from `CODEXBRIDGE_CODEX_COMMAND` first, then normal PATH resolution.

The host publishes `records_from_snapshot(snapshot)` through `BridgeHub.publish`. Expose `/api/admin/codex-status`; never expose `codex_command`, working-directory tokens, or raw app-server errors containing credentials.

Run: `python -m unittest tests.test_codex_collector tests.test_admin_api -v`

Expected: PASS.

- [ ] **Step 10: Run the full CodexBridge suite**

Run: `python -m unittest discover -s tests -v`

Expected: ALL PASS.

- [ ] **Step 11: Commit**

```bash
git add codexbridge/codex codexbridge/messages_store.py codexbridge/hub.py
git add codexbridge/__main__.py codexbridge/admin_api.py
git add tests/test_codex_collector.py tests/test_messages_store.py tests/test_hub.py tests/test_admin_api.py
git commit -m "feat: publish Codex activity through the universal message hub"
```
### Task 3: Dotii 固件统一协议客户端

**Files:**
- Create: `main/codex_bridge_protocol.h`
- Create: `main/codex_bridge_protocol.c`
- Create: `main/codex_bridge_client.h`
- Create: `main/codex_bridge_client.c`
- Create: `test_apps/codex_bridge_protocol/CMakeLists.txt`
- Create: `test_apps/codex_bridge_protocol/main/CMakeLists.txt`
- Create: `test_apps/codex_bridge_protocol/main/test_codex_bridge_protocol.c`
- Modify: `main/CMakeLists.txt`
- Modify: `main/main.c`
- Test: `test_apps/codex_bridge_protocol/main/test_codex_bridge_protocol.c`

**Interfaces:**
- Produces: `codex_bridge_message_t` with `type[24]`, `message_id[80]`, `thread_id[80]`, `kind[16]`, `status[24]`, `title[161]`, `char *body`, `int seq`, `int total`, `bool ignored`, and `cJSON *data`.
- Produces: `codex_bridge_protocol_feed(const char *line, size_t length, codex_bridge_message_t *message) -> esp_err_t`.
- Produces: `codex_bridge_protocol_reset_message(codex_bridge_message_t *message) -> void`.
- Produces: `codex_bridge_protocol_has_capability(const codex_bridge_message_t *message, const char *capability) -> bool`.
- Produces: `codex_bridge_protocol_body(const codex_bridge_message_t *message, const char *message_id) -> const char *`.
- Produces: `codex_bridge_protocol_body_length(const codex_bridge_message_t *message) -> size_t`.
- Produces: `codex_bridge_message_handler_t` taking a parsed message and user context.
- Produces: `codex_bridge_client_start(codex_bridge_message_handler_t handler, void *context) -> esp_err_t`.
- Produces: `codex_bridge_client_stop() -> void`.
- Produces: `codex_bridge_client_is_online() -> bool`.
- Produces: `codex_bridge_client_send_read_ack(const char *message_id) -> esp_err_t`.

- [ ] **Step 1: Write the failing protocol tests**

Create the ESP-IDF Unity test app with these tests:

```c
TEST_CASE("bridge_ready exposes only granted capabilities", "[codex_bridge]")
{
    codex_bridge_message_t message = {0};
    const char *line = "{\"type\":\"bridge_ready\",\"bridge\":\"CodexBridge\",\"version\":\"0.1.0\",\"capabilities\":[\"status\",\"tasks\",\"read_ack\"]}\n";
    TEST_ESP_OK(codex_bridge_protocol_feed(line, strlen(line), &message));
    TEST_ASSERT_EQUAL_STRING("bridge_ready", message.type);
    TEST_ASSERT_TRUE(codex_bridge_protocol_has_capability(&message, "tasks"));
    TEST_ASSERT_FALSE(codex_bridge_protocol_has_capability(&message, "voice"));
    codex_bridge_protocol_reset_message(&message);
}

TEST_CASE("unknown message type is ignored without closing", "[codex_bridge]")
{
    codex_bridge_message_t message = {0};
    const char *line = "{\"type\":\"future_optional_event\",\"value\":1}\n";
    TEST_ESP_OK(codex_bridge_protocol_feed(line, strlen(line), &message));
    TEST_ASSERT_TRUE(message.ignored);
    codex_bridge_protocol_reset_message(&message);
}

TEST_CASE("message chunks reassemble in sequence order", "[codex_bridge]")
{
    codex_bridge_message_t message = {0};
    const char *second =
        "{\"type\":\"message\",\"message_id\":\"m1\",\"thread_id\":\"t1\","
        "\"kind\":\"result\",\"status\":\"completed\",\"title\":\"done\","
        "\"body\":\"second\",\"seq\":1,\"total\":2}\n";
    const char *first =
        "{\"type\":\"message\",\"message_id\":\"m1\",\"thread_id\":\"t1\","
        "\"kind\":\"result\",\"status\":\"completed\",\"title\":\"done\","
        "\"body\":\"first\",\"seq\":0,\"total\":2}\n";
    codex_bridge_protocol_feed(
        second,
        strlen(second),
        &message
    );
    TEST_ASSERT_EQUAL_STRING("second", message.body);
    codex_bridge_protocol_feed(
        first,
        strlen(first),
        &message
    );
    TEST_ASSERT_EQUAL_STRING("firstsecond", codex_bridge_protocol_body(&message, "m1"));
    TEST_ASSERT_EQUAL_INT(2, codex_bridge_protocol_body_length(&message));
    codex_bridge_protocol_reset_message(&message);
}
```

The test app also asserts that a line longer than 768 bytes is rejected without allocation.

- [ ] **Step 2: Run the test app and verify it fails**

Run: `idf.py -C test_apps/codex_bridge_protocol build`

Expected: FAIL because `codex_bridge_protocol.c/.h` do not exist.

- [ ] **Step 3: Implement the pure protocol parser**

Keep socket code out of `codex_bridge_protocol.c`. It owns:

- A 768-byte input-line limit.
- cJSON parsing and unknown-type handling.
- `bridge_ready.capabilities` normalization against the Dotii supported set.
- Message chunk buffering keyed by one current `message_id`.
- A 4096-byte body buffer with UTF-8-safe tail truncation and suffix `\n...[已截断]`.
- Reset of cJSON objects and allocated body buffers.

Run: `idf.py -C test_apps/codex_bridge_protocol build`

Expected: BUILD SUCCESSFUL.

- [ ] **Step 4: Run the on-device protocol tests**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_bridge]` tests PASS.

- [ ] **Step 5: Write the failing socket-client test**

Add an ESP-IDF integration test using a local TCP fixture task:

```c
TEST_CASE("client performs the universal handshake once", "[codex_bridge]")
{
    // Fixture accepts one TCP connection, reads bridge_auth, sends
    // bridge_auth_ok, then records the single bridge_ready received.
    TEST_ASSERT_EQUAL_STRING("bridge_auth", fixture_received[0].type);
    TEST_ASSERT_EQUAL_STRING("bridge_ready", fixture_received[1].type);
    TEST_ASSERT_FALSE(fixture_received[1].has_protocol_field);
}
```

The fixture also sends a discover datagram and asserts the response contains the stable ID, `port:8765`, and the five Dotii capabilities.

- [ ] **Step 6: Run the socket test and verify it fails**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: FAIL because `codex_bridge_client_start` is not implemented.

- [ ] **Step 7: Implement UDP discovery and TCP server**

`codex_bridge_client.c` starts two FreeRTOS tasks after Wi-Fi has an IP:

- UDP task binds 8764, accepts only the exact `CODEXBRIDGE_DISCOVER` payload, and replies with `device`, `device_id`, `name`, `port`, and capabilities.
- TCP task binds 8765 and accepts one active PC session. It validates `bridge_auth.token` against `codex_bridge_token`, sends `bridge_auth_ok`, waits up to 15 seconds for the first `bridge_ready`, stores granted capabilities, and then feeds every complete line to `codex_bridge_protocol_feed`.

Disconnect, malformed JSON, unknown message, or 15 seconds without a PC line closes only the active TCP socket and returns to accept. A new connection replaces the old session. `codex_bridge_client_is_online` becomes true only after the first valid `bridge_ready`.

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_bridge]` tests PASS.

- [ ] **Step 8: Start the client from `app_main`**

Call `codex_bridge_client_start(NULL, NULL)` after `connectivity_start()` so Wi-Fi and netif are initialized first. Record a startup error but do not abort the UI or Bluetooth setup if the client cannot start.

- [ ] **Step 9: Run the complete verification gate**

Run: `idf.py build`

Expected: BUILD SUCCESSFUL.

Run after flashing: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_bridge]` tests PASS.

- [ ] **Step 10: Commit**

```bash
git add main/codex_bridge_protocol.h main/codex_bridge_protocol.c
git add main/codex_bridge_client.h main/codex_bridge_client.c
git add main/CMakeLists.txt main/main.c
git add test_apps/codex_bridge_protocol
git commit -m "feat: run the unified device protocol on Dotii"
```

### Task 4: Dotii 双数据源合并

**Files:**
- Create: `main/codex_snapshot_merge.h`
- Create: `main/codex_snapshot_merge.c`
- Create: `test_apps/codex_bridge_protocol/main/test_codex_snapshot_merge.c`
- Modify: `main/connectivity.h`
- Modify: `main/connectivity.c`
- Modify: `main/codex_bridge_client.c`
- Modify: `main/main.c`
- Modify: `main/CMakeLists.txt`

**Interfaces:**
- Consumes: `codex_bridge_message_t` from Task 3 and `data.account`, `data.task`, `data.tasks` from the CodexBridge collector.
- Modifies: `codex_task_detail_t` with `char message_id[80]` and `uint32_t read_count`.
- Modifies: `codex_snapshot_t` with `bool codex_source_online`, `bool management_source_online`, `char message_id[80]`, and `uint32_t read_count`.
- Produces: `void connectivity_apply_codex_bridge_message(const codex_bridge_message_t *message)`.
- Produces: `void codex_snapshot_merge_status(codex_snapshot_t *target, const cJSON *data)`.
- Produces: `void codex_snapshot_merge_tasks(codex_snapshot_t *target, const cJSON *data, codex_task_detail_t *tasks, size_t *count, size_t capacity)`.
- Produces: `void codex_snapshot_merge_result(codex_snapshot_t *target, const cJSON *data)`.
- Produces: `void codex_snapshot_apply_read_state(codex_snapshot_t *target, codex_task_detail_t *tasks, size_t count, const cJSON *data)`.
- Produces: `void codex_snapshot_apply_source_state(codex_snapshot_t *target, bool codex_online, bool management_online)`.

- [ ] **Step 1: Write the failing merge tests**

Add tests to the ESP-IDF test app:

```c
TEST_CASE("status merges quota fields without erasing Bambu data", "[codex_merge]")
{
    codex_snapshot_t snapshot = {0};
    snapshot.bambu_configured = true;
    snapshot.bambu_status = BAMBU_STATUS_PRINTING;
    cJSON *data = cJSON_Parse(
        "{\"account\":{\"weekly_remaining_percent\":68},"
        "\"task\":{\"status\":\"working\",\"title\":\"Build\"}}"
    );
    codex_snapshot_merge_status(&snapshot, data);
    TEST_ASSERT_EQUAL_INT(68, snapshot.weekly_remaining_percent);
    TEST_ASSERT_EQUAL_INT(CODEX_STATUS_WORKING, snapshot.status);
    TEST_ASSERT_TRUE(snapshot.bambu_configured);
    TEST_ASSERT_EQUAL_INT(BAMBU_STATUS_PRINTING, snapshot.bambu_status);
    cJSON_Delete(data);
}

TEST_CASE("result merge fills final response and task identity", "[codex_merge]")
{
    codex_snapshot_t snapshot = {0};
    cJSON *data = cJSON_Parse(
        "{\"task\":{\"thread_id\":\"done-thread\",\"status\":\"completed\","
        "\"title\":\"Done\",\"last_assistant_message\":\"finished\","
        "\"conversation_text\":\"final answer\"}}"
    );
    codex_snapshot_merge_result(&snapshot, data);
    TEST_ASSERT_EQUAL_STRING("done-thread", snapshot.thread_id);
    TEST_ASSERT_EQUAL_STRING("final answer", snapshot.conversation_text);
    TEST_ASSERT_EQUAL_INT(CODEX_STATUS_COMPLETED, snapshot.status);
    cJSON_Delete(data);
}
```

The merge test also proves an unavailable Codex source never clears Bambu, custom, or display fields.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: FAIL because `codex_snapshot_merge` does not exist.

- [ ] **Step 3: Implement pure snapshot merging**

The merge functions copy only fields owned by the corresponding capability:

- `status`: account quotas, primary task status/title/current action, and generated timestamp.
- `tasks`: up to six ordered task details.
- `result`: final status, title, final reply, conversation text, and message identity.

They never clear Bambu, custom, display, or module fields. Missing fields retain their previous values; invalid values are ignored.

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_merge]` tests PASS.

- [ ] **Step 4: Write the failing dual-source liveness test**

Add:

```c
TEST_CASE("Codex offline does not clear the management snapshot", "[codex_merge]")
{
    codex_snapshot_t snapshot = {0};
    snapshot.valid = true;
    snapshot.bambu_connected = true;
    snapshot.custom_enabled = true;
    codex_snapshot_apply_source_state(&snapshot, false, true);
    TEST_ASSERT_FALSE(snapshot.codex_source_online);
    TEST_ASSERT_TRUE(snapshot.management_source_online);
    TEST_ASSERT_TRUE(snapshot.bambu_connected);
    TEST_ASSERT_TRUE(snapshot.custom_enabled);
    TEST_ASSERT_EQUAL_INT(CODEX_STATUS_OFFLINE, snapshot.status);
}

TEST_CASE("management offline keeps the last Codex result", "[codex_merge]")
{
    codex_snapshot_t snapshot = {0};
    snapshot.valid = true;
    snapshot.status = CODEX_STATUS_COMPLETED;
    codex_snapshot_apply_source_state(&snapshot, true, false);
    TEST_ASSERT_EQUAL_INT(CODEX_STATUS_COMPLETED, snapshot.status);
}

TEST_CASE("read state updates every matching task detail", "[codex_merge]")
{
    codex_snapshot_t snapshot = {0};
    codex_task_detail_t tasks[1] = {0};
    strlcpy(tasks[0].message_id, "task:thread-1", sizeof(tasks[0].message_id));
    strlcpy(snapshot.message_id, "task:thread-1", sizeof(snapshot.message_id));
    cJSON *data = cJSON_Parse(
        "{\"message_id\":\"task:thread-1\",\"read_by\":[\"dotii-a\",\"dotii-b\"]}"
    );
    codex_snapshot_apply_read_state(&snapshot, tasks, 1, data);
    TEST_ASSERT_EQUAL_UINT(2, tasks[0].read_count);
    TEST_ASSERT_EQUAL_UINT(2, snapshot.read_count);
    cJSON_Delete(data);
}
```

- [ ] **Step 5: Run the dual-source tests and verify they fail**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: FAIL because source-state merging is missing.

- [ ] **Step 6: Implement source state and reconnect behavior**

Maintain separate flags for CodexBridge TCP and management HTTP. `codex_snapshot_apply_source_state` stores both flags. Only a stale Codex source changes the Codex status to `offline`; it does not clear Bambu, custom, or display values. Only an unavailable management source leaves the last Bambu/custom/display values in place and marks management as offline in its own settings text.

After Wi-Fi returns, restart both paths without re-running provisioning. After BLE configuration restarts the board, the same stable ID is used and the existing CodexBridge token is read from NVS.

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_merge]` tests PASS.

- [ ] **Step 7: Wire the real handler and publish merged snapshots**

Pass `connectivity_apply_codex_bridge_message` to `codex_bridge_client_start`. The handler calls the pure merge functions, copies the message body into PSRAM, publishes the merged snapshot through `app_state_publish`, and updates the small task ring through `app_state_tasks_publish`.

Remove the management snapshot's `codex` requirement so Bambu/custom/display can still publish before CodexBridge is online. During the migration, if the management response still contains a `codex` object, ignore it; CodexBridge is the only Codex source.

- [ ] **Step 8: Run firmware tests and build**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_merge]` and `[codex_bridge]` tests PASS.

Run: `idf.py build`

Expected: BUILD SUCCESSFUL.

- [ ] **Step 9: Commit**

```bash
git add main/codex_snapshot_merge.h main/codex_snapshot_merge.c
git add main/connectivity.h main/connectivity.c main/codex_bridge_client.c
git add main/main.c main/CMakeLists.txt
git add test_apps/codex_bridge_protocol/main/test_codex_snapshot_merge.c
git commit -m "feat: merge CodexBridge and management data on Dotii"
```


### Task 5: Dotii 详情页 2 秒已读与多设备同步

**Files:**
- Create: `main/codex_read_tracker.h`
- Create: `main/codex_read_tracker.c`
- Create: `test_apps/codex_bridge_protocol/main/test_codex_read_tracker.c`
- Modify: `main/state_ui.c`
- Modify: `main/CMakeLists.txt`

**Interfaces:**
- Consumes: `codex_bridge_client_send_read_ack(const char *message_id) -> esp_err_t`.
- Consumes: `codex_task_detail_t.message_id` and `.read_count` from Task 4.
- Produces: `void codex_read_tracker_visit(const char *message_id, uint32_t now_ms)`.
- Produces: `bool codex_read_tracker_take_due(const char *message_id, uint32_t now_ms)`.
- Produces: `void codex_read_tracker_reset(void)`.

- [ ] **Step 1: Write the failing read-tracker tests**

Add an ESP-IDF Unity test app source with:

```c
TEST_CASE("read tracker fires once after 2000 ms", "[codex_read]")
{
    codex_read_tracker_reset();
    codex_read_tracker_visit("task:t1", 100);
    TEST_ASSERT_FALSE(codex_read_tracker_take_due("task:t1", 2099));
    TEST_ASSERT_TRUE(codex_read_tracker_take_due("task:t1", 2100));
    TEST_ASSERT_FALSE(codex_read_tracker_take_due("task:t1", 5000));
}

TEST_CASE("changing task restarts the read timer", "[codex_read]")
{
    codex_read_tracker_reset();
    codex_read_tracker_visit("task:t1", 100);
    TEST_ASSERT_TRUE(codex_read_tracker_take_due("task:t1", 2100));
    codex_read_tracker_visit("task:t2", 2200);
    TEST_ASSERT_FALSE(codex_read_tracker_take_due("task:t2", 4199));
    TEST_ASSERT_TRUE(codex_read_tracker_take_due("task:t2", 4200));
}

TEST_CASE("empty message id never acknowledges", "[codex_read]")
{
    codex_read_tracker_reset();
    codex_read_tracker_visit("", 100);
    TEST_ASSERT_FALSE(codex_read_tracker_take_due("", 9000));
}
```

- [ ] **Step 2: Run the test app and verify it fails**

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: FAIL because `codex_read_tracker` does not exist.

- [ ] **Step 3: Implement the pure read tracker**

Keep LVGL out of `codex_read_tracker.c`. Store the active message ID, visit timestamp, and a reported flag. A different non-empty ID resets both timestamp and reported flag. `codex_read_tracker_take_due` returns true exactly once when the same non-empty ID has been visible for at least 2000 ms; use wrap-safe `uint32_t` subtraction.

Run: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_read]` tests PASS.

- [ ] **Step 4: Wire the tracker into the detail screen**

Call `codex_read_tracker_visit(s_detail_task->message_id, lv_tick_get())` when the detail screen is entered and when its task changes. In `ui_timer`, while `s_current == s_detail`, call `codex_read_tracker_take_due`; when it returns true, call `codex_bridge_client_send_read_ack(message_id)` and ignore an offline return without disabling future detail visits. Call `codex_read_tracker_reset()` whenever the user leaves `s_detail`.

The existing 1000 ms UI timer is sufficient: acknowledgement occurs on the first timer tick at or after 2000 ms, never before 2000 ms.

- [ ] **Step 5: Verify the read-state display**

Update the detail metadata to include the current `.read_count` when greater than zero, for example `工作中 · 12 分钟 · 2 人已读`. Keep the existing metadata when no other device has read the message.

Run: `idf.py build`

Expected: BUILD SUCCESSFUL. If ESP-IDF is unavailable, record the gate as unverified.

Run after flashing: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`

Expected: all `[codex_bridge]`, `[codex_merge]`, and `[codex_read]` tests PASS.

- [ ] **Step 6: Commit**

```bash
git add main/codex_read_tracker.h main/codex_read_tracker.c main/state_ui.c main/CMakeLists.txt
git add test_apps/codex_bridge_protocol/main/test_codex_read_tracker.c
git commit -m "feat: synchronize Dotii task read state"
```

### Task 6: 管理中心迁入 CodexBridge 并删除 Dotii Codex 采集

**Files:**
- Create: `codexbridge/management/__init__.py`
- Create: `codexbridge/management/__main__.py`
- Create: `codexbridge/management/runtime_paths.py`
- Create: `codexbridge/management/bambu_client.py`
- Create: `codexbridge/management/bluetooth_bridge.py`
- Create: `codexbridge/management/codex_pairing.py`
- Create: `codexbridge/management/firmware_flasher.py`
- Create: `codexbridge/management/platforms/`
- Create: `codexbridge/management/web/`
- Create: `codexbridge/management/api.py`
- Create: `tests/test_management_api.py`
- Create: `tests/test_management_paths.py`
- Modify: `pyproject.toml`
- Delete in `D:/dotii`: `bridge/codex_app_server.py`
- Modify in `D:/dotii`: `bridge/codex_bridge.py`, `bridge/web/app.js`, `bridge/web/index.html`, `packaging/*`, `README.md`, `bridge/tests/`

**Interfaces:**
- Produces: `codexbridge.management.main(argv: list[str] | None = None) -> int`.
- Produces: management HTTP server on `127.0.0.1:8787` with Bambu, custom page, display settings, BLE provisioning, firmware flashing, logs, and runtime-folder routes unchanged.
- Produces: `codexbridge.management.runtime_paths.runtime_folder() -> Path` returning `%LOCALAPPDATA%/StateDisplay` on Windows.
- Consumes: `bridge.codex_bridge_link.claim_codex_bridge_token(device_id, base_url="http://127.0.0.1:8788")` as a local adapter or an equivalent copied implementation in `codexbridge/management/codex_pairing.py`.
- Removes: `/api/v1/codex`, `/api/v1/admin/codex*`, Codex snapshot fields, Codex module switches, Codex tabs, and `codex_app_server.py`.

- [ ] **Step 1: Write the failing management compatibility tests**

Add tests that load a temporary `%LOCALAPPDATA%/StateDisplay/state.json`, set Bambu/custom/display values, start `codexbridge.management.main(["--no-browser"])`, and assert the existing routes return the same values. Add a snapshot assertion:

```python
self.assertNotIn("codex", snapshot)
```

Add a configure-route test proving it rejects a missing `device_id`, claims the Codex Bridge token through the loopback pairing API, and passes `codex_bridge_token` to `start_configure` while preserving the management URL and token.

- [ ] **Step 2: Run the failing management tests**

Run: `python -m unittest tests.test_management_api tests.test_management_paths -v`

Expected: FAIL because `codexbridge.management` does not exist.

- [ ] **Step 3: Move the management center into CodexBridge**

Move the non-Codex modules from `D:/dotii/bridge/` into `codexbridge/management/`. Rewrite imports to use absolute `codexbridge.management.*` imports, package the web assets in `pyproject.toml`, and keep the device-facing route paths, JSON field names, ports, and file formats unchanged.

Use `%LOCALAPPDATA%/StateDisplay` for the migrated Bambu, custom-page, display, BLE, and flasher data. Do not move or rewrite `%LOCALAPPDATA%/CodexBridge/devices.json`.

- [ ] **Step 4: Remove Codex collection from the management center**

Delete the app-server collector and all Codex collection/write routes. Remove the Codex snapshot object, Codex module switch, Codex management tab, and every Codex polling timer. Dotii still receives Codex data only through TCP 8765 from the host.

Run: `python -m unittest tests.test_management_api tests.test_management_paths -v`

Expected: PASS.

- [ ] **Step 5: Remove Codex collection from Dotii**

Delete `bridge/codex_app_server.py`, remove its tests and imports, remove Codex fields from the management snapshot, and make the Dotii portable release entry point point at CodexBridge. Leave the firmware, hardware assets, and non-Codex management code in place during this task; Task 7 removes the duplicate PC packaging entry.

Run: `python -m unittest discover -s bridge/tests -v`

Expected: PASS except for the documented macOS `bluetooth-deps` environment test.

- [ ] **Step 6: Commit CodexBridge and Dotii separately**

In `D:/CodexBridge`:

```bash
git add codexbridge/management pyproject.toml tests/test_management_api.py tests/test_management_paths.py
git commit -m "feat: host the Dotii management center"
```

In `D:/dotii`:

```bash
git add bridge packaging README.md
git commit -m "refactor: move Codex collection to CodexBridge"
```

### Task 7: 单 EXE、单托盘与管理进程生命周期

**Files:**
- Create: `codexbridge/management_process.py`
- Create: `tests/test_management_process.py`
- Create: `packaging/CodexBridge.spec`
- Create: `packaging/build_windows.ps1`
- Modify: `codexbridge/__main__.py`
- Modify: `codexbridge/tray.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Produces: `management_argv(executable: Path, frozen: bool) -> list[str]`.
- Produces: `ManagementProcess(executable: Path, frozen: bool, poll_interval: float = 1.0, restart_limit: int = 3)` with `start() -> None`, `poll() -> int | None`, `stop(timeout: float = 5.0) -> None`, and `url` returning `http://127.0.0.1:8787/`.
- Produces: `codexbridge --management-child` which runs only the loopback management server and never imports the tray.
- Produces: one PyInstaller executable named `CodexBridge.exe`.

- [ ] **Step 1: Write the failing process-lifecycle tests**

Add tests using a fake process factory that record `argv`, `creationflags`, termination, and kill calls:

```python
def test_frozen_host_launches_itself_as_hidden_management_child(self):
    self.assertEqual(
        management_argv(Path("C:/Apps/CodexBridge.exe"), True),
        ["C:/Apps/CodexBridge.exe", "--management-child"],
    )
    self.assertTrue(fake.creationflags & CREATE_NO_WINDOW)

def test_stop_terminates_then_kills_after_timeout(self):
    process = ManagementProcess(...)
    process.start()
    process.stop(timeout=0.01)
    self.assertTrue(fake.terminate_called)
    self.assertTrue(fake.kill_called)
```

Add a test where the child exits unexpectedly three times inside 60 seconds: `ManagementProcess.poll()` must restart it for the first two exits and stop restarting after the third, reporting an error state without creating a second tray.

- [ ] **Step 2: Run the lifecycle tests and verify they fail**

Run: `python -m unittest tests.test_management_process -v`

Expected: FAIL because `management_process` does not exist.

- [ ] **Step 3: Implement the hidden management child and supervisor**

`codexbridge --management-child` must call `codexbridge.management.main(["--no-browser"])`, bind only `127.0.0.1:8787`, and never create a tray icon. The host starts this exact child with `subprocess.CREATE_NO_WINDOW` on Windows and hides the window with `STARTUPINFO` on other platforms when applicable.

The supervisor monitors the child once per second, restarts abnormal exits up to three times in a rolling 60-second window, and stops the child on host shutdown. Use `terminate`, wait `timeout`, then `kill`; do not leave an orphan on normal `exit`.

- [ ] **Step 4: Wire the host startup and tray**

`codexbridge/__main__.py` starts the management supervisor before starting discovery and the tray. `--no-management` suppresses only the child and remains useful for protocol tests. Tray `open_admin` opens `ManagementProcess.url` and never launches `DotiiManagementCenter.exe`.

Run: `python -m unittest tests.test_management_process tests.test_tray_model -v`

Expected: PASS.

- [ ] **Step 5: Build one executable**

Create a PyInstaller spec for a windowed `CodexBridge.exe`. Include `codexbridge.management`, the web assets, icon, platform adapters, and hidden-import data. Do not create a second management executable. `packaging/build_windows.ps1` must run the test suite, build the one executable, and fail if a second `.exe` is produced.

Run: `python -m unittest discover -s tests -v`

Expected: ALL PASS.

Run: `powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`

Expected: `dist/CodexBridge.exe` exists and `dist/DotiiManagementCenter.exe` does not.

Run: `dist/CodexBridge.exe --no-tray --no-management`

Expected: the host starts and `GET http://127.0.0.1:8788/api/admin/status` returns `ok:true`.

- [ ] **Step 6: Commit**

```bash
git add codexbridge/management_process.py codexbridge/__main__.py codexbridge/tray.py
git add tests/test_management_process.py packaging/CodexBridge.spec packaging/build_windows.ps1
git add pyproject.toml README.md
git commit -m "feat: ship one CodexBridge executable and tray"
```

## Plan Self-Review

### Spec Coverage

| Spec requirement | Owning task |
| --- | --- |
| Stable device identity and one pairing token | Task 1 |
| Codex status, tasks, results, notifications, and read synchronization | Tasks 2, 3, and 5 |
| One universal handshake and no protocol branching | Tasks 2, 3, and 7 |
| Per-device supported and enabled capabilities | Existing host plus Tasks 3 and 7 |
| Dotii keeps Bambu, custom page, display settings, BLE, and flashing on the management center | Tasks 4 and 6 |
| Dotii receives only Codex data from CodexBridge | Tasks 3, 4, and 6 |
| Two devices receive broadcasts and shared read state | Tasks 2, 3, and 5 |
| Reconnect, heartbeat, and offline behavior | Tasks 3 and 4 |
| Long text chunking with a 4 KB device limit | Tasks 2 and 3 |
| One EXE and one tray | Task 7 |
| `%LOCALAPPDATA%/StateDisplay` compatibility | Task 6 |

### Interface Consistency

- `MessageRecord.data` is produced in Task 2 and consumed by Task 3.
- `codex_bridge_message_t`, `codex_bridge_protocol_*`, and `codex_bridge_client_*` are produced in Task 3 and consumed by Tasks 4 and 5.
- `codex_task_detail_t.message_id`, `.read_count`, and `codex_snapshot_apply_read_state` are produced in Task 4 and consumed by Task 5.
- `claim_codex_bridge_token` is produced in Task 1 and consumed by Tasks 1, 6, and 7.
- `codexbridge.management.main` is produced in Task 6 and consumed by Task 7.

### Verification Gates

- Python host tests: `python -m unittest discover -s tests -v`.
- Dotii Python tests: `python -m unittest discover -s bridge/tests -v`; the known `bluetooth-deps` failure is environment-only and must be reported verbatim.
- Firmware tests: `idf.py -C test_apps/codex_bridge_protocol -p <PORT> flash monitor`.
- Firmware build: `idf.py build`; if `idf.py` is unavailable, the gate remains unverified.
- Packaging: one `dist/CodexBridge.exe`, no `DotiiManagementCenter.exe`.
