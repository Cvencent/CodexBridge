# CodexBridge 本机管理与伴侣接口

> **文档导航：** [项目说明](../README.md) · [CBDP v1 线协议](CBDP-V1-PROTOCOL.md) · **本机管理与伴侣接口**

本文档记录 CodexBridge 的**非设备协议**接口：手机伴侣 API、本机管理 API、
待发送队列，以及 Dotii 管理中心快照边界。

**设备接入统一以 [CBDP v1 线协议](CBDP-V1-PROTOCOL.md) 为准。** 本文档不定义
设备发现、握手、能力协商、消息或事件模型，也不得据此实现新设备。旧 JSON 行
协议只作为兼容网关存在，映射规则见 [CBDP v1 线协议](CBDP-V1-PROTOCOL.md) 第 24 节。

## 1. 待发送队列

队列属于线程，不属于某台设备，由 CodexBridge 统一持有并持久化到
`%LOCALAPPDATA%\CodexBridge\queues.json`。手机、ESP 和管理页面看到的是同一份 FIFO
队列；线程空闲时 Bridge 立即把队首转成 `turn.start`，线程进行中时保持 `pending`，
turn 结束后再派发下一项。手机不再自行决定 `steer` 或 `start`。

### 1.1 能力与方法

队列命令要求 `queue` 能力。手机默认启用；硬件必须在设备握手声明的 `supported`
能力中包含 `queue` 后才启用（CBDP v1 为 `supported_caps` 位，旧 JSON 固件为
`bridge_auth_ok.capabilities`）。未声明 `queue` 的旧客户端继续使用
`turn.start` / `mode=steer`。

| 方法 | 必需参数 | 可选参数 |
| --- | --- | --- |
| `queue.list` | `thread_id` | |
| `queue.enqueue` | `thread_id`, `text` | `model`, `effort` |
| `queue.edit` | `queue_id`, `text`, `expected_revision` | `model`, `effort` |
| `queue.delete` | `queue_id`, `expected_revision` | |
| `queue.steer` | `queue_id`, `expected_revision` | |

HTTP 手机使用现有 `POST /api/companion/commands` 信封；CBDP v1 设备使用
[CBDP v1 线协议](CBDP-V1-PROTOCOL.md) 的 `COMMAND` 信封。大结果继续使用分片返回。

旧式的 `POST /api/companion/threads/<thread_id>/messages` 保持兼容：已协商
`queue` 的手机发送 `mode=start`（或省略 mode）时映射为 `queue.enqueue`，返回
队列项；`mode=steer` 仍是立即引导。未声明 `queue` 的旧手机继续直接调用
`turn.start`。

### 1.2 队列项

```json
{
  "queue_id": "q_...",
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

`status` 为 `pending`、`dispatching`、`failed`、`dispatched`、`steered` 或 `deleted`。
终态项不再出现在快照中，但必须广播相应移除事件。编辑、删除和引导必须携带
`expected_revision`；不匹配返回 `queue_conflict`。`queue.enqueue` 不要求 revision，
但相同 `request_id` 必须幂等。

### 1.3 队列事件

手机 SSE 与设备出站事件共用同一套队列事件模型：

| 事件 | 含义 |
| --- | --- |
| `queue_snapshot` | 连接、重连或 `resync_required` 后的权威快照 |
| `queue_item_added` | 新增队列项 |
| `queue_item_updated` | 编辑、状态变化或 revision 变化 |
| `queue_item_removed` | 派发、引导或删除后的移除，带 `reason` |
| `queue_paused` | 派发失败后暂停该线程自动派发 |

客户端按 `queue_id` 和 revision 合并，不能按事件到达顺序推断队列顺序。每个事件带
全局 `revision`、`thread_id` 和 `server_time`。派发失败时项变为 `failed` 并暂停该线程，
避免对同一错误无上限热重试；暂停状态通过 `queue_snapshot` / `queue.list` 的
`paused` 布尔字段暴露。

暂停必须可恢复，且不需要额外的恢复命令。以下任一操作都清除暂停并唤醒派发：
`queue.enqueue`、`queue.edit`、删除该线程的失败项、删除后该线程已无失败项，
以及该线程有 turn 正常完成。因此用户删掉失败项后直接重发即可恢复，不会卡住。

SSE 和 TCP 设备事件按同一能力分类：`queue_*` 只发送给启用 `queue` 的客户端，
`approval_*`、`turn_*` / `resync_required` 和其余任务事件分别要求 `approval`、
`status` 和 `tasks`。声明 `queue` 的手机在 HTTP SSE 连接建立时按线程收到
`queue_snapshot`；CBDP v1 设备在握手完成后的订阅回放中收到同一快照。

### 1.4 上限与恢复

- 每个线程最多 `100` 项，单个未完成项最多 `10000` 个字符。
- 每次新增、编辑、删除和状态变化都原子落盘；Bridge 重启后重新载入，并在线程空闲时继续派发。
- 队列使用 `request_id` 幂等缓存和线程级串行派发，避免重复 `turn.start`。


## 2. 统一设备与管理接口

`DeviceRegistry` 是手机和硬件设备身份、注册状态与能力的唯一权威来源。
手机仍通过配对码登记，硬件通过发现与后续的注册批准流程登记；两者进入同一份
`devices.json`（版本 `2`），管理页和内部事件只使用同一份公共设备视图。

### 2.1 统一设备记录与公开视图

内部记录保留硬件令牌和手机令牌摘要，但公共视图绝不返回 `token` 或
`token_hash`，只返回 `has_token`。`kind` 取值 `phone` / `hardware`，
`transport` 取值 `companion_http` / `device_tcp`，`registration_state`
取值 `pending` / `approved` / `rejected` / `revoked`。手机配对成功后为
`approved + pairing_code`；硬件无令牌时为 `pending`，批准后为
`approved`。

`GET /api/admin/devices` 返回统一列表，每项字段为：

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
  "last_address": "192.168.1.23",
  "has_token": true,
  "supported": ["status", "tasks", "control", "approval", "queue"],
  "enabled": ["status", "tasks", "control", "approval", "queue"],
  "max_text_chars": null,
  "queued_count": 2
}
```

`GET /api/admin/companion` 继续保留 `phones` 字段，但它只从统一设备视图派生，
不再形成第二份手机列表。旧版 `companion.json` 中的 `phones` 会在启动时合并进
统一注册表，升级后原 `phone_id`、令牌摘要、`client_id`、能力和最近在线时间保持
不变；旧文件保留用于回滚。

### 2.2 状态与设备操作

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/admin/status` | 版本与设备数量 |
| `GET` | `/api/admin/devices` | 统一设备列表，含手机和硬件 |
| `POST` | `/api/admin/devices/remove` | 移除设备 |
| `POST` | `/api/admin/devices/capabilities` | 更新设备生效能力 |
| `POST` | `/api/admin/devices/approve` | 批准待注册硬件并下发令牌 |
| `POST` | `/api/admin/devices/reject` | 拒绝待注册硬件 |
| `POST` | `/api/admin/devices/revoke` | 撤销已批准硬件并清空令牌 |
| `GET` | `/api/admin/pairing?device_id=...` | 生成并返回硬件配对令牌 |

### 2.3 语音与常规设置

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

### 2.4 Dotii 管理中心快照接口

Dotii 管理中心默认监听 `127.0.0.1:8787`，它与通用设备 TCP 协议并行存在：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 管理中心健康检查 |
| `GET` | `/api/v1/snapshot` | 读取 Dotii 管理快照 |
| `POST` | `/api/v1/snapshot` | 写入 `schema_version = 1` 的管理快照信封 |

`/api/v1/snapshot` 使用请求头 `X-Bridge-Token` 鉴权。快照只承载
Bambu、自定义页、显示和模块等管理配置；Codex 状态、任务、结果和统一消息
明确不在这里，仍然只走 [CBDP v1 线协议](CBDP-V1-PROTOCOL.md) 的设备协议。因此，Dotii
管理中心接口不是第二套 Codex 设备协议。


## 3. 手机伴侣接口

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

### 3.1 配对与鉴权

- `POST /api/companion/pair` 请求体为 `{"code":"123456","name":"Pixel","client_id":"<安装标识>"}`。`client_id` 是可选字段，手机端应使用安装级稳定标识；旧客户端不传时电脑端保持兼容。配对码在电脑端管理页面生成，`10` 分钟有效，最多尝试 `5` 次，成功使用一次后立即失效。
- 配对成功后返回 `{"ok":true,"phone_id":"phone-xxxxxxxxxxxx","token":"cbp_...","bridge":{...}}`。同一 `client_id` 再次配对时复用原 `phone_id`，轮换令牌并更新名称，因此不会生成重复设备。令牌只在这次响应中返回，电脑端只保存 SHA-256 摘要。
- 轮换令牌不会让已经拿到旧令牌的手机被永久拒绝：同一 `client_id` 重新配对后，电脑端仍会在 `1` 小时内接受该手机此前最多 `4` 个旧令牌，一旦手机用最新令牌成功访问，这些旧令牌立即失效。这样即使配对响应因电脑重启或网络中断丢失，手机也能继续使用，不必让用户手动重新配对。
- 后续请求使用 `Authorization: Bearer <token>`；SSE 允许使用 `?token=`。未配对返回 `401 unauthorized`，电脑端关闭手机连接时返回 `403 companion_disabled`，配对码错误返回 `403 pairing_failed`。
- 网络临时断开或电脑重启导致的 `401` 不应清除手机端已保存的地址、令牌和 `client_id`；客户端恢复到同一地址后继续使用原令牌，只有用户在设置中主动解除配对或电脑端移除设备时才需要重新配对。
- 鉴权不落盘：每个带令牌的请求只在该配对文件的修改时间或大小变化时才重新读取并重新投影设备列表，其余情况直接命中内存状态。同一进程内的重复请求不产生额外磁盘写入，设备端按 `2` 秒级别轮询不会造成电脑端持续占用 CPU。
- 电脑端允许 HTTP/1.1 长连接：同一客户端可复用一条 TCP 连接连续发起多个请求，空闲 `30` 秒后由服务端关闭。手机端应复用连接而不是每次轮询新建连接；`/api/companion/events`（SSE）与 `/cbdp/v1/ws`（WebSocket）仍按各自的流式语义保持独立连接。

### 3.2 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/companion/ping` | 无需鉴权，返回服务名、版本、端口 |
| POST | `/api/companion/pair` | 无需鉴权，使用配对码换取令牌 |
| GET | `/api/companion/status` | 额度、当前任务与待处理审批 |
| GET | `/api/companion/threads` | 线程列表，支持 `limit`、`search`、`cursor` |
| POST | `/api/companion/threads` | 新建线程，可带 `cwd`、`model`；桌面接管暂未完成时返回 `desktop_handoff: "pending"` |
| GET | `/api/companion/threads/<id>` | 线程详情与归一化消息 |
| POST | `/api/companion/threads/<id>/read` | 标记该线程当前完成状态为已读，返回最新 `status` |
| POST | `/api/companion/threads/<id>/messages` | 发送消息，可带 `model`、`effort`；`mode=steer` 表示追加到进行中的任务；响应中的 `continued_as`（非空时）指向实际承接历史的线程 id |
| POST | `/api/companion/threads/<id>/interrupt` | 中断任务，`turn_id` 可省略 |
| POST | `/api/companion/approvals/<request_id>` | 审批决定，`decision` 取 `accept`、`acceptForSession`、`decline`、`cancel` |
| GET | `/api/companion/models` | 可用模型与推理档位 |
| GET | `/api/companion/projects` | 最近项目目录 |
| GET | `/api/companion/events` | SSE 事件流，可用 `thread_id` 过滤 |

### 3.3 消息与事件

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

默认 `show_intermediate_events` 为 `false`。关闭时，线程详情和 SSE 只保留
`user`、`agent`，以及审批、错误和状态等控制事件；`commentary`、`reasoning`、
`command`、`fileChange`、`tool`、`plan`、`notice` 和命令输出等中间内容在电脑端过滤，
不会发送给任何已配对手机或 ESP 设备。开启该全局设置后恢复完整事件输出。

SSE 每条事件为一行 `data:` JSON，可能的 `type`：`hello`、`turn_started`、`turn_completed`、
`turn_interrupted`、`item_started`、`item_completed`、`agent_delta`、`command_output`、
`token_usage`、`thread_status`、`thread_name`、`thread_forked`、`threads_changed`、`resync_required`、
`rate_limits`、`approval_request`、`approval_resolved`、`error`。手机端必须忽略未知 `type`。`turn_completed` 还携带电脑端 turn 的真实 `completed_at` 秒级时间戳、`read` 布尔值与 `error_message`（失败原因，成功时为空字符串）；手机端应使用前两者判断完成先后、给状态栏提醒去重，并在 `read=true` 时不再提醒，失败时直接展示 `error_message`。审批请求超过 `600` 秒未处理会在电脑端失效。

Bridge 有任务运行时每 2 秒、空闲时每 15 秒校准最近线程的外部变更，
变化时发送 `threads_changed`（携带
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

手机新建任务先用 `threadSource=user` 创建，并在共享 app-server 路径立即用中性占位名
`新任务` 执行 `thread/name/set` 提交 rollout，随后释放 writer、让桌面打开
`codex://threads/<id>` 并尝试确认桌面 owner。若桌面在 10 秒内没有完成 owner discovery，
或 deep link 无法打开，HTTP 仍返回已经创建成功的线程并附带
`desktop_handoff: "pending"`；首轮 turn 继续走共享 app-server，rollout 生成后桌面即可发现。
只有 `thread/start` 或返回线程 ID 本身失败时才返回 503。pending 状态不会保留
`_desktop_required`，避免后续 turn 永远等待不存在的桌面 owner。
首条用户消息成功后，Bridge 会生成简洁标题，通过 `thread/name/set` 持久化，
发布 `thread_name` 与 `threads_changed`，并使桌面的 `['tasks']`、
`['list-pinned-threads']`、`['archived-threads','local']` 查询失效。这样桌面端会
正常加载并立即刷新，不会生成旧的 `CodexBridge` 占位会话。桌面 IPC 是 Bridge 内部适配层，不属于
ESP/设备协议；设备只使用 [CBDP v1 线协议](CBDP-V1-PROTOCOL.md) 定义的命令协议。

完成与失败的提醒在多台客户端之间保持一致：`completed`/`failed` 的线程在任一台设备
打开（`GET /api/companion/threads/<id>` 或 `POST /api/companion/threads/<id>/read`）后即被
标记为全局已读，线程列表状态回落为 `idle`，并向所有在线客户端广播 `thread_status`
的 `idle` 事件，其他设备据此撤回尚未展示的完成提醒。

终态必须带有效完成时间（2026-10-19 实现修正）：

- `completed` / `failed` 的线程与任务必须同时返回 `completed_at > 0`。上游未给出
  `completedAt` 时，Bridge 依次使用 turn 的最后活动时间、线程最后更新时间、本机观测
  时间补齐；同一线程的终态时间戳只增不减。
- `failed` / `interrupted` 但拿不到该 turn 自己的结束时间时，视为网络重连、供应商
  限流重试或流重置留下的瞬态结果，对外保持 `working`，不得显示为 `failed`。只有
  确实拿到结束时间，或该 turn 已静默超过陈旧窗口并以最后活动时间落定，才发布失败。
- 缺少 turn 终态证据时不得推断 `completed`；无法观察的线程一律按 `idle` 处理。
- `threads.list`、`thread.read`、`thread_status` 和 `turn_completed` 必须给出同一组
  `status` / `completed_at` / `turn_id`。`turn_completed` 在瞬态失败时返回
  `status=working`、`completed_at=0`，客户端不得把它当作结束。
- 已读接口不再依赖 `completed_at`：只要线程不处于 `running` 类活动状态，Bridge 会用
  完成时间、最后更新时间或本机时间写入已读水位，因此 `completed_at = 0` 的历史线程
  也能在刷新后保持 `idle`。

### 3.4 电脑端占用线程时的续写

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

### 3.5 本机管理

事件恢复：SSE `id` 与 JSON 的 `event_id` 为同一不透明游标，不得按数字比较。
客户端重连通过 `Last-Event-ID` 请求头或 `after` 参数提交最后已接收游标。
Bridge 在进程内保留最近 1000 条事件，原子地重放游标后的事件并接入实时流。
Bridge 重启、游标过期或慢消费者队列溢出时发送 `resync_required`，客户端必须
重新读取列表、当前任务和审批快照。事件携带 `server_time`（epoch 秒）。
首次连接不重放历史事件；此机制不保证手机离线期间的系统通知送达。
手机与未来设备客户端应按事件 ID 去重，并按任务/轮次 ID 合并状态。

桌面跨客户端 IPC 是 Bridge 内部适配协议，不是设备接入协议。
设备不得依赖桌面的命名管道、私有消息版本或 conversationState 结构。

- `GET /api/admin/companion` 返回开关、局域网地址、当前端口、端口健康状态与已配对手机；手机列表由统一设备注册表派生。
- `GET /api/admin/devices` 同时返回手机和硬件；公共字段包含 `kind`、`transport`、`registration_state`、`registration_method`、`online`、`last_address`、`has_token`、`queued_count`，不包含任何令牌或令牌摘要。
- `POST /api/admin/companion` 支持 `{"enabled":true}`、`{"show_intermediate_events":true}`、`{"action":"new_code"}`、`{"action":"remove_phone","phone_id":"phone-xxxxxxxxxxxx"}`、`{"action":"set_port","kind":"companion","port":8568}` 和 `{"action":"set_port","kind":"discovery","port":8569}`。
- `GET /api/admin/codex-status` 返回 `codex.enabled`、采集状态与最后快照时间。
- `POST /api/admin/codex-status` 支持 `{"enabled":false}` 与 `{"enabled":true}`，语义是“空闲采集节奏”而非功能开关。任一时间点只要存在在线设备会话或手机事件流订阅，Bridge 就按原协议的 5 秒节奏采集并推送；`enabled:false` 只把“无任何客户端连接”时的采集降为低速兜底（数据仍持续刷新，不会中断），有客户端出现时立即恢复全速。`enabled:true` 表示空闲时也保持全速。设备连接、心跳、语音、任务列表与 CBDP 线协议行为不因该设置而减少。
- 本机管理接口仍只允许回环地址访问；手机伴侣接口不开放本机管理能力。

### 3.6 读取缓存与新鲜度

伴侣接口与后台校准共用同一份只读缓存，规则如下，设备端可以据此安排轮询节奏：

- 同一参数的列表、线程正文、日志读取在有效期内不会再次访问 Codex；重复轮询的
  成本为零次 Codex 往返。
- 任务列表使用统一的 80 行宽页，`limit` 只在该页上切片，因此手机与电脑端不会
  各自触发一次全量拉取。带 `search` 或 `cursor` 的请求仍单独取页。
- 任何与该线程相关的变更（本端发起的 start/steer/interrupt、审批、桌面改名、
  `turn_*` 事件）都会让该线程、其 fork 与其 origin 的缓存立即失效；
  `agent_delta` 不触发失效，因为一轮回复每个 token 都会产生一次。
- 对于 rollout 日志无法判定的行，Bridge 只读取一次 turn 快照并把结论保留
  600 秒；日志一旦能判定就以日志为准。
- App Server 进程重启会清空全部缓存。
- 2026-10-23 起，长线程正文改为有界分页读取：线程元数据、
  `thread/turns/list` 摘要页、以及最新 turn 的有界
  `thread/items/list` 头尾页，不再一次性拉取整条线程。这是 PC 侧读取方式与
  缓存策略的变化，**CBDP 线协议字段、消息类型、端口与握手均未变更**，设备端
  无需同步升级。分页项读取同样纳入上面的短缓存规则。
- **2026-10-23 后续修正：** 新版 Codex 桌面可能只保存
  `thread-client-id-v1:local%3A<thread_id>`、
  `thread-reference-capability:<thread_id>`，不再写旧的
  `thread-user-activity-times-v1`。Bridge 现在把这些键视为“桌面已收录该线程”；
  若线程不在桌面 `unreadByIdentity` 且桌面状态文件晚于最新终态结果，就推进共享
  已读水位并清除手机/ESP 的完成与失败角标。没有桌面记录且没有 activity 的纯手机
  线程仍不会被误清除。
- **2026-10-23 安全修正：** UDP `HOME_COUNTERS(0x1E)` 的目标注册此前只检查
  presence，未检查 `registration_state`，导致未批准设备也能读到主机 Codex
  聚合计数。现在只有 `registration_state == approved` 且持有配对令牌的设备会被
  注册为计数目标；批准后立即开始、拒绝或撤销后立即停止。pending / rejected /
  revoked 设备不再收到任何 UDP 计数帧。

因此设备端不需要为了“怕看到旧数据”而缩短轮询间隔：状态变化会在下一次校准
（运行时 2 秒、空闲时 15 秒）内被推送，未变化时缓存读取是常数时间。

### 3.7 会话、限流与持久化实现修正

这些是 Bridge 侧的伴侣接口行为约束；CBDP 线协议字段与握手不受影响。

- **共享 HTTP 会话。** 同一手机设备的 CBDP SSE 与 WebSocket 共用一条
  `CbdpHttpSession`。单条 SSE 或 WebSocket 断开时只注销该网络流，不再连带关闭
  共享会话；会话由 `last_seen` 超过 120 秒 TTL 后回收，或在 Companion 服务停止时
  统一关闭。手机同时建立两条网络流时，一条断开不会让另一条一起断，避免高频重连。
- **长连接限流。** 同一设备合计最多 6 条长连接（SSE 2、WebSocket 2、Companion
  events 2），全服务最多 24 条。超限时 Bridge 直接返回 HTTP `429`，错误码
  `too_many_streams` 或 `server_stream_limit`，并带 `Retry-After: 20` 与
  `Connection: close`。客户端收到后应停止并发新流、按 `Retry-After` 退避，然后
  复用已有单条长连接，而不是无限重试。每条长连接都会占用一个服务端线程，因此
  必须限制单台异常设备的堆积。
- **心跳刷新在线状态。** 手机 Companion 的 SSE/WebSocket 心跳会刷新 `last_seen`。
  管理接口按 120 秒窗口把最近活跃的 `companion_http` 手机视为在线，不再只依赖
  设备 TCP 会话表。已批准且持有配对令牌的硬件设备只要发出有效 UDP discovery
  应答或 presence 通告，也会刷新 `last_seen` 并在最近 30 秒内视为在线；`online`
  只表示可达，任务控制、订阅和上行命令仍要求建立并完成会话。
- **审批提交路由。** Companion 审批提交路由为
  `POST /api/companion/approvals/<request_id>`（路径共 4 段）。它只匹配 4 段
  路径，写成 5 段会落到 404。请求体带 `response` 时走结构化回复，否则使用
  `decision`。
- **配对记录持久化。** `companion.json` 的配对记录为失败关闭加载：读取异常时
  拒绝启动并保留原文件；JSON/UTF-8 损坏时先隔离为
  `companion.json.corrupt*.bak`，再从 `devices.json` 的 phone 投影恢复。写入使用
  fsync + 原子替换，以兼容 Windows。Bridge 重启后不依赖内存状态，手机长期令牌
  不受影响。
- **活跃 turn 判定。** 手机队列的活跃判定把 `interrupted` 且无 `completedAt`
  的 turn 视为活跃，但只在 `LOG_ACTIVE_SECONDS`（150 秒）新鲜窗口内有效。超过
  窗口的 turn 视为已死并释放队列，避免一次被第二个 writer 杀掉的 turn 永久卡住
  队列，直到用户删除会话或重启 Bridge。
- **桌面活跃线程的 owner 路由。** 手机写入桌面活跃线程前必须执行 DesktopIPC
  owner discovery；`CompanionService` 只在显式 `isolated=True` 时才跳过 owner
  发现，即使注入了共享 app-server 也要先查明 owner。通过
  `thread-follower-start-turn` 让手机回复跟随桌面 Codex，避免发送到陈旧的
  app-server；没有实时订阅时，后续回复也不会被第二个 app-server writer 截断。

## 4. 手机命令与协议发现接口

手机在配对后通过 HTTP 命令信封调用统一命令层；CBDP v1 设备使用协议里的
`COMMAND` 消息，两者共用同一份目录、能力校验和幂等逻辑。

- 目录发现：`GET /api/companion/protocol`，返回 `protocol`、`command_schema`
  与 `methods`（每项的 `method`、`capability`、`required`、`optional`）。
- 命令执行：`POST /api/companion/commands`，请求体为
  `{"request_id":"...","method":"...","params":{...}}`。
- 能力协商：`POST /api/companion/capabilities`，请求体
  `{"supported":["status","tasks", ...]}`，返回 `enabled` 交集。

成功响应为 `{"ok": true, "request_id": ..., "method": ..., "result": ...}`；
失败为 `{"ok": false, "request_id": ..., "error": ...}` 并返回非 2xx 状态。

每次逻辑操作都要使用新的 `request_id`，重试必须复用同一个 ID 与请求体。
派发器按设备 ID 在内存中缓存最近 1000 个请求：同一 ID 携带不同请求体返回
`request_id_conflict`，执行抛异常后重试返回 `outcome_unknown` 且不会重复执行。
这是进程生命周期内的有界保证，不是持久化的 exactly-once 投递；服务重启后的
不确定写入必须先对账任务历史，不能直接重试。

| 能力 | 方法 |
| --- | --- |
| status | status.read |
| tasks | threads.list, thread.read, models.list, projects.list |
| control | thread.start, thread.rename, turn.start, turn.steer, turn.interrupt |
| approval | approvals.list, approval.resolve, approval.respond |
| queue | queue.list, queue.enqueue, queue.edit, queue.delete, queue.steer |

`GET /api/companion/status` 会返回当前 `show_intermediate_events` 开关值，供客户端
决定是否展示中间事件；该开关是主机全局设置，默认关闭，关闭时评论、推理、命令
输出、文件变更、工具调用、计划和上下文提示在主机侧就被过滤，不会下发到任何手机
或设备，审批、错误与状态事件不受影响（详见 3.3）。

手机新建任务先以中性占位名提交，使 Codex 桌面能够接管，再用首条用户消息通过
`thread.rename` 落定简洁标题；重命名会发布线程事件并使桌面任务列表查询失效。
桌面 IPC 是 Bridge 内部适配层，不属于本文档定义的公开接口。

`approval.respond` 需要 `approval_id` 与 `response` 对象。输入类回复使用
`{"answers":{"question-id":{"answers":["choice"]}}}`；MCP 类回复使用
`{"action":"accept","content":{}}`（或 decline/cancel）；权限类回复使用
`{"permissions":{},"scope":"turn"}`，空权限不授予任何内容。命令与文件审批保留
显式 `decision` 形式。无效的响应类型不会改变待处理状态，桌面发起的请求仍路由回
其 owner。
