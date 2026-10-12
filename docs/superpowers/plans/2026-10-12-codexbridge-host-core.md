# CodexBridge 宿主骨架与协议核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 CodexBridge 的可运行宿主骨架：通用设备协议 v2、设备注册表、能力与权限模型、发现与设备会话、统一消息与已读、回环管理接口、托盘设备列表。不改动任何设备固件。

**Architecture:** 单进程 Python 应用。core 只依赖标准库；托盘是薄层，只把注册表和消息中心的状态映射成菜单。设备是 TCP 服务端，CodexBridge 主动连接；UDP 只用于发现。协议、能力、注册表、会话、消息中心各自独立模块，彼此通过明确的数据结构通信，便于单测。

**Tech Stack:** Python 3.11+、标准库（socket、json、threading、http.server、unittest）；托盘使用 pystray + Pillow（可选依赖）。

**Spec:** `docs/superpowers/specs/2026-10-12-unified-device-bridge-design.md`

## Global Constraints

- Python 3.11 起，core 不引入第三方运行时依赖；托盘依赖放在 `tray` extra 里。
- 设备端口 8765，发现端口 8764，本机管理接口固定 127.0.0.1:8788。
- 单行协议上限 768 字节；长文本按 480 字节分片；设备侧拼装上限 4 KB。
- 令牌不少于 16 字符；日志与异常信息里不得出现令牌原文。
- 管理接口只接受回环地址，非回环请求一律 403。
- 设备注册表落盘为 `devices.json`，型号默认能力落盘为 `capability-defaults.json`。
- 未知能力一律丢弃，不下发；未知消息类型由接收方忽略。
- 消息中心最多保留 200 条消息。

## Review Focus

- TCP 半包与粘包：一行 JSON 被拆成多次 `recv` 或多次到达被合并时，解析必须仍然正确。
- 多字节 UTF-8 被切断：分片必须按字符边界切，不能产生半个汉字。
- 设备离线重连：重连后必须补发未读消息与最新已读状态，且不重复推送已读过的旧消息。
- 能力开关：关掉的能力既不下发数据，也不接受设备发起的该能力事件。
- 令牌泄漏：任何日志、异常、管理接口响应里都不能回显令牌原文。

## File Structure

```
pyproject.toml                     打包与可选依赖
README.md                          项目说明与运行方式
codexbridge/__init__.py            版本号
codexbridge/__main__.py            进程入口：hub + 管理接口 + 托盘
codexbridge/protocol/messages.py   行 JSON 编解码、分片
codexbridge/protocol/capabilities.py  能力清单、型号默认、生效集合
codexbridge/devices/registry.py    设备注册表与令牌
codexbridge/devices/discovery.py   UDP 发现
codexbridge/devices/session.py     单设备 TCP 会话
codexbridge/messages_store.py      消息存储与已读
codexbridge/hub.py                 广播、能力过滤、设备事件处理
codexbridge/admin_api.py           回环管理接口
codexbridge/tray.py                托盘菜单（薄层）
tests/fake_device.py               测试用假设备（TCP 服务端）
tests/test_*.py                    各模块单测
```

## Task 1: 仓库骨架与测试入口

**Files:**
- Create: `pyproject.toml`, `README.md`, `codexbridge/__init__.py`, `codexbridge/protocol/__init__.py`, `codexbridge/devices/__init__.py`, `tests/__init__.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Produces: `codexbridge.VERSION: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_package.py
import unittest
import codexbridge

class PackageTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(codexbridge.VERSION, "0.1.0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_package -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'codexbridge'`

- [ ] **Step 3: Create the package skeleton**

`pyproject.toml` 用 setuptools，`requires-python = ">=3.11"`，`dependencies = []`，`[project.optional-dependencies] tray = ["pystray>=0.19", "Pillow>=10"]`；`codexbridge.VERSION = "0.1.0"`。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_package -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md codexbridge tests
git commit -m "chore: scaffold codexbridge package"
```

## Task 2: 行 JSON 编解码与分片

**Files:**
- Create: `codexbridge/protocol/messages.py`
- Test: `tests/test_protocol_messages.py`

**Interfaces:**
- Produces: `encode(message: dict) -> bytes`、`decode(line: bytes) -> dict | None`、`split_text(text: str, limit: int = TEXT_CHUNK_BYTES) -> list[str]`、`wire_messages(message: dict) -> list[bytes]`、常量 `MAX_LINE_BYTES = 768`、`TEXT_CHUNK_BYTES = 480`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_protocol_messages.py
import unittest
from codexbridge.protocol.messages import decode, encode, split_text, wire_messages

class MessageWireTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode(encode({"type": "bridge_ready"})), {"type": "bridge_ready"})

    def test_non_json_line_is_not_a_message(self):
        self.assertIsNone(decode(b"[device] boot ok"))

    def test_encode_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            encode(["not", "a", "dict"])

    def test_split_text_keeps_characters_intact(self):
        chunks = split_text("中文" * 400, limit=480)
        self.assertTrue(all(len(c.encode("utf-8")) <= 480 for c in chunks))
        self.assertEqual("".join(chunks), "中文" * 400)

    def test_long_message_is_chunked_with_sequence(self):
        records = wire_messages({"type": "message", "body": "中" * 600})
        self.assertGreater(len(records), 1)
        self.assertEqual(decode(records[0])["total"], len(records))
        self.assertEqual(decode(records[-1])["seq"], len(records) - 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_protocol_messages -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'codexbridge.protocol.messages'`

- [ ] **Step 3: Implement the codec**

`encode` 用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 加 `\n` 并编码 UTF-8；超长控制消息抛 `ValueError`。`decode` 遇到非 `{` 开头或 JSON 解析失败返回 `None`。`split_text` 按字符累加，保证每片 UTF-8 字节数不超过 `limit`。`wire_messages` 只对 `type == "message"` 的 `body` 分片，逐片补 `seq` 与 `total`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_protocol_messages -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/protocol/messages.py tests/test_protocol_messages.py
git commit -m "feat: add line json codec and text chunking"
```

## Task 3: 能力与权限模型

**Files:**
- Create: `codexbridge/protocol/capabilities.py`
- Test: `tests/test_capabilities.py`

**Interfaces:**
- Produces: `FEATURES: tuple[str, ...]`、`MINIMAL: frozenset[str]`、`MODEL_DEFAULTS: dict[str, frozenset[str]]`、`normalize(values: Iterable[str]) -> set[str]`、`effective(supported: Iterable[str], enabled: Iterable[str]) -> set[str]`、`default_for(model: str) -> set[str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capabilities.py
import unittest
from codexbridge.protocol.capabilities import MINIMAL, default_for, effective, normalize

class CapabilityTest(unittest.TestCase):
    def test_unknown_capability_is_dropped(self):
        self.assertEqual(normalize(["status", "nope"]), {"status"})

    def test_effective_is_intersection(self):
        self.assertEqual(effective(["status", "text"], ["text", "read_ack"]), {"text"})

    def test_unknown_model_gets_minimal(self):
        self.assertEqual(default_for("unknown-model"), set(MINIMAL))

    def test_ai_passport_default_is_minimal(self):
        self.assertEqual(default_for("FoloOS-AI-Passport"), set(MINIMAL))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_capabilities -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement the model**

`FEATURES` 至少包含 `status`、`text`、`tasks`、`results`、`approval`、`audio`、`read_ack`、`voice`、`word_bear`。`MINIMAL = frozenset({"status", "text", "read_ack"})`。`MODEL_DEFAULTS` 里 `FoloOS-AI-Passport` 取 `MINIMAL`，`dotii` 取 `MINIMAL | {"tasks", "results"}`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_capabilities -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/protocol/capabilities.py tests/test_capabilities.py
git commit -m "feat: add capability and permission model"

## Task 4: 设备注册表

**Files:**
- Create: `codexbridge/devices/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `DeviceRecord`（dataclass：`device_id`、`model`、`name`、`token`、`supported: set[str]`、`enabled: set[str]`、`last_seen: float | None`）、`DeviceRegistry(path: Path)`，方法 `load()`、`save()`、`get(device_id) -> DeviceRecord | None`、`upsert(device_id, *, model, name=None, token=None, supported=None) -> DeviceRecord`、`remove(device_id) -> bool`、`set_enabled(device_id, enabled: Iterable[str])`、`mint_token(device_id) -> str`、`migrate_legacy(legacy_path: Path) -> bool`；`path` 为 `None` 时只驻内存，不落盘

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_registry.py
import json
import tempfile
import unittest
from pathlib import Path
from codexbridge.devices.registry import DeviceRegistry

class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "devices.json"
        self.registry = DeviceRegistry(self.path)

    def test_round_trip(self):
        self.registry.upsert("d1", model="dotii", supported=["status", "text"])
        self.registry.save()
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertEqual(reloaded.get("d1").model, "dotii")
        self.assertEqual(reloaded.get("d1").enabled, {"status", "text", "read_ack"})

    def test_enabled_drops_unknown_capability(self):
        self.registry.upsert("d1", model="dotii")
        self.registry.set_enabled("d1", ["status", "nope"])
        self.assertEqual(self.registry.get("d1").enabled, {"status"})

    def test_mint_token_is_long_and_stable(self):
        first = self.registry.mint_token("d1")
        self.assertGreaterEqual(len(first), 16)
        self.assertEqual(first, self.registry.mint_token("d1"))

    def test_migrate_legacy_bridge_json(self):
        legacy = Path(self.tmp.name) / "bridge.json"
        legacy.write_text(json.dumps({"host": "192.168.1.23", "port": 8765, "token": "abcdefghijklmnop"}))
        self.assertTrue(self.registry.migrate_legacy(legacy))
        record = self.registry.get("legacy:192.168.1.23")
        self.assertEqual(record.token, "abcdefghijklmnop")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_registry -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement the registry**

`upsert` 时若记录不存在，`enabled` 取 `default_for(model)`，`supported` 取传入值；`load()` 对缺字段的旧文件给安全默认；`save()` 用临时文件加 `os.replace` 原子写；`mint_token` 用 `secrets.token_urlsafe(24)` 且只在设备没有令牌时生成。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_registry -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/registry.py tests/test_registry.py
git commit -m "feat: add device registry and legacy migration"
```

## Task 5: UDP 发现

**Files:**
- Create: `codexbridge/devices/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces: `DISCOVERY_PORT = 8764`、`DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER_V2"`、`DiscoveryResult`（dataclass：`host`、`device`、`device_id`、`port`、`protocol`、`capabilities`）、`parse_reply(payload: bytes, host: str) -> DiscoveryResult | None`、`discover(timeout: float = 3.0, targets: Iterable[str] = ("255.255.255.255",)) -> list[DiscoveryResult]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_discovery.py
import socket
import threading
import unittest
from codexbridge.devices.discovery import parse_reply, discover, DISCOVERY_PORT

class DiscoveryTest(unittest.TestCase):
    def test_parse_v2_reply(self):
        result = parse_reply(b'{"device":"dotii","device_id":"a1","port":8765,"protocol":2,"capabilities":["status"]}', "10.0.0.5")
        self.assertEqual((result.protocol, result.device_id, result.port), (2, "a1", 8765))
        self.assertEqual(result.capabilities, ["status"])

    def test_parse_v1_reply_without_protocol(self):
        result = parse_reply(b'{"device":"FoloOS-AI-Passport","port":8765}', "10.0.0.6")
        self.assertEqual(result.protocol, 1)
        self.assertEqual(result.capabilities, [])

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_reply(b"not json", "10.0.0.7"))

    def test_loopback_round_trip(self):
        stop = threading.Event()
        def responder():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", DISCOVERY_PORT))
            sock.settimeout(0.2)
            while not stop.is_set():
                try:
                    _, source = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                sock.sendto(b'{"device":"dotii","device_id":"a1","port":8765,"protocol":2}', source)
            sock.close()
        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        try:
            results = discover(timeout=1.0, targets=("127.0.0.1",))
        finally:
            stop.set()
            thread.join(timeout=1.0)
        self.assertEqual([r.device_id for r in results], ["a1"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_discovery -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement discovery**

`discover` 用 `SO_BROADCAST`、`settimeout` 的 UDP socket 向每个目标发一次请求，在超时窗口内收集并解析应答，按 `(device_id, host)` 去重；解析失败直接跳过。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_discovery -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/discovery.py tests/test_discovery.py
git commit -m "feat: add udp discovery"
```

## Task 6: 单设备会话与假设备

**Files:**
- Create: `codexbridge/devices/session.py`, `tests/fake_device.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Produces: `DeviceSession(host, port, token, *, protocol=2, device_id=None, on_event=None, on_online=None, on_offline=None, heartbeat_seconds=5.0, timeout_seconds=15.0)`，方法 `run_once() -> None`（连接、握手、心跳、读到断线为止）、`send(message: dict) -> None`；`tests/fake_device.py` 的 `FakeDevice`（上下文管理器，暴露 `host`、`port`、`received: list[dict]`、`send_event(event: dict)`、`send_raw_split(payload: bytes)`、`stall()`、`expect_handshake(token)`）

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session.py
import unittest
from codexbridge.devices.session import DeviceSession
from tests.fake_device import FakeDevice

class SessionTest(unittest.TestCase):
    def test_handshake_and_heartbeat(self):
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(device.host, device.port, "abcdefghijklmnop",
                                    heartbeat_seconds=0.2, timeout_seconds=1.0)
            session.run_once()  # 设备在收到一次心跳后主动关闭
            self.assertTrue(any(m["type"] == "bridge_ready" for m in device.received))

    def test_device_event_reaches_callback(self):
        seen = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.after_handshake(lambda: device.send_event({"type": "read_ack", "message_id": "m1"}))
            session = DeviceSession(device.host, device.port, "abcdefghijklmnop",
                                    on_event=seen.append, heartbeat_seconds=0.2, timeout_seconds=1.0)
            session.run_once()
        self.assertEqual(seen[0]["message_id"], "m1")

    def test_stalled_device_goes_offline(self):
        states = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.stall()
            session = DeviceSession(device.host, device.port, "abcdefghijklmnop",
                                    on_offline=lambda: states.append("offline"),
                                    heartbeat_seconds=0.1, timeout_seconds=0.5)
            session.run_once()
        self.assertIn("offline", states)

    def test_line_split_across_packets_is_reassembled(self):
        seen = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.after_handshake(lambda: device.send_raw_split(b'{"type":"read_ack","message_id":"m1"}' + bytes([10])))
            session = DeviceSession(device.host, device.port, "abcdefghijklmnop",
                                    on_event=seen.append, heartbeat_seconds=0.2, timeout_seconds=1.0)
            session.run_once()
        self.assertEqual(seen[0]["message_id"], "m1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_session -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement the session and the fake device**

`run_once` 依次：`socket.create_connection`、发 `hello` 收 `hello_ok`、发 `bridge_auth` 收 `bridge_auth_ok`、调用 `on_online`、进入循环（发心跳、`select` 读行、`decode` 后交给 `on_event`、超过 `timeout_seconds` 没有收到任何数据就判定掉线并退出）。`FakeDevice` 用 `socketserver.ThreadingTCPServer` 实现同样顺序，暴露 `received` 便于断言。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_session -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/devices/session.py tests/fake_device.py tests/test_session.py
git commit -m "feat: add device session with fake device harness"

## Task 7: 消息存储与消息中心

**Files:**
- Create: `codexbridge/messages_store.py`, `codexbridge/hub.py`
- Test: `tests/test_messages_store.py`, `tests/test_hub.py`

**Interfaces:**
- Produces: `MessageRecord`（dataclass：`message_id`、`thread_id`、`kind`、`status`、`title`、`body`、`created_at`、`read_by: set[str]`）、`MessageStore(max_messages: int = 200)`，方法 `add(record) -> MessageRecord`、`get(message_id)`、`mark_read(message_id, device_id) -> MessageRecord | None`、`read_by(message_id) -> set[str]`、`pending_for(device_id, capabilities: set[str], unread_only: bool = True) -> list[MessageRecord]`
- Produces: `BridgeHub(registry, store=None)`，方法 `publish(record) -> None`、`attach(device_id, session) -> None`、`detach(device_id) -> None`、`handle_device_event(device_id, event) -> None`、`capabilities_for(device_id) -> set[str]`、`state_for(device_id) -> dict`
- `kind` 到能力的映射固定为：`status`→`status`、`task`→`tasks`、`result`→`results`、`approval`→`approval`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_messages_store.py
import unittest
from codexbridge.messages_store import MessageRecord, MessageStore

class MessageStoreTest(unittest.TestCase):
    def test_mark_read_records_device(self):
        store = MessageStore()
        store.add(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        store.mark_read("m1", "d1")
        self.assertEqual(store.read_by("m1"), {"d1"})

    def test_pending_filters_by_capability(self):
        store = MessageStore()
        store.add(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        self.assertEqual([r.message_id for r in store.pending_for("d1", {"results"})], ["m1"])
        self.assertEqual(store.pending_for("d1", {"status"}), [])

    def test_oldest_message_is_evicted(self):
        store = MessageStore(max_messages=2)
        for index in range(3):
            store.add(MessageRecord(f"m{index}", "t1", "status", "working", "t", "b", float(index)))
        self.assertIsNone(store.get("m0"))
```

```python
# tests/test_hub.py
import unittest
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord

class FakeSession:
    def __init__(self):
        self.sent = []
    def send(self, message):
        self.sent.append(message)

class HubTest(unittest.TestCase):
    def setUp(self):
        self.hub = BridgeHub(DeviceRegistry(None))
        self.hub.registry.upsert("d1", model="dotii", supported=["status", "text", "tasks", "results", "read_ack"])
        self.hub.registry.upsert("d2", model="FoloOS-AI-Passport", supported=["status", "text", "read_ack"])
        self.s1, self.s2 = FakeSession(), FakeSession()
        self.hub.attach("d1", self.s1)
        self.hub.attach("d2", self.s2)

    def test_publish_only_reaches_capable_devices(self):
        self.hub.publish(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        self.assertEqual([m["type"] for m in self.s1.sent], ["message"])
        self.assertEqual(self.s2.sent, [])

    def test_read_ack_broadcasts_read_state(self):
        self.hub.publish(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        self.s1.sent.clear()
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})
        self.assertEqual(self.s2.sent[-1]["type"], "read_state")
        self.assertEqual(self.s2.sent[-1]["read_by"], ["d1"])

    def test_capability_less_event_is_rejected(self):
        self.hub.handle_device_event("d2", {"type": "voice_command", "text": "hi"})
        self.assertEqual(self.s2.sent[-1]["type"], "error")

    def test_reconnect_state_includes_unread_messages(self):
        self.hub.publish(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        state = self.hub.state_for("d1")
        self.assertEqual([m["message_id"] for m in state["messages"]], ["m1"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_messages_store tests.test_hub -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement store and hub**

`MessageStore` 用 `OrderedDict` 保持顺序，超过上限淘汰最旧。`BridgeHub.publish` 先存后广播，只发给生效能力包含该 `kind` 对应能力的会话，序列化时用 `wire_messages` 分片。`handle_device_event` 只处理 `read_ack`（写 `read_by` 后向他设备广播 `read_state`）与 `cancel_task`；其余事件先校验能力，缺能力就回 `error`。`state_for` 返回 `{"messages": [...], "read_state": [...]}`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_messages_store tests.test_hub -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/messages_store.py codexbridge/hub.py tests/test_messages_store.py tests/test_hub.py
git commit -m "feat: add message store, broadcast and read state"
```

## Task 8: 回环管理接口

**Files:**
- Create: `codexbridge/admin_api.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Produces: `handle_request(method: str, path: str, query: dict[str, list[str]], body: bytes, client_host: str, hub, registry) -> tuple[int, dict]`、`AdminApi(hub, registry, host="127.0.0.1", port=8788)`（`start()`、`stop()`、`url`）
- 路由：`GET /api/v1/admin/status`、`GET /api/v1/admin/devices`、`POST /api/v1/admin/devices/remove`、`POST /api/v1/admin/devices/capabilities`、`GET /api/v1/admin/pairing`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_api.py
import json
import unittest
from pathlib import Path
import tempfile
from codexbridge import VERSION
from codexbridge.admin_api import handle_request
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub

class AdminApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.registry.upsert("d1", model="dotii", supported=["status", "text"])
        self.hub = BridgeHub(self.registry)

    def test_non_loopback_is_forbidden(self):
        status, _ = handle_request("GET", "/api/v1/admin/status", {}, b"", "10.0.0.9", self.hub, self.registry)
        self.assertEqual(status, 403)

    def test_status_reports_version(self):
        status, payload = handle_request("GET", "/api/v1/admin/status", {}, b"", "127.0.0.1", self.hub, self.registry)
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], VERSION)

    def test_pairing_mints_token_and_stores_it(self):
        status, payload = handle_request("GET", "/api/v1/admin/pairing", {"device_id": ["d2"]}, b"", "127.0.0.1", self.hub, self.registry)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["token"]), 16)
        self.assertEqual(self.registry.get("d2").token, payload["token"])

    def test_devices_list_hides_token(self):
        status, payload = handle_request("GET", "/api/v1/admin/devices", {}, b"", "127.0.0.1", self.hub, self.registry)
        self.assertEqual(status, 200)
        self.assertNotIn("token", json.dumps(payload))

    def test_capabilities_update_filters_unknown(self):
        body = json.dumps({"device_id": "d1", "enabled": ["status", "nope"]}).encode()
        status, _ = handle_request("POST", "/api/v1/admin/devices/capabilities", {}, body, "127.0.0.1", self.hub, self.registry)
        self.assertEqual(status, 200)
        self.assertEqual(self.registry.get("d1").enabled, {"status"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_admin_api -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement the admin API**

`handle_request` 先判断 `client_host` 是否回环（`127.0.0.1`、`::1`），不是就返回 403。`AdminApi` 用 `http.server.ThreadingHTTPServer` 包一层，把请求转成 `handle_request`；`POST` 路由校验 `Content-Type: application/json` 与请求体上限 8 KB。设备能力变更后调用 `hub.push_capabilities(device_id)` 下发 `capabilities_update`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_admin_api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add codexbridge/admin_api.py tests/test_admin_api.py
git commit -m "feat: add loopback admin api"
```

## Task 9: 托盘菜单模型与进程入口

**Files:**
- Create: `codexbridge/tray.py`, `codexbridge/__main__.py`
- Test: `tests/test_tray_model.py`

**Interfaces:**
- Produces: `MenuItem`（dataclass：`label`、`kind`（`"action"`|`"check"`|`"submenu"`）、`action_id: str | None`、`checked: bool | None`、`children: list[MenuItem]`）、`build_menu_model(registry, hub) -> list[MenuItem]`、`tray.run(hub, registry) -> None`（导入 pystray 失败时抛出带安装提示的 `RuntimeError`）
- `__main__.py` 支持 `--no-tray`：只跑 hub、发现轮询与回环管理接口，便于无桌面环境验证

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tray_model.py
import unittest
from pathlib import Path
import tempfile
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.tray import build_menu_model

class TrayModelTest(unittest.TestCase):
    def test_menu_lists_each_device_with_capability_checks(self):
        tmp = tempfile.TemporaryDirectory()
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert("d1", model="dotii", name="桌面屏", supported=["status", "text"])
        model = build_menu_model(registry, BridgeHub(registry))
        device_menu = next(item for item in model if item.kind == "submenu" and "桌面屏" in item.label)
        checks = [child for child in device_menu.children if child.kind == "check"]
        self.assertTrue(any(child.action_id == "toggle_capability:d1:status" for child in checks))
        self.assertTrue(all(child.checked is not None for child in checks))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_tray_model -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: Implement the tray layer and entry point**

`build_menu_model` 每台设备一个 submenu，子项包含 `配对设备…`、`手动添加 IP…`、能力复选（`toggle_capability:<device_id>:<feature>`）、`移除设备`；顶层包含 `打开管理中心`、`打开日志目录`、`退出`。`tray.run` 把模型映射成 pystray 菜单并在点击时调用 `registry.set_enabled` 与 `hub.push_capabilities`。`__main__.py` 组装 `DeviceRegistry`、`BridgeHub`、`AdminApi`、发现轮询线程与托盘。

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_tray_model -v`
Expected: PASS

- [ ] **Step 5: 全量验证并提交**

Run: `python -m unittest discover -s tests -v`
Expected: 全部 PASS

Run: `python -m codexbridge --no-tray` 后访问 `http://127.0.0.1:8788/api/v1/admin/status`
Expected: 返回 `{"ok": true, "version": "0.1.0", ...}`

```bash
git add codexbridge/tray.py codexbridge/__main__.py tests/test_tray_model.py
git commit -m "feat: add tray menu model and host entry point"
```

## 本计划的边界与后续计划

本计划只交付 PC 端宿主骨架，验收标准是：单元测试全绿、`--no-tray` 模式能起服务、假设备能完成握手并收到按能力过滤的消息。它不改任何设备固件，也不包含真实 Codex 采集。

后续计划（各自独立交付）：

1. CodexBridge Codex 采集接入（移植 FoloOS 的 `codex_backend` 与 `windows_codex_backend`，保留许可证与出处）。
2. Dotii 设备：固件 `codex_bridge_client` 与双数据源合并；管理中心去掉 Codex 采集与配网下发令牌。
3. 已读同步真机闭环：Dotii 停留 2 秒上报、AI Passport 加 `read_ack`。
4. 单托盘打包与迁移：PyInstaller 打包、管理中心作为子进程、`FoloOS/bridge.json` 迁移。
```
```
