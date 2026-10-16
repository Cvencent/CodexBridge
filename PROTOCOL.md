# CodexBridge 通用设备协议

> **文档导航：** [项目说明](README.md) · **通用协议**

本文档描述 CodexBridge 当前代码实现的通用设备协议，供 Dotii、AI Passport
以及其他局域网设备实现接入。协议字段名、消息 `type` 和 JSON 示例保持英文，
说明文字使用中文。

本文档以仓库当前实现为准。设计稿中未在代码中落地的字段或行为不会写成既有
协议；后续只增加可选字段或新消息类型，不建立第二套握手。

## 维护要求

任何会改变设备发现、TCP 会话、握手、能力、消息字段、事件、错误码、分片规则
或本文件记录的管理接口的改动，都必须在同一个提交中同步更新本文件。修改协议
行为时，同时更新相关测试和示例；不确定是否影响协议时，按需要更新处理。

## 1. 范围与角色

CodexBridge 是 PC 端协议主机，负责：

- 通过 UDP 发现局域网设备。
- 主动连接设备 TCP 服务端并完成鉴权握手。
- 保存每台设备的 `supported` 和 `enabled` 能力。
- 汇总 Codex 状态、任务、结果与通知，统一为 `message` 下发。
- 接收设备已读、审批、语音和任务相关事件。
- 在设备重连后重放未读消息和最新已读状态。

设备负责：

- 监听 UDP `8764`，响应固定发现请求。
- 监听 TCP `8765`，接受 CodexBridge 的连接。
- 校验配对令牌，返回稳定的设备身份和声明能力。
- 按生效能力处理消息；未知消息类型必须忽略。
- 在支持 `read_ack` 时上报已读，在支持 `audio` 时上传录音分片。

协议是局域网内点对点会话，不经过云端中转，也不经过 CodexBridge 的本机管理
HTTP API。

## 2. 端口与连接拓扑

| 用途 | 传输 | 端口 | 发起方 |
| --- | --- | --- | --- |
| 设备发现 | UDP | `8764` | CodexBridge |
| 设备会话 | TCP | `8765` | CodexBridge 主动连接设备 |
| CodexBridge 本机管理 | HTTP | `127.0.0.1:8788` | 本机浏览器或本机程序 |
| Dotii 管理中心 | HTTP | `127.0.0.1:8787` | 本机浏览器；设备端仅访问其快照接口 |
| 手机发现 | UDP | `8569`（可在管理页修改） | 手机广播，CodexBridge 应答 |
| 手机伴侣 API | HTTP + SSE | `TCP 8568`（可在管理页修改） | 已配对手机 |

设备 TCP 会话使用 UTF-8 JSON，每条消息一行，以 `\n` 分隔。设备是 TCP 服务端，
CodexBridge 是 TCP 客户端。设备切换 IP 后仍必须上报同一个稳定
`device_id`。

## 3. UDP 发现协议

### 3.1 请求

CodexBridge 向目标地址的 UDP `8764` 发送以下明文请求，不带换行：

```text
CODEXBRIDGE_DISCOVER
```

默认目标是广播地址 `255.255.255.255`。发现超时由 CodexBridge 发起方控制，
`discover()` 的默认单轮超时为 `3.0` 秒；常驻程序的发现循环使用 `1.0` 秒一轮、约每 `5` 秒重新扫描。

### 3.2 应答

设备向请求来源地址和端口返回一个 UTF-8 JSON 对象，最大 `1024` 字节：

```json
{
  "device": "dotii",
  "device_id": "a1",
  "name": "Desk",
  "port": 8765,
  "capabilities": ["status", "tasks"]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device` | string | 是 | 设备型号或设备类别，不能为空 |
| `device_id` | string | 是 | 稳定设备 ID；IP 变化不得改变 |
| `port` | integer | 是 | TCP 会话端口，必须在 `1..65535` |
| `name` | string | 否 | 显示名称；空值按未提供处理 |
| `capabilities` | string[] | 否 | 设备声明的能力；未知能力会被忽略 |

应答不是有效 JSON、超过 1024 字节、缺少必填字段，或 `port` 非法时，CodexBridge
忽略该应答。

### 3.3 稳定身份

`device_id` 与设备令牌、能力配置和消息记录关联。设备重启、换 IP 或 DHCP
重新分配地址后必须继续使用原 `device_id`。建议由硬件唯一信息派生，例如 MAC
地址的稳定哈希。

## 4. TCP 会话与 JSON 行协议

### 4.1 传输规则

- 字符编码：UTF-8。
- 消息格式：换行分隔 JSON，每条消息一行。
- 单行上限：`768` 字节，包含末尾换行。
- 接收方必须缓存不完整分片，按 `\n` 切分后解析。
- 非 JSON 行、非对象 JSON、超长行均忽略，不因单行错误断开会话。
- 协议消息的 `type` 使用小写字符串；未知类型必须忽略。

### 4.2 时间与连接参数

| 参数 | 当前值 | 说明 |
| --- | --- | --- |
| 心跳间隔 | `15` 秒 | CodexBridge 重复发送 `bridge_ready`（可配置，仅作存活探测） |
| 连接超时 | `15` 秒 | TCP 建连与等待握手应答的超时 |
| 令牌最短长度 | `16` 字符 | 少于 16 字符的令牌会被拒绝 |

设备可在 `15` 秒未收到 CodexBridge 任何消息时判定连接失效并重新等待连接。

心跳只用于确认链路存活；设备需要展示的 Codex 状态由设备主动拉取（见第 9 章
`codex_state_request`），因此心跳周期不承担状态同步职责，可以被放宽。

## 5. 鉴权握手

握手顺序固定如下，不提供版本分支或并行握手：

### 5.1 CodexBridge -> 设备

```json
{"type":"bridge_auth","token":"<配对令牌>"}
```

### 5.2 设备 -> CodexBridge

设备校验令牌并返回：

```json
{
  "type": "bridge_auth_ok",
  "device_id": "a1",
  "model": "dotii",
  "name": "Desk",
  "firmware": "1.0.0",
  "capabilities": ["status", "text", "read_ack"]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `bridge_auth_ok` |
| `device_id` | string | 是 | 必须与注册记录一致 |
| `model` | string | 建议 | 用于型号默认能力 |
| `name` | string | 否 | 显示名称 |
| `firmware` | string | 否 | 固件版本，当前只透传记录，不参与握手判断 |
| `capabilities` | string[] | 是 | 设备固件实际支持的能力 |
| `max_text_chars` | integer | 否 | 设备能渲染的正文字符上限；缺省或 `null` 表示不限 |

如果令牌错误、回包不是 `bridge_auth_ok`，或 `device_id` 与预期不一致，
CodexBridge 结束本次会话，不进入在线状态。

### 5.3 CodexBridge -> 设备

握手成功后，CodexBridge 发送：

```json
{
  "type": "bridge_ready",
  "bridge": "CodexBridge",
  "version": "1.4.0",
  "capabilities": ["read_ack", "status", "text"]
}
```

`capabilities` 是本次会话握手时计算出的生效能力，即设备 `supported` 与用户
`enabled` 的交集。首次 `bridge_ready` 完成能力授予；之后按心跳间隔重复发送
`bridge_ready` 作为心跳，并继续携带本次会话已授予的能力。运行时权限变化通过
`capabilities_update` 推送。

设备收到 `bridge_ready` 后会话进入在线状态，可以发送已授权事件，也可以接收
CodexBridge 下发的消息。

## 6. 重连与状态重放

CodexBridge 在发现设备并取得已保存令牌后建立独立会话。当前实现的重连策略是：

- 设备离线后从活动会话表移除。
- 发现线程周期性重新发现设备。
- 再次发现后重新执行同一套握手。
- 不限制重试次数，也不建立协议版本协商。

设备重新在线后，CodexBridge 按生效能力推送：

1. 该设备尚未读过、且符合其能力的 `message`。
2. 有已读记录的可见消息对应的最新 `read_state`。
3. 如果设备具备 `tasks` 能力，CodexBridge 会主动补发一次项目目录
   `codex_catalog_begin` / `codex_catalog_item` / `codex_catalog_end`，
   不带 `request_id`，用于固件在重连后已经处于目录加载态时退出加载。
   设备在正常目录请求期间仍应按 `request_id` 过滤响应。

设备维度的已读按 `message_id` 和设备维度保存，用于兼容各设备的本地去重。除此之外
CodexBridge 维护一份全局已读标记：任一客户端（手机伴侣或 ESP 设备）读过一条消息后，
其他客户端重连或刷新时都不会再把它当作未读下发。重放的是在线期间仍存在于消息中心
的消息；消息中心当前最多保留 `200` 条记录。

## 7. 能力协商与权限模型

### 7.1 能力全集

当前代码认识的能力：

```text
status
text
tasks
results
approval
audio
read_ack
voice
word_bear
quota
```

未知能力不会写入设备配置，也不会下发。`word_bear` 当前是已知的预留能力；本文尚未定义它的专用消息。

### 7.2 最小能力集与型号默认值

最小能力集：

```text
status
text
read_ack
```

当前型号默认值：

| 型号 | 默认能力 |
| --- | --- |
| `FoloOS-AI-Passport` | `status`, `text`, `read_ack`, `tasks`, `results`, `audio`, `voice`, `approval` |
| `dotii` | `status`, `text`, `tasks`, `results`, `read_ack`, `quota` |
| 未知型号 | `status`, `text`, `read_ack` |

设备首次出现时，当前默认计算为 `default_for(model) ∩ (supported ∪ MINIMAL)`，因此最小能力集中的能力不会被设备漏报直接抹掉。
`supported` 来自 `bridge_auth_ok.capabilities`，`enabled` 是本机用户允许的
能力，二者都必须是已知能力。

设备每次握手都会重新声明 `supported`。只要本机用户没有手动改过该设备的能力开关
（注册表中的 `customized` 为假），`enabled` 就随声明重新推导：
固件新增了 `audio` 之类的 capability 后，设备一连上就能用，不需要在本机再点一次；
设备撤掉某个 capability 时，`enabled` 同步收缩。用户一旦手动勾选过，本机选择优先，
主机的默认值不再覆盖它。

### 7.3 生效能力

```text
effective = supported ∩ enabled
```

CodexBridge 只向设备下发生效能力对应的数据，也只接受生效能力对应的事件。
设备发送未授权事件时，CodexBridge 返回：

```json
{"type":"error","code":"capability_not_enabled"}
```

### 7.4 通用正文预算（text budget）

不同设备的内存与屏幕差异极大，协议**不为任何设备硬编码截断长度**，而是由
设备自己声明能渲染多少正文，CodexBridge 按声明裁剪。这样一套协议同时服务
ESP32 与桌面/手机客户端。

设备在 `bridge_auth_ok` 中携带可选字段 `max_text_chars`：

```json
{"type":"bridge_auth_ok","device_id":"aipassport-1","max_text_chars":512}
```

| 取值 | 含义 |
| --- | --- |
| 正整数 | 每条 `message` 的 `body` 最多保留该字符数，超出以 `...` 结尾 |
| 缺省 / `null` / `0` / 负数 | 不限，CodexBridge 发送完整正文（仍按 768 字节单行上限分片） |

取值会被夹到 `16..100000` 区间。CodexBridge 只在 `type == "message"` 的记录上
应用预算，其它消息类型不受影响。

本机可在管理页为每台设备覆盖该值（`POST /api/admin/devices/capabilities` 的
可选字段 `max_text_chars`）：传正整数覆盖设备声明，传 `null` 恢复不限，
缺省字段表示保留设备声明值。覆盖值随设备记录持久化。

裁剪发生在正文分片之前，因此设备收到的分片总长仍受第 4 节的单行上限约束。

### 7.5 运行时更新

本机管理页面保存能力后，CodexBridge 向在线设备推送：

```json
{"type":"capabilities_update","enabled":["status","text","read_ack"]}
```

`enabled` 是重新计算后的生效能力集合，不是用户界面上所有勾选项的原始数组。
设备收到后应立即更新本地可用页面或菜单，不需要重连。

## 8. CodexBridge -> 设备消息

### 8.1 `bridge_ready`

握手确认和心跳。字段见第 5.3 节。

### 8.2 `capabilities_update`

能力更新。字段见第 7.5 节。

### 8.3 `message`

统一消息。基本字段：

```json
{
  "type": "message",
  "message_id": "task:t1",
  "thread_id": "t1",
  "kind": "task",
  "status": "working",
  "title": "Codex 任务",
  "body": "正在处理",
  "created_at": 1234567890.0
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message_id` | string | 是 | 消息唯一 ID |
| `thread_id` | string | 是 | 对应 Codex 线程 |
| `kind` | string | 是 | 见第 8.4 节 |
| `status` | string | 是 | 当前状态，例如 `working`、`completed`、`failed` |
| `title` | string | 是 | 标题，UTF-8 最长 160 字节 |
| `body` | string | 是 | 正文；长文本会分片 |
| `created_at` | number | 是 | Unix 时间戳，单位秒 |
| `data` | object | 否 | 结构化上下文；设备可按需读取 |
| `seq` | integer | 分片时是 | 分片序号，从 `0` 开始 |
| `total` | integer | 分片时是 | 分片总数 |

### 8.4 `kind` 与能力映射

| `kind` | 所需能力 | 用途 |
| --- | --- | --- |
| `status` | `status` | Codex 运行状态 |
| `task` | `tasks` | 任务列表或当前任务 |
| `result` | `results` | 已完成或失败的任务结果 |
| `approval` | `approval` | 等待审批的消息 |
| `codex_status` | `quota` | Codex 配额、用量与运行状态快照 |

`text` 不是单独的 `kind`；文字消息使用上述消息模型下发，能力开关决定设备是否
接收对应类别。

### 8.5 `codex_status`

配额与用量消息。`data.quota` 中所有字段都是可选的，缺失或类型错误时设备应显示
`--`，不得外推。年份/日期使用 Unix 时间戳（秒）。

```json
{
  "type": "message",
  "message_id": "codex_status:t1",
  "thread_id": "t1",
  "kind": "codex_status",
  "status": "working",
  "title": "Codex 配额与状态",
  "body": "",
  "created_at": 1234567890.0,
  "data": {
    "quota": {
      "five_hour_remaining_percent": 64,
      "weekly_remaining_percent": 71,
      "weekly_tokens": 12345,
      "seven_day_tokens": 43210,
      "five_hour_reset_at": 1770000000,
      "weekly_reset_at": 1771000000,
      "plan_type": "plus",
      "task_status": "working",
      "thread_id": "t1",
      "current_action": "Running tests",
      "token_count": 42,
      "plan_completed": 2,
      "plan_total": 5
    }
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `five_hour_remaining_percent` | number | 5 小时窗口剩余百分比 |
| `weekly_remaining_percent` | number | 周窗口剩余百分比 |
| `weekly_tokens` | number | 近 7 日 Token |
| `seven_day_tokens` | number | 7 日 Token（若上游提供该独立字段） |
| `five_hour_reset_at` | number | 5 小时额度重置时间（epoch 秒） |
| `weekly_reset_at` | number | 周额度重置时间（epoch 秒） |
| `plan_type` | string | 套餐类型 |
| `task_status` | string | 当前任务状态 |
| `thread_id` | string | 当前线程 ID |
| `current_action` | string | 当前动作 |
| `token_count` | number | 当前任务 Token 数 |
| `plan_completed` | number | 已完成计划项 |
| `plan_total` | number | 计划项总数 |

`thread_id`、`current_action` 与 `plan_type` 会被截断到适合单行 768 字节上限的长度，
设备端字段不足时仍按缺失处理。

### 8.6 `read_state`

已读状态广播：

```json
{
  "type": "read_state",
  "message_id": "task:t1",
  "read_by": ["device-a", "device-b"],
  "read_at": 1234567890.0
}
```

`read_by` 是已经上报已读的设备 ID 列表，按字典序排序。重放状态下可能不包含
`read_at`；实时广播包含该字段。

### 8.7 语音处理结果

设备上传录音后，CodexBridge 通过同一会话返回：

```json
{"type":"transcript","text":"识别出的文字"}
```

识别失败或录音无效时返回：

```json
{"type":"error","text":"可读的错误说明"}
```

语音识别来源在本机管理页面配置，设备协议不需要知道当前使用的是内置识别还是
OpenAI 兼容接口。

## 9. 设备 -> CodexBridge 事件

### 9.1 `read_ack`

上报一条消息已读：

```json
{"type":"read_ack","message_id":"task:t1"}
```

要求能力：`read_ack`。

CodexBridge 会将该设备写入消息的 `read_by`，并向其他支持 `read_ack` 的在线
设备广播 `read_state`。

### 9.2 `cancel_task`

```json
{"type":"cancel_task","thread_id":"t1"}
```

要求能力：`tasks`。

兼容旧设备的 `tasks` 能力，转为 `turn.interrupt`，返回 `command_result`。
新设备应使用通用命令及 `control` 能力。可提供稳定 `request_id` 去重；
旧设备不提供时由 Bridge 生成，不保证跨重发去重。

### 9.3 `approval_decision`

```json
{"type":"approval_decision","request_id":"r1","decision":"approve"}
```

要求能力：`approval`。

转为 `approval.resolve` 并返回 `command_result`。`approval_id` 为 Bridge
审批整数 ID；旧设备也可在 `request_id` 中传此整数 ID。
`approve`/`deny` 兼容映射到 `accept`/`decline`；其余支持 `acceptForSession`、
`cancel`。工具提问、权限与 MCP 应使用通用 `approval.respond` 结构化响应。

### 9.4 `voice_command`

```json
{"type":"voice_command","text":"帮我检查构建结果"}
```

要求能力：`voice`。当前通用协议层只做识别与能力校验，不返回额外的通用协议应答。

### 9.5 录音事件

录音事件要求能力：`audio`。

开始录音：

```json
{"type":"record_start","sample_rate":16000,"channels":1,"bits":16}
```

音频分片使用 Base64 编码的 16-bit 单声道 PCM：

```json
{"type":"audio_chunk","data":"<base64-pcm>"}
```

结束录音，`bytes` 为设备侧发送的 PCM 总字节数，用于完整性校验：

```json
{"type":"record_stop","bytes":32000}
```

取消录音：

```json
{"type":"record_cancel"}
```

约束：

| 参数 | 当前值 |
| --- | --- |
| 采样率 | `8000..48000` Hz |
| 声道 | `1` |
| 位深 | `16` |
| 最短录音 | `0.4` 秒 |
| 最长录音 | `120` 秒 |

### 9.6 Codex 目录与线程事件

以下事件都要求能力 `tasks`，用于设备侧发起目录、项目或线程操作。除
`codex_catalog_cancel` 和 `codex_selection_confirm` 外，设备应在事件中携带
递增的 `request_id`；CodexBridge 会在对应的目录、选中或错误响应中回显同一个
`request_id`。设备应按 `request_id` 过滤过期响应。

```json
{"type":"codex_catalog_request","request_id":1}
{"type":"codex_project_select","id":"p1","request_id":2}
{"type":"codex_thread_select","id":"t1","request_id":3}
{"type":"codex_thread_new","request_id":4}
{"type":"codex_selection_confirm","request_id":3}
{"type":"codex_catalog_cancel"}
```

目录请求返回当前 Codex Desktop 的项目和线程。项目和线程使用短 ID：项目为
`p0`、`p1` 等，线程为 `t0`、`t1` 等；项目多于 7 个时最后一项为
`p_more`，用于切换下一页。线程按项目的目录归属或桌面端项目 assignment
过滤，只返回属于当前项目的任务。`id` 字段是设备后续选择和确认时使用的目录
ID，不是 Codex 的原始 UUID。

选择线程只读取本地会话记录并返回摘要，不会调用 `thread/read` 或启动新的
任务，因此不会把任务从 Codex Desktop 抢到 Bridge 进程。

项目目录示例：

```json
{"type":"codex_catalog_begin","scope":"projects","request_id":1}
{"type":"codex_catalog_item","scope":"projects","id":"p0","title":"项目名","detail":"项目 1/6","request_id":1}
{"type":"codex_catalog_end","scope":"projects","request_id":1}
```

选择项目后返回线程目录，`scope` 为 `threads`。线程目录只包含真实任务；
“新建任务”由设备界面自行添加。选择线程成功后返回：

```json
{"type":"codex_selected","title":"任务名","project":"项目名","last_message":"最后一段文本","request_id":3}
```

`last_message` 只包含最后一个可展示的助手文本片段，长度受设备协议单行上限
约束。选择成功由设备发送 `codex_selection_confirm` 确认，放弃浏览时发送
`codex_catalog_cancel`；两者都不要求 CodexBridge 返回额外应答。

`codex_thread_new` 由 Bridge 调用 `thread/start` 真实新建任务，可携带可选的
`model`；成功后返回 `codex_selected` 直接进入该任务，并重新下发线程目录，
使新建任务出现在列表中。失败时返回可读错误。

未授权事件返回 `capability_not_enabled`。

### 9.7 `codex_state_request`

设备按需拉取当前 Codex 任务计数，用于在设备界面（例如首页）展示“工作中 /
待办”状态。该事件要求能力 `status`；设备可以携带可选的 `request_id`，应答会
原样回显，便于设备过滤过期响应。

```json
{"type":"codex_state_request","request_id":7}
```

CodexBridge 应答：

```json
{"type":"codex_state","running":1,"done":2,"failed":0,"request_id":7}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `codex_state` |
| `running` | integer | 是 | 当前处于进行中状态的任务数 |
| `done` | integer | 是 | 已完成的最近结果数 |
| `failed` | integer | 是 | 失败的结果数 |
| `request_id` | integer | 否 | 仅当请求携带时回显 |

计数由 CodexBridge 从统一消息存储实时聚合，不需要设备维护第二套统计。
设备应自行控制拉取频率（例如前台 15 秒、息屏 60 秒），避免频繁唤醒射频。
设备上线时 CodexBridge 额外主动下发一次快照，避免刚重连时界面空白。
未授权事件返回 `capability_not_enabled`。

### 9.8 能力对应表

| 事件 | 所需能力 |
| --- | --- |
| `read_ack` | `read_ack` |
| `approval_decision` | `approval` |
| `cancel_task` | `tasks` |
| `record_start` | `audio` |
| `audio_chunk` | `audio` |
| `record_stop` | `audio` |
| `record_cancel` | `audio` |
| `voice_command` | `voice` |
| `codex_catalog_request` | `tasks` |
| `codex_project_select` | `tasks` |
| `codex_thread_select` | `tasks` |
| `codex_thread_new` | `tasks` |
| `codex_state_request` | `status` |

未知事件类型由 CodexBridge 忽略，不会因此断开会话。

## 10. 统一消息模型与长文本分片

### 10.1 消息归一

Codex 状态、任务和结果在 CodexBridge 内部统一为 `MessageRecord`，再转换为设备
可见的 `message`：

| 内部字段 | 线上字段 | 说明 |
| --- | --- | --- |
| `message_id` | `message_id` | 消息 ID |
| `thread_id` | `thread_id` | 线程 ID |
| `kind` | `kind` | 消息类别 |
| `status` | `status` | 当前状态 |
| `title` | `title` | 标题 |
| `body` | `body` | 正文 |
| `created_at` | `created_at` | 创建时间 |
| `data` | `data` | 可选结构化数据 |

### 10.2 分片规则

| 参数 | 当前值 |
| --- | --- |
| 单行上限 | `768` 字节 |
| 正文分片目标 | `480` 字节 |
| 标题上限 | `160` 字节 |
| 截断后缀 | `...` |

正文按 UTF-8 字符边界切片，不会切断多字节字符。所有分片共享同一个
`message_id`，通过 `seq` 和 `total` 重组。`seq` 从 `0` 开始。设备应至少保留
每个消息当前分片的拼接缓存，并在完成后按消息 ID 替换已有内容。

标题超过 160 字节时截断并追加 `...`。分片大小按整条记录的真实开销计算：
序号等元数据与可选 `data` 会挤占单行预算，因此正文分片上限是 `480` 字节与
剩余行预算中的较小值。如果编码后仍然超过单行上限，发送方会继续降低正文分片
大小；预算不足时先丢弃可选的 `data` 而不是丢弃整条消息。

## 11. 已读同步

### 11.1 上报

设备显示消息后发送：

```json
{"type":"read_ack","message_id":"task:t1"}
```

### 11.2 广播

CodexBridge 记录 `read_by`，并向其他支持 `read_ack` 的在线设备发送：

```json
{
  "type": "read_state",
  "message_id": "task:t1",
  "read_by": ["device-a"],
  "read_at": 1234567890.0
}
```

### 11.3 重连重放

设备重新连接后，CodexBridge 推送尚未被任何客户端读过的消息，并推送带有已读
记录的 `read_state`。任一设备上报 `read_ack` 后，CodexBridge 同时更新全局已读标记，
因此其他在线设备会收到 `read_state`，稍后重连的设备也不会再收到该消息。这保证同一
消息在多台设备之间保持一致，不依赖设备本地缓存。

## 12. 错误与边界

### 12.1 协议错误

当前通用协议只定义一种结构化错误码：

```json
{"type":"error","code":"capability_not_enabled"}
```

含义：设备发起了未生效能力对应的事件。其他业务错误可能以 `text` 字段返回，
例如语音识别失败、录音格式无效或录音不完整。

### 12.2 必须忽略的输入

- 未知消息类型。
- 未知能力名称。
- 非 JSON 行、JSON 数组或标量。
- 超过 768 字节的单行。
- 缺少必要字段的消息。
- 设备未授权能力对应的事件。

忽略单条消息不等同于断开连接。鉴权失败、身份不一致或 TCP 关闭会结束会话；
握手完成后的会话不会因为暂时收不到设备消息而被 PC 侧超时断开。

## 13. 本机管理接口

本节接口仅用于 CodexBridge 本机管理页面，不属于设备通用协议。管理服务默认
监听 `127.0.0.1:8788`，并拒绝非回环来源。

### 13.1 状态与设备

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/admin/status` | 版本与设备数量 |
| `GET` | `/api/admin/devices` | 设备列表、在线状态、`supported`、`enabled` |
| `POST` | `/api/admin/devices/remove` | 移除设备 |
| `POST` | `/api/admin/devices/capabilities` | 更新设备生效能力 |
| `GET` | `/api/admin/pairing?device_id=...` | 生成并返回配对令牌 |

### 13.2 语音与常规设置

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/admin/settings/speech` | 读取语音识别配置，隐藏 API Key |
| `POST` | `/api/admin/settings/speech` | 保存语音识别配置 |
| `POST` | `/api/admin/settings/speech/test` | 测试 OpenAI 兼容转写接口 |
| `GET` | `/api/admin/settings/general` | 读取开机自启动设置 |
| `POST` | `/api/admin/settings/general` | 更新开机自启动或打开日志目录 |

管理接口最多接受 `8192` 字节请求体；JSON 请求必须使用
`Content-Type: application/json`。API Key 不会通过 GET 返回；留空表示保留已
保存的 Key，使用 `clear_api_key: true` 才清除。

### 13.3 Dotii 管理中心快照接口

Dotii 管理中心默认监听 `127.0.0.1:8787`，它与通用设备 TCP 协议并行存在：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 管理中心健康检查 |
| `GET` | `/api/v1/snapshot` | 读取 Dotii 管理快照 |
| `POST` | `/api/v1/snapshot` | 写入 `schema_version = 1` 的管理快照信封 |

`/api/v1/snapshot` 使用请求头 `X-Bridge-Token` 鉴权。快照只承载
Bambu、自定义页、显示和模块等管理配置；Codex 状态、任务、结果和统一消息
明确不在这里，仍然只走本文件第 4 至第 12 节的设备 TCP 协议。因此，Dotii
管理中心接口不是第二套 Codex 设备协议。

## 14. 安全要求

- 每台设备使用独立配对令牌，长度至少 `16` 字符。
- TCP `8765` 和 UDP `8764` 只在可信局域网内使用，不要暴露到公网。
- 本机管理 API 只监听回环地址，非回环请求返回 `403`。
- 设备 ID、令牌、能力、消息文本和音频都有明确长度或大小边界。
- 日志和错误响应不得泄露配对令牌或 API Key。
- 设备必须在 `bridge_auth_ok` 中上报自身稳定的 `device_id`，CodexBridge 会严格比对。
- 设备端必须忽略未知消息类型，避免协议扩展导致旧固件断线。

## 15. 协议演进

- 保持同一套发现、握手和 JSON 行格式。
- 新功能优先增加可选字段或新消息类型。
- 新能力加入能力清单，由 `supported ∩ enabled` 决定是否启用。
- 不增加协议版本分支、并行握手或旧设备专用通道。
- 不兼容变更必须在设备固件、CodexBridge 和测试中同步完成。

## 16. 最小接入示例

设备实现的最小可行流程：

1. 监听 UDP `8764`，收到 `CODEXBRIDGE_DISCOVER` 后返回身份和 TCP 端口。
2. 监听 TCP `8765`，接受 CodexBridge 连接。
3. 收到 `bridge_auth` 后校验令牌，返回 `bridge_auth_ok`。
4. 收到 `bridge_ready` 后保存 `capabilities`。
5. 收到 `message` 后按 `seq` 和 `total` 拼接正文。
6. 支持 `read_ack` 时，用户读完消息后发送 `read_ack`。
7. 对未知事件类型不做处理；对未授权事件不主动发送。

完整交互示例：

```text
PC -> 设备: CODEXBRIDGE_DISCOVER
设备 -> PC: {"device":"dotii","device_id":"a1","name":"Desk","port":8765,"capabilities":["status","text","read_ack"]}
PC -> 设备: {"type":"bridge_auth","token":"..."}
设备 -> PC: {"type":"bridge_auth_ok","device_id":"a1","model":"dotii","name":"Desk","firmware":"1.0.0","capabilities":["status","text","read_ack"]}
PC -> 设备: {"type":"bridge_ready","bridge":"CodexBridge","version":"1.4.0","capabilities":["read_ack","status","text"]}
PC -> 设备: {"type":"message","message_id":"task:t1","thread_id":"t1","kind":"task","status":"working","title":"Codex 任务","body":"正在处理","created_at":1234567890.0,"seq":0,"total":1}
设备 -> PC: {"type":"read_ack","message_id":"task:t1"}
PC -> 设备: {"type":"read_state","message_id":"task:t1","read_by":["a1"],"read_at":1234567890.0}
## 17. 手机伴侣接口

手机端不实现设备 UDP/TCP 协议，而是使用 CodexBridge 直接托管的伴侣接口。手机与电脑在同一
局域网内，通过配对码换取长期令牌后，可以查看线程、读取消息、远程发起任务、切换模型并处理审批。

| 用途 | 传输 | 端口 | 发起方 |
| --- | --- | --- | --- |
| 手机发现 | UDP | `8569` | 手机广播，CodexBridge 应答 |
| 手机伴侣 API | HTTP + SSE | `TCP 8568`（可在管理页修改） | 已配对手机 |

发现请求为明文 `CODEXBRIDGE_COMPANION_DISCOVER`（不带换行），应答为不超过 512 字节的 JSON：

```json
{"service":"codexbridge-companion","name":"DESKTOP","version":"1.4.0","port":8568,"discovery_port":8569,"requires_pairing":true}
```

默认连接端口为 `8568`，默认发现端口为 `8569`，并在管理页面显示每个端口的健康状态。
手机连接端口和手机发现端口都可以在电脑端修改；修改发现端口后，手机端“扫描端口”
必须填写同一个值。`8764` 和 `8765` 是设备固件定义的固定端口。`8790` 由本机其他程序
保留，CodexBridge 不接受配置或回退到该端口；旧版本保存的 `8790` 会在升级后自动迁移为
`8568`，配对令牌保持不变。修改端口时，新端口先完成监听才会释放旧端口；目标端口无效
或被占用时继续使用原端口。

### 17.1 配对与鉴权

- `POST /api/companion/pair` 请求体为 `{"code":"123456","name":"Pixel"}`；配对码在电脑端管理页面生成，`10` 分钟有效，最多尝试 `5` 次，成功使用一次后立即失效。
- 配对成功后返回 `{"ok":true,"phone_id":"phone-xxxxxxxxxxxx","token":"cbp_...","bridge":{...}}`。令牌只在这次响应中返回，电脑端只保存 SHA-256 摘要。
- 后续请求使用 `Authorization: Bearer <token>`；SSE 允许使用 `?token=`。未配对返回 `401 unauthorized`，电脑端关闭手机连接时返回 `403 companion_disabled`，配对码错误返回 `403 pairing_failed`。

### 17.2 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/companion/ping` | 无需鉴权，返回服务名、版本、端口 |
| POST | `/api/companion/pair` | 无需鉴权，使用配对码换取令牌 |
| GET | `/api/companion/status` | 额度、当前任务与待处理审批 |
| GET | `/api/companion/threads` | 线程列表，支持 `limit`、`search`、`cursor` |
| POST | `/api/companion/threads` | 新建线程，可带 `cwd`、`model` |
| GET | `/api/companion/threads/<id>` | 线程详情与归一化消息 |
| POST | `/api/companion/threads/<id>/read` | 标记该线程当前完成状态为已读，返回最新 `status` |
| POST | `/api/companion/threads/<id>/messages` | 发送消息，可带 `model`、`effort`；`mode=steer` 表示追加到进行中的任务；响应中的 `continued_as`（非空时）指向实际承接历史的线程 id |
| POST | `/api/companion/threads/<id>/interrupt` | 中断任务，`turn_id` 可省略 |
| POST | `/api/companion/approvals/<request_id>` | 审批决定，`decision` 取 `accept`、`acceptForSession`、`decline`、`cancel` |
| GET | `/api/companion/models` | 可用模型与推理档位 |
| GET | `/api/companion/projects` | 最近项目目录 |
| GET | `/api/companion/events` | SSE 事件流，可用 `thread_id` 过滤 |

### 17.3 消息与事件

线程详情中的消息统一为 `kind`：

| `kind` | 说明 |
| --- | --- |
| `user` | 用户消息 |
| `agent` | 最终回复 |
| `commentary` | 进行中的可见进展 |
| `reasoning` | 推理摘要 |
| `command` | 命令执行，`detail.output` 为截断输出 |
| `fileChange` | 文件变更，`detail.paths` 与 `detail.diff` |
| `tool` | 工具调用 |
| `plan` | 计划步骤 |
| `notice` | 系统提示，例如上下文压缩 |

SSE 每条事件为一行 `data:` JSON，可能的 `type`：`hello`、`turn_started`、`turn_completed`、
`turn_interrupted`、`item_started`、`item_completed`、`agent_delta`、`command_output`、
`token_usage`、`thread_status`、`thread_name`、`thread_forked`、`threads_changed`、`resync_required`、
`rate_limits`、`approval_request`、`approval_resolved`、`error`。手机端必须忽略未知 `type`。`turn_completed` 还携带电脑端 turn 的真实 `completed_at` 秒级时间戳、`read` 布尔值与 `error_message`（失败原因，成功时为空字符串）；手机端应使用前两者判断完成先后、给状态栏提醒去重，并在 `read=true` 时不再提醒，失败时直接展示 `error_message`。审批请求超过 `600` 秒未处理会在电脑端失效。

Bridge 每 2 秒校准最近线程的外部变更，变化时发送 `threads_changed`（携带
`thread_id`），并对观测期间发生的完成补发 `turn_completed`。首次扫描不会重放
历史完成。客户端必须按 `thread_id` 与 `turn_id` 去重，并在 SSE 断开后重连。
Windows 没有可用的桌面共享 App Server socket，Bridge 因此对桌面已知任务使用
`owner_routed`：先通过桌面内部 IPC 找到当前 owner，再把 start、steer、interrupt
和审批定向发给它，并订阅该 thread 的状态快照。这个模式是逐任务定向，不是全局
共享执行；`connection.desktop_realtime_shared` 仍为 `false`。
消息里显式携带的 `model`/`effort` 会先通过桌面 IPC 写入该线程的设置再发起 turn：桌面 follower turn 默认继承线程设置，只把模型放进 turn 请求会被桌面忽略。
桌面 IPC 不可用或握手期间 owner 尚未建立时，写操作返回 `desktop_unavailable`
或 `desktop_handoff_pending`，不会静默退回独立 App Server 执行，避免同一 thread
被两个 writer 同时写入。

手机新建任务先用 `threadSource=user` 创建，并用中性占位名 `新任务` 提交 rollout，
立即释放 writer，再让桌面打开 `codex://threads/<id>`，确认桌面成为 owner 后才返回成功。
首条用户消息成功后，Bridge 会生成简洁标题，通过 `thread/name/set` 持久化，
发布 `thread_name` 与 `threads_changed`，并使桌面的 `['tasks']`、
`['list-pinned-threads']`、`['archived-threads','local']` 查询失效。这样桌面端会
正常加载并立即刷新，不会生成旧的 `CodexBridge` 占位会话。桌面 IPC 是 Bridge 内部适配层，不属于
ESP/设备协议；设备只使用后文的通用命令协议。

完成与失败的提醒在多台客户端之间保持一致：`completed`/`failed` 的线程在任一台设备
打开（`GET /api/companion/threads/<id>` 或 `POST /api/companion/threads/<id>/read`）后即被
标记为全局已读，线程列表状态回落为 `idle`，并向所有在线客户端广播 `thread_status`
的 `idle` 事件，其他设备据此撤回尚未展示的完成提醒。

### 17.4 电脑端占用线程时的续写

app-server 对同一线程只允许一个 writer。电脑端 Codex（或另一个 app-server 进程）打开某线程后，
可能仍留下 `~/.codex/thread-writer-locks/<thread_id>.lock`。CodexBridge 收到“已有活跃 writer”时
优先清除这个陈旧锁并重新 `thread/resume`，随后手机和电脑继续使用同一个线程 id，历史不复制。

- 清锁成功时 `POST /api/companion/threads/<id>/messages` 的 `continued_as` 为空；所有 SSE 事件、
  线程列表和详情都使用原线程 id。
- 电脑端 finish 后可能不会自动重渲染旧快照。`turn_completed` 事件会标记 `append_only=true`，客户端
  收到后可重新拉取该线程详情，把电脑端这次对话追加到同一会话里。
- 只有锁文件无法删除、resume 仍被拒绝时，才退回到旧的 `thread/fork` 兜底。fork 会重命名为
  `手机继续 · <原任务名>`，映射保存在 `%LOCALAPPDATA%\CodexBridge\fork-map.json`；`continued_as`
  非空时手机可提示这条极端路径。
- 若 fork 也失败，返回明确的中文错误，提示先在电脑端关闭该会话后重试。
- `mode=steer` 需带进行中的 `expectedTurnId`；当前没有进行中的对话时返回“该任务当前没有
  进行中的对话，无法追加消息”。

### 17.5 本机管理

事件恢复：SSE `id` 与 JSON 的 `event_id` 为同一不透明游标，不得按数字比较。
客户端重连通过 `Last-Event-ID` 请求头或 `after` 参数提交最后已接收游标。
Bridge 在进程内保留最近 1000 条事件，原子地重放游标后的事件并接入实时流。
Bridge 重启、游标过期或慢消费者队列溢出时发送 `resync_required`，客户端必须
重新读取列表、当前任务和审批快照。事件携带 `server_time`（epoch 秒）。
首次连接不重放历史事件；此机制不保证手机离线期间的系统通知送达。
手机与未来设备客户端应按事件 ID 去重，并按任务/轮次 ID 合并状态。

桌面跨客户端 IPC 是 Bridge 内部适配协议，不是设备接入协议。
设备不得依赖桌面的命名管道、私有消息版本或 conversationState 结构。

- `GET /api/admin/companion` 返回开关、局域网地址、当前端口、端口健康状态与已配对手机。
- `POST /api/admin/companion` 支持 `{"enabled":true}`、`{"action":"new_code"}`、`{"action":"remove_phone","phone_id":"phone-xxxxxxxxxxxx"}`、`{"action":"set_port","kind":"companion","port":8568}` 和 `{"action":"set_port","kind":"discovery","port":8569}`。
- 本机管理接口仍只允许回环地址访问；手机伴侣接口不开放本机管理能力。
```
