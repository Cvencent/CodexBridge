# CodexBridge 统一设备、注册与待发送队列设计

日期：2026-10-17  
状态：待评审

## 1. 背景

CodexBridge 当前同时维护两套身份和交互：

- 手机通过 `pairing code` 配对，记录在 `companion.json`，使用 HTTP + SSE。
- Dotii、AI Passport 等硬件通过 UDP `8764` 发现、TCP `8765` 连接，记录在
  `devices.json`，使用换行分隔 JSON。

手机在对话进行中发送消息时会直接调用 `turn.steer`，没有待发送队列；管理页面
把“手机连接”和“设备”拆成两个区域；硬件设备被发现后会自动登记，管理页面只能
事后生成令牌，不能把设备主动申请注册变成一次明确的批准动作。

本次改造把三者收敛到同一套设备模型、注册状态和任务命令协议，并新增由
CodexBridge 持久化、跨客户端同步的线程级待发送队列。

## 2. 目标

1. 手机和硬件都表现为同一种 `device`，管理页面只有一个设备中心。
2. 硬件设备可以主动申请注册，管理页面显示待批准设备，由用户批准或拒绝。
3. 手机继续使用配对码；配对码本身就是一次本机批准，不再出现第二套“手机不是
   设备”的页面模型。
4. 对话进行中发送的新消息默认进入 CodexBridge 统一队列，不再由手机直接
   `steer`。
5. 队列项支持 `引导`、`编辑`、`删除`，并由所有在线客户端同步。
6. 新客户端使用统一的 `queue.*` 命令；未声明 `queue` 能力的旧客户端保持原有
   行为，避免升级期间断档。
7. CodexBridge 重启后队列不丢失，线程空闲时继续按顺序派发。

## 3. 非目标

- 不把 CodexBridge 做成云端中转，所有注册和队列同步仍只在同一局域网内。
- 不改变 Dotii 的 Bambu、自定义页、显示设置、BLE 配网和烧录链路。
- 不要求桌面 Codex 显示 Bridge 队列。桌面 Codex 仍是线程的执行 owner；Bridge
  在派发队列项时把 turn 交给它。
- 不引入第二套 TCP 握手、协议版本分支或手机专用执行通道。
- 本次不实现队列优先级、定时发送、失败自动重试和队列跨电脑同步。

## 4. 已确认决策

- 待发送队列由电脑端 CodexBridge 统一持有并持久化。
- 队列的所有变更通过同一事件模型广播给手机、ESP 和其他 Bridge 客户端。
- `引导` 只对当前正在执行的 turn 生效，表示把队列项注入当前 turn。
- 手机输入发送后先进入队列；线程空闲时 Bridge 立即派发，线程进行中时等待。
- 硬件注册使用“设备主动申请 + 管理页批准”；手机注册使用“管理页生成配对码 +
  手机输入配对码”，两者最终都进入同一个已批准设备注册表。

## 5. 术语

| 术语 | 含义 |
| --- | --- |
| 设备 | 统一身份，包含手机、ESP 硬件和未来的其他客户端 |
| 手机 | `kind = phone`、`transport = companion_http` 的设备 |
| 硬件 | `kind = hardware`、`transport = device_tcp` 的设备 |
| 待批准 | 设备已经出现，但还没有获得设备令牌 |
| 已批准 | 设备拥有有效令牌，可以建立业务会话 |
| 队列项 | 一条等待发送或等待引导的用户消息 |
| 派发 | Bridge 把队列项转换成一次 `turn.start` |
| 引导 | Bridge 把队列项通过 `turn.steer` 注入当前进行中的 turn |

## 6. 总体架构

```text
                         CodexBridge
  +----------------+     +------------------+
  | 手机伴侣 HTTP  |---->| DeviceRegistry   |
  | /api/companion |     | 统一设备身份      |
  +----------------+     +---------+--------+
                                  |
  +----------------+     +--------v--------+
  | ESP TCP 8765   |---->| CommandDispatcher|
  | JSON 行协议    |     | 统一命令路由      |
  +----------------+     +---------+--------+
                                  |
                        +---------v--------+
                        | QueueService     |
                        | 线程级待发送队列  |
                        +---------+--------+
                                  |
                        +---------v--------+
                        | CompanionService |
                        | Codex app-server |
                        +------------------+
```

核心约束：

- `DeviceRegistry` 是设备身份和注册状态的唯一权威来源。
- `QueueService` 是队列状态和队列顺序的唯一权威来源。
- 手机 HTTP、ESP TCP 和本机管理页都只通过 `CommandDispatcher` 修改队列。
- 所有队列变更先落盘、再广播；客户端收到事件后重新对齐 `revision`。

## 7. 统一设备模型

### 7.1 DeviceRecord

设备记录扩展以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | string | 稳定身份，手机使用现有 `phone_id`，硬件使用现有 `device_id` |
| `kind` | string | `phone`、`hardware`，未来可扩展 |
| `transport` | string | `companion_http`、`device_tcp` |
| `name` | string | 用户可见名称 |
| `model` | string | 型号或设备类别 |
| `registration_state` | string | `pending`、`approved`、`rejected`、`revoked` |
| `registration_method` | string | `pairing_code`、`admin_approval` |
| `supported` | string[] | 设备声明支持的能力 |
| `enabled` | string[] | 本机允许的能力 |
| `last_seen` | number | 最近一次有效请求或会话时间 |
| `last_address` | string | 最近一次来源地址，只用于展示和排查 |
| `client_id` | string | 手机安装标识；硬件可为空 |
| `max_text_chars` | integer/null | 每台设备自己的正文预算 |
| `customized` | boolean | 用户是否手动改过能力开关 |
| `has_token` | boolean | 是否已经签发过设备令牌 |

令牌不进入公开 API。硬件需要向设备出示令牌，手机只需要保存令牌摘要；两种存储
细节都封装在设备记录内部。管理页和事件流只返回 `has_token`。

### 7.2 设备公共视图

`GET /api/admin/devices` 和内部事件使用同一公共视图：

```json
{
  "device_id": "phone-a1b2c3",
  "kind": "phone",
  "transport": "companion_http",
  "name": "Pixel",
  "model": "Android",
  "registration_state": "approved",
  "registration_method": "pairing_code",
  "online": true,
  "last_seen": 1234567890.0,
  "has_token": true,
  "supported": ["status", "tasks", "control", "approval", "queue"],
  "enabled": ["status", "tasks", "control", "approval", "queue"],
  "queued_count": 2
}
```

手机与硬件共用 `queued_count`、在线状态、能力开关和移除动作。管理页不再从
`companion.phones` 单独渲染一份列表。

### 7.3 设备标识兼容

- 手机继续使用 `phone-...` 形式的 `device_id`，避免升级后所有手机都变成新设备。
- 硬件继续使用固件上报的稳定 `device_id`。
- `kind` 和 `transport` 由注册入口决定，不要求设备自己声明。
- `model` 对手机默认使用 `Android`，对硬件沿用发现应答或握手信息。

## 8. 注册与发现

### 8.1 手机注册

手机继续使用配对码，但流程落到统一设备注册表：

1. 管理页生成 6 位配对码，10 分钟有效，最多尝试 5 次。
2. 手机调用 `POST /api/companion/pair`，携带 `code`、`name`、`client_id`。
3. CodexBridge 验证配对码，立即把手机登记为
   `registration_state = approved`、`registration_method = pairing_code`。
4. 手机获得 `phone_id` 和令牌；设备记录保存令牌摘要。
5. 同一 `client_id` 再次配对时更新原记录并轮换令牌，不创建重复设备。

配对码是手机场景的本机批准凭据。手机不会进入 `pending` 状态，因为用户已经通过
管理页生成了批准码。

### 8.2 硬件发现

硬件仍由 CodexBridge 主动发现并连接设备 TCP 服务端。UDP 应答新增注册字段：

```json
{
  "device": "dotii",
  "device_id": "dotii-a1",
  "name": "Desk",
  "port": 8765,
  "capabilities": ["status", "text", "tasks", "read_ack", "queue"],
  "registration_state": "pending",
  "registration_nonce": "r_7f5c..."
}
```

规则：

- `registration_state` 缺省时按 `approved` 处理，兼容已有固件。
- `registration_state = pending` 时必须携带 `registration_nonce`。
- `registration_nonce` 是 16 到 64 字符的一次性随机值，绑定
  `device_id + 来源地址 + 发现时间`，有效期 10 分钟。
- 未批准的设备只进入设备中心的待批准列表，不建立业务会话，不下发消息。
- 已批准设备重复发现时只刷新 `last_seen` 和地址，不改变已批准状态。

### 8.3 管理页批准硬件

待批准设备在管理页显示：

- 设备名称、型号、`device_id`
- 来源地址和最近发现时间
- 设备声明的能力
- `批准`、`拒绝` 两个动作

批准动作只允许本机回环管理接口调用：

```text
POST /api/admin/devices/approve
{"device_id":"dotii-a1"}
```

拒绝动作：

```text
POST /api/admin/devices/reject
{"device_id":"dotii-a1"}
```

批准后 CodexBridge 生成新令牌，进入 8.4 的注册下发流程。拒绝后设备保持
`rejected`，直到设备更换 `registration_nonce` 或用户在管理页选择重新注册。

### 8.4 注册下发握手

注册下发不是第二套业务握手，它只在设备还没有令牌时执行一次：

```text
PC -> 设备:
{"type":"register_offer","registration_nonce":"r_7f5c...","token":"<新设备令牌>","bridge":"CodexBridge"}

设备 -> PC:
{"type":"register_ok","device_id":"dotii-a1","accepted":true}
```

失败应答：

```json
{"type":"register_error","code":"registration_nonce_invalid"}
```

规则：

- `register_offer` 必须携带与最新发现一致的 `registration_nonce`。
- 设备收到后立即持久化令牌，返回 `register_ok`，随后关闭本次连接。
- CodexBridge 收到 `register_ok` 后把设备置为 `approved`，开始正常的
  `bridge_auth` 会话。
- 设备已有令牌时忽略 `register_offer`，返回 `registration_already_approved`。
- 注册下发失败时保留 `pending` 状态和错误原因，管理页可点击 `重新批准`。

正常的 `bridge_auth` / `bridge_auth_ok` / `bridge_ready` 握手保持不变；注册下发
完成后的下一次连接才进入该流程。

### 8.5 硬件注册状态机

```text
pending --批准成功--> approved --发现/重连--> 正常 bridge_auth
   |                     |
   |--拒绝----------> rejected
   |                     |
   |--重新注册-------> revoked --重新发现--> pending
```

状态变化通过管理页轮询刷新；管理页最多每 3 秒拉取一次设备列表。

### 8.6 发现与业务会话顺序

```text
UDP 发现
   |
   +-- approved + 有令牌 --> TCP bridge_auth
   |
   +-- pending ----------> 管理页等待批准
                               |
                               +-- 批准 --> register_offer --> register_ok --> TCP bridge_auth
```

待批准设备不进入在线设备列表，也不占用 Bridge 会话。

## 9. 待发送队列

### 9.1 队列作用域

每个 `thread_id` 一条 FIFO 队列。队列项属于线程，不属于设备：

- 手机 A 加入的消息，手机 B 能看到。
- 手机看到的消息，ESP 只有在声明并启用 `queue` 能力时才收到队列事件。
- 桌面 Codex 仍是线程执行 owner，队列派发通过现有 owner 路由完成。

### 9.2 队列项结构

```json
{
  "queue_id": "q_01J...",
  "thread_id": "thread-123",
  "text": "继续检查超时问题",
  "model": "",
  "effort": "",
  "status": "pending",
  "revision": 1,
  "created_at": 1770000000.0,
  "updated_at": 1770000000.0,
  "created_by": "phone-a1b2c3",
  "created_by_kind": "phone",
  "error": ""
}
```

`status` 取值：

| 状态 | 含义 | 是否可见 |
| --- | --- | --- |
| `pending` | 等待派发或引导 | 是 |
| `dispatching` | Bridge 正在发起 `turn.start` | 是 |
| `failed` | 派发失败，等待用户编辑或删除 | 是 |
| `dispatched` | 已转换成用户消息 | 否，事件中带原因 |
| `steered` | 已注入当前 turn | 否，事件中带原因 |
| `deleted` | 用户删除 | 否，事件中带原因 |

终态项不继续出现在队列快照中，但必须广播删除或终态事件，供其他客户端收敛。

### 9.3 入队语义

所有新客户端使用同一条路径：

```text
queue.enqueue(thread_id, text, model?, effort?)
```

行为：

1. 校验线程、文本长度和队列上限。
2. 写入一条 `pending` 队列项，返回最新 `revision`。
3. 如果线程没有活动 turn，Bridge 立即调度该队列项。
4. 如果线程有活动 turn，队列项保持 `pending`，不再自动调用 steer。
5. 当前 turn 结束后，Bridge 自动派发队首项。

这使手机端行为与电脑 Codex 的“继续输入，先排队”一致，同时保留用户显式
`引导` 的能力。

### 9.4 引导

客户端对 `pending` 队列项调用：

```text
queue.steer(queue_id, expected_revision)
```

行为：

1. 重新确认线程存在活动 turn。
2. 通过现有 `steer_turn` 注入文本。
3. 成功后将队列项标记为 `steered` 并广播移除事件。
4. 如果活动 turn 已结束，返回 `no_active_turn`，队列项保持不变。

`引导` 不改变剩余队列项的顺序。

### 9.5 编辑

客户端对 `pending` 或 `failed` 队列项调用：

```text
queue.edit(queue_id, text, model?, effort?, expected_revision)
```

行为：

1. 校验 `expected_revision`，防止旧页面覆盖新状态。
2. 更新文本、模型和推理档位，`revision` 加一，状态回到 `pending`。
3. 广播 `queue_item_updated`。
4. 若线程空闲，立即尝试派发。

### 9.6 删除

客户端调用：

```text
queue.delete(queue_id, expected_revision)
```

删除 `pending` 或 `failed` 项并广播移除事件。已经进入 `dispatching` 的项不允许
删除，用户应使用现有的 `turn.interrupt` 中断当前 turn。

### 9.7 自动派发

`QueueService` 为每个线程维护一个串行派发锁：

1. 线程没有活动 turn 时，取最早的非终态项。
2. 把状态改为 `dispatching` 并广播。
3. 调用 `CompanionService.start_turn(...)`。
4. 成功后广播 `queue_item_removed(reason=dispatched)`，再调度下一项。
5. 调用失败时把该项改为 `failed`，暂停该线程的自动派发并广播
   `queue_paused`，避免失败项导致忙循环。

用户编辑或删除失败项后，对应线程重新允许自动派发。

### 9.8 并发与幂等

- 所有队列命令使用现有 `CommandDispatcher` 的 `request_id` 幂等缓存。
- 队列项带 `revision`。编辑、删除、引导必须携带 `expected_revision`；不匹配
  返回 `queue_conflict`。
- `queue.enqueue` 不要求 `expected_revision`，但重复 `request_id` 必须返回同一
  队列项，不能重复入队。
- 同一线程的派发必须串行，避免两个客户端同时把同一项转成 turn。
- 队列快照带全局 `revision`，客户端必须按线程合并，不能用事件到达顺序推断
  队列顺序。

### 9.9 队列事件

PC 到手机 SSE 和 PC 到 ESP TCP 复用同一组事件语义：

| 事件 | 用途 |
| --- | --- |
| `queue_snapshot` | 连接、重连、`resync_required` 后的权威快照 |
| `queue_item_added` | 新增队列项 |
| `queue_item_updated` | 编辑、`revision` 或状态变化 |
| `queue_item_removed` | 派发、引导或删除后的移除 |
| `queue_paused` | 某项派发失败，线程自动派发暂停 |

HTTP SSE 事件示例：

```json
{
  "type": "queue_item_added",
  "thread_id": "thread-123",
  "queue_id": "q_01J...",
  "revision": 4,
  "item": {
    "queue_id": "q_01J...",
    "thread_id": "thread-123",
    "text": "继续检查超时问题",
    "status": "pending",
    "revision": 1
  }
}
```

TCP 设备事件使用同一 JSON，放在现有的 `device_event` 信封中。

### 9.10 持久化

队列保存到：

```text
%LOCALAPPDATA%\CodexBridge\queues.json
```

要求：

- 每次新增、编辑、删除、状态变化后原子写入。
- 单线程最多 100 项，未完成项文本最多 10000 字符。
- Bridge 重启后重新载入队列。
- 启动时如果线程空闲，继续派发；如果线程仍在执行，保持等待。
- 原始用户消息进入线程后不再保留队列副本，避免消息中心重复计数。

## 10. 统一命令协议

### 10.1 新增能力

能力清单新增：

```text
queue
```

默认值：

- 手机：默认启用 `queue`。
- Dotii 和其他硬件：先声明再启用；固件未声明时不会收到队列事件。
- 旧客户端：未声明 `queue`，继续使用原有 `turn.start` / `mode=steer` 行为。

### 10.2 新增方法

| 方法 | 必需能力 | 参数 |
| --- | --- | --- |
| `queue.list` | `queue` | `thread_id` |
| `queue.enqueue` | `queue` | `thread_id`、`text`；可选 `model`、`effort` |
| `queue.edit` | `queue` | `queue_id`、`text`、`expected_revision`；可选 `model`、`effort` |
| `queue.delete` | `queue` | `queue_id`、`expected_revision` |
| `queue.steer` | `queue` | `queue_id`、`expected_revision` |

HTTP 客户端仍使用：

```text
POST /api/companion/commands
{"request_id":"...","method":"queue.enqueue","params":{...}}
```

TCP 设备使用现有命令信封：

```json
{"type":"command","request_id":"device-42-0001","method":"queue.enqueue","params":{"thread_id":"thread-123","text":"继续检查超时问题"}}
```

大结果继续使用现有 `command_chunk` / `command_request_chunk`。

### 10.3 兼容行为

- `POST /api/companion/threads/<id>/messages` 的 `mode=start`：
  - 手机已协商 `queue`：映射为 `queue.enqueue`。
  - 手机未协商 `queue`：保持旧行为。
- `mode=steer`：保留为立即引导，不创建队列项。
- `turn.start`：保留为立即启动 turn，供旧设备和内部派发使用。
- `thread.read` 返回 `queue` 快照；`threads.list` 返回 `queued_count`。

## 11. 管理页面

管理页面统一为三个区域：

### 11.1 连接与发现

- 允许手机连接
- 显示中间事件
- 手机服务端口
- 手机发现端口
- 端口状态

### 11.2 添加设备

- `生成手机配对码`
- 显示待批准硬件数量
- 显示最近发现的未注册设备

### 11.3 设备中心

设备中心只保留一份列表，按以下顺序排列：

1. 待批准设备
2. 在线设备
3. 离线设备

筛选项：

```text
全部
手机
硬件
待批准
```

每个设备显示：

- 名称、`kind`、`model`、`device_id`
- 注册状态和在线状态
- 最近在线时间、来源地址
- 支持的传输方式
- 能力和正文预算
- `queued_count`

动作：

| 状态 | 动作 |
| --- | --- |
| pending 硬件 | `批准`、`拒绝` |
| approved 手机 | `保存能力`、`解除配对` |
| approved 硬件 | `保存能力`、`重新注册`、`移除设备` |
| rejected | `重新注册`、`移除设备` |

旧版“已配对手机”列表删除，手机与硬件共用同一卡片结构和能力开关。

### 11.4 管理接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/admin/devices` | 统一设备列表 |
| GET | `/api/admin/devices/pending` | 待批准设备 |
| POST | `/api/admin/devices/approve` | 批准硬件注册 |
| POST | `/api/admin/devices/reject` | 拒绝硬件注册 |
| POST | `/api/admin/devices/revoke` | 撤销已有令牌并重新注册 |
| POST | `/api/admin/devices/remove` | 移除设备 |
| POST | `/api/admin/devices/capabilities` | 更新 `supported` / `enabled` |
| GET | `/api/admin/pairing` | 生成手机配对码 |

`GET /api/admin/companion` 保留，但 `phones` 字段由统一设备列表派生；旧管理脚本
不会因为字段被移除而失败。

## 12. 手机端

### 12.1 发送

- 输入框始终调用 `queue.enqueue`。
- 线程空闲时 Bridge 会立即派发，手机端仍显示短暂“正在发送”。
- 线程进行中时显示队列区域，输入框提示“发送后加入队列”。
- 不在客户端自行决定 `steer` 或 `start`。

### 12.2 队列区域

在输入框上方显示当前线程队列：

```text
待发送 2
[输入内容]  引导  编辑  删除
[输入内容]  编辑  删除
```

规则：

- `引导` 只在当前 turn 进行中可用。
- `编辑` 打开单行到多行编辑框，保存后更新队列项。
- `删除` 需要二次确认。
- `failed` 项允许编辑和删除，编辑后自动回到 `pending`。
- 当前没有队列时，不显示空队列卡片。

### 12.3 事件与重连

- SSE 事件按 `queue_id` 和 `queue revision` 合并。
- SSE 断开时继续使用本地快照，重连后先请求 `thread.read` 获取权威队列。
- `resync_required` 时重新读取线程列表和线程详情。

### 12.4 旧客户端

旧手机 APK 没有 `queue` 能力，继续走原有 `mode=steer` 或 `mode=start` 路径；
升级后的手机首次连接时通过 `/api/companion/capabilities` 声明 `queue`。

## 13. ESP 与其他硬件

### 13.1 注册

- 无令牌设备在 UDP 应答中声明 `registration_state = pending` 和
  `registration_nonce`。
- 设备不主动连接 PC，继续监听 TCP `8765`。
- 管理页批准后，CodexBridge 连接设备的 TCP 端口并发送 `register_offer`。
- 设备保存令牌并返回 `register_ok`。
- 下一次连接开始正常 `bridge_auth`。

### 13.2 队列能力

- 设备在 `bridge_auth_ok.capabilities` 中声明 `queue`。
- 只读队列需要 `queue` 能力。
- 设备可以调用 `queue.list`、`queue.enqueue`、`queue.edit`、`queue.delete` 和
  `queue.steer`。
- 未声明 `queue` 时，现有消息、已读、审批和任务能力保持不变。
- 队列事件使用现有 `device_event` 和分片机制，设备必须忽略未知事件类型。

### 13.3 最小显示要求

首版硬件不必显示完整队列，但应能显示：

- 当前线程是否有待发送项
- 当前 turn 是否进行中
- 是否需要用户批准

完整编辑界面可以作为后续固件能力增加，不影响协议。

## 14. 错误处理

新增业务错误码：

| 错误码 | 含义 |
| --- | --- |
| `queue_not_found` | 线程没有队列 |
| `queue_item_not_found` | 队列项不存在或已经终态 |
| `queue_conflict` | `expected_revision` 不匹配 |
| `queue_full` | 线程队列达到上限 |
| `queue_text_too_long` | 文本超过 10000 字符 |
| `no_active_turn` | 引导时没有进行中的 turn |
| `queue_dispatch_failed` | 派发到 Codex 失败 |
| `registration_pending` | 设备仍在等待批准 |
| `registration_rejected` | 设备已被拒绝 |
| `registration_nonce_invalid` | 注册随机值无效或过期 |
| `registration_already_approved` | 设备已经拥有令牌 |
| `device_not_pending` | 设备不在待批准状态 |

现有 `capability_not_enabled` 仍适用于未声明 `queue` 的设备。

## 15. 安全要求

- 注册批准、拒绝、撤销和重新注册只允许本机回环管理接口调用。
- `registration_nonce` 单次有效，10 分钟过期，不能跨设备复用。
- `register_offer` 只允许在设备处于 `pending` 时发送，且必须匹配
  `device_id + nonce + 来源地址`。
- 设备令牌至少 32 字节随机值；日志、管理页和错误响应不得输出令牌。
- 配对码和注册批准都设置频率限制，避免同局域网暴力尝试。
- 队列文本、线程数量、单线程项数、单消息长度、注册请求体都有明确上限。
- 管理 API 继续只监听 `127.0.0.1`。

## 16. 迁移与兼容

### 16.1 devices.json

原 `devices.json` 记录自动迁移为版本 2：

- 已有令牌的硬件：`kind = hardware`、`transport = device_tcp`、
  `registration_state = approved`。
- 没有令牌的硬件：`registration_state = pending`。
- 能力字段保持原样，新增 `queue_supported` / `queue_enabled` 派生字段。

### 16.2 companion.json

原 `companion.phones` 在启动时合并到统一设备注册表：

- `phone_id` 保留为 `device_id`。
- `kind = phone`、`transport = companion_http`。
- `registration_state = approved`、`registration_method = pairing_code`。
- `token_hash`、`client_id`、`enabled`、`last_seen` 原样保留。
- 旧手机记录默认补上 `queue` 能力，保持“手机默认启用队列”；硬件不会因为这次
  迁移自动获得 `queue`，仍以固件声明为准。

`companion.json` 继续保存全局开关、端口和配对码；手机列表不再从该文件单独渲染。
迁移完成后保留旧文件，避免回滚时丢失数据。

### 16.3 旧客户端

- 未声明 `queue` 的手机和硬件继续使用原有 `turn.start`、`mode=steer` 和消息语义。
- 旧版 `GET /api/admin/companion` 仍返回 `phones` 字段，内容来自统一设备列表。
- 旧固件的 UDP 发现应答没有注册字段，按 `approved` 处理，不影响现有设备连接。

## 17. 测试

### 17.1 单元测试

- 设备注册表版本迁移、手机记录合并、令牌隐藏。
- pending -> approved -> `register_offer` -> `register_ok` 状态机。
- 注册拒绝、过期、nonce 不匹配、重复批准。
- 队列新增、编辑、删除、引导、`revision` 冲突和 `request_id` 幂等。
- 线程空闲立即派发、进行中等待、结束后自动派发。
- 派发失败暂停、编辑失败项后恢复。
- 队列持久化、重启恢复、线程状态恢复。
- `queue` 能力协商和旧客户端兼容路径。

### 17.2 接口测试

- `GET /api/admin/devices` 同时返回手机和硬件。
- `POST /api/admin/devices/approve` 只允许回环访问。
- `POST /api/companion/commands` 的 `queue.*` 成功和错误响应。
- SSE 队列事件顺序、快照恢复和 `resync_required`。
- TCP 设备的 `command_result`、`device_event` 和分片。

### 17.3 集成测试

- 两台手机同时查看同一线程，任一手机新增、编辑、删除队列项，另一台同步。
- 手机加入队列，桌面 Codex owner 执行当前 turn，Bridge 在结束后派发下一项。
- 手机点击 `引导`，当前 turn 收到 steer 输入，队列项从两端消失。
- 未注册 ESP 出现在待批准列表，批准后成功建立正常 Bridge 会话。
- 旧手机 APK 不声明 `queue` 时保持原有发送行为。

### 17.4 真机验收

- Windows 管理页只显示一份统一设备列表，手机和 ESP 都在其中。
- ESP 无令牌时主动出现在待批准区域，批准后可正常显示状态和消息。
- 手机进行中发送后先出现在队列，可引导、编辑、删除。
- 关闭手机再重开，队列仍与电脑端一致。
- Bridge 重启后队列不丢失。

## 18. 实施阶段

### 阶段 1：队列核心与协议

- 新增 `QueueService`、`queues.json` 和原子写入。
- 新增 `queue.*` 命令、`queue` 能力和事件。
- 接入 `CompanionService` 的 turn 生命周期和派发。
- 更新 `PROTOCOL.md` 与核心测试。

### 阶段 2：统一设备注册表和页面

- 扩展 `DeviceRecord` 的 `kind`、`transport`、`registration_state`。
- 迁移 `devices.json` 和 `companion.json.phones`。
- 管理页合并设备列表和状态筛选。
- 保留旧管理接口兼容。

### 阶段 3：硬件主动注册

- UDP 应答增加注册字段。
- 实现 `register_offer` / `register_ok` / `register_error`。
- 管理页增加批准、拒绝、重新注册。
- 更新 ESP 固件接入流程和协议文档。

### 阶段 4：手机队列界面

- 手机能力协商加入 `queue`。
- 发送改为 `queue.enqueue`。
- 对话页增加队列区域、引导、编辑、删除。
- 处理 SSE 事件、断线重连和本地乐观更新。

### 阶段 5：验证与发布

- 运行完整 Python 测试。
- 构建 Android APK 并完成真机回归。
- 使用假 ESP 和真 ESP 验证注册、会话和队列事件。
- 更新 README、PROTOCOL、release notes。

## 19. 验收标准

- 手机和管理页对同一线程的队列看到同一份数据。
- 进行中发送的消息默认排队，不再被手机直接 steer。
- 队列项可以从任一已连接客户端执行引导、编辑、删除。
- 队列在 Bridge 重启后保留，并在可派发时继续执行。
- 管理页面只有一个设备中心，手机和 ESP 使用同一注册状态、能力开关和移除动作。
- 无令牌 ESP 必须经过管理页批准才能获得令牌并上线。
- 手机配对码流程保持可用，不要求用户为已配对手机重新操作。
- 旧客户端未声明 `queue` 时行为不回归。
- 所有协议改动同步更新 `PROTOCOL.md`，相关测试通过。
