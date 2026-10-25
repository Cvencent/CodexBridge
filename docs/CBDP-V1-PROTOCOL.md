# CodexBridge Device Protocol v1 (CBDP v1) 线协议规范

> **文档导航：** [项目说明](../README.md) · **CBDP v1 协议** · [本机管理与伴侣接口](companion-and-admin-api.md)

- 状态：Frozen v1.0，供 Bridge、手机端、ESP32、ESP32-S3 和桌面端并行实现
- 协议名：CodexBridge Device Protocol v1
- 协议缩写：CBDP v1
- 目标版本：CodexBridge 2.0
- 线格式版本：`major = 1`，`minor = 0`
- 日期：2026-10-17
- 后续协议补充：2026-10-24（新增 `PAIRING(0x1F)` 无线配对消息）
- 规范范围：发现、传输、会话、鉴权、能力、订阅、事件、命令、写入、音频、审批、ACK、重传、分片、错误、兼容和一致性测试

本文档是正式线协议基线。客户端 Agent、ESP Agent、Bridge Agent 和桌面端 Agent
必须以本文档中的字节布局、字段编号、状态机和错误语义为准。实现之间不得通过
私有约定补字段，也不得在同一条连接上建立第二套握手。

架构动机、迁移阶段和验收边界见
[CBDP v1 架构重构设计](superpowers/specs/2026-10-17-cbdp-v1-architecture-refactor-design.md)。
本文档只定义协议，不规定 Bridge 内部代码模块如何命名。

## 1. 规范用语与实现边界

文中使用以下规范词：

- **MUST**：实现必须满足，否则不视为 CBDP v1 实现。
- **MUST NOT**：实现必须禁止，否则视为协议违规。
- **SHOULD**：除非有明确理由，否则应满足。
- **MAY**：实现可以选择，接收方必须容忍两种行为。

协议由以下角色组成：

| 角色 | 说明 |
|---|---|
| Bridge | 唯一事实源、协议主机、事件订阅中心、writer lease 持有者、命令执行入口 |
| Device | ESP32、ESP32-S3、手机或其他设备客户端 |
| Desktop Adapter | 桌面端接入 Bridge 的适配器；在接管线程上必须通过 Bridge 写入 |
| Legacy Gateway | 只做旧 JSON 行协议到 CBDP v1 的翻译，不保存第二套业务状态 |

CBDP v1 的核心不变量：

1. Bridge 是事件、投影、订阅、writer lease 和命令去重的唯一权威。
2. 设备只表达意图，不直接操作 Codex 内部实现。
3. 手机、ESP 和桌面共享同一事件模型、同一命令编号和同一错误码。
4. `micro`、`panel`、`full` 只是预算、默认能力和传输策略不同，不是三套协议。
5. Bridge 是唯一权威写入者。桌面 Adapter、手机和设备都只能通过 Bridge 提交写入意图。
6. 未持有有效 writer lease 的写操作必须被拒绝；检测到桌面外部直写时，Bridge
   必须撤销该线程的 writer lease、进入 resync，并广播所有权变化。
7. 设备断线恢复使用事件 cursor，不使用传输层序号恢复业务状态。
8. 未知 TLV 必须跳过，未知可选能力不得导致连接关闭。

### 1.1 协议全集与本轮实现边界

本文档定义完整的 CBDP v1 协议全集，包括本轮实现和后续才会实现的稳定编号与
字段。协议中已经出现一个编号，只代表该编号已经分配，**不代表本轮已经可用**。

客户端可见的能力位、命令、事件和能力覆盖矩阵只使用两个状态。每个条目都必须
明确选择其中一个状态，不能使用“待实现”“预留”“后续”“当前状态”“协议已定义”
或“部分可映射”等模糊表述：

| 状态 | 含义 |
|---|---|
| `[本轮实现]` | Bridge 与具备对应能力的客户端在本轮必须实现并互操作 |
| `[本轮不实现]` | 本轮只保留完整协议定义，任何端点不得声明、发送或依赖 |

本轮实现子集是一个明确白名单，不以“文档里定义了编号”为可用依据：

`[本轮实现]` 能力位：

```text
status text tasks results approval audio voice read_ack control history
telemetry subscribe_live subscribe_thread subscribe_summary writer_lease multi_page
tls device_config command_status queue queue_manage guidance message_edit
message_delete thread_manage structured_input attachment draft thread_search
thread_lifecycle thread_fork thread_settings message_revision item_stream plan
reasoning_summary tool_events mcp account context_usage read_unread project_manage
notice server_request input_capabilities queue_priority message_reference model_switch
cross_device_active resume file_reference image_input audio_input events_since
```

`[本轮不实现]` 能力位：

```text
compression aead_reserved batch vendor context_compact interrupt_mode
realtime_reserved structured_approval thread_goal experimental_ext
```

第 7.1 节列出的全部消息类型属于 `[本轮实现]`；每个消息类型仍只承载其可用
命令、事件和字段。消息类型可解析不代表其中引用的 `[本轮不实现]` 能力已经可用。

命令和事件的状态由所需能力继承：所需能力全部是 `[本轮实现]` 时，命令或事件
才是 `[本轮实现]`；所需能力包含任一 `[本轮不实现]` 项时，命令或事件为
`[本轮不实现]`。同一能力同时出现在两个范围时，以更窄的 `[本轮不实现]` 为准。

`micro`、`panel`、`full` 只是在同一实现能力集上的预算和策略不同，不是三套
协议。profile 不会把 `[本轮不实现]` 能力变成可用；任何设备也不能通过声明
profile 绕过能力求交。

`[本轮不实现]` 的硬性行为：

1. 设备 MUST NOT 在 `supported_caps` 中声明对应能力位。
2. 设备 MUST NOT 发送对应命令、事件、flag、TLV 或厂商扩展值。
3. 设备 MUST NOT 在 UI 中显示入口，也 MUST NOT 依赖对应事件改变业务状态。
4. Bridge MUST NOT 向设备发送对应事件，也 MUST NOT 要求设备填写对应字段。
5. 设备发送已知能力位但该能力未启用时，Bridge 返回 `CAPABILITY_NOT_ENABLED`。
6. 设备发送未实现的方法、消息或安全模式时，Bridge 返回
   `UNSUPPORTED_FEATURE`；不得按其他版本猜测执行。
7. 客户端收到未声明能力的事件或未知扩展字段时，必须安全跳过并推进 cursor，
   不得把它解释成本轮可用功能。

设备能力仍按 `supported ∩ policy ∩ transport` 求交，并可由管理页面裁剪。
未声明能力不得被设备假定存在。管理页面不得把 `[本轮不实现]` 能力显示成可勾选
的当前开关，不得让配置结果暗示 Bridge 已经支持。客户端只在明确收到带
`[本轮实现]` 状态的能力、命令或事件时才创建入口。

固定头 `reserved` 字节、未使用 flag 位、未分配 TAG/方法号/事件号是线格式安全
机制，不是能力承诺。发送方不得使用未分配值，接收方必须忽略未知可选值。

## 2. 版本与演进

### 2.1 版本字段

固定头中的 `version_major` 必须为 `1`。`version_minor` 放在头部 TLV 中，
当前为 `0`。

```text
major 变化：允许线格式不兼容变化。
minor 变化：只允许增加可选字段、可选能力或新消息类型。
```

发送方声明的 `minor` 可以高于接收方。接收方必须忽略未知 TLV，并在需要响应时
使用自己支持的 minor 回包。

### 2.2 兼容规则

- 同一 major 内，已有字段的编号、类型、单位和语义不得改变。
- 新字段必须有默认值；缺少字段时按默认值处理。
- 旧字段不得被删除后复用为不同语义。
- 新消息类型不能改变旧消息类型的解析。
- 未识别消息类型不得导致连接关闭。如果消息带有 `REQUEST` 标志或
  `request_id`，接收方应返回 `ERROR`，错误码为 `UNSUPPORTED_FEATURE`。
- 设置了 `COMPRESSED` 标志而接收方未实现压缩时，必须返回
  `UNSUPPORTED_FEATURE`，不得尝试猜测压缩算法；本轮所有端点都未实现压缩。

### 2.3 保留编号

- `0x0000` 保留给空值和未分配。
- `0x00F0..0x00FF` 保留给传输层和测试向量。
- `0x0100..0x01FF` 是 CBDP v1 标准应用扩展区，编号见 8.11。
- `0x0200..0x7FFF` 是未来标准扩展区；未分配 tag 必须按 6.1 跳过。
- `0x8000..0xFFFF` 保留给厂商扩展；只有同时声明 `vendor` 能力并设置
  `VENDOR_EXTENSION` 才能发送。本轮 `vendor` 属于 `[本轮不实现]`。
- `0x01C0..0x01FF` 是实验性 TLV 空间；`[本轮不实现]` 发送方不得使用，
  接收方必须按未知 TLV 安全跳过。

## 3. 传输绑定

### 3.1 默认端口

| 用途 | 传输 | 默认端口 | 发起方 |
|---|---|---:|---|
| 设备发现 | UDP | `8764` | Bridge 或设备 |
| ESP TCP 会话 | TCP binary | `8765` | Bridge 连接设备，兼容现有拓扑 |
| Bridge 原始 TCP 会话 | TCP binary | `8766` | full 客户端连接 Bridge，可选 |
| 手机和桌面 WebSocket | WebSocket binary | `8568` 的 `/cbdp/v1/ws` | 客户端连接 Bridge |
| 手机 SSE 兼容 | HTTP + SSE | `8568` 的 `/cbdp/v1/events` | 客户端连接 Bridge |
| 旧 JSON 行协议 | TCP JSON | 现有端口 | 只经兼容网关 |

所有端口都允许在管理中配置。端口变化不改变线协议。

### 3.1.1 手机 Companion 端口上的 CBDP（v1.0 实现后追加）

Bridge 在手机 Companion HTTP 端口（默认 `8568`）上同时提供 CBDP v1 和旧
`/api/companion/*` 接口。两种路径共用同一个 Bridge 权威状态，不建立第二套
事件或业务存储。

| 路径 | 传输 | 方向 | 鉴权 |
|---|---|---|---|
| `/cbdp/v1/ws` | WebSocket binary | 双向 | HTTP `Authorization: Bearer <配对凭据>` |
| `/cbdp/v1/events` | HTTP + SSE | Bridge 到客户端 | HTTP `Authorization: Bearer <配对凭据>` |
| `POST /cbdp/v1/frame` | HTTP binary body | 客户端到 Bridge | HTTP `Authorization: Bearer <配对凭据>` |

手机端接入规则如下：

- 三条路径都先用 HTTP `Authorization: Bearer <配对凭据>` 完成传输层鉴权。
  会话建立后仍执行标准 CBDP 状态机：`HELLO -> WELCOME -> AUTH -> AUTH_OK`。
  也就是说 HTTP 鉴权与会话鉴权是两个独立层次，缺一不可。
- Bridge 用同一个配对凭据作为 CBDP AUTH 的 HMAC key。客户端必须按 11.5
  计算 `auth_proof`，携带完整 32 字节，不能跳过 AUTH 直接发送订阅或命令。
- WebSocket 的每个 binary message 可以包含一个或多个完整 CBDP 帧；接收方
  按 `header_len + payload_len` 切分，不能按 message 边界猜测。
- SSE 的 `event:` 固定为 `cbdp`，`data:` 是完整 CBDP 帧的标准 Base64。
  `id:` 是帧的 cursor；断线重连时用 `Last-Event-ID` 作为 `RESYNC.cursor`。
- SSE 只能承载 Bridge 到客户端方向。客户端发往 Bridge 的帧必须走同一
  HTTP 连接上的 `POST /cbdp/v1/frame`（完整 CBDP 帧作为 body）。
- WebSocket 与 SSE 路径使用同一个 gateway、事件主链、订阅管理器、writer
  lease 和引用注册表；会话断开时 Bridge 必须清理该设备的订阅。
- 管理接口 `GET /api/admin/companion` 返回 `cbdp_v1` 能力对象，包含协议版本、
  路径、鉴权方式、端口和启用状态。新客户端据此选择协议，不要先用 401/404
  探测。
- 普通 JSON 响应和 SSE 响应都会返回 `Connection: close`。这是正常的传输
  生命周期，客户端应按退避重新连接，不得把它当成业务错误。

### 3.2 传输语义

同一逻辑消息在不同 transport 上必须保持相同的字段语义和事件编号：

| Transport | 帧承载方式 | 说明 |
|---|---|---|
| TCP binary | 直接发送固定头和 TLV | ESP 默认 |
| TCP + TLS | TLS 内直接发送 CBDP 帧 | full 或支持 TLS 的 panel |
| WebSocket binary | 每个 WebSocket binary message 承载一个或多个完整 CBDP 帧 | 手机和桌面默认 |
| SSE | `event: cbdp`，`data:` 为 Base64 编码的完整 CBDP 帧 | 只承载 Bridge 到客户端 |
| HTTP 命令 | `POST /cbdp/v1/frame`，body 为完整 CBDP 帧 | SSE 客户端的反向通道 |
| Legacy TCP JSON | 由兼容网关翻译 | 不作为新实现路径 |

WebSocket 客户端如果在一个 binary message 中发送多个帧，接收方必须按
`header_len + payload_len` 切分，不得按 message 边界猜测协议帧边界。

SSE 的 `id:` 字段必须为该帧的 `cursor`。客户端重连时使用
`Last-Event-ID`，Bridge 将其视为 `RESYNC.cursor`。

### 3.3 连接方向

CBDP 会话在握手完成后是语义对称的，但建连方向有两种绑定：

1. **Device Listener Binding**：ESP 监听 TCP，Bridge 主动连接。连接建立后，
   设备立即发送 HELLO。
2. **Bridge Listener Binding**：Bridge 监听 TCP 或 WebSocket，客户端连接。
   客户端连接后立即发送 HELLO。

无论底层由谁发起 TCP 或 WebSocket 连接，协议角色都固定为：

```text
Device -> HELLO
Bridge -> WELCOME
Device -> AUTH
Bridge -> AUTH_OK
```

Bridge 始终是鉴权权威。TCP 连接方向只影响谁调用 `connect()`，不影响谁发
HELLO、谁校验 AUTH，也不影响消息字段语义。服务端必须在连接建立后设置
`hello_timeout`，默认 10 秒；超时未收到 HELLO 时发送 `ERROR(UNAUTHORIZED)`
并关闭连接。

## 4. 基础编码

### 4.1 字节序与对齐

- 所有多字节整数使用**大端序**。
- 不进行结构体对齐，不插入填充字节。
- 所有长度均为无符号值。
- 传输层帧总长度必须等于 `header_len + payload_len`。
- 接收方必须拒绝超出本地 `max_frame_bytes` 的帧。

### 4.2 基础类型

| 类型 | 线格式 | 说明 |
|---|---|---|
| `u8` | 1 byte | 无符号整数 |
| `u16` | 2 bytes | 无符号整数 |
| `u32` | 4 bytes | 无符号整数 |
| `u64` | 8 bytes | 无符号整数 |
| `i64` | 8 bytes | 二进制补码 |
| `bool` | 1 byte | `0` 为 false，非零为 true；发送方只发 `0x00` 或 `0x01` |
| `utf8` | 原始 UTF-8 字节 | 不带 NUL 终止符 |
| `bytes` | 原始字节 | 长度由 TLV `len` 给出 |
| `tlv` | 子 TLV 序列 | 长度为所有子 TLV 的总长度 |
| `array` | `u16 count` + count 个 `tlv` | 元素顺序有意义 |
| `bitmap` | `u64` | 能力和传输策略位图；位号从最低有效位 0 开始 |

### 4.3 字符串限制

| 字段 | 最大线长度 | 说明 |
|---|---:|---|
| `device_id` | 64 bytes | 稳定身份，不得因 IP 变化而变化 |
| `thread_id` | 128 bytes | Codex 外部 ID，full profile 可选 |
| `turn_id` | 128 bytes | Codex 外部 ID，full profile 可选 |
| `message_id` | 128 bytes | Codex 外部 ID，full profile 可选 |
| `model` / `name` | 64 bytes | 显示和策略使用 |
| `firmware` | 64 bytes | 设备固件版本 |
| `body` | 受 profile 限制 | 超限必须截断并设置 `TEXT_TRUNCATED` |
| `title` | 256 bytes | 列表摘要使用 |
| `error_message` | 256 bytes | 调试信息，不得包含令牌 |
| `reason` | 256 bytes | 状态变化原因 |

### 4.4 时间

所有时间戳使用 Unix epoch 毫秒，类型为 `i64`。`0` 表示未知或未提供。
设备时间与 Bridge 时间不一致时，Bridge 以自身时间为准写入事件，同时保留
`client_timestamp_ms` 作为诊断字段。

### 4.5 紧凑对象引用

为了支持低内存设备，Bridge 为持久实体分配 32 位对象引用：

```text
ref 范围：1 .. 0x0FFFFFFF
0：保留
0x10000000..0xFFFFFFFF：保留给 topic_id 命名空间，不作为对象 ref
```

`thread_ref`、`turn_ref`、`message_ref`、`approval_ref` 和 `device_ref` 由
Bridge 在投影存储中分配并持久化。设备可以只保存 ref，不保存完整字符串 ID。
full profile 可以同时接收字符串 ID，便于调试和与 Codex 外部工具互操作。

Bridge 数据库重建后可以重新分配 ref。分配变化必须通过 `RESYNC_REQUIRED`
或完整快照通知设备，旧 ref 不得跨 Bridge 数据库代际继续解释。

## 5. 固定帧头

### 5.1 布局

每个 CBDP 帧都有 28 字节固定头：

```text
offset  size  field          类型
0       2     magic          bytes = CB 01
2       1     version_major  u8
3       1     message_type   u8
4       2     flags          u16
6       2     header_len     u16
8       4     payload_len    u32
12      4     topic_id       u32
16      4     seq            u32
20      4     ack_seq        u32
24      2     code           u16
26      1     qos            u8
27      1     reserved       u8
28      ...   header_tlv     TLV block
28+hdr  ...   payload        bytes
```

Python 参考声明：

```python
import struct

HEADER = struct.Struct(">2sBBHHIIIIHBB")
assert HEADER.size == 28
MAGIC = b"\xCB\x01"
```

### 5.2 字段语义

| 字段 | 语义 |
|---|---|
| `magic` | 必须为 `CB 01`，否则不是 CBDP v1 帧 |
| `version_major` | 必须为 `1` |
| `message_type` | 见第 7 章 |
| `flags` | 见第 6 章 |
| `header_len` | 固定头 28 字节加头部 TLV 区的总长度；最小 28 |
| `payload_len` | 固定头和头部 TLV 之后的二进制 payload 长度；可以为 0 |
| `topic_id` | 订阅主题；控制消息使用 `0`，事件按第 15 章的 topic 规则填写 |
| `seq` | 发送方向传输序号；控制帧和 ACK 使用 0 |
| `ack_seq` | 对对端传输序号的累计确认；0 表示没有确认 |
| `code` | `RESULT` 和 `ERROR` 的业务错误码；其他消息为 0 |
| `qos` | 0=`critical`，1=`state`，2=`telemetry` |
| `reserved` | 必须为 `0`；接收方不得依赖其中的位 |

### 5.3 header TLV 与 payload

- `header_tlv` 承载消息字段、策略、订阅参数、事件元数据和小文本。
- `payload` 只承载大块二进制数据。
- `SEND_CHUNK`、`AUDIO_CHUNK` 和 `FRAGMENT` 使用 payload。
- 其他消息必须令 `payload_len = 0`，所有字段放入 header TLV。
- `content.read` 的结果正文放在 payload；其他 `RESULT` 的字段放在 header TLV。
- header TLV 内不允许出现未完整结束的 TLV。

规范编码器 SHOULD 按 tag 升序输出 TLV，以便产生稳定测试向量。接收方 MUST
接受任意 TLV 顺序。

## 6. TLV 与标志

### 6.1 TLV 格式

```text
offset  size  field
0       2     tag
2       2     len
4       len   value
```

`len` 是 value 的字节数，不包含 tag 和 len。

规则：

1. 一个 TLV 块中最多 256 个 TLV。
2. 嵌套 TLV 深度最多 8 层。
3. 未知 tag 必须跳过。
4. 已知 tag 重复出现时，除明确声明为可重复的 tag 外，均视为 `INVALID_FRAME`。
5. `len` 超出剩余字节、TLV 截断或存在尾随垃圾时，视为 `INVALID_FRAME`。
6. 不含子 TLV 的空 TLV 合法；是否允许由具体字段语义决定。
7. 所有枚举值只使用已定义值；未知枚举值必须按最近的保守默认行为处理。

### 6.1.1 容器字段的确定编码

为避免不同端各自发明 `array` 和 `tlv` 的内部布局，本协议统一规定：

| 类型 | 编码 |
|---|---|
| `tlv` | 直接的子 TLV 序列，不额外携带总数 |
| `array` | `u16 count`，随后连续 `count` 个元素；每个元素本身是一个完整 `tlv` 值区 |

`array` 的每个元素使用一个“元素 tag + u16 len + value”包裹。元素 tag 必须
是该数组声明的元素 tag，例如 `questions` 的元素 tag 为 `0x0130`。
解析器不得依赖元素按 tag 升序、不得依赖未声明字段的存在。数组元素中的字段
按字段语义复用第 8 章 tag，不得为同一语义创建临时字符串键。

`tlv` 容器字段本身是容器，不再重复包裹一个同名 tag。例如
`event_payload` 的编码是：

```text
00 38 [len_hi len_lo] <子 TLV 字节>
```

而不是：

```text
00 38 [len] 00 38 [inner_len] ...
```

如果某个字段的 schema 同时允许 `thread_ref` 和 `thread_id`，编码器 MUST
至少发送其中一个；同时发送时必须指向同一对象。接收方必须在两者都出现时
校验一致性。

所有对象型 TLV 的必需字段和可选字段按其章节定义。章节未声明为可选的字段，
在需要该对象的消息中 MUST 存在。章节声明为可选的字段缺失时使用该字段的
默认值；不允许用空字符串、0 或空数组隐式伪造“字段存在”。

### 6.1.2 选择器与 one-of 规则

命令参数中的对象选择器必须满足以下规则：

1. `thread_ref` 与 `thread_id` 最多出现一个；省略时只有命令明确允许
   `use_active_thread` 才使用当前活动线程。两者同时出现时，Bridge 必须验证
   指向同一对象，否则返回 `INVALID_PARAM`。
2. `message_ref` 与 `message_id` 最多出现一个；两者同时出现时必须指向同一
   消息。
3. `approval_ref` 与 `approval_id` 最多出现一个；两者同时出现时必须指向同一
   审批。
4. `turn_ref` 与 `turn_id` 最多出现一个；在省略 `turn_ref` 的命令中，
   由 `thread` 当前活动 turn 解析。
5. `content_ref` 与 `upload_id` 在 `attachment.add` 中必须二选一。
6. `body` 与 `input_parts` 在 `turn.start`、`turn.steer` 和 `queue.add` 中
   必须二选一；`input_parts` 非空时不得同时发送 `body`。
7. 只有声明了 `use_active_thread` 的 `SEND_BEGIN`、`turn.start`、`turn.steer`
   和 `queue.add` 可以省略线程选择器。省略时 `use_active_thread` 必须为
   `true`，并且设备必须已通过 `active_state.read` 得到活动线程。
8. 任何必需选择器缺失、多个互斥选择器同时出现或引用对象不存在时，接收方
   返回 `INVALID_PARAM` 或 `NOT_FOUND`，不得猜测。

### 6.1.3 幂等键规范化

客户端 MUST 为每个逻辑写操作生成稳定的 16 字节 `idem`，并在重试时复用完全
相同的值和完全相同的方法参数。推荐生成方式：

```text
idem = first_16_bytes(SHA-256(
  "CBDP1-IDEM" || device_id || request_id || method_id || canonical_params
))
```

`canonical_params` 的确定性规则：

1. 只编码方法 schema 中声明为参数的 TLV。
2. 对参数 TLV 按 tag 升序排列。
3. 使用 TLV 原始编码，不加入 JSON、空格、调试字段或默认值。
4. 重复参数按 schema 声明的顺序编码。
5. `request_id` 和 `client_timestamp_ms` 不进入 `canonical_params`。

如果客户端无法稳定生成规范化参数，MUST 在重试时复用第一次发送的原始
`idem`；Bridge 以 `(device_id, idem)` 为幂等键，不依赖客户端重新计算。

### 6.2 标志位

| 值 | 名称 | 语义 |
|---:|---|---|
| `0x0001` | `MORE_FRAGMENTS` | 当前帧是传输分片，且不是最后一片 |
| `0x0002` | `COMPRESSED` | `[本轮不实现]` 帧 payload 已压缩；本轮发送方 MUST NOT 设置，接收方收到后返回 `UNSUPPORTED_FEATURE` |
| `0x0004` | `CRC32_PRESENT` | header TLV 中存在 `payload_crc32` |
| `0x0008` | `URGENT` | 高优先级控制事件，应优先于普通状态发送 |
| `0x0010` | `NO_ACK` | 发送方明确不要求传输 ACK；critical 不得设置 |
| `0x0020` | `TEXT_TRUNCATED` | 文本字段已按 profile 截断 |
| `0x0040` | `SNAPSHOT_PAGE` | 当前 `SNAPSHOT` 是多页快照的一页 |
| `0x0080` | `RESUMED` | 当前会话由 cursor 恢复 |
| `0x0100` | `REQUEST` | 当前消息是需要响应的请求 |
| `0x0200` | `RESPONSE` | 当前消息是对请求的响应 |
| `0x0400` | `EVENT` | 当前消息承载事件 |
| `0x0800` | `COMMAND` | 当前消息承载命令 |
| `0x8000` | `VENDOR_EXTENSION` | `[本轮不实现]` 厂商扩展消息或 TLV；必须与 `vendor` 能力同时使用，本轮发送方 MUST NOT 设置 |

未定义位必须按 0 发送。接收方遇到未定义的已设置位时 MUST 忽略该位，除非该位
属于厂商扩展范围但未设置 `VENDOR_EXTENSION`；这种情况下必须返回
`UNSUPPORTED_FEATURE`。所有实现只能设置本节标为 `[本轮实现]` 的 flag；
不得通过未知 flag 改变已有字段语义。

`COMPRESSED` 和 `VENDOR_EXTENSION` 虽然保留稳定位号，但属于
`[本轮不实现]`。发送方 MUST NOT 设置；Bridge 或客户端收到后必须返回
`UNSUPPORTED_FEATURE`，不得把它当成功解析的普通帧处理。

### 6.3 QoS 值

| 值 | 名称 | ACK 要求 | 合并 | 丢弃 |
|---:|---|---|---|---|
| `0` | `critical` | 必须 ACK | 禁止合并 | 禁止丢弃 |
| `1` | `state` | 建议 ACK，可 piggyback | 允许按 topic 合并 | 队列满时只保留最新状态 |
| `2` | `telemetry` | 可 `NO_ACK` | 允许采样和合并 | 允许丢弃 |

审批、最终完成、最终失败、writer 冲突、错误和 resync 必须使用 `critical`。

## 7. 消息类型

### 7.1 类型编号

| 值 | 名称 | 方向 | 说明 |
|---:|---|---|---|
| `0x01` | `HELLO` | 客户端到 Bridge | 会话能力和身份声明 |
| `0x02` | `WELCOME` | Bridge 到客户端 | 会话、nonce、策略和限制 |
| `0x03` | `AUTH` | 客户端到 Bridge | HMAC 或 token 证明 |
| `0x04` | `AUTH_OK` | Bridge 到客户端 | 鉴权完成、生效能力和 ref |
| `0x05` | `PING` | 双向 | 心跳请求 |
| `0x06` | `PONG` | 双向 | 心跳响应 |
| `0x07` | `SUBSCRIBE` | 客户端到 Bridge | 订阅 topic、scope、qos、cursor |
| `0x08` | `UNSUBSCRIBE` | 客户端到 Bridge | 取消订阅 |
| `0x09` | `SNAPSHOT` | Bridge 到客户端 | 当前投影分页快照 |
| `0x0A` | `DELTA` | Bridge 到客户端 | 单个领域事件 |
| `0x0B` | `ACK` | 双向 | 传输累计确认和事件 cursor 确认 |
| `0x0C` | `RESYNC` | 双向 | 请求或要求重新同步 |
| `0x0D` | `COMMAND` | 双向 | 通用命令请求 |
| `0x0E` | `RESULT` | 双向 | 命令或命令流结果 |
| `0x0F` | `ERROR` | 双向 | 协议或业务错误 |
| `0x10` | `SEND_BEGIN` | 客户端到 Bridge | 文本回复事务开始 |
| `0x11` | `SEND_CHUNK` | 客户端到 Bridge | 文本回复分片 |
| `0x12` | `SEND_END` | 客户端到 Bridge | 文本回复事务结束 |
| `0x13` | `AUDIO_BEGIN` | 客户端到 Bridge | 音频回复事务开始 |
| `0x14` | `AUDIO_CHUNK` | 客户端到 Bridge | 音频数据分片 |
| `0x15` | `AUDIO_END` | 客户端到 Bridge | 音频回复事务结束 |
| `0x16` | `APPROVAL_REQUEST` | Bridge 到客户端 | 审批请求 |
| `0x17` | `APPROVAL_RESOLVE` | 客户端到 Bridge | 审批决定 |
| `0x18` | `DEVICE_STATUS` | 双向 | 设备在线、队列和状态 |
| `0x19` | `CAPABILITY_UPDATE` | Bridge 到客户端 | 运行时能力或策略更新 |
| `0x1A` | `CLOSE` | 双向 | 有序关闭会话 |
| `0x1B` | `FRAGMENT` | 双向 | 传输层通用分片 |
| `0x1C` | `DISCOVERY` | 双向 | UDP 发现请求 |
| `0x1D` | `DISCOVERY_REPLY` | 双向 | UDP 发现响应 |
| `0x1E` | `HOME_COUNTERS` | Bridge 到设备 | UDP 单播的首页聚合计数 |
| `0x1F` | `PAIRING` | Bridge 到设备 | 无线配对令牌下发；`[2026-10-24 补充]` |

`0x20` 及以上未分配。所有未分配消息类型都不得被当成已知消息解析。

### 7.2 消息公共字段

除 `PING`、`PONG`、`ACK` 和 `CLOSE` 外，请求类消息 MUST 携带
`request_id`。`HELLO`、`AUTH`、`SUBSCRIBE`、`UNSUBSCRIBE`、`COMMAND` 和
`APPROVAL_RESOLVE` 都属于请求类消息。响应类消息 MUST 回显对应
`request_id`。`APPROVAL_REQUEST` 是 Bridge 发起的推送，不是请求类消息，
不携带 `request_id`，也不要求设备发送通用响应；设备只用 `approval_ref` /
`approval_id` 关联后续的 `APPROVAL_RESOLVE` 请求。

`SEND_*` 和 `AUDIO_*` 事务把 `tx_id` 作为事务请求编号；`RESULT.request_id`
等于 `tx_id`，不需要再携带第二个请求编号。

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x002A` | `request_id` | 请求必填；同一连接内唯一 |
| `0x002B` | `idem` | 写命令必填；16 字节幂等键 |

对于 `SEND_*` 和 `AUDIO_*`，`RESULT.request_id` 等于事务的 `tx_id`。

### 7.3 控制消息字段

以下消息即使字段为空也必须遵守固定 schema；除明确注明外，缺少必填字段时
返回 `INVALID_FRAME` 或 `INVALID_PARAM`。

`PING`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0097` | `ping_id` | 可选；0 表示无关联 ID |
| `0x0098` | `server_time_ms` | 可选；由 Bridge 提供 |

`PONG`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0099` | `echo` | 若 PING 带 `ping_id` 则必填 |
| `0x0098` | `server_time_ms` | 可选 |

`ACK`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0030` | `topic_id` | 带 cursor 时必填 |
| `0x0031` | `scope` | 带 cursor 时必填 |
| `0x0033` | `cursor` | 可选；只有已安全应用事件后才发送 |

`UNSUBSCRIBE`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0030` | `topic_id` | 必填 |
| `0x0031` | `scope` | 必填 |
| `0x002A` | `request_id` | 必填 |

`DEVICE_STATUS`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0090` | `online` | 必填 |
| `0x0091` | `device_state` | 必填 |
| `0x0092` | `queue_depth_current` | 可选，默认 0 |
| `0x0093` | `metrics` | 可选 |
| `0x0094` | `last_seen_ms` | 可选 |

`device_state`：

| 值 | 名称 |
|---:|---|
| `0` | `offline` |
| `1` | `online` |
| `2` | `degraded` |
| `3` | `draining` |

`CAPABILITY_UPDATE`：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0009` | `effective_caps` | 必填 |
| `0x0017` | `policy_version` | 必填 |
| `0x0095` | `applied_policy` | 可选 |
| `0x00AA` | `capability_catalog` | 可选 |
| `0x002A` | `request_id` | 可选；无请求时为 0 |

`FRAGMENT` 的消息字段固定为 14.2 定义；`fragment_index = 0` 缺
`original_header_tlv` 视为 `FRAGMENT_ERROR`。

`PONG.echo` MUST 等于对应 `PING.ping_id`。`PING` 未携带 `ping_id` 时，
`PONG.echo` 可以省略或为 0。

## 8. 全局 TLV 编号

全局 TLV 必须同时满足两项唯一性约束：

1. 一个 tag 只能对应一个字段。
2. 一个字段名只能对应一个 tag。

客户端可以按 tag 或字段名生成唯一 codec 映射；如果同一字段名需要表达不同语义，
必须分配不同的稳定字段名。实现不能覆盖本节的标准字段名。

TLV tag、消息类型、标志位、事件编号和命令方法号属于彼此独立的编号空间。相同
十六进制值在不同编号空间中没有关联，客户端不得尝试复用映射表。

### 8.1 核心身份与会话

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0001` | `u16` | `version_minor` |
| `0x0002` | `utf8` | `device_id` |
| `0x0003` | `utf8` | `model` |
| `0x0004` | `utf8` | `firmware` |
| `0x0005` | `utf8` | `name` |
| `0x0006` | `u8` | `profile` |
| `0x0007` | `bitmap` | `supported_caps` |
| `0x0008` | `bitmap` | `enabled_caps` |
| `0x0009` | `bitmap` | `effective_caps` |
| `0x000A` | `u8` | `transport_security` |
| `0x000B` | `bytes(16)` | `client_nonce` |
| `0x000C` | `bytes(16)` | `session_id` |
| `0x000D` | `bytes(16)` | `server_nonce` |
| `0x000E` | `bytes(32)` | `auth_proof` |
| `0x000F` | `u8` | `auth_mode` |

### 8.2 预算与策略

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0010` | `u32` | `max_frame_bytes` |
| `0x0011` | `u32` | `max_text_chars` |
| `0x0012` | `u16` | `queue_depth` |
| `0x0013` | `u16` | `heartbeat_seconds` |
| `0x0014` | `u16` | `history_depth` |
| `0x0015` | `u16` | `max_subscriptions` |
| `0x0016` | `u32` | `max_audio_bytes` |
| `0x0017` | `u32` | `policy_version` |
| `0x0018` | `bitmap` | `transport_allowed` |
| `0x0019` | `u16` | `snapshot_page_objects` |
| `0x001A` | `u16` | `max_chunk_bytes` |
| `0x001B` | `u16` | `coalesce_window_ms` |
| `0x001C` | `utf8` | `policy_reason` |
| `0x001D` | `u32` | `request_ttl_seconds` |
| `0x001E` | `u32` | `writer_lease_seconds` |
| `0x001F` | `u32` | `max_text_bytes` |

### 8.3 对象引用与关联

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0020` | `u32` | `device_ref` |
| `0x0021` | `u32` | `thread_ref` |
| `0x0022` | `utf8` | `thread_id` |
| `0x0023` | `u32` | `turn_ref` |
| `0x0024` | `utf8` | `turn_id` |
| `0x0025` | `u32` | `message_ref` |
| `0x0026` | `utf8` | `message_id` |
| `0x0027` | `u32` | `approval_ref` |
| `0x0028` | `utf8` | `approval_id` |
| `0x0029` | `bytes(16)` | `event_id` |
| `0x002A` | `u32` | `request_id` |
| `0x002B` | `bytes(16)` | `idem` |
| `0x002C` | `u16` | `method_id` |
| `0x002D` | `u32` | `tx_id` |
| `0x002E` | `u32` | `snapshot_id` |
| `0x002F` | `u32` | `owner_device_ref` |

### 8.4 Topic、事件与同步

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0030` | `u32` | `topic_id` |
| `0x0031` | `u8` | `scope` |
| `0x0032` | `u8` | `qos` |
| `0x0033` | `u64` | `cursor` |
| `0x0034` | `u64` | `topic_seq` |
| `0x0035` | `u64` | `global_seq` |
| `0x0036` | `u64` | `revision` |
| `0x0037` | `u16` | `event_type` |
| `0x0038` | `tlv` | `event_payload` |
| `0x0039` | `u64` | `coalesced_from_seq` |
| `0x003A` | `u16` | `coalesced_count` |
| `0x003B` | `u16` | `event_count` |
| `0x003C` | `u64` | `snapshot_revision` |
| `0x003D` | `u16` | `page_index` |
| `0x003E` | `u16` | `page_count` |
| `0x003F` | `u8` | `object_type` |

`object_type` 使用以下稳定编号。除 `extension` 外，`objects` 数组中每个元素
MUST 是 `object` TLV，且必须在同一对象内携带对应对象字段：

| 值 | 名称 | 必需对象字段 |
|---:|---|---|
| `0` | `thread` | `thread_ref`，可选 `title`, `status`, `revision`, `owner_state`, `updated_at_ms` |
| `1` | `turn` | `turn_ref`, `thread_ref`, `status`, `revision` |
| `2` | `message` | `message_ref`, `thread_ref`, `kind`, `status` |
| `3` | `approval` | `approval_ref`, `thread_ref`, `turn_ref`, `approval_kind`, `prompt`, `options`, `expires_at_ms` |
| `4` | `device` | `device_ref`, `device_id`, `profile`, `online` |
| `5` | `queue_item` | `queue_item_ref`, `thread_ref`, `queue_position`, `queue_state`, `input_parts` |
| `6` | `guidance` | `guidance_ref`, `thread_ref`, `turn_ref`, `guidance_text`, `guidance_version`, `guidance_state` |
| `7` | `attachment` | `attachment_ref`, `thread_ref`, `attachment_kind`, `filename`, `mime_type`, `byte_size`, `content_ref` |
| `8` | `draft` | `draft_ref`, `thread_ref`, `draft_revision` |
| `9` | `model` | `model_name`, `model_capabilities` |
| `10` | `project` | `project_ref`, 可选 `project_id`, `title` |
| `11` | `plan_item` | `plan_item_text`, `plan_item_status` |
| `12` | `mcp_server` | `mcp_server_id`, `mcp_server_status`, `mcp_auth_status` |
| `13` | `search_result` | `thread_ref`, `search_scope`, `search_snippet` |
| `14` | `event` | `event_id`, `global_seq`, `event_type`, `event_payload` |
| `15` | `extension` | `[本轮不实现]` 扩展对象；必须携带 `extension_blob`。未知 `object_type` 仍按 opaque object 跳过 |

`status` 字段按对象类型解释：

| 对象 | 字段 | 值 |
|---|---|---|
| `thread` | `status` | `thread_state` |
| `turn` | `status` | `0=pending,1=running,2=completed,3=failed,4=interrupted` |
| `message` | `status` | `0=pending,1=streaming,2=completed,3=failed,4=interrupted` |
| `approval` | `status` | `0=pending,1=resolved,2=expired,3=cancelled` |
| `attachment` | `status` | `upload_state` |
| `device` | `online` | `0/1`，未知时为 `0` |
| `mcp_server` | 状态字段 | 见 `mcp_server_status` 和 `mcp_auth_status` |
| `queue_item` | `queue_state` | `0=pending,1=starting,2=running,3=completed,4=failed,5=cancelled` |
| `guidance` | `guidance_state` | `0=pending,1=applied,2=cancelled,3=failed` |

`thread_state` 的稳定编号：

| 值 | 名称 |
|---:|---|
| `0` | `idle` |
| `1` | `running` |
| `2` | `completed` |
| `3` | `failed` |
| `4` | `interrupted` |
| `5` | `archived` |
| `6` | `unknown` |

`owner_state` 的稳定编号：

| 值 | 名称 |
|---:|---|
| `0` | `unmanaged` |
| `1` | `observed` |
| `2` | `claimed` |
| `3` | `managed` |

未知 `object_type` 必须按 opaque object 跳过；客户端不得把它当作 thread 或
message 解析。`objects` 数组的元素 tag 固定为 `0x00A0`。

### 8.5 内容与对象

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0040` | `utf8` | `title` |
| `0x0041` | `utf8` | `summary` |
| `0x0042` | `utf8` | `body` |
| `0x0043` | `utf8` | `body_ref` |
| `0x0044` | `u16` | `status` |
| `0x0045` | `u8` | `kind` |
| `0x0046` | `u8` | `content_type` |
| `0x0047` | `u32` | `total_bytes` |
| `0x0048` | `u32` | `offset` |
| `0x0049` | `bytes` | `data` |
| `0x004A` | `u32` | `crc32` |
| `0x004B` | `i64` | `created_at_ms` |
| `0x004C` | `i64` | `updated_at_ms` |
| `0x004D` | `i64` | `started_at_ms` |
| `0x004E` | `i64` | `completed_at_ms` |
| `0x004F` | `utf8` | `current_action` |
| `0x0050` | `tlv` | `token_usage` |
| `0x0051` | `utf8` | `model_name` |
| `0x0052` | `utf8` | `cwd` |
| `0x0053` | `utf8` | `locale` |
| `0x0054` | `i64` | `read_at_ms` |
| `0x0055` | `u8` | `sender` |
| `0x0056` | `utf8` | `truncation_tail` |
| `0x0057` | `utf8` | `source` |
| `0x0058` | `u8` | `content_kind` |
| `0x0059` | `u32` | `next_offset` |
| `0x005A` | `u8` | `content_encoding` |
| `0x005B` | `u32` | `max_bytes` |

### 8.6 命令与结果

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0060` | `tlv` | `params` |
| `0x0061` | `tlv` | `result` |
| `0x0062` | `utf8` | `error_message` |
| `0x0063` | `tlv` | `error_detail` |
| `0x0064` | `u8` | `state` |
| `0x0065` | `i64` | `accepted_at_ms` |
| `0x0066` | `i64` | `command_completed_at_ms` |
| `0x0067` | `u32` | `retry_after_ms` |
| `0x0068` | `u8` | `priority` |
| `0x0069` | `u32` | `deadline_ms` |
| `0x006A` | `bool` | `force` |
| `0x006B` | `u64` | `expected_revision` |
| `0x006C` | `u32` | `lease_generation` |
| `0x006D` | `i64` | `lease_expires_at_ms` |

### 8.7 写入与音频

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0070` | `u8` | `mode` |
| `0x0071` | `u64` | `base_revision` |
| `0x0072` | `i64` | `client_timestamp_ms` |
| `0x0073` | `u8` | `codec` |
| `0x0074` | `u32` | `sample_rate` |
| `0x0075` | `u8` | `channels` |
| `0x0076` | `u8` | `bits_per_sample` |
| `0x0077` | `u32` | `duration_ms` |
| `0x0078` | `utf8` | `transcript` |
| `0x0079` | `bytes(32)` | `sha256` |
| `0x007A` | `tlv` | `audio_format` |
| `0x007B` | `bool` | `use_active_thread` |

### 8.8 审批

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0080` | `utf8` | `prompt` |
| `0x0081` | `array` | `options` |
| `0x0082` | `u8` | `decision` |
| `0x0083` | `tlv` | `response` |
| `0x0084` | `u8` | `risk` |
| `0x0085` | `i64` | `expires_at_ms` |
| `0x0086` | `u8` | `default_decision` |
| `0x0087` | `u8` | `approval_kind` |

### 8.9 设备状态与心跳

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x0090` | `bool` | `online` |
| `0x0091` | `u8` | `device_state` |
| `0x0092` | `u16` | `queue_depth_current` |
| `0x0093` | `tlv` | `metrics` |
| `0x0094` | `i64` | `last_seen_ms` |
| `0x0095` | `tlv` | `applied_policy` |
| `0x0096` | `utf8` | `reason` |
| `0x0097` | `u32` | `ping_id` |
| `0x0098` | `i64` | `server_time_ms` |
| `0x0099` | `u32` | `echo` |
| `0x009A` | `u16` | `load` |
| `0x009B` | `u64` | `memory_used_bytes` |
| `0x009C` | `u64` | `memory_total_bytes` |
| `0x009D` | `u16` | `cpu_permille` |
| `0x009E` | `u64` | `uptime_seconds` |
| `0x009F` | `u16` | `queue_high_watermark` |

### 8.10 快照、分片与发现

| Tag | 类型 | 字段 |
|---:|---|---|
| `0x00A0` | `tlv` | `object` |
| `0x00A1` | `array` | `objects` |
| `0x00A2` | `u16` | `object_count` |
| `0x00A3` | `bool` | `complete` |
| `0x00A4` | `tlv` | `event` |
| `0x00A5` | `u64` | `next_cursor` |
| `0x00A6` | `u8` | `snapshot_kind` |
| `0x00A7` | `u8` | `thread_state` |
| `0x00A8` | `u8` | `owner_state` |
| `0x00A9` | `u8` | `resync_reason` |
| `0x00AA` | `array` | `capability_catalog` |
| `0x00AB` | `u8` | `capability_source` |
| `0x00AC` | `bitmap` | `capability_bits` |
| `0x00B0` | `u16` | `port` |
| `0x00B1` | `bytes(16)` | `discovery_nonce` |
| `0x00B2` | `u8` | `discovery_scope` |
| `0x00B3` | `bytes(16)` | `bridge_instance_id` |
| `0x00B4` | `u8` | `priority_class` |
| `0x00B5` | `bytes` | `capability_blob` |
| `0x00B6` | `u16` | `window_frames` |
| `0x00B7` | `u32` | `window_bytes` |
| `0x00BC` | `u16` | `running_count` | 当前运行中的任务数；`codex.state` 事件使用 |
| `0x00BD` | `u16` | `completed_unread_count` | 全局未读的完成结果数；`codex.state` 事件使用 |
| `0x00BE` | `u16` | `failed_unread_count` | 全局未读的失败结果数；`codex.state` 事件使用 |
| `0x00C0` | `u8` | `pairing_state` |
| `0x00C1` | `utf8` | `pairing_token` |
| `0x00B8` | `u32` | `retransmit_timeout_ms` |
| `0x00B9` | `u8` | `max_retries` |
| `0x00BA` | `u32` | `fragment_timeout_ms` |
| `0x00BB` | `u16` | `ack_delay_ms` |
| `0x00F0` | `u32` | `fragment_stream_id` |
| `0x00F1` | `u16` | `fragment_index` |
| `0x00F2` | `u16` | `fragment_total` |
| `0x00F3` | `u8` | `original_type` |
| `0x00F4` | `u16` | `original_flags` |
| `0x00F5` | `u32` | `original_payload_len` |
| `0x00F6` | `tlv` | `original_header_tlv` |
| `0x00FF` | `u32` | `payload_crc32` |

### 8.11 标准应用扩展 TLV

本节编号属于 CBDP v1 的稳定应用区。应用区编号必须按语义分组分配；新增字段只能
追加到所属分组末尾。任何实现都必须能跳过不认识的应用 TLV，不得因为字段缺失
而改变已有字段语义。

#### 8.11.1 队列、引导与消息修订

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0100` | `u32` | `queue_item_ref` | 队列项稳定引用 |
| `0x0101` | `u32` | `queue_position` | 从 0 开始的可视位置 |
| `0x0102` | `u8` | `queue_state` | `0=pending,1=starting,2=running,3=completed,4=failed,5=cancelled` |
| `0x0103` | `u16` | `queue_size` | 当前队列项总数 |
| `0x0104` | `tlv` | `queue_item` | 单个队列项结构 |
| `0x0105` | `array` | `queue_items` | 有序队列项数组 |
| `0x0106` | `u8` | `queue_source` | `0=device,1=bridge,2=desktop,3=automation` |
| `0x0107` | `u64` | `queue_due_at_ms` | 可选计划启动时间；0 表示立即 |
| `0x0108` | `u32` | `guidance_ref` | 引导条目引用 |
| `0x0109` | `utf8` | `guidance_text` | 注入活跃 turn 的引导文本 |
| `0x010A` | `u64` | `guidance_version` | 引导版本，用于乐观更新 |
| `0x010B` | `u8` | `guidance_state` | `0=pending,1=applied,2=cancelled,3=failed` |
| `0x010C` | `array` | `guidance_items` | 有序引导条目数组 |
| `0x0110` | `u32` | `revision_of_ref` | 被修订对象的引用 |
| `0x0111` | `bool` | `tombstone` | 对象已删除但保留位置 |
| `0x0112` | `u32` | `range_start_ref` | 删除或回退范围起点，含 |
| `0x0113` | `u32` | `range_end_ref` | 删除或回退范围终点，含 |
| `0x0114` | `u32` | `revert_to_ref` | 回退到的目标对象 |
| `0x0115` | `u32` | `branch_ref` | 回退或分叉产生的新分支引用 |
| `0x0116` | `u32` | `replaced_by_ref` | 被新 revision 替代后的引用 |
| `0x0117` | `u8` | `change_origin` | `0=user,1=agent,2=device,3=bridge,4=system` |
| `0x0118` | `utf8` | `change_reason` | 编辑、删除或回退原因 |

`queue_item` 和 `guidance` 条目使用 `tlv`：其子字段复用对象引用、文本、状态、
时间和优先级 tag，不允许为同一语义另造平行字符串字段。

#### 8.11.2 线程生命周期、设置与目标

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0120` | `bool` | `archived` | 线程归档状态 |
| `0x0121` | `bool` | `pinned` | 线程置顶状态 |
| `0x0122` | `u32` | `project_ref` | 项目紧凑引用 |
| `0x0123` | `utf8` | `project_id` | 项目外部 ID，full 可选 |
| `0x0124` | `utf8` | `effort` | 推理档位，例如 low/medium/high |
| `0x0125` | `u8` | `approval_policy` | `0=untrusted,1=on-failure,2=on-request,3=never,4=granular` |
| `0x0126` | `u8` | `approvals_reviewer` | `0=user,1=auto,2=guardian` |
| `0x0127` | `u8` | `sandbox_policy` | `0=read-only,1=workspace-write,2=danger-full-access,3=custom` |
| `0x0128` | `utf8` | `personality` | Codex 人格或交互风格名称 |
| `0x0129` | `utf8` | `goal_text` | 线程目标文本；`[本轮不实现]` |
| `0x012A` | `u8` | `goal_status` | `0=active,1=completed,2=cancelled,3=blocked`；`[本轮不实现]` |
| `0x012B` | `u32` | `goal_ref` | 线程目标稳定引用；`[本轮不实现]` |
| `0x012C` | `u64` | `settings_revision` | 线程设置 revision |
| `0x012D` | `bool` | `ephemeral` | 线程是否临时、不进入普通历史 |
| `0x012E` | `utf8` | `source_kind` | `user`、`automation`、`api` 等来源类别 |
| `0x012F` | `utf8` | `metadata_json` | 有界 JSON 元数据；full 可读取，低内存设备可忽略 |

#### 8.11.3 结构化输入与审批

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0130` | `array` | `questions` | 一组结构化问题 |
| `0x0131` | `utf8` | `question_id` | 问题 ID |
| `0x0132` | `utf8` | `question_header` | 短标题 |
| `0x0133` | `utf8` | `question_text` | 问题正文 |
| `0x0134` | `bool` | `is_other` | 是否允许自由补充 |
| `0x0135` | `bool` | `is_secret` | 是否敏感输入，不得回显或日志化 |
| `0x0136` | `bool` | `is_blocking` | 是否阻塞 turn 直到回答 |
| `0x0137` | `array` | `question_options` | 问题选项数组 |
| `0x0138` | `array` | `answers` | 回答数组 |
| `0x0139` | `utf8` | `answer_id` | 回答对应的问题 ID |
| `0x013A` | `utf8` | `answer_value` | 选项值或自由文本 |
| `0x013B` | `u8` | `answer_state` | `0=unanswered,1=answered,2=cancelled,3=invalid` |
| `0x013C` | `u32` | `input_ref` | 结构化输入或 server request 引用 |
| `0x013D` | `tlv` | `approval_scope` | 审批影响范围；`[本轮不实现]` |
| `0x013E` | `array` | `permissions` | 权限列表；`[本轮不实现]` |
| `0x013F` | `tlv` | `execpolicy_amendment` | 允许持久化的 exec policy 修订；`[本轮不实现]` |
| `0x0140` | `tlv` | `network_policy_amendment` | 网络策略修订；`[本轮不实现]` |
| `0x0141` | `utf8` | `mcp_action` | MCP action 名称或类型 |
| `0x0142` | `utf8` | `mcp_content` | MCP 返回的文本或结构化内容 |
| `0x0143` | `utf8` | `mcp_server_id` | MCP server ID |
| `0x0144` | `array` | `requested_permissions` | 请求授予的权限集合；`[本轮不实现]` |
| `0x0145` | `utf8` | `approval_title` | 审批主标题，低内存设备优先显示 |
| `0x0146` | `utf8` | `approval_detail` | 审批详细说明 |
| `0x0147` | `bool` | `session_wide` | 基础 `accept_for_session` 的会话范围标记，`[本轮实现]`；作为 `approval_scope` 子字段时 `[本轮不实现]` |
| `0x0148` | `u8` | `scope_kind` | `0=command,1=path,2=domain,3=tool,4=mcp_server,5=thread,6=session`；`[本轮不实现]` |
| `0x0149` | `utf8` | `scope_pattern` | 与 `scope_kind` 对应的匹配模式；空模式表示该范围的默认策略；`[本轮不实现]` |
| `0x014A` | `i64` | `scope_expires_at_ms` | 审批范围过期时间；0 表示不过期；`[本轮不实现]` |
| `0x014B` | `u8` | `permission_kind` | `0=command,1=file_read,2=file_write,3=network,4=mcp_tool,5=other`；`[本轮不实现]` |
| `0x014C` | `utf8` | `permission_value` | 权限的具体值；文本不得包含凭据或秘密；`[本轮不实现]` |
| `0x014D` | `utf8` | `policy_amendment_id` | 策略修订稳定 ID；`[本轮不实现]` |
| `0x014E` | `u32` | `policy_amendment_version` | 策略修订版本，单调递增；`[本轮不实现]` |
| `0x014F` | `array` | `policy_amendment_rules` | 策略修订规则数组；每项为 `rule` 对象；`[本轮不实现]` |

`question_options` 元素为子 TLV，必须包含 `answer_value`，可选
`option_label`。`answers` 元素必须包含 `answer_id`，并按该问题的类型携带
`answer_value`。敏感的 `answer_value` 在日志、事件投影和兼容网关中必须脱敏。

`policy_amendment_rules` 的元素 tag 固定为 `0x014F`，每项是 `rule` TLV：

| rule 子字段 | 必填性 | 说明 |
|---|---|---|
| `scope_kind` | 必填 | 规则作用的范围类型 |
| `permission_kind` | 必填 | 规则授予或调整的权限类型 |
| `scope_pattern` | 可选 | 范围匹配模式 |
| `permission_value` | 可选 | 权限值 |
| `scope_expires_at_ms` | 可选 | 规则过期时间；0 表示不过期 |

`execpolicy_amendment` 和 `network_policy_amendment` 属于 `[本轮不实现]`。它们的所有规则必须使用同一
`rule` 编码。接收方不得把规则解释成自由格式 JSON；`rules` 为空数组时表示该
amendment 只改变策略版本，不新增具体规则。

#### 8.11.4 附件、输入部件与引用

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0150` | `u32` | `attachment_ref` | 附件引用 |
| `0x0151` | `utf8` | `attachment_id` | 附件外部 ID |
| `0x0152` | `u8` | `attachment_kind` | `0=file,1=image,2=audio,3=other` |
| `0x0153` | `utf8` | `filename` | 文件名 |
| `0x0154` | `utf8` | `mime_type` | MIME 类型 |
| `0x0155` | `u32` | `byte_size` | 内容字节数 |
| `0x0156` | `bytes(32)` | `content_sha256` | 内容 SHA-256 |
| `0x0157` | `utf8` | `content_ref` | Bridge 内容存储引用 |
| `0x0158` | `u32` | `upload_id` | 上传事务 ID |
| `0x0159` | `u32` | `upload_offset` | 上传偏移 |
| `0x015A` | `u8` | `upload_state` | `0=pending,1=uploading,2=complete,3=failed,4=cancelled` |
| `0x015B` | `array` | `input_parts` | 有序消息输入部件 |
| `0x015C` | `u8` | `part_kind` | `0=text,1=file_ref,2=image,3=audio,4=tool_result` |
| `0x015D` | `u32` | `part_ref` | 输入部件引用 |
| `0x015E` | `u16` | `image_width` | 图片宽度 |
| `0x015F` | `u16` | `image_height` | 图片高度 |
| `0x0160` | `array` | `references` | 消息引用列表 |
| `0x0161` | `u8` | `reference_kind` | `0=message,1=thread,2=file,3=item,4=goal`；`4` 属于 `[本轮不实现]` |
| `0x0162` | `u32` | `reference_ref` | 被引用对象引用 |
| `0x0163` | `utf8` | `reference_text` | 引用时展示的短文本 |
| `0x0164` | `u32` | `reference_range_start` | 引用范围起点 |
| `0x0165` | `u32` | `reference_range_end` | 引用范围终点 |
| `0x0166` | `utf8` | `alt_text` | 图片或无正文内容替代文本 |
| `0x0167` | `utf8` | `option_label` | 选项显示文本；不得用于承载机器值 |

#### 8.11.5 草稿、批处理与活动状态

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0168` | `u32` | `draft_ref` | 草稿引用 |
| `0x0169` | `u64` | `draft_revision` | 草稿 revision |
| `0x016A` | `utf8` | `draft_text` | 草稿文本 |
| `0x016B` | `array` | `draft_parts` | 草稿输入部件 |
| `0x016C` | `i64` | `draft_expires_at_ms` | 草稿过期时间；0 表示不自动过期 |
| `0x016D` | `array` | `batch_ops` | 批处理操作数组；`[本轮不实现]` |
| `0x016E` | `u16` | `batch_op_index` | 批处理操作序号；`[本轮不实现]` |
| `0x016F` | `u16` | `batch_op_method` | 批处理子操作 `method_id`；`[本轮不实现]` |
| `0x0170` | `u32` | `active_thread_ref` | 当前活动线程 |
| `0x0171` | `u32` | `active_turn_ref` | 当前活动 turn；0 表示空闲 |
| `0x0172` | `u32` | `active_device_ref` | 最后设置活动状态的设备 |
| `0x0173` | `u64` | `active_revision` | 活动状态 revision |
| `0x0174` | `bool` | `resume_required` | 设备是否需要重新获取线程快照 |
| `0x0175` | `u64` | `resume_cursor` | 推荐恢复 cursor |
| `0x0176` | `u32` | `resume_thread_ref` | 恢复目标线程 |
| `0x0177` | `bytes(16)` | `batch_op_idem` | 批处理子操作的幂等键；`[本轮不实现]` |
| `0x0178` | `u32` | `batch_op_request_id` | 批处理子操作请求 ID；`[本轮不实现]` |
| `0x0179` | `utf8` | `model_capability_value` | 模型能力值；按 `model_capability_kind` 解释 |
| `0x017A` | `u32` | `target_request_id` | 要查询的命令 request_id；仅用于命令参数 |

#### 8.11.6 额度、上下文、工具与通知

| Tag | 类型 | 字段 | 说明 |
|---:|---|---|---|
| `0x0180` | `u32` | `account_ref` | 账户紧凑引用 |
| `0x0181` | `utf8` | `account_id` | 账户外部 ID |
| `0x0182` | `utf8` | `plan_type` | 套餐类型 |
| `0x0183` | `array` | `rate_limits` | 额度窗口数组 |
| `0x0184` | `utf8` | `rate_limit_window` | 例如 five_hour、weekly |
| `0x0185` | `u8` | `remaining_percent` | 0..100 的剩余百分比 |
| `0x0186` | `i64` | `reset_at_ms` | 窗口重置时间 |
| `0x0187` | `tlv` | `context_usage` | 上下文使用结构 |
| `0x0188` | `u32` | `context_window_tokens` | 上下文窗口 token 上限 |
| `0x0189` | `u32` | `context_used_tokens` | 当前已用 token |
| `0x018A` | `u8` | `compaction_state` | `0=requested,1=running,2=completed,3=failed`；`[本轮不实现]` |
| `0x018B` | `utf8` | `compaction_reason` | 压缩触发原因或失败原因；`[本轮不实现]` |
| `0x018C` | `utf8` | `compaction_summary` | 压缩结果摘要；`[本轮不实现]` |
| `0x018D` | `u64` | `seven_day_tokens` | 最近 7 日累计 token |
| `0x018E` | `utf8` | `seven_day_as_of` | 7 日统计截止日 |
| `0x018F` | `u8` | `seven_day_window_days` | 统计窗口天数，恒为 7 |
| `0x0190` | `u32` | `item_ref` | 通用 item 引用 |
| `0x0191` | `u8` | `item_type` | `0=command,1=file_change,2=mcp_tool,3=dynamic_tool,4=web_search,5=plan,6=reasoning,7=notice,8=context_compaction`；`8` 属于 `[本轮不实现]` |
| `0x0192` | `u8` | `item_status` | `0=pending,1=running,2=completed,3=failed,4=cancelled` |
| `0x0193` | `utf8` | `tool_name` | 工具或 item 名称 |
| `0x0194` | `utf8` | `tool_call_id` | 工具调用 ID |
| `0x0195` | `utf8` | `tool_input` | 工具输入摘要或序列化文本 |
| `0x0196` | `utf8` | `tool_output` | 工具输出摘要或序列化文本 |
| `0x0197` | `utf8` | `tool_error` | 工具错误摘要 |
| `0x0198` | `u8` | `mcp_server_status` | `0=starting,1=ready,2=degraded,3=auth_required,4=failed` |
| `0x0199` | `u8` | `mcp_auth_status` | `0=unknown,1=not_required,2=required,3=authorized` |
| `0x019A` | `u8` | `notice_kind` | `0=info,1=warning,2=rate_limit,3=system,4=compaction`；`4` 属于 `[本轮不实现]` |
| `0x019B` | `utf8` | `notice_text` | 通知正文 |
| `0x019C` | `u32` | `server_request_ref` | 服务端请求引用 |
| `0x019D` | `u8` | `server_request_state` | `0=pending,1=answered,2=cancelled,3=expired` |
| `0x019E` | `array` | `model_capabilities` | 模型能力声明 |
| `0x019F` | `u8` | `model_capability_kind` | `0=effort,1=vision,2=audio,3=tools,4=mcp` |
| `0x01A0` | `utf8` | `reasoning_summary` | 可见推理摘要 |
| `0x01A1` | `array` | `plan_items` | 计划项数组 |
| `0x01A2` | `utf8` | `plan_item_text` | 单个计划项正文 |
| `0x01A3` | `u8` | `plan_item_status` | `0=pending,1=in_progress,2=completed` |
| `0x01A4` | `u8` | `interrupt_mode` | `0=immediate,1=after_turn`；`[本轮不实现]` |
| `0x01A5` | `bool` | `has_more` | 分页或事件窗口是否还有后续 |
| `0x01A6` | `u32` | `window_id` | 事件窗口 ID |
| `0x01A7` | `array` | `mcp_servers` | MCP server 状态数组 |
| `0x01A8` | `utf8` | `tool_display_name` | 面向用户的工具名称 |
| `0x01A9` | `utf8` | `tool_arguments_summary` | 已脱敏参数摘要 |
| `0x01AA` | `utf8` | `tool_result_summary` | 已脱敏结果摘要 |
| `0x01AB` | `u32` | `referenced_tool_item_ref` | 引用工具结果 |
| `0x01AC` | `bool` | `redacted` | 内容已脱敏或隐藏 |
| `0x01AD` | `utf8` | `redaction_reason` | 脱敏原因 |
| `0x01AE` | `u32` | `token_total` | 本 turn 或线程累计 token |
| `0x01AF` | `u32` | `token_input` | 输入 token |
| `0x01B0` | `u32` | `token_output` | 输出 token |
| `0x01B1` | `u32` | `token_cached` | cached token |
| `0x01B2` | `u32` | `token_reasoning` | reasoning token 摘要值 |
| `0x01B3` | `u16` | `http_status` | 可选上游状态，便于 full 客户端展示 |
| `0x01B4` | `utf8` | `upstream_request_id` | 上游请求 ID，不得包含凭据 |
| `0x01B5` | `bool` | `terminal` | 事件或 item 是否已达终态 |
| `0x01B6` | `i64` | `last_activity_at_ms` | 线程最后活动时间 |
| `0x01B7` | `u16` | `unread_count` | 线程未读数 |
| `0x01B8` | `bool` | `muted` | 线程是否静音 |
| `0x01B9` | `array` | `read_states` | 多设备已读状态数组 |
| `0x01BA` | `u32` | `read_state_device_ref` | 已读状态设备 |
| `0x01BB` | `i64` | `read_state_at_ms` | 已读时间 |
| `0x01BC` | `u16` | `search_result_count` | 搜索结果总数 |
| `0x01BD` | `utf8` | `search_snippet` | 搜索结果摘要 |
| `0x01BE` | `u8` | `search_scope` | `0=title,1=all,2=message,3=file` |
| `0x01BF` | `bytes` | `extension_blob` | `extension` 对象的 opaque 扩展数据；`[本轮不实现]` |

### 8.12 标准对象 schema

本节是设备端实现的权威对象表。所有字段都放在对应容器 TLV 的值区中；除注明
`可选` 外都必须存在。缺省值只在字段明确标注时使用。数组元素是完整 TLV，元素
tag 必须等于该数组的 tag；解析器必须跳过不认识的元素字段。

| 对象 | 编码位置 | 必需字段 | 可选字段 |
|---|---|---|---|
| `token_usage` | `0x0050` `tlv` | `token_total`, `token_input`, `token_output` | `token_cached`, `token_reasoning` |
| `params` | `0x0060` `tlv` | 由方法 schema 决定 | 由方法 schema 决定 |
| `result` | `0x0061` `tlv` | 由方法结果 schema 决定 | `error_message`, `retry_after_ms` |
| `error_detail` | `0x0063` `tlv` | `error_message` | `retry_after_ms`, `reason`, `http_status` |
| `audio_format` | `0x007A` `tlv` | `sample_rate`, `channels`, `bits_per_sample` | `duration_ms` |
| `options` | `0x0081` `array` | 每项 `decision` | 每项 `prompt`, `risk` |
| `response` | `0x0083` `tlv` | 由 `approval_kind` 决定 | 由 `approval_kind` 决定 |
| `metrics` | `0x0093` `tlv` | 无；允许空对象 | `load`, `memory_used_bytes`, `memory_total_bytes`, `cpu_permille`, `uptime_seconds`, `queue_high_watermark` |
| `applied_policy` | `0x0095` `tlv` | `policy_version` | `max_frame_bytes`, `max_text_chars`, `max_text_bytes`, `queue_depth`, `heartbeat_seconds`, `history_depth`, `max_subscriptions`, `max_audio_bytes`, `snapshot_page_objects`, `max_chunk_bytes`, `coalesce_window_ms`, `window_frames`, `window_bytes`, `retransmit_timeout_ms`, `max_retries`, `fragment_timeout_ms`, `ack_delay_ms` |
| `object` | `0x00A0` `tlv` | `object_type` | 按 `object_type` 增加必需字段 |
| `objects` | `0x00A1` `array` | 每项是 `object` 元素 | 无 |
| `event` | `0x00A4` `tlv` | 与 `DELTA` 外层字段相同 | `coalesced_from_seq`, `coalesced_count` |
| `capability_catalog` | `0x00AA` `array` | 每项 `capability_bits`, `capability_source` | 每项 `priority_class` |
| `queue_item` | `0x0104` `tlv` | `queue_item_ref`, `thread_ref`, `queue_position`, `queue_state`, `input_parts`, `queue_source`, `created_at_ms`, `updated_at_ms` | `priority`, `queue_due_at_ms` |
| `guidance_items` | `0x010C` `array` | 每项 `guidance_ref`, `thread_ref`, `turn_ref`, `guidance_text`, `guidance_version`, `guidance_state` | 每项 `change_reason` |
| `questions` | `0x0130` `array` | 每项 `question_id`, `question_header`, `question_text`, `question_options`, `is_other`, `is_secret`, `is_blocking` | 无 |
| `question_options` | `0x0137` `array` | 每项 `answer_value` | 每项 `option_label` |
| `answers` | `0x0138` `array` | 每项 `answer_id` | 每项 `answer_value`, `answer_state` |
| `approval_scope` | `0x013D` `tlv` | `scope_kind` | `scope_pattern`, `scope_expires_at_ms`, `session_wide`；整个对象 `[本轮不实现]` |
| `permissions` | `0x013E` `array` | 每项 `permission_kind` | 每项 `permission_value`；整个数组 `[本轮不实现]` |
| `execpolicy_amendment` | `0x013F` `tlv` | `policy_amendment_id`, `policy_amendment_version`, `policy_amendment_rules` | `scope_expires_at_ms`；整个对象 `[本轮不实现]` |
| `network_policy_amendment` | `0x0140` `tlv` | `policy_amendment_id`, `policy_amendment_version`, `policy_amendment_rules` | `scope_expires_at_ms`；整个对象 `[本轮不实现]` |
| `requested_permissions` | `0x0144` `array` | 每项 `permission_kind` | 每项 `permission_value`；整个数组 `[本轮不实现]` |
| `input_parts` | `0x015B` `array` | 每项 `part_kind`, `part_ref` | 每项 `body`, `content_ref`, `filename`, `mime_type`, `alt_text`, `attachment_ref`, `image_width`, `image_height` |
| `references` | `0x0160` `array` | 每项 `reference_kind`, `reference_ref` | 每项 `reference_text`, `reference_range_start`, `reference_range_end` |
| `draft_parts` | `0x016B` `array` | 每项 `part_kind`, `part_ref` | 每项 `body`, `content_ref`, `reference_kind`, `reference_ref` |
| `rate_limits` | `0x0183` `array` | 每项 `rate_limit_window`, `remaining_percent`, `reset_at_ms` | 无 |
| `context_usage` | `0x0187` `tlv` | `context_window_tokens`, `context_used_tokens` | `token_usage` |
| `model_capabilities` | `0x019E` `array` | 每项 `model_capability_kind` | 每项 `model_capability_value` |
| `plan_items` | `0x01A1` `array` | 每项 `plan_item_text`, `plan_item_status` | 无 |
| `mcp_servers` | `0x01A7` `array` | 每项 `mcp_server_id`, `mcp_server_status`, `mcp_auth_status` | 每项 `reason` |
| `read_states` | `0x01B9` `array` | 每项 `read_state_device_ref` | 每项 `read_state_at_ms` |

`array` 的空数组编码为 `00 00`。`tlv` 对象允许未知可选子字段，但不得缺少
必需字段。对象字段类型必须与第 8 章全局表一致；同一 tag 在同一对象中重复时
视为 `INVALID_FRAME`。

`policy_amendment_rules` 是数组，元素 tag 固定为 `0x014F`，并按本节的 `rule`
schema 编码。每个 `rule` 至少包含 `scope_kind` 和 `permission_kind`。
未知规则子字段必须跳过，规则数组不得用 JSON 字符串替代。

## 9. Profile、能力与策略

### 9.1 Profile 编号

| 值 | Profile | 典型设备 | 默认行为 |
|---:|---|---|---|
| `0` | `micro` | ESP32，无 PSRAM 或极小内存 | 摘要、短文本、低频 live、无历史 |
| `1` | `panel` | ESP32-S3 带屏设备 | 摘要、有限消息、审批、控制和音频 |
| `2` | `full` | 手机、桌面、浏览器 | 分页历史、完整事件、完整控制面 |

设备在 HELLO 中声明 profile。Bridge 可以按设备策略覆盖 profile，但必须通过
WELCOME 或 CAPABILITY_UPDATE 把最终生效值通知设备。profile 不是权限，设备
不能通过修改 HELLO 获得超出本机策略的能力。

### 9.2 能力位图

`supported_caps`、`enabled_caps`、`effective_caps`、`transport_allowed` 和
`capability_bits` 使用 8 字节 `bitmap`。位号 `0` 是最低有效位。未定义位必须
忽略，不得导致握手失败。

| 位 | 名称 | 说明 | 状态 |
|---:|---|---|---|
| `0` | `status` | 接收简要状态和设备心跳相关事件 | `[本轮实现]` |
| `1` | `text` | 接收文本消息摘要或正文 | `[本轮实现]` |
| `2` | `tasks` | 列出线程、读取线程和任务状态 | `[本轮实现]` |
| `3` | `results` | 接收最终结果、失败和完成事件 | `[本轮实现]` |
| `4` | `approval` | 接收审批请求并提交决定 | `[本轮实现]` |
| `5` | `audio` | 上传音频数据 | `[本轮实现]` |
| `6` | `voice` | 发起语音命令或语音回复 | `[本轮实现]` |
| `7` | `read_ack` | 上报消息已读 | `[本轮实现]` |
| `8` | `control` | 发起控制命令，包括新建线程和中断 turn | `[本轮实现]` |
| `9` | `history` | 读取历史消息和分页快照 | `[本轮实现]` |
| `10` | `telemetry` | 接收可丢弃的遥测事件 | `[本轮实现]` |
| `11` | `subscribe_live` | 建立 live 订阅 | `[本轮实现]` |
| `12` | `subscribe_thread` | 订阅单个线程详情 | `[本轮实现]` |
| `13` | `subscribe_summary` | 订阅线程摘要列表 | `[本轮实现]` |
| `14` | `writer_lease` | 请求和释放线程 writer lease | `[本轮实现]` |
| `15` | `multi_page` | 支持多页快照 | `[本轮实现]` |
| `16` | `compression` | 支持帧级压缩 | `[本轮不实现]` |
| `17` | `tls` | 支持 TLS 传输 | `[本轮实现]` |
| `18` | `aead_reserved` | 预留帧级 AEAD | `[本轮不实现]` |
| `19` | `device_config` | 接收设备策略和配置更新 | `[本轮实现]` |
| `20` | `command_status` | 查询长时间运行命令的状态 | `[本轮实现]` |
| `21` | `queue` | 在会话内提交多条待处理消息 | `[本轮实现]` |
| `22` | `queue_manage` | 查看、取消、重排和提升队列项 | `[本轮实现]` |
| `23` | `guidance` | 向运行中的 turn 注入引导而不产生用户消息 | `[本轮实现]` |
| `24` | `message_edit` | 编辑已提交的用户消息并触发回退 | `[本轮实现]` |
| `25` | `message_delete` | 删除消息，可按范围级联 | `[本轮实现]` |
| `26` | `thread_manage` | 重命名、归档、置顶、分叉和删除线程 | `[本轮实现]` |
| `27` | `structured_input` | 回答包含多个问题和多字段的结构化输入 | `[本轮实现]` |
| `28` | `attachment` | 上传附件引用或二进制附件 | `[本轮实现]` |
| `29` | `draft` | 同步设备未发送的草案和编辑状态 | `[本轮实现]` |
| `30` | `batch` | 同会话原子批处理多个命令 | `[本轮不实现]` |
| `31` | `vendor` | 使用厂商扩展号和扩展语义 | `[本轮不实现]` |
| `32` | `thread_search` | 全文或标题搜索线程 | `[本轮实现]` |
| `33` | `thread_lifecycle` | 归档、取消归档、恢复、删除和分叉线程 | `[本轮实现]` |
| `34` | `thread_fork` | 从指定消息或 turn 分叉新线程 | `[本轮实现]` |
| `35` | `thread_settings` | 读取和修改模型、推理档位、cwd 与线程元数据 | `[本轮实现]` |
| `36` | `message_revision` | 编辑消息、回退分支和 revision 冲突处理 | `[本轮实现]` |
| `37` | `item_stream` | 接收命令、文件变更、计划和推理摘要等 item 事件 | `[本轮实现]` |
| `38` | `plan` | 接收和更新计划项 | `[本轮实现]` |
| `39` | `reasoning_summary` | 接收模型推理摘要，不接收隐藏推理 | `[本轮实现]` |
| `40` | `tool_events` | 接收工具调用、进度、结果和失败事件 | `[本轮实现]` |
| `41` | `mcp` | 读取 MCP 状态、响应 MCP action 和 elicitation | `[本轮实现]` |
| `42` | `account` | 读取账户、套餐和额度状态 | `[本轮实现]` |
| `43` | `context_usage` | 读取上下文窗口和 token 使用量 | `[本轮实现]` |
| `44` | `context_compact` | 请求上下文压缩并接收压缩状态 | `[本轮不实现]` |
| `45` | `read_unread` | 读取、上报和跨设备同步已读/未读 | `[本轮实现]` |
| `46` | `project_manage` | 读取项目和线程项目归属 | `[本轮实现]` |
| `47` | `notice` | 接收系统提示、压缩提示和上游通知 | `[本轮实现]` |
| `48` | `server_request` | 接收需要用户回答的服务端请求 | `[本轮实现]` |
| `49` | `input_capabilities` | 读取设备支持的输入部件和交互能力 | `[本轮实现]` |
| `50` | `queue_priority` | 队列项优先级和提升操作 | `[本轮实现]` |
| `51` | `message_reference` | 引用消息、文件、线程或工具结果发消息 | `[本轮实现]` |
| `52` | `model_switch` | 在 turn 级切换模型，不修改线程默认设置 | `[本轮实现]` |
| `53` | `interrupt_mode` | 选择立即中断或完成后中断 | `[本轮不实现]` |
| `54` | `cross_device_active` | 同步多设备当前活动线程和活动 turn | `[本轮实现]` |
| `55` | `resume` | 断线后从设备 checkpoint 恢复线程和 turn 视图 | `[本轮实现]` |
| `56` | `realtime_reserved` | 预留实时媒体能力 | `[本轮不实现]` |
| `57` | `file_reference` | 使用文件引用而非内联正文 | `[本轮实现]` |
| `58` | `image_input` | 上传或引用图片输入 | `[本轮实现]` |
| `59` | `audio_input` | 上传或引用音频输入 | `[本轮实现]` |
| `60` | `structured_approval` | 提交带 scope、权限或 policy amendment 的结构化审批 | `[本轮不实现]` |
| `61` | `events_since` | 按 topic/cursor 批量拉取事件窗口 | `[本轮实现]` |
| `62` | `thread_goal` | 读取、设置和清除线程目标 | `[本轮不实现]` |
| `63` | `experimental_ext` | 使用实验性标准扩展号或实验语义 | `[本轮不实现]` |

**两套词表的统一（v1.0 实现修正）：** Bridge 的设备注册表同时保存旧 JSON
设备与 CBDP 设备，因此 `supported`/`enabled` 必须使用本表 9.2 的 CBDP 名称。
旧固件仍在使用的 `quota` 是 CBDP 位 `42`（`account`）的别名，Bridge 必须按别名
映射，不能把它当作未知能力丢弃。对 `protocol = cbdp` 的设备，首次登记的
`enabled` 默认取设备声明的完整集合（再减去 1.1 的 `[本轮不实现]` 能力），不得
再用旧型号的 legacy 默认集合裁剪；否则 `subscribe_*`、`multi_page`、
`device_config` 等位会在 AUTH_OK 之前被剪掉，设备永远无法建立订阅。

### 9.3 能力求交

最终能力必须由三方求交：

```text
effective = device.supported
          ∩ bridge.policy.enabled
          ∩ transport.allowed
```

规则：

1. 设备只声明自己固件确实实现的能力。
2. Bridge 在管理页面保存每台设备的 enabled 集合。
3. transport allowed 由连接安全级别、网络类型和连接方向决定。
4. 设备不能通过重复 HELLO、AUTH 或 CAPABILITY_UPDATE 提升 effective。
5. 设备发送未生效能力的消息时，Bridge 返回 `CAPABILITY_NOT_ENABLED`。
6. Bridge 不向设备发送未生效能力的事件或命令。

### 9.4 默认 profile 预算

| 项目 | `micro` | `panel` | `full` |
|---|---:|---:|---:|
| `max_frame_bytes` | 256 | 1024 | 65536 |
| `max_text_chars` | 160 | 512 | 不限，按分页或流式 |
| `max_text_bytes` | 480 | 1536 | 不限制 |
| `history_depth` | 0 | 2 | 按分页请求 |
| `queue_depth` | 8 | 32 | 256 |
| `heartbeat_seconds` | 30 | 15 | 10 |
| `max_subscriptions` | 2 | 4 | 32 |
| `snapshot_page_objects` | 0 | 2 | 50 |
| `max_chunk_bytes` | 96 | 384 | 16384 |
| `coalesce_window_ms` | 300 | 150 | 100 |
| `window_frames` | 3 | 8 | 64 |
| `window_bytes` | 768 | 8192 | 1048576 |

这些值是默认模板，管理页面可以按设备覆盖。覆盖值必须在 `WELCOME` 的
`applied_policy` 或 `CAPABILITY_UPDATE` 中返回。设备必须使用 Bridge 返回的
预算，不得仅按 profile 猜测。

### 9.5 能力目录

`capability_catalog` 是可选的 TLV 数组，每项包含：

```text
capability_bits    bitmap (u64)
capability_source  u8   0=device, 1=bridge-policy, 2=transport
priority_class     u8
```

设备首次接入、策略变化和调试时必须能够请求或接收最新目录。目录只描述能力，
不授予能力。

目录元素固定使用 tag `0x00AA`，编码为：

```text
00 AA 00 len
  [00 AC 00 08 capability_bits]
  [00 AB 00 01 capability_source]
  [00 B4 00 01 priority_class]      可选
```

`capability_bits` 是 8 字节大端位图。`capability_source` 只表示该行来自设备、
策略还是 transport，不改变 `effective_caps` 的求交结果。

## 10. UDP 发现

### 10.1 请求与响应

发现使用固定头加 TLV，不使用 JSON：

```text
DISCOVERY(0x1C)
  profile
  device_id            可选；设备已配对时提供
  discovery_nonce      必填，16 字节
  discovery_scope       0=link-local, 1=subnet, 2=unicast
  port                  设备 TCP 端口；Bridge 请求时为 Bridge 端口

DISCOVERY_REPLY(0x1D)
  device_id            必填
  model
  firmware
  profile
  supported_caps
  port
  bridge_instance_id   仅 Bridge 回复时提供
  discovery_nonce      必须回显
  transport_security
```

### 10.2 发现规则

- 发现报文最大 1024 字节。
- 收到请求后 MUST 回显 `discovery_nonce`，防止旧响应污染会话。
- 接收方按 `bridge_instance_id + device_id` 去重。
- 不匹配的 nonce 必须忽略。
- 发现报文不是会话鉴权，不得替代 AUTH。
- 设备切换 IP 后必须继续使用相同 `device_id`。

## 11. 握手与鉴权

### 11.1 握手顺序

```text
Device -> HELLO
Bridge -> WELCOME
[HELLO 携带 pairing_state=request 时：Bridge -> PAIRING]
Device -> AUTH
Bridge -> AUTH_OK
双方 -> PING / PONG
```

任何一步超过超时、消息类型不匹配或字段缺失，接收方发送 `ERROR` 并关闭连接。
错误响应可以是最后一帧，发送完成后允许半关闭。

### 11.2 HELLO

`HELLO` flags 必须包含 `REQUEST`。必填字段：

| Tag | 字段 |
|---|---|
| `0x0001` | `version_minor` |
| `0x0002` | `device_id` |
| `0x0003` | `model` |
| `0x0004` | `firmware` |
| `0x0006` | `profile` |
| `0x0007` | `supported_caps` |
| `0x000A` | `transport_security` |
| `0x002A` | `request_id` |

可选字段：`name`、`client_nonce`、`capability_blob`、`locale`、
`policy_version`。

设备 MUST 在建立会话后 10 秒内发送 HELLO。`client_nonce` 如果提供，
必须为 16 字节随机值，并不得在设备生命周期内复用。

### 11.3 WELCOME

`WELCOME` flags 必须包含 `RESPONSE`。必填字段：

| Tag | 字段 |
|---|---|
| `0x0001` | `version_minor` |
| `0x000C` | `session_id` |
| `0x000D` | `server_nonce` |
| `0x000F` | `auth_mode` |
| `0x0010` | `max_frame_bytes` |
| `0x0011` | `max_text_chars` |
| `0x001F` | `max_text_bytes` |
| `0x0012` | `queue_depth` |
| `0x0013` | `heartbeat_seconds` |
| `0x001A` | `max_chunk_bytes` |
| `0x001E` | `writer_lease_seconds` |
| `0x002A` | `request_id` |
| `0x0098` | `server_time_ms` |
| `0x00C0` | `pairing_state`（仅无线配对请求，可选） |

可选字段：`profile`、`enabled_caps`、`transport_allowed`、`policy_version`、
`applied_policy`、`window_frames`、`window_bytes`、`retransmit_timeout_ms`、
`max_retries`、`fragment_timeout_ms` 和 `ack_delay_ms`。

`session_id` 和 `server_nonce` 必须每次会话重新生成。`session_id` 长度为
16 字节。Bridge 必须拒绝把上一会话的 AUTH 重放到新会话。

### 11.4 AUTH

`AUTH` flags 必须包含 `REQUEST`。必填字段：

| Tag | 字段 |
|---|---|
| `0x000C` | `session_id` |
| `0x000E` | `auth_proof` |
| `0x000F` | `auth_mode` |
| `0x002A` | `request_id` |

`auth_mode`：

| 值 | 名称 | 说明 |
|---:|---|---|
| `0` | `pairing_token` | 兼容或调试；只允许受控局域网策略 |
| `1` | `hmac_sha256` | 推荐；用于 micro、panel 和 full |
| `2` | `mtls` | full profile 可选 |

`pairing_token` 模式下 `auth_proof` 固定为 32 字节：

```text
auth_proof = SHA-256(
    "CBDP1-TOKEN" || pairing_token || session_id
    || client_nonce || server_nonce || device_id
)
```

该模式仅允许显式调试策略使用。`mtls` 模式下 `auth_proof` 仍必须携带 32
字节值：设备发送 `SHA-256(client_certificate)` 的摘要；Bridge 必须在 TLS 层
验证客户端证书，并校验该摘要与证书匹配。
未启用对应模式的 Bridge 必须返回 `UNSUPPORTED_FEATURE` 并关闭连接。

### 11.5 HMAC 证明

推荐模式使用 HMAC-SHA256：

```text
auth_proof = HMAC-SHA256(
    key   = pairing_token,
    data  = "CBDP1-AUTH"
          || session_id
          || client_nonce
          || server_nonce
          || device_id
)
```

字段编码固定如下：

- `session_id`、`client_nonce`、`server_nonce`：原始 16 字节。
- `device_id`：UTF-8 原始字节，不加长度前缀。
- 固定前缀：ASCII 字节 `CBDP1-AUTH`。

如果 HELLO 未提供 `client_nonce`，使用 16 个 `0x00` 字节。AUTH 必须携带
完整 32 字节 `auth_proof`。比较时使用常量时间比较。

### 11.6 AUTH_OK

Bridge 在 AUTH 通过后发送 `AUTH_OK`，flags 包含 `RESPONSE`。必填字段：

| Tag | 字段 |
|---|---|
| `0x0002` | `device_id` |
| `0x0009` | `effective_caps` |
| `0x0017` | `policy_version` |
| `0x0020` | `device_ref` |
| `0x002A` | `request_id` |

可选字段：`profile`、`enabled_caps`、`applied_policy`、`capability_catalog`、
`lease_generation` 和 `owner_device_ref`。

只有收到 AUTH_OK 后，会话才进入 `READY`。AUTH_OK 之前，除了 HELLO、
WELCOME、PAIRING、AUTH、ERROR、PING 和 PONG，其他消息都必须拒绝。

### 11.7 Token 与密钥

- `pairing_token` 至少 16 字节，建议 32 字节随机值。
- 设备只保存令牌摘要或安全存储中的令牌，不在日志中输出。
- Bridge 必须支持按设备撤销和轮换令牌。
- 令牌轮换可以要求重连；旧令牌不得继续用于 HMAC。
- `plain_lan_debug` 只能对回环地址、测试设备或显式打开的调试策略生效。

### 11.8 无线配对（2026-10-24 协议补充）

> 本节是 CBDP v1 原始规范落地后的补充。旧设备不发送 `pairing_state` 时，
> HELLO/WELCOME/AUTH 流程保持完全不变。新增此流程是为了让没有 USB、无法使用
> `register_offer` 的设备也能在本地管理员批准后取得配对令牌。

没有本地令牌的设备在连接后发送的 `HELLO` 中携带
`pairing_state = PAIRING_STATE_REQUEST(1)`。Bridge 仍先返回 `WELCOME`，随后按
本机审批状态二选一：

```text
Device -> HELLO(pairing_state=request)
Bridge -> WELCOME

已批准且已有令牌：
Bridge -> PAIRING(pairing_state=offer, pairing_token=<16..64 bytes>)
Device -> AUTH(标准 HMAC pairing_token)

尚未批准或没有令牌：
Bridge -> ERROR(POLICY_DENIED, PAIRING_PENDING, reason=pairing_pending)
```

规则：

1. `pairing_state` 缺省值为 `PAIRING_STATE_NONE(0)`；HELLO 只接受 `0` 或
   `PAIRING_STATE_REQUEST(1)`。其他值返回 `INVALID_PARAM`。
2. `PAIRING(0x1F)` 在 v1 中只有 Bridge 到设备方向。flags MUST 包含
   `RESPONSE`，`request_id` 必须回显触发它的 HELLO `request_id`。
3. PAIRING header MUST 包含 `pairing_state = PAIRING_STATE_OFFER(2)` 和
   `pairing_token`。令牌按 UTF-8 编码后长度为 16..64 字节。
4. 设备收到 OFFER 后必须把令牌保存到安全存储或 NVS，然后使用标准 AUTH 登录；
   不得把令牌写入普通日志、遥测或调试输出。无线配对不建立第二套鉴权算法。
5. Bridge 只有在设备已被本地管理员批准并且注册表保存了令牌时才能返回 OFFER。
   未批准时不得先下发临时令牌，也不得把“待批准”伪装成鉴权成功。
   在 Device Listener Binding 下，未批准设备不会被 Bridge 拨号，因此设备只会在
   UDP presence 后停留在管理页“待批准”；`PAIRING_PENDING` 只适用于已经建立
   CBDP 连接、但注册表尚未持有可用令牌的场景。
6. 收到 `PAIRING_PENDING` 的设备应关闭当前会话并按指数退避重新 HELLO；管理员
   批准后，下一次 HELLO 会取得令牌。设备已经持有有效令牌时 SHOULD 省略
   `pairing_state` 并直接走标准 AUTH。
7. 令牌撤销或轮换后，旧令牌的 AUTH 必须失败；设备清除旧令牌后重新发送
   `pairing_state=request`。明文局域网上的首次令牌下发依赖本地管理员显式批准，
   启用 TLS 时可获得传输层保护。
8. `PAIRING` 不改变能力位图、订阅、命令、事件或错误码编号；未实现此消息的旧
   设备不会发送 `pairing_state`，因此保持兼容。

设备端可直接照做的实现清单、代码位置、字节示例和验收用例见
[CBDP `PAIRING(0x1F)` 设备端接入说明](CBDP-0x1F-pairing-device-handoff-2026-10-24.md)。

## 12. 会话状态机

```text
DISCONNECTED
  -> CONNECTED
  -> HELLO_RECEIVED
  -> AUTHENTICATED
  -> READY
  -> STALE
  -> READY
  -> CLOSING
  -> DISCONNECTED
```

状态定义：

| 状态 | 允许动作 |
|---|---|
| `CONNECTED` | 收发 HELLO、PING、ERROR |
| `HELLO_RECEIVED` | 只等待 AUTH |
| `AUTHENTICATED` | 等待 AUTH_OK 确认或进入 READY |
| `READY` | 订阅、命令、事件、审批和状态 |
| `STALE` | 不接收新命令；等待 resync 或重新鉴权 |
| `CLOSING` | 只允许 CLOSE、ERROR 和已排队控制帧 |

超时默认值：

| 阶段 | 超时 |
|---|---:|
| 等待 HELLO | 10 秒 |
| 等待 AUTH | 10 秒 |
| 等待 AUTH_OK | 10 秒 |
| 心跳未响应 | `3 × heartbeat_seconds` |
| writer lease | `writer_lease_seconds` |

## 13. 传输序号、ACK、窗口与重传

### 13.1 双向独立序号

每个方向维护自己的 `seq`：

```text
Device -> Bridge:  device_seq
Bridge -> Device:  bridge_seq
```

首个需要 ACK 的帧 `seq = 1`，之后每次发送可确认帧加 1。`seq` 到达
`0xFFFFFFFF` 后回绕到 1。`PING`、`PONG`、`ACK`、`CLOSE` 和发现消息不占用
传输序号，发送时 `seq = 0`。

设备断线重连后，传输序号必须从 1 重新开始。业务恢复使用 cursor，不使用
传输序号。

Bridge 实现补充（v1.0 实现后修正，2026-10-19）：

- 上述“不占用传输序号”的豁免只覆盖 `PING`、`PONG`、`ACK`、`CLOSE` 和发现消息。
  `RESULT`、`ERROR`、`DELTA`、`WELCOME`、`AUTH_OK` 等其余帧都属于可确认帧，
  `seq` 必须非零并逐帧递增。Bridge 早期版本给 `RESULT`/`ERROR` 发 `seq = 0`
  是缺陷，已修正。
- `ack_seq` 表示发送方已连续收到对方多大的 `seq`。`RESULT`/`ERROR` 可以
  piggyback 当前累计值，用于让对端清除重传窗口；`ack_seq = 0` 只表示“尚未
  确认任何帧”，不表示“无需确认”。协议不强制每个响应帧都带非零 `ack_seq`，
  但发送方 SHOULD 在能确定累计值时携带。

### 13.2 累计确认

`ACK.ack_seq = N` 表示：

1. 已收到 `seq <= N` 的所有帧。
2. `N` 之前的帧不需要重传。
3. 如果有缺口，接收方只确认缺口之前的连续前缀。

`ACK` 的 `seq = 0`。ACK 可以携带 `cursor`，用于确认业务事件已持久消费。

### 13.3 发送窗口

发送方只有在以下条件都满足时才能发送下一帧：

```text
inflight_frames < window_frames
inflight_bytes  < window_bytes
```

`critical` 帧不得因普通 `state` 或 `telemetry` 帧占用窗口而无限等待。
发送队列应按优先级分为 critical、state、telemetry 三级。

### 13.4 重传

- 重传定时器默认 `retransmit_timeout_ms = 1000`。
- 连续重传使用指数退避，最大 8 秒。
- `max_retries` 默认 6。
- 重传必须使用相同 `seq` 和相同字节内容。
- 重复收到同一 `seq` 时，接收方必须丢弃重复帧并再次确认累计前缀。
- 重传耗尽后，发送方发送 `DEVICE_STATUS` 或 `ERROR`，并将会话标记为
  `STALE`。

### 13.5 No-ACK

只有 `telemetry` 可以设置 `NO_ACK`。`state` 可以延迟 ACK 或 piggyback。
`critical` 必须单独确认或被后续 ACK 覆盖。

## 14. 分片与重组

### 14.1 何时分片

单帧超过 transport 或 profile 的 `max_frame_bytes` 时必须分片。不允许依赖
IP 分片，也不允许把单个大帧直接交给 TCP 后认为逻辑消息完整。

### 14.2 FRAGMENT 消息

通用分片使用 `FRAGMENT(0x1B)`：

| Tag | 字段 |
|---|---|
| `0x00F0` | `fragment_stream_id` |
| `0x00F1` | `fragment_index`，从 0 开始 |
| `0x00F2` | `fragment_total` |
| `0x00F3` | `original_type` |
| `0x00F4` | `original_flags` |
| `0x00F5` | `original_payload_len` |
| `0x00F6` | `original_header_tlv` |

分片 payload 放在帧的 `payload` 区。原始消息的 header TLV 必须完整放在
`original_header_tlv` 中；该字段只允许出现在 `fragment_index = 0`。
`FRAGMENT` 自身的 header TLV 只包含分片控制和原始 header，
`header_len` 必须覆盖每片自己的固定 TLV。

规则：

1. `fragment_index < fragment_total`。
2. `fragment_total >= 1`，默认上限 512。
3. 同一 `fragment_stream_id` 只能有一个进行中的重组。
4. 分片可以乱序到达，接收方按 index 重组。
5. 重组总长度不得超过原消息或 profile 限制。
6. 重组超时默认 30 秒，超时丢弃整个流。
7. 最后一片重组完成后，用 `original_type`、`original_flags`、
   `original_header_tlv`、重组后的 payload 和 `original_payload_len` 重建原始帧。
8. 如果设置 `CRC32_PRESENT`，必须校验 `payload_crc32`。
9. 缺少 `original_header_tlv`、index 0 未提供原始头部或原始头部与已定义
   消息类型不一致时，返回 `FRAGMENT_ERROR`。

### 14.3 文本和音频分片

`SEND_CHUNK` 和 `AUDIO_CHUNK` 是应用层事务分片，不替代 `FRAGMENT`。
它们可以被 `FRAGMENT` 再包裹。接收方必须先按消息类型重组应用层事务，再按
`FRAGMENT` 还原传输帧，顺序不得颠倒。

## 15. Topic、订阅、快照与增量

### 15.1 topic_id 布局

`topic_id` 使用 32 位编号，空间被划分为：

```text
0x00000000                 控制主题
0x10000000..0x1FFFFFFF     thread 主题
0x20000000..0x2FFFFFFF     device 主题
0x30000000..0x3FFFFFFF     approval 主题
0x40000000..0x4FFFFFFF     command 主题
0x50000000..0x5FFFFFFF     system 主题
0x60000000..0xFFFFFFFF     未分配
```

控制主题 `0` 用于握手、心跳、能力和错误。`system` 主题承载设备上线、离线和
resync。对象 ref 不得与 topic_id 混用。

系统 topic 固定值：

| topic_id | 名称 | 内容 |
|---:|---|---|
| `0x50000000` | `system.bridge` | Bridge 状态、上游状态和全局公告 |
| `0x50000001` | `system.devices` | 设备上线、离线、策略和负载 |
| `0x50000002` | `system.resync` | 系统级 resync 信号 |
| `0x50000003` | `system.thread_summaries` | 线程摘要列表的持续变化 |

线程列表命令不需要为虚拟列表分配 topic。客户端如果希望持续接收列表变化，
应订阅 system.thread_summaries。Bridge 必须在首次响应中给出该 topic 的稳定
编号。

Bridge 分配 topic_id 时必须保证同一 Bridge 数据库代际内稳定。设备持久化
订阅时可以同时保存 `topic_id` 和对象 ref。数据库代际变化后必须重新快照。

**线程对象必须带 `topic_id`。** `threads.list` / `thread.read` 返回的 thread 对象
（`object_type = 0`）必须携带该线程的稳定 `topic_id`（tag `0x0030`）。面板类
设备没有其他途径得知线程 topic，只能从该字段取得后再发送 `SUBSCRIBE(topic_id,
scope = thread)`；缺失时设备无法订阅线程消息。

### 15.2 scope

| 值 | 名称 | 事件范围 | 历史 |
|---:|---|---|---|
| `0` | `summary` | 线程列表摘要、状态、未读数 | 无 |
| `1` | `thread` | 单个线程的元数据和有限消息 | profile 决定 |
| `2` | `live` | 当前 turn、审批、控制结果 | 按 live 订阅推送 |

一个设备可以同时订阅同一 topic 的多个 scope。Bridge 必须按
`(device_id, topic_id, scope)` 维存储 cursor 和 ACK 水位。

### 15.3 QoS 与合并

事件必须带有效 QoS：

- `critical`：审批、最终完成、失败、writer 冲突、resync 和错误。
- `state`：标题、状态、进度、模型和当前动作。
- `telemetry`：token usage、耗时、队列深度和采样指标。

Bridge 可以合并连续 `state` 事件。合并后必须设置：

```text
coalesced_from_seq
coalesced_count
```

事件语义必须保证“最后状态生效”。合并不得跨越 `critical` 事件。

### 15.4 SUBSCRIBE

`SUBSCRIBE` flags 包含 `REQUEST`，必填字段：

| Tag | 字段 |
|---|---|
| `0x0030` | `topic_id` |
| `0x0031` | `scope` |
| `0x0032` | `qos` |
| `0x002A` | `request_id` |

可选字段：

| Tag | 字段 | 说明 |
|---|---|---|
| `0x0033` | `cursor` | 从该 cursor 之后恢复 |
| `0x0021` | `thread_ref` | summary/cursor 缺失时的对象定位 |
| `0x0022` | `thread_id` | full profile 可选 |
| `0x00A6` | `snapshot_kind` | 请求首次快照类型 |
| `0x0019` | `snapshot_page_objects` | 本设备希望的分页大小 |

成功响应为 `RESULT`，包含 `topic_id`、`scope` 和当前 `cursor`。
重复订阅相同键时，Bridge 必须幂等处理并返回当前 cursor。超出
`max_subscriptions` 时返回 `BUSY`。

订阅是事件投递的硬边界：除 `SUBSCRIBE` 的显式回放结果外，Bridge 不得向设备
发送任何未订阅 `(topic_id, scope)` 的 `DELTA`。`send_event()` 或等价会话发送
入口也必须再次校验订阅，不能只依赖调用方过滤。设备可以据此丢弃未订阅帧，
但 Bridge 不应先占用网络和接收缓冲再让设备丢弃。

省略 `cursor` 等价于 `cursor = 0`，即从该 topic 的历史起点回放全部事件。
只想接收新事件的客户端必须先拿到当前 cursor 再订阅，不能依赖“省略即只推新
事件”。回放可能包含上百条 `DELTA`，Bridge 必须在设备工作线程里发送回放，
不得阻塞该设备的 socket 读取循环；否则正在进行的 `AUDIO_*` / `SEND_*` 上行
会因接收窗口不排空而失败。

`SUBSCRIBE` 必须支持原子“快照加增量”切换。Bridge 处理流程：

```text
读取订阅键当前 revision 和 cursor
  有可恢复 cursor：先注册增量队列，再回放 cursor 后的事件
  无可恢复 cursor：固定 snapshot_revision，注册增量队列，再生成快照
快照传输完成并收到设备 ACK 后，切到纯增量
```

从注册增量队列到快照完成期间，DELTA 必须排队或按 topic 合并，不得在快照
完成前丢弃。快照中的 `snapshot_revision` 和 `next_cursor` 必须来自同一读
事务。设备只有在完整快照提交后才能推进到 `next_cursor`。

### 15.5 UNSUBSCRIBE

`UNSUBSCRIBE` 必须带 `topic_id`、`scope` 和 `request_id`。取消订阅后，Bridge
可以释放 live follow 引用计数，但保留 device checkpoint。取消订阅不删除历史
cursor。

### 15.6 SNAPSHOT

`SNAPSHOT` 从投影存储读取当前状态，用于首次接入或 resync：

```text
SNAPSHOT(
  snapshot_id,
  snapshot_kind,
  snapshot_revision,
  page_index,
  page_count,
  objects[],
  next_cursor,
  complete
)
```

规则：

1. `snapshot_revision` 必须来自同一投影读事务。
2. 多页快照期间 revision 不得变化；如果变化，Bridge 必须重新开始快照。
3. 每页的 `page_index` 从 0 开始。
4. 最后一页设置 `complete = true`，并给出 `next_cursor`。
5. 设备只有在完整接收所有页后才能把快照提交为本地状态。
6. 快照中的对象使用 `thread_ref`、`turn_ref`、`message_ref` 等 ref。
7. 快照不包含正文全文，除非文件明确请求且 profile 允许。

快照对象元素固定使用 `0x00A0`：

```text
object = 00 A0 [len] {
  object_type
  按 8.4 的 object_type 表填入该对象的必需字段
}
```

`threads.list`、`thread.read`、`approvals.list`、`attachment.list` 和
`projects.list` 的 `objects` 也使用同一编码。分页时 `next_cursor` 是继续读取
下一批对象的 opaque cursor，设备不得解释其内部含义。

### 15.7 DELTA

`DELTA` 承载一个已提交事件：

```text
DELTA(
  event_id,
  global_seq,
  topic_id,
  topic_seq,
  revision,
  event_type,
  qos,
  event_payload
)
```

设备按 `topic_id + topic_seq` 去重。`revision` 必须单调递增。收到旧 revision
时必须忽略，收到超出预期的 revision 时必须请求 resync。

**实现要求（不因传输 profile 放宽）：** `event_id`、`global_seq`、`topic_seq`、
`revision` 和真实 `topic_id` 对 `DELTA` 是必填的。`seq` 必须按 13.1 分配非零
传输序号，使该帧可被 ACK/重传覆盖。兼容翻译层只能裁剪业务字段，不得省略上
述事件信封字段，也不得用 `topic_id = 0` 或 `seq = 0` 代替。`topic_id = 0`
仅用于控制主题；事件不得借用它。接收方若收到缺少事件信封的 `DELTA`，应按
`INVALID_FRAME` 处理。

### 15.8 ACK 与 cursor

设备收到并成功持久化或应用事件后发送：

```text
ACK(
  ack_seq = 传输层累计确认,
  topic_id,
  scope,
  cursor = 已持久化的事件 cursor
)
```

`cursor` 表示该设备已经安全处理到的事件位置，不是“看到过”。对于
`telemetry`，可以在内存中应用后立即推进。对于 `critical`，必须先持久化或
提交到设备状态，再推进。

`ACK` 的完整字段表：

| Tag | 字段 | 必填性 |
|---|---|---|
| `0x0030` | `topic_id` | 带 cursor 时必填 |
| `0x0031` | `scope` | 带 cursor 时必填 |
| `0x0033` | `cursor` | 可选；只确认已安全应用的事件 |

`ack_seq` 位于固定头，不放在 TLV。设备可以只 ACK 传输层序号而不推进 cursor。

### 15.9 RESYNC

`RESYNC` 可以由任一侧发起，必填字段：

| Tag | 字段 |
|---|---|
| `0x00A9` | `resync_reason` |
| `0x002A` | `request_id` |

`resync_reason`：

| 值 | 名称 | 语义 |
|---:|---|---|
| `0` | `gap` | 检测到事件序号缺口 |
| `1` | `stale_cursor` | cursor 已被清理 |
| `2` | `revision_mismatch` | 投影 revision 不连续 |
| `3` | `ref_generation` | Bridge 数据库代际变化 |
| `4` | `policy_change` | 传输或能力策略变化 |
| `5` | `manual` | 用户手动触发 |

收到 `RESYNC` 后，设备应发送带 `cursor=0` 或 `snapshot_id=0` 的
`SUBSCRIBE`/`SNAPSHOT` 请求。`RESYNC_REQUIRED` 作为 ERROR 或 RESULT 的
状态返回时，原业务请求不视为失败重试成功。

### 15.10 历史深度

- `micro` 默认 `history_depth = 0`，只接收当前状态和新的关键事件。
- `panel` 默认 `history_depth = 2`，可以保留最近两条消息摘要。
- `full` 通过 `thread` scope 分页读取，不在连接建立时自动下载全部历史。

设备不得因为历史不可用而拒绝接收新消息或审批。

## 16. 事件编号

### 16.1 事件编号分配

事件类型使用 `u16`。Bridge 可以在同 major 内新增事件类型，设备必须忽略未
定义的事件，并在 `ACK` 中推进 cursor。

Thread 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0101` | `thread.created` | `state` | `[本轮实现]` |
| `0x0102` | `thread.updated` | `state` | `[本轮实现]` |
| `0x0103` | `thread.title.changed` | `state` | `[本轮实现]` |
| `0x0104` | `thread.status.changed` | `state` | `[本轮实现]` |
| `0x0105` | `thread.owner.changed` | `critical` | `[本轮实现]` |
| `0x0106` | `thread.deleted` | `critical` | `[本轮实现]` |

Turn 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0201` | `turn.started` | `state` | `[本轮实现]` |
| `0x0202` | `turn.updated` | `state` | `[本轮实现]` |
| `0x0203` | `turn.completed` | `critical` | `[本轮实现]` |
| `0x0204` | `turn.failed` | `critical` | `[本轮实现]` |
| `0x0205` | `turn.interrupted` | `critical` | `[本轮实现]` |
| `0x0206` | `turn.progress` | `telemetry` | `[本轮实现]` |

Message 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0301` | `message.appended` | `critical` | `[本轮实现]` |
| `0x0302` | `message.updated` | `state` | `[本轮实现]` |
| `0x0303` | `message.read` | `state` | `[本轮实现]` |
| `0x0304` | `message.deleted` | `state` | `[本轮实现]` |

Approval 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0401` | `approval.requested` | `critical` | `[本轮实现]` |
| `0x0402` | `approval.resolved` | `critical` | `[本轮实现]` |
| `0x0403` | `approval.expired` | `critical` | `[本轮实现]` |

Command 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0501` | `command.accepted` | `state` | `[本轮实现]` |
| `0x0502` | `command.progress` | `telemetry` | `[本轮实现]` |
| `0x0503` | `command.completed` | `critical` | `[本轮实现]` |
| `0x0504` | `command.failed` | `critical` | `[本轮实现]` |
| `0x0505` | `command.unknown` | `critical` | `[本轮实现]` |

Device 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0601` | `device.online` | `state` | `[本轮实现]` |
| `0x0602` | `device.offline` | `state` | `[本轮实现]` |
| `0x0603` | `device.policy.changed` | `critical` | `[本轮实现]` |
| `0x0604` | `device.writer.changed` | `critical` | `[本轮实现]` |
| `0x0605` | `device.load` | `telemetry` | `[本轮实现]` |

System 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0701` | `system.resync.required` | `critical` | `[本轮实现]` |
| `0x0702` | `system.shutdown` | `critical` | `[本轮实现]` |
| `0x0703` | `system.upstream.changed` | `state` | `[本轮实现]` |
| `0x0704` | `system.codex_state` | `state` | `[本轮实现]` |

Queue 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0801` | `queue.changed` | `state` | `[本轮实现]` |
| `0x0802` | `queue.item.added` | `state` | `[本轮实现]` |
| `0x0803` | `queue.item.updated` | `state` | `[本轮实现]` |
| `0x0804` | `queue.item.started` | `critical` | `[本轮实现]` |
| `0x0805` | `queue.item.completed` | `critical` | `[本轮实现]` |
| `0x0806` | `queue.item.failed` | `critical` | `[本轮实现]` |
| `0x0807` | `queue.item.cancelled` | `state` | `[本轮实现]` |
| `0x0808` | `queue.reordered` | `state` | `[本轮实现]` |

Guidance 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0901` | `guidance.requested` | `state` | `[本轮实现]` |
| `0x0902` | `guidance.updated` | `state` | `[本轮实现]` |
| `0x0903` | `guidance.applied` | `critical` | `[本轮实现]` |
| `0x0904` | `guidance.cancelled` | `state` | `[本轮实现]` |
| `0x0905` | `guidance.failed` | `critical` | `[本轮实现]` |

Revision 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0A01` | `message.revised` | `critical` | `[本轮实现]` |
| `0x0A02` | `message.range.deleted` | `critical` | `[本轮实现]` |
| `0x0A03` | `thread.reverted` | `critical` | `[本轮实现]` |
| `0x0A04` | `thread.branched` | `critical` | `[本轮实现]` |
| `0x0A05` | `tombstone.changed` | `state` | `[本轮实现]` |
| `0x0A06` | `revision.conflict` | `critical` | `[本轮实现]` |

Thread lifecycle / settings 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0B01` | `thread.archived` | `critical` | `[本轮实现]` |
| `0x0B02` | `thread.unarchived` | `critical` | `[本轮实现]` |
| `0x0B03` | `thread.pinned` | `state` | `[本轮实现]` |
| `0x0B04` | `thread.unpinned` | `state` | `[本轮实现]` |
| `0x0B05` | `thread.forked` | `critical` | `[本轮实现]` |
| `0x0B06` | `thread.restored` | `critical` | `[本轮实现]` |
| `0x0B07` | `thread.settings.changed` | `state` | `[本轮实现]` |
| `0x0B08` | `thread.metadata.changed` | `state` | `[本轮实现]` |
| `0x0B09` | `thread.goal.changed` | `state` | `[本轮不实现]` |
| `0x0B0A` | `thread.goal.cleared` | `state` | `[本轮不实现]` |
| `0x0B0B` | `thread.read_state.changed` | `state` | `[本轮实现]` |
| `0x0B0C` | `thread.activity.changed` | `telemetry` | `[本轮实现]` |

Input / tool / MCP / notice 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0C01` | `input.requested` | `critical` | `[本轮实现]` |
| `0x0C02` | `input.answered` | `critical` | `[本轮实现]` |
| `0x0C03` | `input.cancelled` | `critical` | `[本轮实现]` |
| `0x0C04` | `item.started` | `state` | `[本轮实现]` |
| `0x0C05` | `item.updated` | `state` | `[本轮实现]` |
| `0x0C06` | `item.completed` | `critical` | `[本轮实现]` |
| `0x0C07` | `item.failed` | `critical` | `[本轮实现]` |
| `0x0C08` | `tool.progress` | `telemetry` | `[本轮实现]` |
| `0x0C09` | `tool.completed` | `state` | `[本轮实现]` |
| `0x0C0A` | `tool.failed` | `critical` | `[本轮实现]` |
| `0x0C0B` | `mcp.status.changed` | `state` | `[本轮实现]` |
| `0x0C0C` | `mcp.request` | `critical` | `[本轮实现]` |
| `0x0C0D` | `mcp.oauth.completed` | `state` | `[本轮实现]` |
| `0x0C0E` | `notice.created` | `state` | `[本轮实现]` |
| `0x0C0F` | `server.request.created` | `critical` | `[本轮实现]` |
| `0x0C10` | `server.request.resolved` | `critical` | `[本轮实现]` |
| `0x0C11` | `plan.updated` | `state` | `[本轮实现]` |
| `0x0C12` | `reasoning.summary` | `telemetry` | `[本轮实现]` |
| `0x0C13` | `tool.redacted` | `state` | `[本轮实现]` |

Attachment / draft 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0D01` | `attachment.added` | `state` | `[本轮实现]` |
| `0x0D02` | `attachment.updated` | `state` | `[本轮实现]` |
| `0x0D03` | `attachment.removed` | `state` | `[本轮实现]` |
| `0x0D04` | `attachment.upload.state` | `telemetry` | `[本轮实现]` |
| `0x0D05` | `draft.updated` | `state` | `[本轮实现]` |
| `0x0D06` | `draft.deleted` | `state` | `[本轮实现]` |
| `0x0D07` | `draft.expired` | `state` | `[本轮实现]` |

Account / context / sync 事件：

| 值 | 名称 | QoS | 状态 |
|---:|---|---|---|
| `0x0E01` | `account.changed` | `state` | `[本轮实现]` |
| `0x0E02` | `rate_limits.changed` | `state` | `[本轮实现]` |
| `0x0E03` | `token_usage.changed` | `telemetry` | `[本轮实现]` |
| `0x0E04` | `context.usage.changed` | `state` | `[本轮实现]` |
| `0x0E05` | `context.compaction.started` | `state` | `[本轮不实现]` |
| `0x0E06` | `context.compaction.completed` | `critical` | `[本轮不实现]` |
| `0x0E07` | `context.compaction.failed` | `critical` | `[本轮不实现]` |
| `0x0E08` | `active.thread.changed` | `state` | `[本轮实现]` |
| `0x0E09` | `active.turn.changed` | `state` | `[本轮实现]` |
| `0x0E0A` | `events.since.result` | `state` | `[本轮实现]` |
| `0x0E0B` | `resume.required` | `critical` | `[本轮实现]` |
| `0x0E0C` | `search.result` | `state` | `[本轮实现]` |
| `0x0E0D` | `model.catalog.changed` | `state` | `[本轮实现]` |
| `0x0E0E` | `project.catalog.changed` | `state` | `[本轮实现]` |

### 16.2 事件 payload

事件 payload 必须包含事件自身的最小字段。公共字段放外层 DELTA，类型特有字段
放在 `event_payload`。

`thread.created` / `thread.updated`：

```text
thread_ref, title, status, revision, owner_state,
cwd, model_name, created_at_ms, updated_at_ms
```

`thread.title.changed`：

```text
thread_ref, title, revision, updated_at_ms
```

`thread.status.changed`：

```text
thread_ref, status, revision, current_action, updated_at_ms
```

`thread.owner.changed`：

```text
thread_ref, owner_state, owner_device_ref, lease_generation,
lease_expires_at_ms, revision, reason
```

`turn.started` / `turn.updated`：

```text
thread_ref, turn_ref, status, revision, started_at_ms,
current_action, token_usage
```

`turn.completed` / `turn.failed` / `turn.interrupted`：

```text
thread_ref, turn_ref, status, revision, completed_at_ms,
summary, token_usage
```

`message.appended` / `message.updated`：

```text
thread_ref, turn_ref, message_ref, kind, status, sender,
summary, body_ref, created_at_ms, updated_at_ms
```

`message.read`：

```text
thread_ref, message_ref, read_at_ms, device_ref
```

`message.deleted`：

```text
thread_ref, message_ref, tombstone, revision,
range_start_ref, range_end_ref, change_reason
```

`thread.deleted`：

```text
thread_ref, tombstone, revision, change_reason, updated_at_ms
```

`approval.requested`：

```text
approval_ref, thread_ref, turn_ref, approval_kind, prompt,
options, risk, expires_at_ms, default_decision
```

`approval.resolved`：

```text
approval_ref, decision, device_ref, completed_at_ms
```

`command.accepted` / `command.completed` / `command.failed`：

```text
request_id, method_id, state, result, error_message,
accepted_at_ms, command_completed_at_ms
```

`device.online` / `device.offline`：

```text
device_ref, device_id, profile, last_seen_ms
```

`device.policy.changed`：

```text
device_ref, effective_caps, applied_policy, policy_version,
policy_reason, revision
```

`turn.progress`：

```text
thread_ref, turn_ref, status, revision, current_action,
token_usage, updated_at_ms
```

`command.progress` / `command.unknown`：

```text
request_id, method_id, state, result, error_message,
accepted_at_ms, command_completed_at_ms, retry_after_ms
```

`approval.expired`：

```text
approval_ref, thread_ref, turn_ref, decision,
default_decision, completed_at_ms, reason
```

`device.writer.changed` / `device.load`：

```text
device_ref, owner_state, owner_device_ref, lease_generation,
lease_expires_at_ms, load, last_seen_ms, revision
```

`system.resync.required` / `system.shutdown` / `system.upstream.changed`：

```text
resync_reason, revision, server_time_ms, reason,
policy_version, upstream_request_id
```

`system.codex_state`（`0x0704`，2026-10-20 追加）：

```text
running_count, completed_unread_count, failed_unread_count
```

- 这是首页“运行中 / 完成 / 失败”聚合计数，必须在
  `system.thread_summaries`（`0x50000003`）topic 的 `summary` scope 上以
  `state` QoS 下发。设备订阅该 topic + scope 即可收到。
- Bridge 在运行集合或全局未读数变化时发布一条新事件；计数不变时不重复发布。
  设备重连或显式请求状态时，Bridge 直接补发一条携带当前计数的等价事件。
- 三个计数都是 `u16`，字段缺失按 `0` 解释。客户端不得把未知事件类型当作错误，
  应按 §16.1 忽略并在 `ACK` 中推进 cursor。
- 该事件替代 legacy `codex_state` 字典在 CBDP 会话上的作用；legacy 字典仍保留给
  旧 JSON 设备。
- **2026-10-20 后续修正（跨端已读）：** 桌面 Codex 打开任务也会推进共享已读水位。
  Bridge 读取本机 `~/.codex/.codex-global-state.json` 的
  `thread-user-activity-times-v1` 与 `electron-thread-read-state-v1.unreadByIdentity`；
  线程有桌面活动且不在桌面未读集合中、且桌面状态已晚于该线程最新终态结果时，
  视为桌面已读，实体化为
  `message.read` / 线程已读水位，并在 summary 主题发布新的 `system.codex_state`。
  手机和两个 ESP 因此会同时收到已读撤回，`completed_unread_count` /
  `failed_unread_count` 减一并落回空闲，不会各自停在旧计数。
- **2026-10-20 后续语义修正：** `running_count` 统计当前所有未终结 turn 对应的
  `working`、`waiting`、`queued`、`starting` 任务，任一集合变化即发布新事件；
  没有执行中的 turn 时为 `0`。`completed_unread_count` / `failed_unread_count`
  是稳定 `message_id` 去重后的全局未读终态结果数，只在设备已读水位推进时下降。
  Bridge 会把终态结果持久化；进程重启、断线重连、事件重放和周期快照不得把
  计数归零或重复递增。设备以计数相较上一快照“增加”触发一次本地提示音，
  因此同一终态结果不得产生第二个计数增量。

`queue.changed`：

```text
thread_ref, queue_size, revision, queue_items
```

`queue.item.added` / `queue.item.updated`：

```text
thread_ref, queue_item_ref, queue_item, queue_position,
queue_state, revision
```

`queue.item.started` / `queue.item.completed` / `queue.item.failed` /
`queue.item.cancelled`：

```text
thread_ref, queue_item_ref, turn_ref, queue_state,
revision, completed_at_ms, reason
```

`queue.reordered`：

```text
thread_ref, queue_items, queue_size, revision, change_origin
```

`guidance.requested` / `guidance.updated`：

```text
thread_ref, turn_ref, guidance_ref, guidance_text,
guidance_version, guidance_state, revision
```

`guidance.applied` / `guidance.cancelled` / `guidance.failed`：

```text
thread_ref, turn_ref, guidance_ref, guidance_version,
guidance_state, revision, reason
```

`message.revised`：

```text
thread_ref, message_ref, revision_of_ref, replaced_by_ref,
body_ref, summary, expected_revision, revision, change_origin
```

`message.range.deleted` / `tombstone.changed`：

```text
thread_ref, range_start_ref, range_end_ref,
message_ref, tombstone, revision, change_reason
```

`thread.reverted` / `thread.branched`：

```text
thread_ref, revert_to_ref, branch_ref, source,
revision, updated_at_ms
```

`revision.conflict`：

```text
thread_ref, message_ref, expected_revision, revision,
replaced_by_ref, reason
```

`thread.archived` / `thread.unarchived` / `thread.pinned` /
`thread.unpinned` / `thread.restored`：

```text
thread_ref, archived, pinned, revision, updated_at_ms
```

`thread.forked`：

```text
thread_ref, branch_ref, source, project_ref,
model_name, revision, updated_at_ms
```

`thread.settings.changed`：

```text
thread_ref, model_name, effort, cwd, approval_policy,
approvals_reviewer, sandbox_policy, settings_revision, revision
```

`thread.metadata.changed`：

```text
thread_ref, title, metadata_json, settings_revision, revision
```

`thread.goal.changed`：`[本轮不实现]`

```text
thread_ref, goal_ref, goal_text, goal_status, revision, updated_at_ms
```

`thread.goal.cleared`：`[本轮不实现]`

```text
thread_ref, goal_ref, goal_status, revision, updated_at_ms
```

`thread.read_state.changed`：

```text
thread_ref, read_states, read_state_device_ref,
read_state_at_ms, unread_count, revision
```

`thread.activity.changed`：

```text
thread_ref, active_turn_ref, last_activity_at_ms,
revision, updated_at_ms
```

`input.requested`：

```text
input_ref, thread_ref, turn_ref, questions,
is_blocking, expires_at_ms, revision
```

`input.answered` / `input.cancelled`：

```text
input_ref, thread_ref, turn_ref, answers,
answer_state, device_ref, completed_at_ms
```

`item.started` / `item.updated` / `item.completed` / `item.failed`：

```text
thread_ref, turn_ref, item_ref, item_type, item_status,
tool_name, tool_display_name, tool_call_id,
tool_arguments_summary, tool_result_summary, tool_error,
body_ref, redacted, revision, started_at_ms, completed_at_ms
```

`tool.progress` / `tool.completed` / `tool.failed`：

```text
thread_ref, turn_ref, item_ref, tool_name, tool_call_id,
item_status, tool_arguments_summary, tool_result_summary,
tool_error, terminal, revision
```

`mcp.status.changed` / `mcp.oauth.completed`：

```text
device_ref, mcp_server_id, mcp_server_status,
mcp_auth_status, reason, revision
```

`mcp.request`：

```text
approval_ref, thread_ref, turn_ref, mcp_server_id,
mcp_action, mcp_content, requested_permissions [本轮不实现],
expires_at_ms, revision
```

`notice.created`：

```text
thread_ref, notice_kind, notice_text, revision, created_at_ms
```

`server.request.created` / `server.request.resolved`：

```text
server_request_ref, server_request_state, thread_ref, turn_ref,
questions, answers, expires_at_ms, completed_at_ms
```

`plan.updated`：

```text
thread_ref, turn_ref, plan_items, revision
```

`reasoning.summary`：

```text
thread_ref, turn_ref, reasoning_summary,
redacted, redaction_reason, revision
```

`tool.redacted`：

```text
thread_ref, turn_ref, item_ref, redacted,
redaction_reason, revision
```

`attachment.added` / `attachment.updated`：

```text
thread_ref, attachment_ref, attachment_id, attachment_kind,
filename, mime_type, byte_size, content_sha256, content_ref, revision
```

`attachment.removed`：

```text
thread_ref, attachment_ref, attachment_id, revision
```

`attachment.upload.state`：

```text
thread_ref, attachment_ref, upload_id, upload_offset,
upload_state, byte_size, reason
```

`draft.updated` / `draft.deleted` / `draft.expired`：

```text
thread_ref, draft_ref, draft_revision, draft_text,
draft_expires_at_ms, revision, reason
```

`account.changed` / `rate_limits.changed` / `token_usage.changed`：

```text
account_ref, account_id, plan_type, rate_limits,
token_usage, revision
```

这三个事件的具体线约定：

- `account.changed`：携带 `plan_type`。上游未报告套餐时字段缺省，不推断。
- `rate_limits.changed`：`rate_limits` 是数组，每项包含 `rate_limit_window`（`five_hour` / `weekly`）、`remaining_percent` 与可选 `reset_at_ms`。只上报上游实际报告的窗口，缺失的窗口完全缺省，设备显示 `--`。
- `token_usage.changed`：`seven_day_tokens` 为最近 7 天累计 token；`seven_day_window_days` 恒为 7；`seven_day_as_of` 为统计截止日。
- `remaining_percent` 是剩余百分比（0..100），**不是** `usedPercent`；Bridge 在下发前完成换算，设备不得再次取反。
- 上游 `resetsAt` 为 epoch 秒，线上 `reset_at_ms` 为 epoch 毫秒；单位换算只在 Bridge 做一次。
- 三个事件都在 `system.thread_summaries` 的 summary scope 上发布，且仅发给生效能力包含 `account` 的设备。
- 同一份快照只发布一次；数值未变时不重复追加事件。

`context.usage.changed`：

```text
thread_ref, context_usage, context_window_tokens,
context_used_tokens, token_usage, revision
context_used_tokens, token_usage, revision
```

`active.thread.changed` / `active.turn.changed`：

```text
active_thread_ref, active_turn_ref, active_device_ref,
active_revision, resume_required, resume_cursor, resume_thread_ref
```

`events.since.result`：

```text
window_id, has_more, next_cursor, event_count, revision
```

`resume.required`：

```text
resume_thread_ref, resume_cursor, resume_required,
resync_reason, revision
```

`search.result`：

```text
search_scope, search_snippet, search_result_count,
thread_ref, message_ref, revision
```

`model.catalog.changed`：

```text
model_name, model_capabilities, revision
```

`context.compaction.started` / `context.compaction.completed` / `context.compaction.failed`：`[本轮不实现]`

```text
thread_ref, turn_ref, compaction_state, compaction_reason,
compaction_summary, context_window_tokens, context_used_tokens,
token_usage, revision, completed_at_ms
```
`project.catalog.changed`：

```text
project_ref, project_id, revision
```

## 17. 命令协议

### 17.1 命令编号

命令使用 `method_id` 枚举，参数放在 `params` TLV。所有命令都必须支持
`request_id` 和 `idem`。

| 值 | 方法 | 所需能力 | 说明 | 状态 |
|---:|---|---|---|---|
| `0x0001` | `status.read` | `status` | 读取 Bridge 和设备状态 | `[本轮实现]` |
| `0x0002` | `threads.list` | `tasks` | 读取线程摘要列表 | `[本轮实现]` |
| `0x0003` | `thread.read` | `tasks` | 读取线程元数据和有限消息 | `[本轮实现]` |
| `0x0004` | `thread.start` | `control` | 新建并接管线程 | `[本轮实现]` |
| `0x0005` | `turn.start` | `control` | 在空闲线程启动 turn | `[本轮实现]` |
| `0x0006` | `turn.steer` | `control` | 向运行中的 turn 追加输入 | `[本轮实现]` |
| `0x0007` | `turn.interrupt` | `control` | 中断当前 turn | `[本轮实现]` |
| `0x0008` | `approvals.list` | `approval` | 列出待审批项 | `[本轮实现]` |
| `0x0009` | `approval.resolve` | `approval` | 提交审批决定 | `[本轮实现]` |
| `0x000A` | `thread.claim` | `writer_lease` | 显式接管线程 | `[本轮实现]` |
| `0x000B` | `thread.release` | `writer_lease` | 释放 writer lease | `[本轮实现]` |
| `0x000C` | `models.list` | `tasks` | 列出模型 | `[本轮实现]` |
| `0x000D` | `projects.list` | `tasks` | 列出项目 | `[本轮实现]` |
| `0x000E` | `device.policy.read` | `device_config` | 读取本设备生效策略 | `[本轮实现]` |
| `0x000F` | `command.status` | `command_status` | 查询长命令状态 | `[本轮实现]` |
| `0x0010` | `device.config.update` | `device_config` | 更新设备本地策略副本 | `[本轮实现]` |
| `0x0011` | `thread.search` | `thread_search` | 按标题、消息或文件搜索线程 | `[本轮实现]` |
| `0x0012` | `thread.rename` | `thread_manage` | 重命名线程 | `[本轮实现]` |
| `0x0013` | `thread.archive` | `thread_lifecycle` | 归档线程 | `[本轮实现]` |
| `0x0014` | `thread.unarchive` | `thread_lifecycle` | 取消归档 | `[本轮实现]` |
| `0x0015` | `thread.pin` | `thread_manage` | 置顶线程 | `[本轮实现]` |
| `0x0016` | `thread.unpin` | `thread_manage` | 取消置顶 | `[本轮实现]` |
| `0x0017` | `thread.fork` | `thread_fork` | 从线程、turn 或消息分叉 | `[本轮实现]` |
| `0x0018` | `thread.delete` | `thread_lifecycle` | 删除线程 | `[本轮实现]` |
| `0x0019` | `thread.restore` | `thread_lifecycle` | 恢复已删除线程 | `[本轮实现]` |
| `0x001A` | `thread.settings.read` | `thread_settings` | 读取线程设置 | `[本轮实现]` |
| `0x001B` | `thread.settings.update` | `thread_settings` | 修改模型、effort、cwd 等设置 | `[本轮实现]` |
| `0x001C` | `thread.metadata.update` | `thread_settings` | 修改标题、来源和有界元数据 | `[本轮实现]` |
| `0x001D` | `thread.revert` | `message_revision` | 回退线程到指定消息或 turn | `[本轮实现]` |
| `0x001E` | `thread.read_state.update` | `read_unread` | 上报线程已读或未读 | `[本轮实现]` |
| `0x001F` | `thread.goal.get` | `thread_goal` | 读取线程目标； | `[本轮不实现]` |
| `0x0020` | `thread.goal.set` | `thread_goal` | 设置线程目标； | `[本轮不实现]` |
| `0x0021` | `thread.goal.clear` | `thread_goal` | 清除线程目标； | `[本轮不实现]` |
| `0x0030` | `queue.add` | `queue` | 添加待处理消息 | `[本轮实现]` |
| `0x0031` | `queue.list` | `queue` | 列出线程队列 | `[本轮实现]` |
| `0x0032` | `queue.update` | `queue_manage` | 修改队列项文本、附件或计划时间 | `[本轮实现]` |
| `0x0033` | `queue.delete` | `queue_manage` | 取消并删除队列项 | `[本轮实现]` |
| `0x0034` | `queue.reorder` | `queue_manage` | 重排队列项 | `[本轮实现]` |
| `0x0035` | `queue.promote` | `queue_priority` | 提升队列项优先级或位置 | `[本轮实现]` |
| `0x0036` | `queue.start` | `queue_manage` | 立即启动指定队列项 | `[本轮实现]` |
| `0x0040` | `guidance.send` | `guidance` | 向活跃 turn 注入引导 | `[本轮实现]` |
| `0x0041` | `guidance.update` | `guidance` | 修改尚未应用的引导 | `[本轮实现]` |
| `0x0042` | `guidance.cancel` | `guidance` | 取消引导 | `[本轮实现]` |
| `0x0050` | `message.edit` | `message_revision` | 编辑用户消息并生成新 revision | `[本轮实现]` |
| `0x0051` | `message.delete` | `message_delete` | 删除单条消息 | `[本轮实现]` |
| `0x0052` | `message.range.delete` | `message_delete` | 删除消息范围并写入 tombstone | `[本轮实现]` |
| `0x0060` | `attachment.add` | `attachment` | 添加已有内容引用或完成上传的附件 | `[本轮实现]` |
| `0x0061` | `attachment.list` | `attachment` | 列出线程附件 | `[本轮实现]` |
| `0x0062` | `attachment.remove` | `attachment` | 移除线程附件 | `[本轮实现]` |
| `0x0063` | `attachment.upload.begin` | `attachment` | 开始附件上传事务 | `[本轮实现]` |
| `0x0064` | `attachment.upload.chunk` | `attachment` | 上传附件分片 | `[本轮实现]` |
| `0x0065` | `attachment.upload.end` | `attachment` | 完成附件上传 | `[本轮实现]` |
| `0x0066` | `attachment.upload.cancel` | `attachment` | 取消附件上传 | `[本轮实现]` |
| `0x0070` | `draft.update` | `draft` | 写入或更新设备草稿 | `[本轮实现]` |
| `0x0071` | `draft.delete` | `draft` | 删除设备草稿 | `[本轮实现]` |
| `0x0080` | `input.respond` | `structured_input` | 回答多问题结构化输入 | `[本轮实现]` |
| `0x0081` | `tool.respond` | `server_request` | 响应工具或服务端请求 | `[本轮实现]` |
| `0x0082` | `mcp.respond` | `mcp` | 响应 MCP action、content 或 elicitation | `[本轮实现]` |
| `0x0090` | `account.read` | `account` | 读取账户和套餐 | `[本轮实现]` |
| `0x0091` | `account.rate_limits.read` | `account` | 读取额度窗口 | `[本轮实现]` |
| `0x0092` | `account.token_usage.read` | `account` | 读取 token 使用量 | `[本轮实现]` |
| `0x0093` | `context.read` | `context_usage` | 读取上下文窗口和使用量 | `[本轮实现]` |
| `0x0094` | `context.compact` | `context_compact` | 请求上下文压缩； | `[本轮不实现]` |
| `0x00A0` | `message.read.update` | `read_unread` | 上报单条消息已读/未读 | `[本轮实现]` |
| `0x00B1` | `active_state.read` | `cross_device_active` | 读取跨设备活动状态 | `[本轮实现]` |
| `0x00B2` | `active_state.set` | `cross_device_active` | 设置当前活动线程和 turn | `[本轮实现]` |
| `0x00B3` | `events.since` | `events_since` | 按 cursor 拉取事件窗口 | `[本轮实现]` |
| `0x00B4` | `batch.execute` | `batch` | 原子批处理多个命令； | `[本轮不实现]` |
| `0x00C0` | `input.capabilities.read` | `input_capabilities` | 读取输入、附件和交互能力 | `[本轮实现]` |
| `0x00C1` | `content.read` | `text` 或 `attachment` | 按 `content_ref` 分页读取正文、工具输出或附件内容；按内容类型校验对应能力 | `[本轮实现]` |

未定义方法返回 `UNSUPPORTED_FEATURE`。设备未开启能力时返回
`CAPABILITY_NOT_ENABLED`。参数缺失或类型错误返回 `INVALID_PARAM`。

### 17.1.1 命令参数 TLV

除下表列出的参数外，`COMMAND` 公共字段仍按 17.2 提供 `request_id`、`idem`、
`method_id`、`deadline_ms`、`priority`、`expected_revision`、
`client_timestamp_ms` 和 `force`。表中标签均位于 `params` 内，除非明确说明。

| 方法 | 必填参数 TLV | 可选参数 TLV | 结果关键字段 | 状态 |
|---|---|---|---|---|
| `status.read` | 无 | 无 | `online`, `metrics`, `applied_policy` | `[本轮实现]` |
| `threads.list` | 无 | `cursor`, `history_depth`, `search_snippet`, `project_ref` | `objects`, `next_cursor`, `object_count` | `[本轮实现]` |
| `thread.read` | `thread_ref` / `thread_id` | `cursor`, `history_depth` | `thread_ref`, `status`, `objects`, `next_cursor` | `[本轮实现]`；`history_depth` 映射为最多返回的消息条数，省略时使用设备 profile 的历史预算 |
| `thread.start` | 无 | `title`, `cwd`, `model_name`, `effort`, `project_ref` | `thread_ref`, `owner_state`, `revision` | `[本轮实现]`；`project_ref` 由 Bridge 解析成项目目录 |
| `turn.start` | `thread_ref` / `thread_id`, `body` 或 `input_parts` | `model_name`, `effort`, `part_ref`, `references`, `deadline_ms` | `thread_ref`, `turn_ref`, `status` | `[本轮实现]` |
| `turn.steer` | `thread_ref` / `thread_id`, `body` 或 `input_parts` | `turn_ref`, `part_ref`, `references` | `thread_ref`, `turn_ref`, `status` | `[本轮实现]` |
| `turn.interrupt` | `thread_ref` / `thread_id` | `turn_ref`, `reason`；`interrupt_mode` 为 `[本轮不实现]` | `thread_ref`, `turn_ref`, `status` | `[本轮实现]`（不含 `interrupt_mode`） |
| `approvals.list` | 无 | `thread_ref`, `cursor` | `objects`, `next_cursor`, `object_count` | `[本轮实现]` |
| `approval.resolve` | `approval_ref` / `approval_id`, `decision` | `session_wide`；`response` 的 scope/permissions/amendment 部分为 `[本轮不实现]` | `approval_ref`, `decision`, `revision` | `[本轮实现]`（`decision=0..3`；`decision=4/5` 为 `[本轮不实现]`） |
| `thread.claim` | `thread_ref` / `thread_id` | `force`, `lease_generation` | `owner_state`, `lease_generation`, `lease_expires_at_ms` | `[本轮实现]` |
| `thread.release` | `thread_ref` / `thread_id`, `lease_generation` | `reason` | `owner_state`, `revision` | `[本轮实现]` |
| `models.list` | 无 | 无 | `model_name`, `model_capabilities` | `[本轮实现]` |
| `projects.list` | 无 | `cursor` | `objects`, `next_cursor` | `[本轮实现]` |
| `device.policy.read` | 无 | 无 | `effective_caps`, `applied_policy`, `policy_version` | `[本轮实现]` |
| `command.status` | `target_request_id` | 无 | `state`, `result`, `error_message` | `[本轮实现]` |
| `device.config.update` | `applied_policy` | `effective_caps`, `policy_version` | `applied_policy`, `revision` | `[本轮实现]` |
| `thread.search` | `search_scope`, `body` | `cursor`, `history_depth` | `search_result_count`, `objects`, `next_cursor` | `[本轮实现]` |
| `thread.rename` | `thread_ref` / `thread_id`, `title` | `settings_revision` | `thread_ref`, `revision` | `[本轮实现]` |
| `thread.archive` | `thread_ref` / `thread_id` | `force` | `thread_ref`, `archived`, `revision` | `[本轮实现]` |
| `thread.unarchive` | `thread_ref` / `thread_id` | `force` | `thread_ref`, `archived`, `revision` | `[本轮实现]` |
| `thread.pin` | `thread_ref` / `thread_id` | `expected_revision` | `thread_ref`, `pinned`, `revision` | `[本轮实现]` |
| `thread.unpin` | `thread_ref` / `thread_id` | `expected_revision` | `thread_ref`, `pinned`, `revision` | `[本轮实现]` |
| `thread.fork` | `thread_ref` / `thread_id` | `revert_to_ref`, `model_name`, `effort`, `project_ref`, `title` | `thread_ref`, `branch_ref`, `revision` | `[本轮实现]` |
| `thread.delete` | `thread_ref` / `thread_id` | `force`, `expected_revision` | `thread_ref`, `tombstone`, `revision` | `[本轮实现]` |
| `thread.restore` | `thread_ref` / `thread_id` | `force`, `expected_revision` | `thread_ref`, `tombstone`, `revision` | `[本轮实现]` |
| `thread.settings.read` | `thread_ref` / `thread_id` | 无 | `model_name`, `effort`, `cwd`, `settings_revision` | `[本轮实现]` |
| `thread.settings.update` | `thread_ref` / `thread_id` | `model_name`, `effort`, `cwd`, `approval_policy`, `approvals_reviewer`, `sandbox_policy`, `personality` | `thread_ref`, `settings_revision`, `revision` | `[本轮实现]` |

`thread.settings.update` 的 `sandbox_policy(0x0127)` 线值仍是
`0=read-only, 1=workspace-write, 2=danger-full-access, 3=custom`。
Bridge 内部同时兼容紧凑字符串和
`{"type":"readOnly"|"workspaceWrite"|"dangerFullAccess"}` 对象，并分别翻译为
app-server 的 `SandboxPolicy` 对象与桌面 IPC 的点分字符串。该差异不出现在
CBDP 线上；设备始终只发送协议枚举。Bridge 的 app-server 初始化必须声明
`capabilities.experimentalApi=true`，否则上游会拒绝 `thread/settings/update`。

`thread.settings.read` 的策略字段以 Bridge 的持久化镜像为准：app-server 的
`thread/read` 只返回 `model`、`cwd`、`reasoningEffort`，不返回
`approvalPolicy`、`sandboxPolicy` 或 `approvalsReviewer`。Bridge 在每次更新成功
后把 `approval_policy`、`approvals_reviewer`、`sandbox_policy` 和
`personality` 写入 `%LOCALAPPDATA%\CodexBridge\thread-settings.json`，读取时
优先返回该镜像，再以线程对象字段作为回退。桌面创建的线程还会先通过与
`turn.start` 相同的 owner discovery 路由到真实 writer，避免落到不识路该线程的
Bridge app-server。
| `thread.metadata.update` | `thread_ref` / `thread_id` | `title`, `metadata_json`, `source_kind` | `thread_ref`, `settings_revision`, `revision` | `[本轮实现]` |
| `thread.revert` | `thread_ref` / `thread_id`, `revert_to_ref` | `branch_ref`, `force` | `thread_ref`, `branch_ref`, `revision` | `[本轮实现]` |
| `thread.read_state.update` | `thread_ref` / `thread_id`, `read_at_ms` | `unread_count` | `thread_ref`, `read_states`, `revision` | `[本轮实现]` |
| `queue.add` | `thread_ref` / `thread_id`, `input_parts` 或 `body` | `priority`, `queue_due_at_ms`, `model_name`, `effort` | `queue_item_ref`, `queue_position`, `queue_size`, `revision` | `[本轮实现]` |
| `queue.list` | `thread_ref` / `thread_id` | `cursor`, `history_depth` | `queue_items`, `queue_size`, `revision` | `[本轮实现]` |
| `queue.update` | `thread_ref` / `thread_id`, `queue_item_ref` | `body`, `input_parts`, `priority`, `queue_due_at_ms` | `queue_item`, `queue_position`, `revision` | `[本轮实现]` |
| `queue.delete` | `thread_ref` / `thread_id`, `queue_item_ref` | `force` | `queue_item_ref`, `queue_state`, `revision` | `[本轮实现]` |
| `queue.reorder` | `thread_ref` / `thread_id`, `queue_items` | `expected_revision` | `queue_items`, `queue_size`, `revision` | `[本轮实现]` |
| `queue.promote` | `thread_ref` / `thread_id`, `queue_item_ref` | `queue_position`, `priority` | `queue_item_ref`, `queue_position`, `revision` | `[本轮实现]` |
| `queue.start` | `thread_ref` / `thread_id`, `queue_item_ref` | `force` | `queue_item_ref`, `queue_state`, `turn_ref` | `[本轮实现]` |
| `guidance.send` | `thread_ref` / `thread_id`, `guidance_text` | `turn_ref`, `client_timestamp_ms` | `guidance_ref`, `guidance_version`, `guidance_state` | `[本轮实现]` |
| `guidance.update` | `guidance_ref`, `guidance_text` | `guidance_version` | `guidance_ref`, `guidance_version` | `[本轮实现]` |
| `guidance.cancel` | `guidance_ref` | `guidance_version`, `reason` | `guidance_ref`, `guidance_state` | `[本轮实现]` |
| `message.edit` | `thread_ref` / `thread_id`, `message_ref` / `message_id`, `body` | `expected_revision`, `part_ref`, `change_reason` | `message_ref`, `revision`, `branch_ref` | `[本轮实现]` |
| `message.delete` | `thread_ref` / `thread_id`, `message_ref` / `message_id` | `expected_revision`, `change_reason` | `message_ref`, `tombstone`, `revision` | `[本轮实现]` |
| `message.range.delete` | `thread_ref` / `thread_id`, `range_start_ref`, `range_end_ref` | `expected_revision`, `change_reason` | `range_start_ref`, `range_end_ref`, `revision` | `[本轮实现]` |
| `attachment.add` | `thread_ref` / `thread_id`, `attachment_kind`, `content_ref` 或 `upload_id` | `attachment_id`, `filename`, `mime_type`, `byte_size`, `content_sha256`, `alt_text` | `attachment_ref`, `attachment_id`, `revision` | `[本轮实现]` |
| `attachment.list` | `thread_ref` / `thread_id` | `cursor` | `objects`, `next_cursor` | `[本轮实现]` |
| `attachment.remove` | `thread_ref` / `thread_id`, `attachment_ref` / `attachment_id` | `force` | `attachment_ref`, `revision` | `[本轮实现]` |
| `attachment.upload.begin` | `attachment_kind`, `byte_size`, `content_sha256` | `filename`, `mime_type` | `upload_id`, `upload_state`, `upload_offset` | `[本轮实现]` |
| `attachment.upload.chunk` | `upload_id`, `upload_offset`, `data` | `crc32` | `upload_id`, `upload_offset` | `[本轮实现]` |
| `attachment.upload.end` | `upload_id`, `byte_size`, `content_sha256` | `filename`, `mime_type` | `upload_id`, `upload_state`, `content_ref` | `[本轮实现]` |
| `attachment.upload.cancel` | `upload_id` | `reason` | `upload_id`, `upload_state` | `[本轮实现]` |
| `draft.update` | `thread_ref` / `thread_id`, `draft_revision` | `draft_text`, `draft_parts`, `draft_expires_at_ms` | `draft_ref`, `draft_revision` | `[本轮实现]` |
| `draft.delete` | `thread_ref` / `thread_id`, `draft_ref` | `draft_revision` | `draft_ref`, `draft_revision` | `[本轮实现]` |
| `input.respond` | `input_ref`, `answers` | `answer_state` | `input_ref`, `answer_state`, `revision` | `[本轮实现]` |
| `tool.respond` | `server_request_ref`, `response` | `answer_state` | `server_request_ref`, `server_request_state` | `[本轮实现]` |
| `mcp.respond` | `approval_ref`, `decision` | `mcp_action`, `mcp_content`；`requested_permissions`、`session_wide` 为 `[本轮不实现]`（仅属于 `structured_approval`） | `approval_ref`, `decision`, `revision` | `[本轮实现]`（基础 MCP action/content；结构化参数 `[本轮不实现]`） |
| `account.read` | 无 | 无 | `account_ref`, `account_id`, `plan_type` | `[本轮实现]` |
| `account.rate_limits.read` | 无 | 无 | `rate_limits`, `reset_at_ms` | `[本轮实现]` |
| `account.token_usage.read` | 无 | `thread_ref` | `token_usage`, `token_total`, `token_input`, `token_output` | `[本轮实现]` |
| `context.read` | `thread_ref` / `thread_id` | `turn_ref` | `context_usage`, `context_window_tokens`, `context_used_tokens` | `[本轮实现]` |
| `context.compact` | `thread_ref` / `thread_id` | `turn_ref`, `compaction_reason` | `compaction_state`, `compaction_summary` | `[本轮不实现]` |
| `thread.goal.get` | `thread_ref` / `thread_id` | 无 | `goal_ref`, `goal_text`, `goal_status`, `revision` | `[本轮不实现]` |
| `thread.goal.set` | `thread_ref` / `thread_id`, `goal_text` | `expected_revision` | `goal_ref`, `goal_status`, `revision` | `[本轮不实现]` |
| `thread.goal.clear` | `thread_ref` / `thread_id` | `goal_ref`, `expected_revision` | `thread_ref`, `goal_status`, `revision` | `[本轮不实现]` |
| `batch.execute` | `batch_ops` | `atomic`, `expected_revision` | `batch_op_index`, `batch_op_request_id`, `batch_op_idem` | `[本轮不实现]` |
| `message.read.update` | `message_ref` / `message_id`, `read_at_ms` | `thread_ref` / `thread_id` | `message_ref`, `read_states` | `[本轮实现]`；`thread_id` 可从已经由对象引用解析出的 `thread_ref` 补齐，未提供时 Bridge 仍记录消息已读 |
| `active_state.read` | 无 | `device_ref` | `active_thread_ref`, `active_turn_ref`, `active_revision` | `[本轮实现]`；Bridge 尚未选中线程时返回全 0，不报错 |
| `active_state.set` | `active_thread_ref` | `active_turn_ref`, `active_revision` | `active_thread_ref`, `active_turn_ref`, `active_revision` | `[本轮实现]`；引用无法解析返回 `NOT_FOUND` |
| `events.since` | `cursor` | `topic_id`, `event_count`, `history_depth` | `objects`, `event_count`, `next_cursor`, `has_more` | `[本轮实现]` |
| `input.capabilities.read` | 无 | 无 | `model_capabilities`, `input_parts`, `attachment_kind` | `[本轮实现]` |
| `content.read` | `content_ref` | `offset`, `max_bytes`, `content_kind` | `content_ref`, `content_kind`, `content_encoding`, `offset`, `next_offset`, `total_bytes`, `sha256` | `[本轮实现]` |

`content.read` 的能力校验按 `content_kind` 执行：`message_body` 和
`tool_output` 要求 `text`，`attachment` 要求 `attachment`。`offset` 默认 0；
`max_bytes` 缺省时由 Bridge 使用 `max_chunk_bytes`，但不得超过该值。消息事件
中的 `body_ref` 与工具结果中的内容引用都作为 `content_ref` 发送给
`content.read`，不得自行拼接第二个引用格式。

`message.read.update` 的 `read_at_ms` 是按毫秒计的非负 `i64`；`read_at_ms = 0` 表示仅上报单条消息已读、不推进线程级水位；Bridge 把同一标记写入共享已读存储，因此手机、浏览器和其他设备会立即看到同一状态。只带 `message_ref` 的设备请求必须通过 Bridge 的持久引用表解析；解析失败返回 `NOT_FOUND`，不得返回 `INTERNAL`。

`thread.read` 的 `history_depth` 按 §8.2 是 `u16`，语义是最多返回最近多少条消息，不是泛化的 `limit` 参数。Bridge 不得把这个字段直接传给上游命令层；省略该字段时仍按 §9.4 的 profile 历史预算处理。

所有写方法的参数规范化后参与 `idem` 计算。参数中同时出现紧凑 ref 与外部字符串
ID 时，Bridge 必须验证二者指向同一对象，否则返回 `INVALID_PARAM`。设备只发送
自身能力位允许的方法；Gateway 可以翻译旧字段，但不得绕过上述校验。

所有命令参数在进入 `params` 前 MUST 按以下类型编码；不得使用 JSON 字符串
替代数组或对象：

| 参数类型 | 编码 |
|---|---|
| 单个引用 | 对应 `thread_ref`、`message_ref` 等 `u32` TLV |
| 文本 | `utf8` TLV |
| 一组对象 | 第 8.12 节定义的 `array` |
| 一组键值 | 第 8.12 节定义的 `tlv` |
| 分页 | `cursor` + `history_depth` 或 `event_count` |

命令表只定义参数存在性和互斥关系，不定义第二个编码层。客户端不得把 JSON
对象序列化后塞入 `utf8` 参数。唯一例外是 `metadata_json`，它是有界 JSON 文本。

每个方法的成功 `result` 必须以 `tlv` 返回；失败不得伪造 `result`。对于表中
“结果关键字段”列出的字段，客户端只可依赖列出的 tag，不得按字段出现顺序解释。
下面是需要组合对象的命令结果 schema：

| 方法 | `result` 必需字段 | 可选字段 |
|---|---|---|
| `threads.list` | `objects` | `next_cursor`, `object_count` |
| `thread.read` | `thread_ref`, `status`, `objects` | `next_cursor`, `object_count` |
| `approvals.list` | `objects` | `next_cursor`, `object_count` |
| `attachment.list` | `objects` | `next_cursor`, `object_count` |
| `projects.list` | `objects` | `next_cursor`, `object_count` |
| `thread.search` | `search_result_count`, `objects` | `next_cursor` |
| `events.since` | `objects`, `event_count`, `next_cursor`, `has_more` | 无 |
| `input.capabilities.read` | `model_capabilities`, `input_parts` | `attachment_kind` |
| `content.read` | `content_ref`, `content_kind`, `content_encoding`, `offset`, `next_offset`, `total_bytes` | `sha256` |

`next_cursor` 使用 `0` 表示没有下一页。`objects` 为空时返回空数组，不省略字段。

命令参数中的选择器统一适用 6.1.2：

```text
thread selector  = thread_ref | thread_id
message selector = message_ref | message_id
turn selector    = turn_ref | turn_id
approval selector= approval_ref | approval_id
content source   = content_ref | upload_id
text input       = body | input_parts
```

| 符号 | 含义 |
|---|---|
| `A / B` | A 与 B 二选一；只允许一个 |
| `A 或 B` | A 与 B 二选一；只允许一个 |
| `A, B` | A 与 B 都必须存在 |
| 行末“无” | 没有额外参数 |

`thread.read`、`turn.*`、`queue.*`、`message.*` 等命令中，省略 thread selector
只在命令明确允许 `use_active_thread = true` 时合法。`thread.read` 不允许省略。

### 17.2 COMMAND

`COMMAND` flags 包含 `REQUEST` 和 `COMMAND`。必填字段：

| Tag | 字段 |
|---|---|
| `0x002A` | `request_id` |
| `0x002B` | `idem` |
| `0x002C` | `method_id` |
| `0x0060` | `params` |

可选字段：`deadline_ms`、`priority`、`expected_revision`、
`client_timestamp_ms` 和 `force`。

### 17.3 RESULT

`RESULT` flags 包含 `RESPONSE`。必填字段：

| Tag | 字段 |
|---|---|
| `0x002A` | `request_id` |
| `0x002C` | `method_id` |
| `0x0064` | `state` |

`state`：

| 值 | 名称 | 语义 |
|---:|---|---|
| `0` | `accepted` | 命令已接受，后台执行 |
| `1` | `completed` | 命令已执行完成 |
| `2` | `failed` | 命令失败 |
| `3` | `rejected` | 命令被策略或状态拒绝 |
| `4` | `unknown` | 执行结果未知，不允许盲目重试 |

成功时 `result` 必填。失败时 `code` 和 `error_message` 必填，
`error_detail` 可选。

`content.read` 是唯一允许 `RESULT` 携带 payload 的方法。其成功结果字段：

| 字段 | 必填性 |
|---|---|
| `content_ref` | 必填；回显请求 |
| `content_kind` | 必填；`0=message_body, 1=tool_output, 2=attachment, 3=other` |
| `content_encoding` | 必填；`0=utf8, 1=binary` |
| `offset` | 必填；本次返回数据的起始偏移 |
| `next_offset` | 必填；下一次请求的 offset；没有后续时等于 `total_bytes` |
| `total_bytes` | 必填；内容总长度 |
| `sha256` | 建议提供；内容完整 SHA-256 |

成功时 payload 是本次数据，长度不得超过 `max_chunk_bytes`；失败时不读取
payload。设备请求超出 `total_bytes` 的 offset 返回 `INVALID_PARAM`，内容已经
被清理时返回 `CONTENT_UNAVAILABLE`。

### 17.4 幂等性

`idem` 是 16 字节值，必须由客户端随机生成或稳定派生。推荐使用：

```text
idem = first_16_bytes(SHA-256(device_id || request_id || method_id || canonical_params))
```

规则：

1. 同一 `idem` 已成功执行时，Bridge 返回原结果，不重复执行。
2. 同一 `idem` 携带不同 `method_id` 或不同参数时，返回
   `IDEMPOTENCY_CONFLICT`。
3. 幂等记录至少保留 24 小时，或保留到设备确认结果。
4. 跨 Bridge 重启必须尽可能保留已提交写命令的幂等记录。
5. 执行异常返回 `state = unknown`，客户端不得自动再次写入；必须先查询
   `command.status` 或读取投影。
6. 客户端重试 MUST 复用首次请求的 `idem` 和原始方法参数；参数规范化规则见
   6.1.3。Bridge 在返回 `IDEMPOTENCY_CONFLICT` 前不得执行任何副作用。

幂等键固定为 `(device_id, idem)`，**不是** `request_id`。`request_id` 只要求
同一连接内唯一，设备重启后会从 1 重新计数；若用 `request_id` 做键，设备重启后
重放同一条命令就会与自己的历史记录冲突。Bridge 实现必须按 `idem` 查表，并在
`idem` 缺失时才退回 `request_id`（用于旧客户端兼容）。

### 17.5 writer lease 与显式接管

线程所有权状态：

```text
UNMANAGED -> OBSERVED -> CLAIMED -> MANAGED
```

Bridge 始终是唯一权威写入者；`CLAIMED` 和 `MANAGED` 只表示当前持有有效 writer lease，允许 Bridge 串行执行该线程的写入意图。没有有效 lease 的写入返回 `WRITER_CONFLICT`。`thread.claim` 参数：

```text
thread_ref 或 thread_id
force       可选，默认 false
```

接管流程：

1. 检查线程存在。
2. 检查是否有活动 writer。
3. 检查是否有运行中的 turn。
4. 创建或刷新 writer lease。
5. 发布 `thread.owner.changed`。

如果已有 writer 且 `force = false`，返回 `WRITER_CONFLICT`。强制接管必须
由管理页面或其他特权策略授权，不能由普通设备自授权。

`thread.release` 必须校验 `lease_generation`。旧 generation 不得释放新 lease。

### 17.6 多设备命令

多个设备可以并发提交命令，但同一线程的写操作必须串行化：

```text
request -> validate capability
        -> validate idempotency
        -> acquire per-thread writer lock
        -> verify lease generation
        -> execute
        -> append event
        -> release lock
        -> cache result
```

锁顺序必须固定，禁止先获取设备队列锁再获取线程锁，避免死锁。

### 17.7 队列语义

队列是 Codex 线程内的待处理输入序列，不是设备本地 UI 列表。队列事实源始终在
Bridge；手机、ESP 和桌面看到的是同一份有序队列投影。

```text
queue item:
  queue_item_ref
  thread_ref
  queue_position      从 0 开始
  queue_state         pending | starting | running | completed | failed | cancelled
  priority            数值越小越优先；缺省 100
  input_parts         文本、附件、引用或音频引用
  queue_due_at_ms     可选；0 表示立即
  created_at_ms
  updated_at_ms
```

规则：

1. 未指定 `priority` 时按 FIFO；指定 priority 后按 `(priority, created_at_ms)` 排序。
2. `queue.add` 在活跃 turn 中默认只排队，不隐式中断当前 turn。
3. `queue.start` 可以把一个 pending 项提升为立即执行；若线程正忙且
   `force = false`，返回 `BUSY`，不改变队列。
4. `queue.update` 只允许修改 pending 项。已经 starting 或 running 的项必须返回
   `INVALID_STATE`。
5. `queue.delete` 对 pending 项表示取消并删除；对已启动项只取消后续执行，不
   伪造历史不存在。
6. `queue.reorder` 必须提交完整的可见顺序或明确的新位置列表，避免并发重排
   产生不可解释的局部顺序。
7. 队列变更必须发布 `queue.changed` 和对应 `queue.item.*` 事件，事件中的
   `revision` 是线程队列投影 revision。
8. `micro` 和 `panel` 可以只实现 `queue.add`，不实现 `queue_manage`。未实现
   队列能力的设备仍可普通发送，由 Bridge 决定 `turn.start` 或返回 `BUSY`。

Bridge 实现补充（v1.0 实现后追加，无新增线格式字段）：

- 派发失败时 Bridge 把该项置为 `failed`，并可暂停该线程的队列派发，避免对同一
  错误做无上限热重试。暂停状态通过 `queue.list` 快照的 `paused` 布尔字段
  暴露（Bridge 扩展字段，设备可忽略）。
- 暂停必须可恢复。以下任一用户可见操作都会清除暂停：`queue.add`、
  `queue.update`、删除该线程的失败项、删除后该线程已无失败项，以及该线程
  有 turn 正常完成。Bridge 不得要求客户端只能靠 `queue.update` 解除暂停。
- 因此客户端不需要额外的 `queue.resume` 命令；重发（`queue.add`）本身就是
  恢复动作。若仍保留自定义恢复按钮，调用 `queue.update` 或重新 `queue.add`
  都符合本规范。

### 17.8 引导语义

引导是向正在运行的 turn 注入的新指令，不创建一条用户消息，不改变用户可见的
消息历史。它与队列项不同：队列在 turn 之间执行，引导作用于当前 turn。

```text
guidance:
  guidance_ref
  thread_ref
  turn_ref
  guidance_text
  guidance_version
  guidance_state     pending | applied | cancelled | failed
```

规则：

1. `guidance.send` 必须解析到当前活动 turn；没有活动 turn 时返回 `INVALID_STATE`。
2. 引导按 `guidance_version` 做乐观并发控制；旧版本更新返回
   `REVISION_CONFLICT`。
3. `guidance.update` 只允许修改 pending 引导。applied 引导不可修改，只能对后续
   turn 再发新引导。
4. `guidance.cancel` 取消尚未应用的引导；如果已经应用，只发布取消状态，不
   回滚模型已经产生的输出。
5. 引导必须携带来源设备 ref，方便多设备显示“谁发出了这次引导”。
6. ESP 设备只声明 `guidance` 时可以发送纯文本引导；`panel` 可以显示简短状态。
   设备不实现引导时不需要创建本地伪消息。

### 17.9 编辑、删除与回退语义

消息修订不直接改写历史，而是通过 revision、tombstone 或 branch 表达。Bridge
必须保留足以解释事件顺序的服务端状态。

- `message.edit` 生成新的消息 revision，旧 revision 标记为 `replaced_by_ref`。
- `message.delete` 对单条消息写 tombstone；设备显示删除占位，不得把后续消息
  上移成旧位置。
- `message.range.delete` 删除连续范围，范围端点为闭区间，必须验证同一线程内
  顺序。
- `thread.revert` 将线程视图回退到目标消息或 turn；是否保留被回退内容由
  `branch_ref` 表达。没有 branch 时只允许 `force = true` 的特权调用。
- `thread.fork` 从线程、turn 或消息创建新线程，保留来源关系但不复制 writer
  lease。
- 编辑或回退与运行中的 turn 冲突时返回 `REVISION_CONFLICT` 或 `INVALID_STATE`，
  不得让设备自行覆盖。
- 低内存设备可以只显示 tombstone 和最终摘要；不得把删除事件忽略后继续显示
  被删除正文。

### 17.10 结构化输入语义

结构化输入用于一个请求中包含多个问题、多个选项或敏感字段的场景，例如 Codex
的 `request_user_input`。它不占用普通消息历史，除非 Bridge 明确把最终回答
投影为用户输入事件。

每个问题必须包含：

```text
question_id
question_header
question_text
question_options[]
is_other
is_secret
is_blocking
```

回答必须使用同一 `question_id`，以 `answers[]` 返回。规则：

1. `is_blocking = true` 的问题未回答时，turn 保持在等待状态。
2. `is_secret = true` 的答案只在内存中传递，不写入日志、历史投影或兼容网关。
3. `is_other = true` 时允许 `answer_value` 为自由文本；否则必须是声明的选项值。
4. 设备可以分批回答，但 `input.respond` 成功提交后，同一 `input_ref` 的重复
   答案必须返回 `INVALID_STATE`，除非请求本身允许修改。
5. `panel` 可以一次显示一个问题，分多次提交；`full` 可以一次显示完整表单。
6. 设备不支持结构化输入时，Bridge 只发送普通审批或普通文本请求，不降级成
   伪造的自由文本。

### 17.11 附件与输入部件语义

附件引用的是 Bridge 内容存储中的对象，不是设备本地路径。设备上传时使用
`attachment.upload.*` 事务；设备已有内容时可以直接提供 `content_ref`。

上传事务：

```text
begin -> chunk* -> end
   \-> cancel
```

规则：

1. `begin` 返回 `upload_id`，并校验 `byte_size` 和 `content_sha256` 的边界。
2. `chunk` 必须按 `upload_offset` 写入；乱序或越界返回 `INVALID_PARAM`。
3. `end` 必须再次校验总大小和 SHA-256；校验失败返回 `PAYLOAD_TOO_LARGE` 或
   `INVALID_PARAM`。
4. `mime_type` 和 `attachment_kind` 必须匹配；不得把可执行文件伪装成图片。
5. `attachment.add` 只把已经存在且校验通过的内容绑定到线程。
6. `message_reference` 与附件可组合在 `input_parts` 中，顺序即模型看到的输入
   顺序。
7. `micro` 和 `panel` 不声明 `attachment` 时，Bridge 不下发附件事件，也不接受
   附件命令。它们仍可以接收文本摘要或审核结果摘要。

### 17.12 草稿语义

草稿是设备本地未发送编辑状态的同步副本，不属于线程历史，不创建 turn，不触发
模型。Bridge 只保存有界草稿投影，供同一设备重连和多设备显示提示。

1. `draft.update` 必须携带单调递增的 `draft_revision`。
2. `draft_ref` 对同一设备和线程稳定；设备断开清理时由 TTL 或
   `draft.delete` 删除。
3. 草稿文本和附件引用受 profile 预算限制；超限返回 `PAYLOAD_TOO_LARGE`。
4. 发送真实消息后，Bridge 可以删除对应草稿并发布 `draft.deleted`。
5. `draft` 不是消息，不得出现在线程历史、搜索结果或模型上下文中。
6. ESP 端可以不实现草稿；未声明 `draft` 时设备不得收到草稿事件。

### 17.13 批处理与跨设备活动状态

`batch.execute` 属于 `[本轮不实现]`。本轮仅保留完整线格式定义，端点不得发送、声明或依赖。

批处理语义（保留定义，客户端可提前实现解析）：

1. `batch_ops` 是 `array`，每项必须携带 `batch_op_index`、`batch_op_method` 和 `batch_op_idem`。
2. 每个子操作按自身 `method_id` 校验能力、权限、writer lease 和参数 schema；批处理不绕过任何单命令校验。
3. `atomic=true` 表示 Bridge 必须在同一 writer lease 内串行执行；任一子操作失败时，按策略回滚可回滚操作并返回失败。
4. 结果至少返回每个子操作的 `batch_op_index`、`batch_op_request_id`、`batch_op_idem` 和最终状态；执行异常按单命令规则返回 `unknown`，不得盲重试。
5. 子操作事件仍按普通事件编号发出，不创建第二套事件模型。

跨设备活动状态用于让多台设备知道“现在哪台设备正在看哪个线程/turn”：


跨设备活动状态用于让多台设备知道“现在哪台设备正在看哪个线程/turn”：

```text
active_state:
  active_thread_ref
  active_turn_ref
  active_device_ref
  active_revision
  resume_required
  resume_cursor
  resume_thread_ref
```

`active_state.set` 只改变活动提示，不获取 writer lease，不代表可以写入。真正的
写入仍必须调用 `thread.claim` 或经过 Bridge 的自动接管规则。设备断线重连后，
可以用 `events.since` 或 `resume_required` 恢复视图。

## 18. 文本回复事务

### 18.1 SEND_BEGIN

必填字段：

| Tag | 字段 |
|---|---|
| `0x002D` | `tx_id` |
| `0x002B` | `idem` |
| `0x0070` | `mode` |

可选字段：`thread_ref`、`thread_id`、`base_revision`、`use_active_thread`、
`client_timestamp_ms`、`deadline_ms` 和 `total_bytes`。

`mode`：

| 值 | 名称 | 语义 |
|---:|---|---|
| `0` | `auto` | Bridge 根据线程状态决定 start 或 steer |
| `1` | `start` | 只允许启动新 turn |
| `2` | `steer` | 只允许追加到运行中的 turn |
| `3` | `queue` | 允许排队，等当前 turn 完成后启动 |

### 18.2 SEND_CHUNK

必填字段：`tx_id`、`offset`。数据放在 payload。

规则：

1. `offset` 从 0 开始，表示整个 UTF-8 字节流的偏移。
2. 分片可以顺序发送，v1.0 不允许同事务分片交错。
3. 每个 chunk 的 payload 长度不得超过 `max_chunk_bytes`。
4. 接收方若收到重复 offset，必须幂等处理。
5. Bridge 只有在 SEND_END 后才把完整文本交给上游。

### 18.3 SEND_END

必填字段：`tx_id`。可选字段：`total_bytes`、`sha256`。

Bridge 完成校验后返回 RESULT：

```text
accepted：文本已接收，尚未确定 start/steer
completed：已转换为 turn.start 或 turn.steer
failed：无法执行
```

最终结果必须包含实际选择的 `turn_ref`、`thread_ref` 和状态。

### 18.4 状态映射

Bridge 根据线程状态和 writer lease 决定：

| 线程状态 | 决策 |
|---|---|
| 空闲且已接管 | `turn.start` |
| 运行中且已接管 | `turn.steer` |
| 运行中且策略允许排队 | 排队后 `turn.start` 或 `turn.steer` |
| 无有效 lease 且策略允许认领 | Bridge 分配 lease 后 `turn.start` |
| 无有效 lease 且策略不允许 | `WRITER_CONFLICT` 或 `UNAUTHORIZED` |
| 已接管但队列满 | `BUSY` |

设备不发送 `turn.start` 或 `turn.steer` 作为普通文本回复；这些命令只允许
具备 `control` 能力且被策略授权的客户端调用。

## 19. 音频回复事务

### 19.1 AUDIO_BEGIN

必填字段：

| Tag | 字段 |
|---|---|
| `0x002D` | `tx_id` |
| `0x002B` | `idem` |
| `0x0073` | `codec` |

音频格式可以内联或使用 `audio_format` TLV：

```text
sample_rate
channels
bits_per_sample
duration_ms
```

可选字段：`thread_ref`、`thread_id`、`use_active_thread`、`total_bytes`、
`client_timestamp_ms` 和 `transcript`。

线程选择器规则（2026-10-19 实现澄清）：

- `AUDIO_BEGIN` / `SEND_BEGIN` **不要求**先执行 `thread.claim`。`thread.claim`
  管理的是 writer lease（谁可以写入），文本/音频上行只要求能解析出一个线程选择器：
  `thread_id` 优先于 `thread_ref`，两者都缺省时才用 `use_active_thread` 或
  Bridge 当前选中的会话。
- `thread_ref` 必须在 `BEGIN` 时就解析成功。解析不到返回
  `ERROR(NOT_FOUND, "thread_ref")`，不得静默忽略后等到 `END` 才失败，也不得
  报成 `UNSUPPORTED_FEATURE`。
- 没有可用线程时，`END` 返回 `ERROR(INVALID_STATE, "NO_ACTIVE_THREAD")`；
  这不是能力缺失，客户端不应据此关闭 `audio` 能力。

### 19.2 codec 编号

| 值 | 名称 | 说明 |
|---:|---|---|
| `0` | `raw_pcm_s16le` | 低复杂度，适合 micro 调试 |
| `1` | `opus` | 推荐，低带宽和小内存 |
| `2` | `flac` | 无损，不作为低内存默认 |
| `3` | `pcm_alaw` | 电话语音兼容 |

ESP32 默认 SHOULD 使用 Opus 或 raw PCM 短片段。设备必须按 WELCOME 中的
`max_audio_bytes` 和 `max_chunk_bytes` 限制上传。

### 19.3 AUDIO_CHUNK

必填字段：`tx_id`、`offset`。音频数据放在 payload。

规则：

1. `offset` 从 0 开始，表示编码后音频总字节流偏移。
2. 设备不得发送超过 `max_audio_bytes` 的音频。
3. Bridge 可以在达到内存阈值时提前拒绝并返回 `PAYLOAD_TOO_LARGE`。
4. Bridge 不得把完整音频常驻内存；必须流式写临时区或转给 STT 管线。
5. 中断、超时或断线必须清理临时音频。

### 19.4 AUDIO_END

必填字段：`tx_id`。可选字段：`total_bytes`、`sha256`、`transcript`。

Bridge 接收完整音频后执行 STT，并把识别结果转换为文本回复语义：

```text
AUDIO_BEGIN / AUDIO_CHUNK / AUDIO_END
  -> Bridge STT
  -> SEND_BEGIN / SEND_CHUNK / SEND_END semantics
  -> turn.start 或 turn.steer
```

返回 RESULT 应包含 `transcript` 或错误。设备不负责拼装完整上下文，也不负责
调用模型。`voice` 和 `audio` 可以分别开启；只有 audio 的设备可以上传音频但
不显示语音命令入口。

### 19.5 Bridge 实现补充（v1.0 实现后追加）

以下语义在 Bridge 首次落地 SEND / AUDIO 上行事务后明确，供 ESP 与手机端对齐：

- `AUDIO_BEGIN` 必填 `tx_id`；音频格式字段缺省为 `16000 Hz`、单声道、
  `16-bit` PCM。Bridge 必须按 `max_audio_bytes` 限制累积，超限返回
  `PAYLOAD_TOO_LARGE` 并丢弃该事务。
- `AUDIO_CHUNK` 的 `offset` 必须与已接收字节数连续；乱序或缺口返回
  `INVALID_PARAM`，不得拼接出损坏音频。
- `AUDIO_END` 后 Bridge 执行 STT，把识别文本按
  `AUDIO_* -> SEND_* semantics -> turn.start / turn.steer` 提交，并在
  `RESULT` 中携带 `transcript`。识别失败返回 `UPSTREAM_UNAVAILABLE`，不得
  静默丢弃。Bridge 必须在 `error_message(0x0062)` 里保留可操作的识别失败
  原因，并在 `error_detail(0x0063)` 内带 `reason = STT_UNAVAILABLE`；设备
  因此可以区分 STT 上游故障与音频格式、分片或会话状态错误。
- `SEND_END` 和 `AUDIO_END` 必须绑定设备当前选中的会话。设备未选择会话时
  Bridge 必须返回 `ERROR(INVALID_STATE, NO_ACTIVE_THREAD)`，不得既不创建
  turn 也不回错误。
- 上游 Codex 或 STT 调用不得在该设备的 socket 读取线程内同步执行；Bridge
  必须把这类工作交给每设备工作线程，保持读循环持续排空音频分片和心跳。
- `AUDIO_BEGIN` 需要 `audio` 能力；未声明或未启用时返回
  `CAPABILITY_NOT_ENABLED`。

### 19.5.1 `message.read.update` 与 `thread.read` 实现修正（2026-10-19）

联调发现以下 Bridge 行为不符合既有字段语义，修正不改变线格式版本：

- `message.read.update(0x00A0)` 进入共享命令层后必须真正写入 `ReadStateStore`，
  不得只回成功信封。缺 `read_at_ms` 或 `message_id`/`message_ref` 返回
  `INVALID_PARAM`；引用无法解析返回 `NOT_FOUND`。
- `thread.read(0x0003)` 的 `history_depth(0x0014)` 只控制消息条数；
  Gateway 必须先解码 `u16`，再转换成内部 `message_limit`，不能把字段原名或
  泛化 `limit` 传给上游。
- 以上错误都必须返回可解释的 `ERROR`，不得再被统一吞成 `INTERNAL`。

### 19.6 v1.0 实现后修正（2026-10-18）

本节记录 Bridge 首次落地后发现的实现错误与协议澄清。标注为“修正”的条目是
对 8.4 / 8.7 / 8.12 / 17.1.1 既有定义的严格回归，不改变线格式版本，
`major = 1`、`minor = 0`。设备端与手机端应按本节检查自己的编码表。

1. **音频 TLV 编号修正。** 8.7 的音频字段编号为：`codec = 0x0073`、
   `sample_rate = 0x0074`、`channels = 0x0075`、`bits_per_sample = 0x0076`、
   `duration_ms = 0x0077`、`transcript = 0x0078`、`sha256 = 0x0079`、
   `audio_format = 0x007A`。任何把 `sha256` 编到 `0x00B8` 的实现都错了：
   `0x00B8` 是 `retransmit_timeout_ms`。`total_bytes = 0x0047`，
   `offset = 0x0048`。
2. **`objects` 数组必须带计数前缀。** `0x00A1` 的值区按 8.12 是
   `u16 count`，之后是 `count` 个 `object`（元素 tag 固定 `0x00A0`）。
   空数组编码为 `00 00`。只拼接元素、不写计数的实现会让客户端解析为空。
   同理，`options`（`0x0081`）和 `model_capabilities`（`0x019E`）也使用
   `u16 count` + 同 tag 元素的数组编码。
3. **`next_cursor` 是不透明 u64。** Bridge 可以把上游的 opaque 字符串游标
   映射为会话内稳定的 u64；客户端只负责回传该值，不得解释其内部含义。
   `next_cursor = 0` 表示没有下一页。
4. **`SEND_END` / `AUDIO_END` 的 `total_bytes` 若携带则必须与 Bridge 实际
   接收字节数一致；`sha256` 若携带则必须是 32 字节且与实际负载匹配。
   不一致返回 `INVALID_PARAM`。两者仍是可选字段。
5. **线程选择器。** `SEND_*` / `AUDIO_*` 可以在 `BEGIN` 携带
   `thread_ref`、`thread_id` 或 `use_active_thread=1`。显式 `thread_id` 优先
   于 `thread_ref`，二者都缺省时才使用 Bridge 当前选中的会话。没有可用会话
   时返回 `ERROR(INVALID_STATE, NO_ACTIVE_THREAD)`。
6. **结果对象必须含客户端渲染所需字段。** `thread` 对象携带 `thread_ref`、
   `thread_id`、`title`、`status`、`owner_state`，并在可用时携带 `model_name`、
   `cwd`、`summary`、`updated_at_ms`。`message` 对象携带 `message_ref`、
   `thread_ref`、`message_id`、`kind`、`status`、`body`，并在可用时携带
   `turn_ref`、`created_at_ms`。`model` 对象的 `model_capabilities` 数组按
   8.12 编码，kind=0 表示 reasoning effort。
7. **Companion 端口鉴权澄清。** 3.1.1 中“HELLO/WELCOME 后不再单独 AUTH”
   的表述不成立，已改为：HTTP 传输层先校验 `Authorization: Bearer`，之后
   会话内仍执行标准 `HELLO -> WELCOME -> AUTH -> AUTH_OK`。两层缺一不可。
8. **能力发现。** Companion 端口在 `GET /cbdp/v1` 返回 CBDP 能力对象，并在
   `GET /api/companion/ping` 的 `cbdp_v1` 字段同步返回，客户端无需靠
   `401/404` 探测。对象含 `protocol`、`version`、`version_major`、
   `version_minor`、`transport`、`paths`、`auth`、`auth_mode`、`enabled`、
   `port`。

### 19.7 终态与完成时间（2026-10-19 实现修正）

`thread_state` 的 `completed` / `failed` 是终态，客户端依赖它们做已读、提醒和
排序，因此终态必须自带可比较的结束时间：

- 任何 `completed` / `failed` 的 thread 或 turn 都必须同时给出
  `completed_at_ms > 0`。上游缺少 `completedAt` 时，Bridge 依次使用该 turn 的
  最后活动时间、线程最后更新时间、本机观测时间补齐；同一线程的终态时间戳
  只增不减，不得因后续读到旧快照而回退。
- `failed` / `interrupted` 但拿不到该 turn 自己的结束时间时，视为网络重连、
  供应商限流重试或流重置留下的瞬态结果，对外保持 `running`，不得编码成
  `failed`。只有拿到结束时间，或该 turn 已静默超过 Bridge 的陈旧窗口并以最后
  活动时间落定，才编码为 `failed`。
- 缺少 turn 终态证据时不得推断 `completed`；无法观察的线程编码为 `idle`。
- `threads.list`、`thread.read`、`thread.*` 事件和 `turn.completed` 必须给出
  同一组 `status` / `completed_at_ms` / `turn_ref`。瞬态失败时
  `turn.completed` 携带 `status=running`、`completed_at_ms=0`，客户端不得把它
  当作结束。
- 已读不依赖终态时间：只要线程不处于活动状态，Bridge 会用完成时间、最后更新
  时间或本机时间写入已读水位，因此历史上 `completed_at_ms = 0` 的线程在客户端
  读取后也会稳定回到 `idle`。

## 20. 审批协议

### 20.1 APPROVAL_REQUEST

Bridge 在收到 approval 事件后推送给具备 `approval` 能力的设备：

| Tag | 字段 |
|---|---|
| `0x0027` | `approval_ref` |
| `0x0028` | `approval_id` |
| `0x0021` | `thread_ref` |
| `0x0023` | `turn_ref` |
| `0x0087` | `approval_kind` |
| `0x0080` | `prompt` |
| `0x0081` | `options` |
| `0x0084` | `risk` |
| `0x0085` | `expires_at_ms` |
| `0x0086` | `default_decision` |

设备必须显示 prompt 和候选项，或者显示一个可理解的拒绝入口。低内存设备可以
只显示摘要和最多两个选项，但不得伪造选项语义。

`APPROVAL_REQUEST` 是 Bridge 发起的推送，不是请求类消息，不携带 `request_id`，
也不要求设备返回通用响应。设备成功提交决定后，通过 `APPROVAL_RESOLVE` 请求
获得自己的 `RESULT`。

### 20.2 APPROVAL_RESOLVE

设备提交：

| Tag | 字段 |
|---|---|
| `0x0027` | `approval_ref` |
| `0x0028` | `approval_id` |
| `0x0082` | `decision` |
| `0x0083` | `response` |
| `0x002A` | `request_id` |
| `0x002B` | `idem` |

`decision`：

| 值 | 名称 | 状态 |
|---:|---|---|
| `0` | `accept` | `[本轮实现]` |
| `1` | `accept_for_session` | `[本轮实现]` |
| `2` | `decline` | `[本轮实现]` |
| `3` | `cancel` | `[本轮实现]` |
| `4` | `accept_with_execpolicy_amendment` | `[本轮不实现]` |
| `5` | `apply_network_policy_amendment` | `[本轮不实现]` |

`accept_for_session` 只允许用于请求明确允许会话级放行的审批。需要 policy
amendment 的响应必须同时携带 `execpolicy_amendment` 或
`network_policy_amendment`；缺少结构化响应时返回 `INVALID_PARAM`。兼容网关
可以接受旧的 `approve` / `deny` / `acceptForSession` 字符串，但必须先映射到
上述数值，再进入 CBDP 命令路径。
`decision=4/5`、policy amendment 和相关结构化响应属于 `[本轮不实现]`；本轮
客户端不得发送，Bridge 必须返回 `UNSUPPORTED_FEATURE`，不能把它们降级猜测为
基础审批。

Bridge 实现补充（2026-10-20）：

- `APPROVAL_RESOLVE` 的字段直接放在消息 header 里，不是 `params`。Bridge
  必须能从 header 读取 `approval_ref`、`approval_id`、`decision`、`response`。
- `approval_ref` 通过持久引用表解析为外部 `approval_id`；解析失败返回
  `NOT_FOUND`。同时提供 `approval_ref` 与 `approval_id` 时两者必须一致，
  否则返回 `INVALID_PARAM`。
- 携带 `response` 时按结构化响应处理（MCP elicitation、用户输入、权限），
  转成共享命令层的 `approval.respond`；只带 `decision` 时走
  `approval.resolve`。`response` 是目前唯一允许承载 `action`/`content`/
  `answers` 的通道，不能被丢弃成纯 `decision`。

审批采用 first-valid-response-wins。第一个通过校验的响应生效，之后所有响应
返回 `INVALID_STATE`。生效后 Bridge 发布 `approval.resolved`，其他设备更新
本地状态，不得再显示可操作按钮。

### 20.3 超时与过期

审批倒计时以 Bridge 时间为准，使用 `expires_at_ms`。过期后：

```text
apply default_decision
publish approval.expired
reject late decisions with TIMEOUT
```

如果 `default_decision` 缺失，非关键审批默认 `decline`；关键审批必须由策略
显式配置。审批最终结果属于 critical 事件，不得丢弃。

### 20.4 approval_kind 与 risk

`approval_kind`：

| 值 | 名称 | 说明 |
|---:|---|---|
| `0` | `command_execution` | 执行命令或脚本 |
| `1` | `file_change` | 修改文件 |
| `2` | `network_access` | 访问网络 |
| `3` | `permission` | 权限提升 |
| `4` | `mcp_tool` | 调用 MCP 工具 |
| `5` | `user_input` | 需要用户输入 |
| `6` | `other` | 其他类型 |
| `7` | `mcp_elicitation` | MCP server 发起 elicitation |
| `8` | `network_policy` | 网络策略或域名规则变更；`[本轮不实现]` |
| `9` | `execpolicy_amendment` | 命令执行策略修订；`[本轮不实现]` |

> 2026-10-20 修正：Bridge 早期把 `permissions` 编成 `2`（`network_access`）、
> 把 `mcp` 编成 `3`（`permission`）、把 `user_input` 编成 `4`（`mcp_tool`）。
> 这与上表不符，已按上表校正：`command=0`、`file_change=1`、
> `network_access=2`、`permission=3`、`mcp_tool=4`、`user_input=5`、
> `other=6`、`mcp_elicitation=7`。客户端应按上表解析，不要依赖旧映射。

`risk`：

| 值 | 名称 |
|---:|---|
| `0` | `low` |
| `1` | `medium` |
| `2` | `high` |
| `3` | `critical` |

`options` 数组的每个元素是子 TLV：

| Tag | 字段 |
|---|---|
| `0x0082` | `decision` |
| `0x0080` | `label` 或 prompt 文本 |
| `0x0084` | `risk` |

`response` 是子 TLV，按 approval_kind 解析：

```text
command_execution: 无额外字段，decision 决定是否放行
file_change:       无额外字段，decision 决定是否放行
network_access:    无额外字段，decision 决定是否放行
permission:        approval_scope + permissions[]；整个分支 [本轮不实现]
user_input:        answers[]
mcp_tool:          mcp_action + mcp_content；requested_permissions[] 为 [本轮不实现]
mcp_elicitation:   answers[]，可选 mcp_action + mcp_content
network_policy:    decision + network_policy_amendment [本轮不实现]
execpolicy_amendment:
                   decision + execpolicy_amendment [本轮不实现]
other:             decision 决定是否放行，不携带自由格式结构
```

`response` 的完整 schema：

| `approval_kind` | 必填 response 字段 | 可选 response 字段 | 状态 |
|---|---|---|---|
| `command_execution` / `file_change` / `network_access` / `other` | 无 | 无 | `[本轮实现]` |
| `permission` | `approval_scope`, `permissions` | 无 | `[本轮不实现]` |
| `user_input` | `answers` | `answer_state` | `[本轮实现]` |
| `mcp_tool` | `mcp_action`, `mcp_content` | `requested_permissions` | `[本轮实现]`（基础 action/content；`requested_permissions` 为 `[本轮不实现]`） |
| `mcp_elicitation` | `answers` | `mcp_action`, `mcp_content` | `[本轮实现]` |
| `network_policy` | `network_policy_amendment` | `approval_scope` | `[本轮不实现]` |
| `execpolicy_amendment` | `execpolicy_amendment` | `approval_scope` | `[本轮不实现]` |

`accept_for_session` 必须同时携带 `session_wide = true`。`decline` 和 `cancel`
不得携带 amendment。v1.0 无法表达某类 response 的设备必须提交
`decision = decline`，不得使用自由文本代替结构化字段。

### 20.5 必需的状态和枚举语义

为避免各端用不同字符串解释同一状态，下列枚举必须在协议层使用数值：

| 枚举 | 常用值 |
|---|---|
| `thread_state` | `0=idle`, `1=running`, `2=completed`, `3=failed`, `4=interrupted`, `5=archived`, `6=unknown` |
| `owner_state` | `0=unmanaged`, `1=observed`, `2=claimed`, `3=managed` |
| `message.kind` | `0=user_text`, `1=assistant_text`, `2=commentary`, `3=tool`, `4=file_change`, `5=error`, `6=approval`, `7=system` |
| `message.status` | `0=pending`, `1=streaming`, `2=completed`, `3=failed`, `4=interrupted` |
| `sender` | `0=user`, `1=assistant`, `2=tool`, `3=system`, `4=device` |
| `snapshot_kind` | `0=summary`, `1=thread`, `2=threads`, `3=approvals`, `4=devices` |

未知枚举值必须按 `unknown` 或保守默认处理，不得断开连接。设备展示时不得把
未知状态误标为 completed 或 failed。

## 21. 错误码

### 21.1 数值表

`ERROR.code` 和 `RESULT.code` 使用以下编号：

| 值 | 名称 | 可重试 | 说明 |
|---:|---|---|---|
| `0x0000` | `OK` | 不适用 | 成功 |
| `0x0001` | `NOT_FOUND` | 否 | 对象不存在 |
| `0x0002` | `BUSY` | 是 | 资源或队列忙 |
| `0x0003` | `UNAUTHORIZED` | 否 | 鉴权失败或权限不足 |
| `0x0004` | `CAPABILITY_NOT_ENABLED` | 否 | 能力未开启 |
| `0x0005` | `INVALID_FRAME` | 否 | 固定头、TLV 或长度非法 |
| `0x0006` | `INVALID_PARAM` | 否 | 参数类型、范围或组合非法 |
| `0x0007` | `INVALID_STATE` | 否 | 当前状态不允许该操作 |
| `0x0008` | `IDEMPOTENCY_CONFLICT` | 否 | idem 被不同请求复用 |
| `0x0009` | `STALE_CURSOR` | 否 | cursor 已不可恢复 |
| `0x000A` | `RESYNC_REQUIRED` | 是，走 resync | 需要快照 |
| `0x000B` | `UPSTREAM_UNAVAILABLE` | 是 | Codex 上游不可用 |
| `0x000C` | `WRITER_CONFLICT` | 否 | 线程已有 writer |
| `0x000D` | `PAYLOAD_TOO_LARGE` | 否 | 超过帧、文本或音频限制 |
| `0x000E` | `UNSUPPORTED_FEATURE` | 否 | 消息、方法或能力不支持 |
| `0x000F` | `TIMEOUT` | 是 | 操作超时 |
| `0x0010` | `POLICY_DENIED` | 否 | 本机策略拒绝 |
| `0x0011` | `REVISION_CONFLICT` | 否 | expected_revision 不匹配 |
| `0x0012` | `DUPLICATE` | 否 | 重复请求，结果见旧记录 |
| `0x0013` | `RATE_LIMITED` | 是 | 超过设备速率限制 |
| `0x0014` | `CANCELLED` | 否 | 用户或系统取消 |
| `0x0015` | `INTERNAL` | 视情况 | Bridge 内部错误，不要暴露敏感信息 |
| `0x0016` | `PROTOCOL_VERSION` | 否 | major 不兼容 |
| `0x0017` | `AUTH_EXPIRED` | 是，重新鉴权 | 会话或令牌过期 |
| `0x0018` | `TOKEN_ROTATED` | 是，重新配对 | 配对令牌已轮换 |
| `0x0019` | `QUEUE_FULL` | 是 | 发送队列满 |
| `0x001A` | `FRAGMENT_ERROR` | 否 | 分片编号、长度或校验错误 |
| `0x001B` | `CONTENT_UNAVAILABLE` | 否 | 内容已被清理或不可读取；需要重新获取快照或摘要 |

### 21.2 错误响应格式

```text
ERROR(
  code,
  request_id      可选，但请求错误应携带,
  error_message,
  error_detail    可选,
  retry_after_ms  可选
)
```

`ERROR` 的 flags 应包含 `RESPONSE`（如果错误对应请求）。协议级错误可以在没有
request_id 时返回。错误消息不得包含令牌、cookie、私钥、完整 prompt 或用户
隐私正文。

### 21.3 错误分类

- 帧或编码错误：返回 `INVALID_FRAME`，可以继续连接。接收方收到对方主动发来的
  `ERROR` 时，不得再回一个 `ERROR`（尤其不得改写成 `UNSUPPORTED_FEATURE`）；
  记录该错误并按错误语义继续或结束即可，否则会形成错误回环。
- 鉴权错误：返回 `UNAUTHORIZED` 或 `AUTH_EXPIRED`，然后关闭连接。
- 能力错误：拒绝该操作，连接保持。
- 上游错误：返回 `UPSTREAM_UNAVAILABLE`，连接保持。
- writer 冲突：拒绝写入，连接保持。
- resync：可以返回 `RESYNC_REQUIRED`，随后进入快照流程。

## 22. 安全策略

### 22.1 transport_security

| 值 | 名称 | 说明 |
|---:|---|---|
| `0` | `plain_lan` | 仅局域网；测试或兼容 |
| `1` | `tls` | TLS，保护传输 |
| `2` | `aead_reserved` | 预留帧级 AEAD；`[本轮不实现]` |
| `3` | `tls_aead` | TLS 加帧级 AEAD；`[本轮不实现]` |

Bridge 可以按设备要求最低安全级别。低于策略的连接在 AUTH 后返回
`POLICY_DENIED` 或 `UNAUTHORIZED`，并在日志中记录原因。

本轮 `transport_security` 只允许值 `0` 和 `1`。值 `2`、`3` 的定义保留给
CBDP v1 的后续兼容实现，但本轮任何端点 MUST NOT 协商、声明或发送。
`aead_reserved` 能力位必须保持 0。需要加密的 micro、panel 和 full 设备本轮
统一使用 TLS；不得通过厂商扩展改变安全语义，也不得把未知安全模式按明文处理。

### 22.2 认证与重放

- AUTH 每个会话只允许一次。
- `session_id`、`server_nonce` 和 `client_nonce` 必须参与证明。
- 不匹配 nonce、重复认证和未知 session 必须拒绝。
- 设备重连后旧连接上的 ACK 不得确认新连接的传输序号。
- 业务 cursor 可以跨连接恢复，但必须校验设备身份。
- 日志中必须对 device_id 之外的身份字段做必要脱敏。

### 22.3 权限边界

能力（capability）和权限（permission）不是同一个概念：

```text
capability：设备固件和用户策略允许做什么
permission：当前线程和当前 turn 是否允许某个动作
```

即使设备具备 `control`，也不能绕过 writer lease 和非接管线程策略。管理页面
可以进一步禁止某个设备审批、控制或接收正文。

## 23. 关闭与错误连接

`CLOSE` 可以双向发送。字段：

| Tag | 字段 |
|---|---|
| `0x0096` | `reason` |
| `0x0062` | `error_message` |

规则：

1. CLOSE 后不再发送新业务消息。
2. critical 发送队列必须优先发送或明确报告丢弃。
3. 发送方等待 ACK 或短暂 drain，再关闭 socket。
4. 接收方收到 CLOSE 后清理会话临时状态，但保留设备 checkpoint、订阅策略和
writer lease 记录。
5. 非正常断线由心跳和 TCP/TLS/WebSocket 错误检测。

## 24. 旧协议兼容网关映射

### 24.1 映射原则

兼容网关把旧 JSON 行协议翻译为 CBDP v1，但不保存业务状态。旧设备看到的
消息类型和字段可以小于 CBDP v1；网关负责预算裁剪。

### 24.2 信封映射

| 旧 JSON 消息 | CBDP v1 |
|---|---|
| `bridge_auth` | `HELLO` + `AUTH` |
| `bridge_auth_ok` | `WELCOME` + `AUTH_OK` |
| `bridge_ready` | `CAPABILITY_UPDATE` 或 `PING` |
| `capabilities_update` | `CAPABILITY_UPDATE` |
| `message` | `SNAPSHOT` / `DELTA` 的 `message.appended` |
| `read_state` | `DELTA.message.read` |
| `device_event` | `COMMAND` 或 `SEND_*` |
| `command_result` | `RESULT` |
| `command_chunk` | 对应的 `FRAGMENT` 或文本事务分片 |
| `protocol_catalog` | `DEVICE_STATUS` + `capability_catalog` |
| `error` | `ERROR` |

旧版手机 SSE 的 `type` 字段按下表映射；未知 `type` 由网关降级为
`notice.created` 或忽略，不得改变 Bridge 的领域状态：

| 旧 SSE `type` | CBDP v1 事件 / 消息 |
|---|---|
| `hello` | `AUTH_OK` + 当前 `capability_catalog` |
| `turn_started` | `turn.started` |
| `turn_completed` | `turn.completed` / `turn.failed` |
| `turn_interrupted` | `turn.interrupted` |
| `item_started` | `item.started` |
| `item_completed` | `item.completed` / `item.failed` |
| `agent_delta` | `message.updated`，带增量文本标记 |
| `command_output` | `item.updated`，`item_type = command` |
| `token_usage` | `token_usage.changed` |
| `thread_status` | `thread.status.changed` |
| `thread_name` | `thread.title.changed` |
| `thread_forked` | `thread.forked` |
| `threads_changed` | `thread.updated` / `thread.created` / `thread.deleted` |
| `resync_required` | `system.resync.required` |
| `rate_limits` | `rate_limits.changed` |
| `approval_request` | `approval.requested` |
| `approval_resolved` | `approval.resolved` |
| `error` | `ERROR` / `notice.created` |

**兼容网关不生成 DELTA 信封。** 网关把旧 JSON 消息翻译成 CBDP 时，`message`
和 `read_state` 必须复用 Event Spine 的已提交事件（同一 `event_id`、
`global_seq`、`topic_seq`、`revision` 和真实 `topic_id`），而不是另造一个只有
`event_type`/`event_payload` 的 `DELTA`。因此统一规则是：**凡是发给 CBDP
设备的业务事件，都从 Event Spine 出站；兼容翻译只适用于旧 JSON 行协议设备。**

### 24.3 字段映射

| 旧字段 | 新字段 |
|---|---|
| `device_id` | `device_id` |
| `capabilities` | `supported_caps` bitmap |
| `message_id` | `message_ref` + 可选 `message_id` |
| `thread_id` | `thread_ref` + 可选 `thread_id` |
| `kind` | `kind` |
| `status` | `status` |
| `title` | `title` |
| `body` | `body` 或 `body_ref` |
| `seq` / `total` | 应用层事务 offset 或 FRAGMENT index |
| `read_by` | `device_ref` 列表或 `message.read` 事件 |
| `request_id` | `request_id` |
| `ok` / error | `state` + `code` |
| `mode=steer` | `turn.steer` 或 `guidance.send`，按活跃 turn 语义决定 |
| `continued_as` | `thread_ref` + `thread.forked` 或 `thread.restored` |
| `append_only` | `message.appended` 的只追加标记 |
| `show_intermediate_events` | `item_stream`、`tool_events` 和 `telemetry` 能力求交 |
| `owner_routed` | `writer_lease` + `owner_state` 投影，不下发桌面 IPC 细节 |
| `pending_approvals` | `approvals.list` 结果或 `approval.requested` 集合 |

### 24.4 兼容限制

- 网关不实现新的 live、历史和 writer lease 能力。
- 旧设备不能绕过 writer lease。
- 旧设备的已读、审批和语音映射必须经过相同命令验证。
- 网关可以降低事件频率、最长文本和队列深度。
- 网关不得把 `plain_lan` 旧链路标记为 `tls` 或 `aead`。
- 网关必须保留消息、turn、审批和事件的稳定 ID 映射，不能因重放生成新对象。
- 网关不得把旧手机的 `approve`、`deny` 或未知 decision 猜测成 policy
  amendment；无法安全映射时必须返回 `INVALID_PARAM`。

## 25. 逐字节示例

以下示例使用大端序。为便于阅读，十六进制字节之间加空格，实际线格式不加。

### 25.1 最小 PING 帧

最小 PING 不需要 TLV。下面给出固定头；按 5.1，完整帧就是这 28 字节：

```text
CB 01 01 05 00 00 00 1C 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00
00 00 02 00
```

解析：

```text
magic = CB 01
version_major = 01
message_type = 05 (PING)
flags = 0000
header_len = 001C = 28
payload_len = 00000000
topic_id = 00000000
seq = 00000000
ack_seq = 00000000
code = 0000
qos = 02
reserved = 00
```

PONG 使用相同结构，`message_type = 06`。

若 PING 携带 `ping_id = 7`，则头部 TLV 增加：

```text
00 97 00 04 00 00 00 07
```

对应 PONG 必须携带：

```text
00 99 00 04 00 00 00 07
```

### 25.2 HELLO 帧

本节给出固定头和字段 TLV；字段 TLV 位于固定头之后。按 5.1，完整帧是：

设备声明：

```text
device_id = esp32-lab
model = esp32
firmware = 0.1.0
profile = 1
supported_caps = status | history | tasks | approval | control | text
transport_security = plain_lan
```

位图值：

```text
status    = bit 0  = 0x00000001
text      = bit 1  = 0x00000002
tasks     = bit 2  = 0x00000004
approval  = bit 4  = 0x00000010
control   = bit 8  = 0x00000100
history   = bit 9  = 0x00000200
supported = 0x00000317
```

字段 TLV：

```text
00 01 00 02 00 00
00 02 00 09 65 73 70 33 32 2D 6C 61 62
00 03 00 05 65 73 70 33 32
00 04 00 05 30 2E 31 2E 30
00 06 00 01 01
00 07 00 08 00 00 00 00 00 00 03 17
00 0A 00 01 00
00 2A 00 04 00 00 00 01
```

固定头：

```text
CB 01 01 01 01 00 00 5F 00 00 00 00
00 00 00 00 00 00 00 01 00 00 00 00
00 00 00 00
```

`message_type = 01`，`flags = 0x0100`，`header_len = 0x005F = 95`，
`payload_len = 0`。字段 TLV 长度为 `6 + 13 + 9 + 9 + 5 + 12 + 5 + 8 = 67`，
固定头 28，总计 95。完整帧由上面的固定头和“字段 TLV”拼接而成；客户端应按
TLV 解析，不应依赖示例中的空格。

### 25.3 WELCOME 帧

Bridge 返回：

```text
version_minor = 0
session_id = 0102030405060708090A0B0C0D0E0F10
server_nonce = 1112131415161718191A1B1C1D1E1F20
auth_mode = hmac_sha256
max_frame_bytes = 256
max_text_chars = 160
queue_depth = 8
heartbeat_seconds = 30
max_chunk_bytes = 96
writer_lease_seconds = 300
request_id = 1
server_time_ms = 1760000000000
```

`HELLO` 和 `WELCOME` 的 `request_id` 在各自方向独立，不要求数值相同。

### 25.4 AUTH HMAC 示例

```text
pairing_token = "0123456789abcdef0123456789abcdef"
session_id = 0102030405060708090A0B0C0D0E0F10
client_nonce = 00000000000000000000000000000000
server_nonce = 1112131415161718191A1B1C1D1E1F20
device_id = "esp32-lab"

data = "CBDP1-AUTH"
     || 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F 10
     || 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
     || 11 12 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F 20
     || "esp32-lab"

auth_proof = HMAC-SHA256(pairing_token, data)
```

本示例的固定测试向量：

```text
auth_proof = 7D F2 DE C2 FA E6 1A 43 D6 91 00 EA 21 7F 30 06
             7E 05 93 7B F1 87 BC E0 2A AF 35 5F 7D C7 4A 92
```

实现必须使用标准 HMAC-SHA256，不得使用设备自定义的字符串拼接方式。测试时
把该向量和实现输出逐字节比较。

### 25.5 SUBSCRIBE 帧

```text
topic_id = 0x1000002A
scope = summary (0)
qos = state (1)
cursor = 0x0000000000000042
request_id = 17
```

固定头字段为：

```text
message_type = 07
flags = 0100 (REQUEST)
topic_id = 10 00 00 2A
seq = 00 00 00 02
ack_seq = 00 00 00 00
qos = 01
```

TLV 包含 `topic_id`、`scope`、`qos`、`cursor` 和 `request_id`。Bridge 返回
`RESULT`，携带当前 cursor。

### 25.6 DELTA message.appended

```text
message_type = 0A
flags = 0400 (EVENT)
topic_id = 10 00 00 2A
seq = 00 00 00 03
qos = 00 (critical)
```

外层 TLV：

```text
event_id = 16 bytes
global_seq = 16
topic_seq = 9
revision = 7
event_type = 0x0301
event_payload = {
  thread_ref = 1001
  message_ref = 2002
  kind = assistant_text
  status = completed
  summary = "Build passed"
  body_ref = "content:2002"
  created_at_ms = 1760000000123
}
```

设备应用成功后，在后续 ACK 中报告 `cursor = global_seq`。

### 25.7 SEND 文本事务

```text
SEND_BEGIN(
  tx_id = 9001,
  idem = 16 bytes,
  mode = auto,
  thread_ref = 1001,
  total_bytes = 11
)
SEND_CHUNK(
  tx_id = 9001,
  offset = 0,
  payload = "hello world"
)
SEND_END(
  tx_id = 9001,
  total_bytes = 11
)
```

Bridge 计算线程空闲且已接管后，转换为 `turn.start`，返回：

```text
RESULT(
  request_id = 9001,       # 等于 tx_id
  method_id = turn.start,
  state = completed,
  result = { thread_ref = 1001, turn_ref = 3003 }
)
```

如果文本包含多字节 UTF-8，`total_bytes` 和 `offset` 都按字节计算，不按字符
计算。每个 chunk 边界应尽量落在 UTF-8 字符边界；Bridge 必须处理跨 chunk 的
不完整 UTF-8，并在 END 后验证最终文本合法。

### 25.8 审批流程

```text
Bridge -> APPROVAL_REQUEST(
  approval_ref = 4004,
  thread_ref = 1001,
  turn_ref = 3003,
  approval_kind = command_execution,
  prompt = "Run tests?",
  options = [accept, decline],
  expires_at_ms = 1760000300000
)

Device -> APPROVAL_RESOLVE(
  approval_ref = 4004,
  decision = accept,
  request_id = 21,
  idem = 16 bytes
)

Bridge -> RESULT(completed)
Bridge -> DELTA(approval.resolved, critical)
```

第二个设备提交决定时返回 `INVALID_STATE`，但必须收到 `approval.resolved`
事件。

## 26. Profile 完整交互

### 26.1 micro 对话流程

```text
HELLO(profile=micro, supported={status,text,approval,read_ack})
WELCOME(max_frame=256, max_text=160, history=0, queue=8)
AUTH / AUTH_OK(effective={status,text,approval,read_ack})
SUBSCRIBE(system.thread_summaries, summary, state, cursor=0)
SNAPSHOT(complete=true, objects=1)
DELTA(message.appended, critical)
ACK(cursor=N)
SEND_BEGIN / SEND_CHUNK / SEND_END
RESULT(turn.start)
DELTA(approval.requested, critical)
APPROVAL_RESOLVE
DELTA(approval.resolved, critical)
DELTA(turn.completed, critical)
```

micro 不接收完整历史，不接收 `telemetry`，不建立 live 之外的高频订阅。

### 26.2 panel 对话流程

```text
HELLO(profile=panel, supported={status,text,tasks,approval,audio,control,history})
WELCOME(max_frame=1024, max_text=512, history=2, queue=32)
AUTH / AUTH_OK
SUBSCRIBE(thread, scope=summary, qos=state, cursor=0)
SNAPSHOT(complete=true, objects=2)
DELTA(message.appended, critical)
DELTA(message.updated, state)
ACK(cursor=N)
AUDIO_BEGIN / AUDIO_CHUNK / AUDIO_END
RESULT(transcript + turn.steer)
APPROVAL_REQUEST / APPROVAL_RESOLVE
DELTA(turn.completed, critical)
```

panel 可以在本地显示最近两条消息摘要，但不得把历史当作控制面事实源。

### 26.3 full 对话流程

```text
HELLO(profile=full, supported=all)
WELCOME(max_frame=65536, history=paged, queue=256)
AUTH / AUTH_OK
SUBSCRIBE(system.thread_summaries, scope=summary)
SUBSCRIBE(thread, scope=thread, cursor=checkpoint)
SNAPSHOT(page 0..n)
DELTA(...)
COMMAND(thread.claim)
COMMAND(turn.start)
DELTA(events...)
ACK(cursor)
```

full 可以不保存完整正文，但要能按 `body_ref` 请求分页内容。任何设备在收到
`RESYNC_REQUIRED` 后都必须重新按 topic 订阅并接收快照。

正文读取使用：

```text
COMMAND(content.read, params={
  content_ref = body_ref,
  content_kind = message_body,
  offset = next_offset,
  max_bytes = max_chunk_bytes
})
RESULT(
  state=completed,
  result={ content_ref, content_kind, content_encoding,
           offset, next_offset, total_bytes, sha256 },
  payload=<data>
)
```

客户端必须循环读取直到 `next_offset == total_bytes`。设备可以在内存压力下
丢弃已读正文，只需保存 `content_ref` 和 `next_offset`。

## 27. 内存布局与资源约束

### 27.1 Bridge 侧对象模型

桥脑内部不得为每条消息保存独立 Python 对象和重复字符串。实现可以采用以下
分层：

```text
EventArena      已提交事件热记录
ObjectRef       32 位实体引用
InternTable     字符串到 u32 映射
TopicIndex      topic_id 到订阅和最新 revision 的索引
SubscriptionSet 有序小集合或 bitmap
BodyStore       正文本体和内容文件
LeaseTable      线程 writer lease
```

Wire 协议只观察 ref 和 cursor。Bridge 可以重建内部索引，但已发送给设备的
`topic_id`、对象 ref 和 cursor 在数据库代际内必须稳定。

### 27.2 事件热记录布局

热事件头 SHOULD 控制在 32 字节以内：

```text
offset  size  field
0       4     event_type_and_flags
4       4     topic_id_and_qos
8       8     global_seq
16      4     revision
20      4     payload_offset
24      4     payload_length
28      4     created_at_delta
```

正文、JSON、完整事件 payload 和长字符串不得放进热头。热头放在连续 arena 中，
过期事件按批次回收，不逐个 free。

### 27.3 紧凑集合

Bridge 和设备端都应按元素规模选择布局：

| 布局 | 适用条件 | 操作特性 |
|---|---|---|
| `InlineSet` | 元素数小于等于 8，元素宽度小于等于 8 | 连续数组，线性查找 |
| `SegmentedVec` | 元素数小于等于 32 | 分段存储，避免大块搬移 |
| `HashIndex` | 更大集合 | 哈希索引指向 arena |
| `Bitmap` | 固定 ID 范围 | O(1) 包含判断，内存密集但可控 |

阈值是实现参数，必须通过配置或编译常量控制，不能写死在协议对端。

### 27.4 字符串驻留

以下字段在 Bridge 内部 SHOULD 驻留为数值：

```text
thread_id    -> u32 thread_ref
turn_id      -> u32 turn_ref
message_id   -> u32 message_ref
device_id    -> u32 device_ref
event_type   -> u16
method_id    -> u16
```

Wire 上 full profile 可以携带原始字符串 ID，micro 和 panel 默认只携带 ref。
设备不需要保存完整 ID 映射；需要展示时向 Bridge 请求或按 ref 更新本地缓存。

### 27.5 设备端最小状态

micro 设备最小内存状态：

```text
device_ref              u32
session_id             16 bytes
server_nonce           16 bytes
pairing_token_hash     32 bytes
subscription[]         topic_id, scope, qos, cursor
current_cursor         u64
inflight[]             seq, flags, length, retries
reassembly[]           stream_id, index, total, buffer
last_status            小结构
pending_approval[]     最多 1 条
outbound_queue[]       最多 queue_depth 条
```

micro 设备不得保存完整消息历史、完整 TLV 表、所有事件 payload 或模型上下文。
无法容纳的字段必须丢弃或截断，并设置相应 flags。

### 27.6 panel 设备最小状态

panel 在 micro 基础上可以增加：

```text
recent_messages[]       最多 history_depth 条摘要
thread_summary[]        当前可见线程摘要
approval_options[]      当前审批候选项
audio_format            当前音频格式
```

仍然不得把正文全文、完整 rollout 或多个 turn 的历史放入 RAM。

### 27.7 内存压力阶梯

Bridge 和设备端都可以按压力调整策略：

| 压力 | 动作 |
|---:|---|
| 70% | 合并 state 事件，停止 telemetry |
| 80% | 清理冷线程 projection，降低分页缓存 |
| 90% | 取消无 live 引用的 follow，缩短队列 |
| 95% | 只保留 critical 事件和活跃 turn 的最小状态 |

审批、完成、失败、writer 冲突、系统关闭和 resync 属于 critical，禁止因为内存
压力静默丢弃。队列满时对 critical 使用回压和持久化，不用丢弃代替处理。

### 27.8 有界参数

默认有界值：

```text
event_ring_capacity       1000
thread_summary_cache      256
messages_per_thread       50
global_message_meta       4096
idempotency_cache         10000
max_fragment_streams      4
max_reassembly_bytes      65536
```

实现可以将这些值暴露为配置，但设备必须能通过 WELCOME 和 CAPABILITY_UPDATE
知道本次会话的实际预算。内存不能随历史会话总数线性增长。

## 28. 一致性测试向量

### 28.1 编解码

每个实现都必须通过以下测试：

1. 28 字节固定头编码和解析。
2. magic 错误时拒绝帧。
3. `version_major != 1` 时返回 `PROTOCOL_VERSION`。
4. `header_len < 28` 时返回 `INVALID_FRAME`。
5. `payload_len` 与剩余字节不一致时拒绝。
6. TLV 长度溢出、截断和尾随垃圾拒绝。
7. 未知 TLV 安全跳过。
8. 已知 TLV 重复时除了可重复字段外返回 `INVALID_FRAME`。
9. TLV 乱序解析结果一致。
10. header TLV 与 payload 边界不混淆。
11. 第 8.12 节的每个 `tlv` 和 `array` 对象都能按必需/可选字段解析。
12. `object_type` 的每个已定义值都能解析；未知值按 opaque object 跳过。
13. `content.read` 的分页 offset、`next_offset` 和 payload 长度一致。
14. `capability_catalog` 每项的位图长度固定为 8 字节。
15. `[本轮不实现]` 的能力位、命令、事件和 transport security 值在测试中只验证
    拒绝与跳过语义，不得作为本轮可用功能通过测试。

### 28.2 握手与鉴权

1. Device 先发 HELLO，Bridge 不先发 WELCOME。
2. HELLO 超时返回 `UNAUTHORIZED` 并关闭。
3. 错误 session_id、错误 nonce、错误 HMAC 都返回 `UNAUTHORIZED`。
4. 同一 session 重复 AUTH 被拒绝。
5. 旧 session 的 AUTH 重放到新 session 被拒绝。
6. AUTH_OK 之前发送 SUBSCRIBE、COMMAND、DELTA 被拒绝。
7. 令牌轮换后旧令牌返回 `TOKEN_ROTATED`。
8. 设备断开重连后传输 seq 从 1 开始。
9. 重连后 cursor 恢复，不重复处理已确认事件。

### 28.3 分片与 ACK

1. 1、2、512 片重组成功。
2. 乱序分片成功重组。
3. 重复分片幂等处理。
4. 缺片超时丢弃整个 stream。
5. 分片总长超限返回 `FRAGMENT_ERROR`。
6. CRC 错误返回 `FRAGMENT_ERROR`。
7. 累计 ACK 只确认连续前缀。
8. 缺口后的帧不被确认。
9. 重传帧使用原 seq 和原字节。
10. critical 帧不因 state 帧占满窗口而无限等待。

### 28.4 能力与预算

1. 设备声明能力和 Bridge 策略求交。
2. 设备声明超出策略的能力不提升 effective。
3. 策略热更新后 CAPABILITY_UPDATE 立即生效。
4. micro 不接收 history 正文。
5. panel 只接收配置的有限消息。
6. full 可以分页读取 history。
7. 超过 max_text_chars 时设置 TEXT_TRUNCATED。
8. 超过 max_frame_bytes 时返回 `PAYLOAD_TOO_LARGE`。
9. transport allowed 不满足时返回 `POLICY_DENIED`。
10. 未开启能力的事件被拒绝。
11. `micro` 和 `panel` 不声明 `queue_manage`、`attachment`、`draft` 或
    `structured_input` 时，Bridge 不下发对应事件或方法。
12. full 声明 `item_stream`、`tool_events`、`mcp` 后可以接收中间 item 事件；
    未声明时只接收最终消息和控制事件。
13. 能力热更新从 full 子集收缩到 micro 子集后，客户端停止接收新能力事件，
    但已提交命令仍按原 idempotency 结果处理。
14. 未声明 `structured_approval` 的设备不能提交 policy amendment；Bridge
    返回 `CAPABILITY_NOT_ENABLED`。
15. `content.read` 按 `content_kind` 校验 `text` 或 `attachment` 能力。
16. `content.read` 返回的 payload 永不超过 `max_chunk_bytes`。
17. 同一对象的 `thread_ref` 与 `thread_id` 指向不同对象时返回
    `INVALID_PARAM`。
18. `body` 与 `input_parts` 同时出现时返回 `INVALID_PARAM`。

### 28.5 事件与恢复

1. topic_seq 和 revision 单调递增。
2. state 事件合并后保留最新状态。
3. critical 事件不被合并或丢弃。
4. cursor 落后于保留窗口时返回 `RESYNC_REQUIRED`。
5. SNAPSHOT 多页期间使用同一 revision。
6. SNAPSHOT 未完成时设备不提交本地状态。
7. 设备 ACK 后重连只收到后续 DELTA。
8. 设备使用旧 ref 时返回 `RESYNC_REQUIRED`。
9. 审批、完成和失败事件在压力下不丢失。
10. ACK 丢失时发送方重传，接收方不重复应用业务事件。
11. 发送 `[本轮不实现]` 的 `thread.goal.*`、`context.compact` 或 `batch.execute`
    时返回 `UNSUPPORTED_FEATURE`，且不改变任何业务状态。

### 28.6 命令与所有权

1. 同一 idem 重试返回原 RESULT。
2. 同一 idem 不同参数返回 `IDEMPOTENCY_CONFLICT`。
3. 执行异常返回 `state = unknown`，不自动重放。
4. 无有效 writer lease 的线程写入返回 `WRITER_CONFLICT`。
5. 显式接管后 Bridge 可以写，桌面端也必须通过 Bridge 写。
6. 旧 lease generation 释放新 lease 被拒绝。
7. 多个设备并发提交同一线程命令，结果串行化且顺序可解释。
8. 桌面 Adapter 绕过 Bridge 直写会导致租约撤销并广播 `thread.owner.changed`。
9. 审批 first-valid-response-wins。
10. 过期审批返回 `TIMEOUT`，不改变已生效决定。
11. `queue.add` 在忙碌线程中不隐式中断；`queue.start` 的 `BUSY` 不改变队列。
12. 队列并发 reorder 使用 revision 检测，旧 revision 返回
    `REVISION_CONFLICT`。
13. `guidance.send` 对空闲线程返回 `INVALID_STATE`，对活跃 turn 发布
    `guidance.applied`。
14. `message.edit` 产生新 revision，旧 `message_ref` 变为
    `replaced_by_ref`，设备不能覆盖删除 tombstone。
15. `message.range.delete` 的闭区间校验和越界行为符合协议。
16. `thread.revert` 与 `thread.fork` 不复制 writer lease，旧 lease 不得操作
    新分支。
17. `input.respond` 提交多问题回答后，重复回答返回 `INVALID_STATE`，敏感
    字段不出现在日志和事件正文。
18. `attachment.upload.begin/chunk/end` 的乱序、大小和 SHA-256 校验均生效。
19. 没有有效 writer lease 的写入返回 `WRITER_CONFLICT`；桌面 Adapter 绕过
    Bridge 的外写会撤销 lease 并广播 `thread.owner.changed`。
20. `context.compact` 和 `batch.execute` 的保留 schema 只做稳定性测试；
    本轮不得把它们视为可用命令。
21. `events.since` 在 cursor 过期时返回 `RESYNC_REQUIRED`。
22. 旧网关映射 `item_started`、`item_completed`、`token_usage`、
    `thread_name`、`thread_forked`、`rate_limits` 和 `resync_required` 后，
    Bridge 领域状态与直接 CBDP 客户端一致。

### 28.7 性能和内存

1. 100 个历史线程、0 个 live 订阅时，空闲 CPU 接近基线。
2. 单个 171 MiB rollout 变化时，处理成本只与新增内容相关。
3. 10 个 micro/panel/full 混合设备并发时，慢设备不阻塞其他设备。
4. 高频 state 事件在合并窗口内合并。
5. 内存压力 70/80/90/95% 时按阶梯降级。
6. critical 事件在 95% 压力下仍可 ACK 和恢复。
7. 设备断线不导致事件队列无界增长。
8. Bridge 重启后 cursor 可恢复或明确要求 resync。

### 28.8 Codex 能力覆盖矩阵

下表是 CBDP v1 首版与后续扩展的功能覆盖基线。`能力位` 是设备声明和 Bridge
策略求交的键；`适用 profile` 是协议允许声明该能力的范围，不代表设备必须
实现；`状态` 只描述 CBDP v1 线协议适配状态，不改变现有 JSON/companion
API 的行为。

| 功能 | 能力位 | 命令 | 事件 | 主要 TLV tag | 适用 profile | 状态 |
|---|---|---|---|---|---|---|
| 状态与心跳 | `status` | `status.read` | `device.online`, `device.offline`, `device.load` | `0x0090..0x009A` | micro/panel/full | `[本轮实现]` |
| 线程列表与详情 | `tasks`, `text` | `threads.list`, `thread.read` | `thread.created`, `thread.updated` | `0x0021..0x0026`, `0x0040..0x0057` | micro/panel/full | `[本轮实现]` |
| 文本回复 | `text` | `SEND_*` -> `turn.start`/`turn.steer` | `turn.started`, `turn.completed`, `turn.failed` | `0x0070..0x007B` | micro/panel/full | `[本轮实现]` |
| 音频与语音回复 | `audio`, `voice` | `AUDIO_*` -> `turn.start`/`turn.steer` | `turn.*`, `message.*` | `0x0073..0x007A` | panel/full | `[本轮实现]` |
| 基础审批 | `approval` | `approvals.list`, `approval.resolve`（`decision=0..3`）, `mcp.respond`（`mcp_action`/`mcp_content`） | `approval.requested`, `approval.resolved`, `approval.expired` | `0x0080..0x0087` | micro/panel/full | `[本轮实现]` |
| 结构化审批与权限修订 | `structured_approval` | `approval.resolve` 的 `decision=4/5`、`response` scope/permissions/amendment | 不新增事件；仍使用审批事件 | `0x013D..0x014F` | full | `[本轮不实现]` |
| 单条已读 | `read_ack`, `read_unread` | `message.read.update` | `message.read` | `0x0054`, `0x01B7..0x01BB` | micro/panel/full | `[本轮实现]` |
| 队列 | `queue` | `queue.add`, `queue.list` | `queue.changed`, `queue.item.*` | `0x0100..0x0107` | panel/full | `[本轮实现]` |
| 队列管理 | `queue_manage`, `queue_priority` | `queue.update/delete/reorder/promote/start` | `queue.changed`, `queue.reordered` | `0x0100..0x0107` | full | `[本轮实现]` |
| 引导 | `guidance` | `guidance.send/update/cancel` | `guidance.*` | `0x0108..0x010C` | panel/full | `[本轮实现]` |
| 单条消息编辑 | `message_edit`, `message_revision` | `message.edit` | `message.revised`, `revision.conflict` | `0x0110..0x0118` | full | `[本轮实现]` |
| 单条消息删除 | `message_delete` | `message.delete` | `message.deleted`, `tombstone.changed` | `0x0111..0x0118` | full | `[本轮实现]` |
| 范围删除与回退 | `message_delete`, `message_revision` | `message.range.delete`, `thread.revert` | `message.range.deleted`, `thread.reverted` | `0x0112..0x0118` | full | `[本轮实现]` |
| 线程生命周期 | `thread_manage`, `thread_lifecycle` | `thread.rename/archive/unarchive/pin/unpin/delete/restore` | `thread.archived`, `thread.pinned`, `thread.deleted`, `thread.restored` | `0x0120..0x012F` | panel/full | `[本轮实现]` |
| 线程分叉 | `thread_fork` | `thread.fork` | `thread.forked`, `thread.branched` | `0x0115`, `0x0120..0x012F` | full | `[本轮实现]` |
| 线程设置 | `thread_settings` | `thread.settings.read/update`, `thread.metadata.update` | `thread.settings.changed`, `thread.metadata.changed` | `0x0051`, `0x0052`, `0x0124..0x012F` | full | `[本轮实现]` |
| 线程目标 | `thread_goal` | `thread.goal.get/set/clear` | `thread.goal.changed`, `thread.goal.cleared` | `0x0129..0x012B` | full | `[本轮不实现]` |
| 结构化输入 | `structured_input`, `server_request` | `input.respond` | `input.requested`, `input.answered`, `input.cancelled` | `0x0130..0x013C` | panel/full | `[本轮实现]` |
| 服务端请求 | `server_request` | `tool.respond` | `server.request.created`, `server.request.resolved` | `0x019C..0x019D` | full | `[本轮实现]` |
| 附件与输入部件 | `attachment`, `file_reference`, `image_input`, `audio_input` | `attachment.add/list/remove`, `attachment.upload.*` | `attachment.*` | `0x0150..0x0166` | panel/full | `[本轮实现]` |

### 19.8 附件与 `input_parts` 实现修正（2026-10-20）

前面章节定义的附件能力此前只有协议没有 Bridge 实现；本版本补齐：

- `attachment.add/list/remove` 与 `attachment.upload.begin/chunk/end/cancel` 现在
  由 Bridge 统一命令层实现，内容保存在
  `%LOCALAPPDATA%\CodexBridge\attachments\`。每个线程最多保留最近 64 个附件，
  单附件上限 32 MiB，单分片上限 2 MiB。
- 上传分片按 `upload_offset` 顺序写入，乱序返回
  `UPLOAD_OFFSET_MISMATCH`；`content_sha256` 不匹配返回
  `CONTENT_HASH_MISMATCH`；`attachment_kind` 与 `mime_type` 不匹配返回
  `ATTACHMENT_KIND_MISMATCH`。
- `content.read(0x00C1)` 返回分页内容，`content_encoding` 为 `base64`。
- `turn.start(0x0005)` 与 `queue.add(0x0030)` 的 `input_parts(0x015B)` 已解析：
  元素 tag 为 `0x00A0`，图片部件转成 Codex `{"type":"image","url":<inline data URL>}`，
  文本部件按顺序插入。`body` 与 `input_parts` 同时出现仍返回 `INVALID_PARAM`。
- 手机 Companion HTTP 的完整接入流程见
  [CodexBridge-mobile-attachment-api.md](CodexBridge-mobile-attachment-api.md)。

### 19.9 `threads.list` 项目过滤与聚合计数补发（2026-10-20）

联调发现设备在选中项目后无法列出任务，且首页计数长期为 0。Bridge 侧修正：

- `threads.list(0x0002)` 接受可选 `project_ref(0x0122)`。Gateway 通过持久引用表把
  引用解析为 `project_id`；解析失败返回 `NOT_FOUND`，不得落成 `INTERNAL`。
  解析成功后按该项目的 `roots` 对线程 `cwd` 做前缀匹配；`project_id` 与
  `project_name` 两种持久键都可解析。`threads.list` 的可选参数为
  `limit`、`search`、`cursor`、`project_id`。
- 设备订阅 `system.thread_summaries(0x50000003)` 的 `summary` scope 时，Bridge
  必须在回放结束前补齐当前 `system.codex_state(0x0704)`，即使窗口里已有旧计数也
  要用当前值覆盖。若最新计数事件已在回放窗口内，直接补发；若在窗口外但 global_seq
  已大于设备 cursor，直接补发该已提交事件；若窗口外旧事件的 global_seq 不大于
  cursor（例如 cursor = 0 的最新窗口已越过它），Bridge 追加一条内容相同的新事件
  再补发，保证 DELTA 的 `global_seq` 严格大于设备刚报告的 cursor。补发使用同一
  `topic_id`、`summary` scope 与 `state` QoS，字段仍为 `running_count(0x00BC)`、
  `completed_unread_count(0x00BD)`、`failed_unread_count(0x00BE)`。

### 19.10 事件库自检与恢复（2026-10-20 后续修正）

设备反馈 `SUBSCRIBE` 在 summary 主题上返回 `INTERNAL`，只读取证确认
`cbdp-events.sqlite3` 的 `idx_events_topic_seq` 索引页损坏：不带主题过滤的查询
仍可能成功，但协议使用的
`WHERE topic_id = ? ORDER BY global_seq DESC` 直接报
`database disk image is malformed`。本修正不改变 CBDP 线格式和订阅字段，只补齐
Bridge 的故障恢复语义：

- Bridge 打开事件库前必须执行 `PRAGMA quick_check`。返回 `ok` 时不得改动数据文件，
  也不得创建备份。
- 校验失败时，先把主库及现有 `-wal` / `-shm` 复制为
  `cbdp-events.sqlite3.corrupt-<时间戳>`，先执行 WAL checkpoint 把已提交的近期事件
  合并进主库，再对原库执行 `VACUUM INTO`。重建结果必须再次通过 `quick_check`，
  且事件行数不得少于恢复前通过主库和 WAL 可见的行数，之后才能原子替换原库。
  替换前必须移除旧侧车文件，避免旧 WAL 再次覆盖新库。
- 若 `VACUUM INTO` 失败或丢失行，Bridge 必须逐行扫描事件表并把可读行写入新库。
  只有逐行救援也失败时，才保留损坏备份并新建空事件库继续启动；不得静默删除唯一
  副本，也不得让整个 Bridge 因索引损坏无法启动。
- 恢复成功后，`global_seq` / `topic_seq` / `revision` 继续从恢复库的最大值递增，
  设备已有的 cursor 语义不变。原先仍可读取的历史事件不应因重建而丢失。
- 运行期间若存储页再次报错，`SUBSCRIBE` 返回
  `ERROR(INTERNAL, "EVENT_STORE_UNAVAILABLE")`，并在 `error_detail(0x0063)` 带
  `reason = event_store_unavailable`。设备可以把这类错误与普通参数错误区分开，
  按有界退避重试订阅。
- 验收要求：`PRAGMA quick_check` 为 `ok`；AUTH_OK 后 summary `SUBSCRIBE` 收到
  `RESULT`；随后收到包含 `running_count` 的 `system.codex_state(0x0704)`。

### 19.11 手机回复桌面活跃线程的 owner 路由（2026-10-21 后续修正）

手机 Companion 写入必须和桌面 Codex 共用同一 writer，不能因为 Bridge 注入共享
app-server 就跳过 DesktopIPC owner discovery：

- `CompanionService` 的 `client_factory` 只决定 Bridge 自己的 app-server 来源，
  不再表示“与桌面隔离”。隔离必须由显式 `isolated=True` 表达，仅测试和嵌入式
  调用使用。
- 生产 `start_turn` 必须先调用 `_desktop_owner(thread_id)`。发现桌面 owner 时，
  通过 `thread-follower-start-turn` 交给桌面 Codex；桌面端实时显示手机输入和
  Codex 回复，手机流式输出来自同一个 turn。
- 没有桌面 owner 时，手机新建或独立的线程仍使用 Bridge app-server，行为不变。
- 已知的桌面线程若 owner discovery 暂时失败，必须返回错误并保持写入未发送，
  不得清掉 writer lock 后退回第二套 app-server。两个 app-server 同时写同一线程
  会产生竞争、turn 卡死和中途停止。
- 验收要求：桌面打开活跃会话时，手机回复立即出现在桌面；手机流式回复完整结束；
  `thread.read` 的消息列表与桌面一致，无需重启或重新打开桌面会话。

### 19.12 turn 终态事件（2026-10-21 后续修正）

聚合计数 `system.codex_state(0x0704)` 只描述首页徽标数量，不能可靠表达“哪一条
任务刚刚完成”。Bridge 在结果记录落库时同时写线程 scope 的终态事件：

| 结果状态 | event_type | scope | qos |
|---|---|---|---|
| `completed` | `turn.completed(0x0203)` | `thread` | `critical` |
| `failed` | `turn.failed(0x0204)` | `thread` | `critical` |
| `interrupted` | `turn.interrupted(0x0205)` | `thread` | `critical` |

- topic 使用该结果的 `thread_id` 对应 topic，不使用 summary topic。设备选中任务后
  已订阅该线程 topic + `thread` scope，不需要新增订阅。
- Payload 使用既有 tag：`THREAD_ID(0x0022)`、`THREAD_REF(0x0021)`、
  `TURN_ID(0x0024)`、`TURN_REF(0x0023)`、`KIND(0x0045)`、`STATUS(0x0044)`、
  `TITLE(0x0040)`、`SUMMARY(0x0041)`、`BODY(0x0042)`、
  `MESSAGE_ID(0x0026)`、`COMPLETED_AT_MS(0x004E)`。`BODY` 为主显示字段，
  `SUMMARY` 为回退。
- 同一 `thread_id + turn_id` 只写一次；缺少 turn_id 时退化为稳定的 result
  `message_id` 去重。collector 周期重复发布同一结果不得重复产生终态事件。
- `0x0704` 聚合计数字段和 summary scope 语义保持不变。客户端应优先用
  `0x0203/0x0204/0x0205` 做逐任务提醒，用 `0x0704` 更新数量徽标。
- 验收要求：4 秒内连续完成两条不同任务必须产生两条不同 turn 终态事件；其它
  客户端已读导致 `completed_unread` 回落再上涨时，不应仅凭计数重复提醒。

### 19.13 手机配对令牌持久化（2026-10-21 后续修正）

`%LOCALAPPDATA%\CodexBridge\companion.json` 是手机长期令牌的唯一主存储，
`devices.json` 中的 phone 记录是可用于恢复的投影。重启或升级必须保持令牌不变：

- `CompanionStore.load()` 在文件不存在时允许从空状态启动，但必须先尝试从
  `devices.json` 的 phone 记录恢复 token_hash、client_id、enabled 和 last_seen。
- 读取权限/IO 异常必须失败关闭，未成功加载磁盘状态时 `save()` 不得覆盖原文件。
- JSON 或 UTF-8 损坏时先把原文件隔离为 `companion.json.corrupt*.bak`，记录
  `load_warning`，再从 registry 恢复并写出新的健康文件；不得静默丢弃后直接用
  一条新配对覆盖全部旧记录。
- 保存必须先写同目录临时文件、fsync，再原子替换正式文件；Windows 短暂占用时
  必须有限重试，最终失败要返回明确错误，不得静默丢失。
- 验收要求：配对一部手机，重启 Bridge，手机旧令牌仍可授权；15 条手机记录在
  重启后仍保持 15 条，除非用户显式删除。

### 19.14 队列派发与死 turn 兜底（2026-10-21 后续修正）

手机文本进入待发送队列后，Bridge 只有在目标线程没有活跃 turn 时才派发。桌面
Codex 与 Bridge 曾因双 writer 竞争产生一个被杀死、但没有 `completedAt` 的
`interrupted` turn；旧逻辑把它永久视为活跃，队列再也不派发，`on_turn_finished`
也永远不触发。协议侧要求：

- 陈旧窗口内仍有活动的 `interrupted` turn 可以视为活跃，防止真正的桌面 turn
  被队列并发写入。
- 超过 `LOG_ACTIVE_SECONDS`（150 秒）仍无终态时间或新活动的 `interrupted`
  turn 必须视为已死亡，释放该线程的队列，继续派发待发送项。
- 队列不得因为单条失败项或陈旧 turn 永久暂停；新增消息、删除失败项、真实 turn
  完成或陈旧窗口到期都应解除对应线程的阻塞。

### 19.15 首页计数的 UDP 旁路（2026-10-21 后续修正）

AI Passport 首页在后台播放网络电台时，同时存在的电台 TCP 流与 CBDP 控制连接会
耗尽 ESP32-C3 的 DMA 内存（每条连接各自预留 `TCP_SND_BUF + TCP_WND`）。首页只需要
三个聚合数字，因此 Bridge 增加一条轻量 UDP 旁路：

- 新增消息类型 `HOME_COUNTERS(0x1E)`。Bridge 在 UDP 8764 上收到设备 presence 后，
  向该设备单播该帧；只单播，不广播。
- `topic_id` 填 `system.thread_summaries(0x50000003)` 作为标记，`qos = state`、
  flag 带 `EVENT`。header TLV 为 `running_count(0x00BC)`、
  `completed_unread_count(0x00BD)`、`failed_unread_count(0x00BE)`，全部使用既有
  tag，不新增字段。整个报文必须小于一个 MTU。
- 节奏为 1~2 秒一次；计数发生变化时立即补发一次。目标只来自主动 presence 的设备，
  设备离开后不再接收。
- 这是**新增旁路**：`system.codex_state(0x0704)`、summary 订阅、握手和命令流程
  全部保持不变。设备可以据此让首页完全不开 CBDP TCP 会话，进入 Codex 页时再照常
  握手订阅。
- 已批准设备发出有效 UDP discovery/presence 会刷新 `last_seen`，最近 30 秒内即使
  没有 CBDP TCP 会话也按在线展示。`online` 只表示设备可达；任务控制、订阅和上行
  命令仍要求建立并完成 CBDP 会话。pending、rejected、revoked 设备不适用。
  已批准设备即使收到携带旧 pending nonce 的重复 discovery/presence，也不得被降级
  为待批准或清空已配对令牌。
- 双路径到达时由设备侧按"取较大值 + 落地窗口"去重；Bridge 不改变 TCP 计数的语义。
- 验收要求：首页无 TCP 会话时三个数字仍能 1~2 秒内更新；进入 Codex 后任务流程
  无回归；单包不触发 ESP32-C3 的 DMA 申请失败。

| 草稿 | `draft` | `draft.update/delete` | `draft.updated`, `draft.deleted`, `draft.expired` | `0x0168..0x016C` | full | `[本轮实现]` |
| 批处理 | `batch` | `batch.execute` | `command.*`, 子操作对应事件 | `0x016D..0x016F` | full | `[本轮不实现]` |
| 活动线程与恢复 | `cross_device_active`, `resume` | `active_state.read/set`, `events.since` | `active.*`, `events.since.result`, `resume.required` | `0x0170..0x0176`, `0x00A5` | panel/full | `active_state.read/set` `[本轮实现]`；`events.since` 与 `resume.*` `[本轮不实现]` |
| 工具与 item 流 | `item_stream`, `tool_events` | `command.status` | `item.*`, `tool.*`, `tool.redacted` | `0x0190..0x019B`, `0x01A8..0x01AD` | full | `[本轮实现]` |
| 计划 | `plan` | 通过 turn 事件和 `item_stream` | `plan.updated` | `0x01A1..0x01A3` | full | `[本轮实现]` |
| 推理摘要 | `reasoning_summary` | 通过 turn 事件和 `item_stream` | `reasoning.summary` | `0x01A0`, `0x01AC..0x01AD` | full | `[本轮实现]` |
| MCP 状态与交互 | `mcp` | `mcp.respond` (basic action/content) | `mcp.status.changed`, `mcp.request`, `mcp.oauth.completed`; `requested_permissions` is `[本轮不实现]` | `0x0141..0x0143`, `0x0198..0x0199` | full | `[本轮实现]` |
| 账户与额度 | `account` | `account.read`, `account.rate_limits.read`, `account.token_usage.read` | `account.changed`, `rate_limits.changed`, `token_usage.changed` | `0x0180..0x0186`, `0x018D..0x018F`, `0x01AE..0x01B4` | panel/full | `[本轮实现]` |
| 上下文使用 | `context_usage` | `context.read` | `context.usage.changed` | `0x0187..0x0189`, `0x01AE..0x01B4` | panel/full | `[本轮实现]` |
| 上下文压缩 | `context_compact` | `context.compact` | `context.compaction.*` | `0x018A..0x018C` | full | `[本轮不实现]` |
| 搜索 | `thread_search` | `thread.search` | `search.result` | `0x01BC..0x01BE` | full | `[本轮实现]` |
| 模型列表与切换 | `model_switch` | `models.list`, `thread.settings.update` | `model.catalog.changed` | `0x0051`, `0x0124`, `0x019E..0x019F` | panel/full | `[本轮实现]` |
| 项目列表与归属 | `project_manage` | `projects.list`, `thread.settings.update` | `project.catalog.changed` | `0x0122..0x0123` | full | `[本轮实现]` |
| 系统通知 | `notice` | 由 Bridge/上游触发 | `notice.created`, `system.*` | `0x019A..0x019B` | panel/full | `[本轮实现]` |
| 订阅与事件同步 | `subscribe_live`, `subscribe_thread`, `subscribe_summary`, `multi_page`, `history` | `events.since` | `system.*`, `events.since.result` | `0x0030..0x003F`, `0x00A0..0x00A9` | micro/panel/full | `[本轮实现]` |
| 写入所有权 | `writer_lease`, `control` | `thread.claim/release`, `turn.start/steer/interrupt` | `thread.owner.changed`, `device.writer.changed` | `0x006C..0x006D`, `0x00A8` | panel/full | `[本轮实现]` |
| 最终结果 | `results` | 通过 `turn.completed` / `turn.failed` | `turn.completed`, `turn.failed` | `0x0041`, `0x004E`, `0x01B5` | micro/panel/full | `[本轮实现]` |
| 遥测 | `telemetry` | 不适用 | `turn.progress`, `device.load`, `token_usage.changed` | `0x0093`, `0x009A`, `0x01AE..0x01B4` | panel/full | `[本轮实现]` |
| 设备配置 | `device_config` | `device.policy.read`, `device.config.update` | `device.policy.changed` | `0x0017`, `0x0018`, `0x0095` | panel/full | `[本轮实现]` |
| 命令状态 | `command_status` | `command.status` | `command.progress`, `command.completed`, `command.failed`, `command.unknown` | `0x002A`, `0x002C`, `0x0064..0x0067` | panel/full | `[本轮实现]` |
| 输入能力目录 | `input_capabilities` | `input.capabilities.read` | `model.catalog.changed` | `0x019E..0x019F` | panel/full | `[本轮实现]` |
| 消息引用 | `message_reference` | `turn.start`, `queue.add`, `input.respond` 的 `references` | `message.appended` | `0x0160..0x0166`, `0x01AB` | full | `[本轮实现]` |
| 基础中断 | `control` | `turn.interrupt` | `turn.interrupted` | 无 | panel/full | `[本轮实现]` |
| 中断模式 | `interrupt_mode` | `turn.interrupt` 的 `interrupt_mode` 参数 | `turn.interrupted` | `0x01A4` | panel/full | `[本轮不实现]` |
| 实时媒体预留 | `realtime_reserved` | 不适用 | 不适用 | 不分配 | full | `[本轮不实现]` |
| 事件窗口 | `events_since` | `events.since` | `events.since.result` | `0x0033`, `0x00A5`, `0x01A5..0x01A6` | panel/full | `[本轮实现]` |
| 传输安全 | `tls` | 不适用 | 不适用 | `0x000A`, `0x0018` | micro/panel/full | `[本轮实现]` |
| 帧级压缩 | `compression` | 不适用 | 不适用 | `0x0002` | micro/panel/full | `[本轮不实现]` |
| 帧级 AEAD | `aead_reserved` | 不适用 | 不适用 | `0x000A` | micro/panel/full | `[本轮不实现]` |
| 厂商扩展 | `vendor`, `experimental_ext` | 厂商自定义 | 厂商自定义 | `0x01C0..0x01FF`, `0x8000..0xFFFF` | panel/full | `[本轮不实现]` |
| 旧协议网关 | 全部 | 旧 JSON -> `COMMAND` / `RESULT` | 24.2 映射表全部事件 | 8.11 统一字段 | 所有旧设备 | `[本轮实现]` |

`content.read` 不单独占用能力位：读取消息和工具正文使用 `text`，读取附件内容
使用 `attachment`。未声明相应能力的设备不得调用，Bridge 返回
`CAPABILITY_NOT_ENABLED`。

首版手机端最小闭环是：线程列表/详情、普通回复、队列添加、引导、审批、结构化
输入、附件、草稿、已读、线程生命周期、上下文/额度、工具与 MCP 状态、事件恢复。
首版 ESP32-S3 `panel` 最小闭环是：摘要、短文本、审批、音频、有限控制和活动
状态同步。首版 ESP32 `micro` 最小闭环是：摘要、短文本、审批、音频和最小控制
状态。三者共享同一事件编号和错误码，不共享同一内存预算。

### 19.16 无线配对消息（2026-10-24 后续补充）

原始 v1.0 把 `0x1F` 标为保留。为了让没有 USB 的纯 CBDP 设备可以完成首次接入，
后续实现正式分配并标记以下内容：

- 消息类型 `PAIRING(0x1F)`，方向为 Bridge 到设备，作为 HELLO 配对请求的响应。
- TLV `pairing_state(0x00C0)`，取值 `0=none,1=request,2=offer`。
- TLV `pairing_token(0x00C1)`，UTF-8 令牌，长度 16..64 字节。
- `HELLO.pairing_state` 的可选请求语义，以及 `PAIRING_PENDING` 错误原因。
- 纯 CBDP 设备没有旧 JSON `registration_nonce` 时，本地管理员批准会直接生成令牌；
  设备在下一次 HELLO 中通过 `PAIRING` 取得该令牌，不再依赖 `register_offer`。

旧客户端、旧 ESP 固件和旧手机端不需要实现新消息；它们省略 `pairing_state` 即可
继续使用原有 HELLO/WELCOME/AUTH 流程。

### 19.17 `thread.start` 项目目录解析（2026-10-25 后续修正）

设备从 `projects.list` 拿到的是紧凑 `project_ref`，而 Codex app-server 需要真实
工作目录。因此 `thread.start` 携带 `project_ref` 时，Bridge MUST 在网关层把它
解析成项目目录，再把 `cwd` 交给命令层：

1. `project_ref` 通过引用表解析为项目 ID；无法解析返回 `NOT_FOUND`。
2. 项目存在但没有可用目录时返回 `INVALID_PARAM`。
3. 解析结果写入 `cwd`，`project_id` MUST NOT 透传给 app-server。
4. 未携带 `project_ref` 时保持原有行为（使用请求中的 `cwd` 或 Bridge 默认目录）。
5. 同时提供 `project_ref` 与不一致的 `project_id` 时返回 `INVALID_PARAM`。

Bridge 监听绑定和桌面交接两条创建路径 MUST 使用同一个解析出的 `cwd`。

### 19.18 `thread.start` 继承设备最近选择的项目（2026-10-25 后续修正）

现网设备把“选择项目”编码为 `threads.list(project_ref)`：项目对象返回
`project_ref` 后，设备用同一个引用请求该项目的任务列表。部分已发布固件没有
额外的项目参数，新建任务时会发送不带 `cwd` / `project_ref` 的
`thread.start(0x0004)`。为了让这些设备无需升级就能在所选项目下建任务，
Bridge MUST 在 CBDP 网关层按 `device_id` 记住最近一次用于 `threads.list`
查询的 `project_ref`，并在该设备后续无参数 `thread.start` 时把该项目解析成
`cwd`。

优先级固定为：

1. `thread.start` 显式携带的 `cwd`。
2. 同一 `thread.start` 显式携带的 `project_ref` / `project_id`。
3. 该设备最近一次 `threads.list(project_ref)` 记录的项目。
4. Bridge 默认工作目录。

记忆状态 MUST 按设备隔离，且只在 Bridge 进程内保留；设备重连后重新执行
`threads.list(project_ref)` 即可恢复。项目引用无法解析或没有可用目录时沿用
§19.17 的 `NOT_FOUND` / `INVALID_PARAM` 语义。该记忆不能让一个设备的项目
选择影响另一个设备，也不能覆盖 `thread.start` 的显式参数。

### 19.19 `thread.start` 保留桌面项目归属（2026-10-25 后续修正）

Codex app-server 的线程目录和桌面项目归属是两份独立信息。Bridge 解析出项目
目录后，除了把 `project_ref` 转换成 `cwd`，还 MUST 把该项目的 canonical
`project_id` 传递给 `thread/start` 的 `projectId` 字段。否则线程虽然落在正确
目录、rollout 和回复也都正常，Codex 桌面的项目侧边栏仍然不会显示该线程。

规则：

1. 只有 Bridge 项目目录中真实存在的 canonical `project_id` 才能写入
   `projectId`；项目名、未知 ID 和空值 MUST NOT 透传。
2. `project_ref` 解析失败仍按 §19.17 返回 `NOT_FOUND`，不能创建默认目录任务。
3. `cwd` 与 `projectId` MUST 来自同一次项目解析，避免线程落在 A 项目目录却
   归属到 B 项目。
4. 未携带项目信息的旧客户端行为不变，`projectId` 可省略。

## 29. 并行开发接口清单

### 29.1 Bridge Agent

Bridge Agent 负责：

- 固定头、TLV、flags、消息类型和错误码编解码。
- Event Spine、Projection Store、topic_id 和 revision 分配。
- 会话状态机、HMAC 鉴权、能力求交和策略热更新。
- 订阅管理器、cursor、ACK、重传和 resync。
- writer lease、幂等命令、审批仲裁和上游适配器边界。
- 兼容网关只做协议翻译，不复制业务状态。

Bridge 完成定义：

- 所有第 28.1、28.2、28.3、28.4、28.5、28.6 测试通过。
- 空闲无 live 订阅时不建立桌面 follow。
- 设备断线后队列有界，测量值不随历史增长。

### 29.2 手机端 Agent

手机端 Agent 负责：

- TCP/TLS、WebSocket、SSE 绑定和 HTTP 反向通道。
- HELLO、WELCOME、AUTH、AUTH_OK、心跳和 CLOSE。
- full profile 的 SUBSCRIBE、SNAPSHOT、DELTA、ACK 和 RESYNC。
- COMMAND、RESULT、审批、文本和音频事务。
- `content.read` 分页正文、工具输出和附件内容。
- 本地缓存使用 cursor 和 revision，不使用传输 seq 作为业务状态。

手机端完成定义：

- 断线重连可以从 checkpoint 继续。
- 不下载全部历史也能查看线程摘要、发送回复和审批。
- SSE 路径和 WebSocket 路径产生相同领域状态。
- 无有效 writer lease 时不显示可写入口；任何写入意图都必须通过 Bridge 提交。

### 29.3 ESP32 micro Agent

micro Agent 负责：

- TCP binary 和可选 TLS binding；v1.0 不启用帧级 AEAD。
- 小帧编解码、TLV 跳过、长度保护和 CRC 校验。
- 最多 2 个订阅、8 条发送队列、无历史恢复。
- critical 事件、摘要、短文本、审批和音频上传。
- 固定 arena 或静态结构，不使用无界 malloc。

micro 完成定义：

- 能在 256 字节默认帧预算下完成握手、订阅、回复和审批。
- 不保存完整历史，不解析桌面或 rollout 原始协议。
- 断电重连后从 cursor 恢复或进入 SNAPSHOT。
- 内存压力下不丢失 approval、completed、failed 和 resync。

### 29.4 ESP32-S3 panel Agent

panel Agent 负责：

- 1024 字节默认帧、最多 4 个订阅、32 条队列。
- 线程摘要、最多两条最近消息、审批、控制和音频。
- SNAPSHOT 分页和 revision 提交。
- 屏幕状态与协议状态一致，不在设备侧推断业务完成。

panel 完成定义：

- 可以在屏幕显示当前线程摘要、当前动作和审批。
- 可以发送 voice 或 audio 回复。
- 策略变化不需要重连即可生效。
- 不因历史消息缺失而丢失 live 事件。

### 29.5 Desktop Agent

Desktop Agent 负责：

- 作为 Bridge 的受控适配器，而不是第二协议主机。
- 只对 live 订阅线程 follow。
- 任何桌面写入都必须转换为 Bridge 写入意图；不得绕过 writer lease。
- 发现桌面 Adapter 绕过 Bridge 直写时上报 owner 变化和 writer 冲突。
- Bridge 始终是唯一权威写入者；`UNMANAGED` 和 `OBSERVED` 只表示当前没有有效 lease。

Desktop 完成定义：

- 空闲时不产生全量 thread snapshot 广播。
- 关闭 Bridge 后行为恢复到桌面基线。
- 接管线程的写入必须经过 Bridge，且可以被设备观察到。

## 30. 联调里程碑

| 里程碑 | Bridge | 手机 | ESP32 | ESP32-S3 | 验收 |
|---|---|---|---|---|---|
| M0 字节级 codec | 固定头和 TLV | 解析器 | 解析器 | 解析器 | 28.1 通过 |
| M1 握手 | 状态机和 HMAC | 重连 | 小帧握手 | 屏幕握手 | 28.2 通过 |
| M2 事件 | Event Spine 和 topic | SNAPSHOT/DELTA | cursor ACK | 分页快照 | 28.3、28.5 通过 |
| M3 回复 | SEND 到 turn | 文本和音频 | 短文本 | 摘要和语音 | 18、19 通过 |
| M4 控制 | writer lease | 接管和命令 | 审批 | 控制和审批 | 28.6 通过 |
| M5 旧兼容 | Legacy Gateway | 迁移 | 迁移 | 迁移 | 24、28.7 通过 |
| M6 性能 | 有界内存和空闲零 follow | 低流量 | 无历史常驻 | 有界缓存 | 28.7 通过 |

联调顺序不得跳级：先让 codec 和握手稳定，再让事件和 ACK 稳定，最后接控制与
旧协议。所有 Agent 必须共享本文档的测试向量，不能各写一套“看起来能用”的
客户端。
