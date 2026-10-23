# CodexBridge 通用设备协议

> **文档导航：** [项目说明](README.md) · **通用协议** · [CBDP v1 协议](docs/CBDP-V1-PROTOCOL.md)

本文档描述 CodexBridge 当前代码实现的通用设备协议，供 Dotii、AI Passport
以及其他局域网设备实现接入。协议字段名、消息 `type` 和 JSON 示例保持英文，
说明文字使用中文。

CBDP v1 是当前主设备协议；本文档定义旧设备继续接入时使用的 UTF-8 JSON
行协议及兼容网关行为。新设备应优先按 [CBDP v1 线协议](docs/CBDP-V1-PROTOCOL.md)
实现。

本文档以仓库当前实现为准。设计稿中未在代码中落地的字段或行为不会写成既有
协议；后续只增加可选字段或新消息类型，不建立第二套握手。

## 维护要求

任何会改变设备发现、TCP 会话、握手、能力、消息字段、事件、错误码、分片规则
或本文件记录的管理接口的改动，都必须在同一个提交中同步更新本文件。修改协议
行为时，同时更新相关测试和示例；不确定是否影响协议时，按需要更新处理。

## 当前协议实现修正

CBDP v1 的细节以 [CBDP v1 线协议](docs/CBDP-V1-PROTOCOL.md) 为准。2026-10-19 修正了以下 Bridge 行为：

- `thread.read(0x0003)` 的 `history_depth(0x0014)` 按 `u16` 解码，并转换为内部消息条数；省略时使用设备 profile 的历史预算，不再把该字段当作泛化 `limit` 传入共享命令层。
- `message.read.update(0x00A0)` 现在进入共享命令层并写入统一已读存储；`message_ref` / `thread_ref` 先从持久引用表解析，未知引用返回 `NOT_FOUND`。
- `AUDIO_END` 现在会把 STT 的真实失败原因写入 `error_message(0x0062)`，并在 `error_detail(0x0063)` 标记 `reason = STT_UNAVAILABLE`；协议错误码仍是 `UPSTREAM_UNAVAILABLE`，设备因此可以区分识别上游故障与音频参数错误。
- `RESULT` / `ERROR` 现在分配非零递增 `seq`，并在固定头 `ack_seq` 里 piggyback 已连续收到的设备序号。之前它们发 `seq = 0`，而 §13.1 只豁免 PING/PONG/ACK/CLOSE 和发现消息，属于实现缺陷。
- `active_state.read` / `active_state.set` 现已实现：读取当前设备的活动线程/turn，设置时把设备上报的活动线程记录进 Hub。二者都要求 `cross_device_active` 能力；未选中线程时 `read` 返回全 0，引用无法解析时 `set` 返回 `NOT_FOUND`。`events.since` 与 `resume.*` 仍为 `[本轮不实现]`。
- 幂等键固定为 `(device_id, idem)`，不再使用 `request_id`。`request_id` 只在单条连接内唯一，设备重启后从 1 重新计数，用它做键会让新会话与自己的 24 小时历史冲突。
- `SUBSCRIBE` 省略 `cursor` 等价于 `cursor = 0`（从历史起点回放）。回放改由每设备工作线程发送，socket 读取循环在回放期间继续排空设备上行，避免录音被截断。
- `AUDIO_BEGIN` / `SEND_BEGIN` 不要求先 `thread.claim`；writer lease 与上行选线程无关。`thread_ref` 在 `BEGIN` 时解析，解析失败返回 `NOT_FOUND`，不会再落成 `UNSUPPORTED_FEATURE`。
- CBDP 会话对未预期异常返回 `ERROR(INTERNAL)` 并保持连接，不再因单帧异常断开。
- **2026-10-21 后续修正：** Bridge 现在按协议发布 thread 事件：线程首次出现
  写 `thread.created(0x0101)`，标题或状态变化写 `thread.updated(0x0102)`，
  同一标题与状态不重复发布。此前只写 message/turn 事件，导致
  `cbdp-projection.sqlite3` 的 `threads` 表长期为 0，设备侧线程投影同样为空。
  投影层现在也会从 message 与 turn 终态事件兜底生成 thread 行。
- **2026-10-21 后续修正：** 当当前聚合计数没有对应的历史
  `system.codex_state(0x0704)` 时，Bridge 会追加一条最新事件并立即投递给发起
  订阅/拉取的会话；旧实现只追加、不投递，设备重连后仍会停留在 0。
- **2026-10-21 后续修正：** `device.online(0x0601)` / `device.offline(0x0602)`
  改为边沿触发并带 8 秒在线迟滞。失败拨号、重复 detach 和旧会话晚到清理不再
  重复发布离线事件；宽限窗口内重连不会产生一次假离线。旧会话也不能移除已经
  接替它的新会话。
- **2026-10-21 后续修正：** 手机 Companion 的 SSE/WebSocket 心跳会刷新
  `last_seen`。管理接口按 120 秒窗口把最近活跃的 `companion_http` 手机视为在线，
  不再只依赖设备 TCP 会话表。
- **2026-10-22 后续修正：** 同一手机设备的 CBDP SSE 与 WebSocket 共用一条
  `CbdpHttpSession`。单条 SSE 或 WebSocket 断开时只注销该网络流，不再连带关闭
  共享会话；会话改由 `last_seen` 超过 120 秒 TTL 后回收，或在 Companion 服务
  停止时统一关闭。否则手机同时保持两条传输时，任一条断开都会把另一条一并打断，
  造成高频重连。
- **2026-10-22 后续修正：** CBDP 长连接按设备限额限流。同一设备合计
  最多 6 条长流（SSE 2、WebSocket 2、Companion events 2），
  全服务最多 24 条。超限时 Bridge 直接返回 HTTP 429，
  错误码 `too_many_streams` 或 `server_stream_limit`，并带
  `Retry-After: 20` 与 `Connection: close`。设备收到后应停止并发新
  流、按`Retry-After`退避后复用已有单条长连接，而不是无限
  重连。Bridge 侧为每条长流占用一个阻塞线程，若不设上限，单
  台异常设备即可堆积数百条连接并耗尽电脑响应能力。已断开的
  长流会在心跳间隔内释放占用的名额。
- **2026-10-22 后续修正：** `system.codex_state(0x0704)` 和 UDP
  `HOME_COUNTERS(0x1E)` 的 `running` 统一按线程去重统计。collector 扫描当前
  `thread/list` 单页最多 20 条线程，`task` 与 `status` 两类 live 记录都参与计算，
  同一 `thread_id` 只计一次；`working`、`waiting_user`、`queued`、`starting`
  都属于运行中。这样多个并发 in-progress 会话不会因为显示候选窗口或重复记录而
  少算/多算。
- 首页聚合计数在 CBDP 上改用独立事件 `system.codex_state(0x0704)`，在
  `system.thread_summaries(0x50000003)` 的 `summary` scope、`state` QoS 下发，
  携带 `running_count(0x00BC)`、`completed_unread_count(0x00BD)`、
  `failed_unread_count(0x00BE)`。旧的 `codex_state` 字典无法翻译成 CBDP DELTA，
  之前在 CBDP 会话上被静默丢弃，是首页“运行中”恒为 0 的原因。
- **2026-10-20 后续修正：** `running` 现在覆盖 `working`、`waiting_user`、
  `queued`、`starting` 四种未终结状态，并与任务列表的状态集合保持一致。
  设备公开消息和状态快照把内部 `waiting_user` 统一编码为 `waiting`；嵌套在
  `data.task` / `data.tasks` 的任务状态也使用同一名称。
- **2026-10-20 后续修正：** 完成与失败结果是持久化通知记录，写入
  `%LOCALAPPDATA%\CodexBridge\messages.json`。Bridge 重启后仍按共享
  `read-state.json` 水位恢复 `completed_unread` / `failed_unread`；同一
  `message_id` 重发只替换原记录，不会重复累加。只有已读 ACK 或线程已读水位
  推进时计数才下降，不会因快照重发、重连重放或周期性刷新归零。
- Companion 审批提交路由修正为 `POST /api/companion/approvals/<request_id>`
  （路径只有 4 段，旧代码按 5 段判断导致恒 404）；请求体带 `response` 时走
  结构化审批，否则走 `decision`。
- CBDP `APPROVAL_RESOLVE` 现已实现：字段位于消息 header，接受
  `approval_ref`（经引用表解析，失败返回 `NOT_FOUND`）或 `approval_id`，
  携带 `response` 时转成结构化 `approval.respond`。
- 审批 `approval_kind` 按协议 §20.4 校正为 `command=0`、`file_change=1`、
  `network_access=2`、`permission=3`、`mcp_tool=4`、`user_input=5`、
  `other=6`、`mcp_elicitation=7`；旧实现的 permission/mcp/user_input 编号是错的。
- `approval.requested/resolved/expired` 现在同时写入 CBDP Event Spine，
  面板等 CBDP 设备能收到 `0x0401/0x0402/0x0403`。
- 新增 `thread.settings.read/update`（CBDP `0x001A/0x001B`），可读写模型、
  effort、cwd 以及 `approval_policy` / `approvals_reviewer` /
  `sandbox_policy` / `personality`。
- **2026-10-20 后续修正：** 独立 app-server 在 `initialize` 时声明
  `capabilities.experimentalApi=true`，否则 `thread/settings/update` 会被
  上游以实验 API 未启用拒绝。`sandbox_policy` 在命令层同时接受协议字符串
  (`read-only` / `workspace-write` / `danger-full-access`) 与内部标签对象
  (`{"type":"readOnly"}` / `{"type":"workspaceWrite"}` /
  `{"type":"dangerFullAccess"}`)；Bridge 分别翻译成 app-server 的
  `SandboxPolicy` 对象和桌面 IPC 的点分字符串，设备不需要感知上游差异。
- **2026-10-20 后续修正：** `thread.settings.update` 与 `turn.start` 使用同一条
  owner discovery 路径：先通过桌面 IPC 找到线程当前 writer，再由该 writer
  执行设置更新。桌面创建的线程不会再因为 Bridge 自己的 app-server 不认识而
  返回 `thread not found`。
- **2026-10-20 后续修正：** app-server 的 `thread/read` 不返回
  `approvalPolicy` / `sandboxPolicy` / `approvalsReviewer`。Bridge 会把成功
  应用的线程策略写入 `%LOCALAPPDATA%\CodexBridge\thread-settings.json`，
  `thread.settings.read` 优先读取这份权威镜像；无法读取上游线程时也仍能返回
  上次生效值。`effort` 同时兼容上游的 `reasoningEffort` 字段名。
- **2026-10-20 附件实现：** 新增 `%LOCALAPPDATA%\CodexBridge\attachments\` 内容
  存储与分片上传事务，落地协议中的 `attachment.add/list/remove`（CBDP
  `0x0060..0x0062`）、`attachment.upload.begin/chunk/end/cancel`
  （`0x0063..0x0066`）和 `content.read`（`0x00C1`）。`turn.start` / `queue.add`
  的 `input_parts` 会把图片附件转成 Codex 的 `input_image`（内联 data URL）输入。
  Companion HTTP 侧新增 `POST /api/companion/threads/<id>/attachments`、
  `POST /api/companion/uploads/<upload_id>/chunk|end|cancel`、
  `GET /api/companion/threads/<id>/attachments`、
  `GET /api/companion/attachments/<id>` 与附件删除接口；手机接入细节见
  [手机端附件接入说明](docs/CodexBridge-mobile-attachment-api.md)。
- **2026-10-20 后续修正：** `threads.list` 现在接受 `project_ref(0x0122)`：Bridge
  经持久引用表解析成 `project_id`，再按该项目的 `roots` 与线程 `cwd` 前缀匹配过滤；
  引用无法解析返回 `NOT_FOUND`，不再落成 `INTERNAL`。`threads.list` 的可选参数因此为
  `limit`、`search`、`cursor`、`project_id`。
- **2026-10-20 后续修正：** Codex 桌面端读取任务也计入全局已读。Bridge 读取
  `~/.codex/.codex-global-state.json` 中 `thread-user-activity-times-v1` 与
  `electron-thread-read-state-v1.unreadByIdentity`：线程有桌面操作记录、且不在桌面
  未读集合中时，Bridge 按同一 `read-state.json` 水位把该线程推进为已读，并向手机
  和在线 ESP 广播 `read_state` 与空闲线程状态；`completed_unread` /
  `failed_unread` 同步减少。桌面未读集合是权威依据：线程不在其中即视为桌面已读；
  但桌面状态文件必须晚于该线程最新终态结果，避免在桌面尚未归类新结果时提前清掉。
- **2026-10-20 后续修正：** 设备在订阅
  `system.thread_summaries(0x50000003)` 的 `summary` scope 时，Bridge 总会在
  回放结束前补齐当前聚合计数；即使回放窗口里已有旧的 `system.codex_state(0x0704)`，
  也会以当前计数覆盖。若最新计数事件在本次回放窗口内，直接补发；若在窗口外但
  global_seq 已大于设备 cursor，直接补发该事件；若窗口外的旧事件 global_seq 不大于
  cursor（例如 cursor = 0 的最新窗口已越过它），Bridge 追加一条内容相同的新事件再
  补发，保证 DELTA 的 `global_seq` 严格大于设备刚报告的 cursor。这样两台 cursor
  不同的 ESP 不会各自停在历史计数上，首页“运行中/完成/失败”也不会永久停在 0。
- **2026-10-20 后续修正：** Bridge 启动时会用 `PRAGMA quick_check` 校验
  `cbdp-events.sqlite3`。校验失败时，先把主库及 WAL/SHM 备份为
  `cbdp-events.sqlite3.corrupt-<时间戳>`，先把已提交 WAL 合并进主库，再用
  `VACUUM INTO` 重建并原子替换。重建结果的行数不得超过恢复前可见行数；如果无法
  无损重建，则逐行读取表页做救援。只有逐行救援也失败时，才保留损坏备份并新建
  空事件库，不让损坏索引拖垮设备订阅。
  替换成功后，`SUBSCRIBE` 的主题回放查询必须使用新索引恢复正常。若运行期间仍
  出现存储错误，`ERROR(INTERNAL)` 必须带
  `reason=event_store_unavailable`，设备可据此重试而不会再收到无信息的 INTERNAL。
- **2026-10-20 后续修正：** CBDP 会话只能收到自己已订阅 `(topic_id, scope)`
  的 `DELTA`。订阅后的新事件由 gateway 统一投递，Hub 的 attach 重放也只对匹配
  订阅调用 `send_event()`；会话层再次校验订阅，避免其他调用点把未订阅事件直接
  推给设备。未订阅事件不得占用设备网络与 lwIP 接收缓冲。
- **2026-10-21 后续修正：** 手机写入桌面活跃线程前必须执行 DesktopIPC owner
  discovery。`CompanionService` 只有显式 `isolated=True` 时才跳过桌面发现；生产
  环境即使注入共享 app-server，也要先查桌面 owner，并通过
  `thread-follower-start-turn` 把手机回复交给桌面 Codex。这样桌面端能实时看到
  手机输入和回复，线程也不会被第二个 app-server writer 竞争截断。
- **2026-10-21 后续修正：** CBDP 结果记录现在同时写入线程 scope 的 turn 终态
  事件：`turn.completed(0x0203)`、`turn.failed(0x0204)`、
  `turn.interrupted(0x0205)`，QoS 为 `critical`。事件携带 thread/turn/message
  id、status、title、summary、body 和 completed_at_ms，供设备按具体任务提醒；
  同一 `turn_id` 只写一次。`system.codex_state(0x0704)` 继续保留，只负责首页
  聚合计数，不再承担“哪条任务完成”的推断职责。
- **2026-10-21 后续修正：** `companion.json` 的配对记录改为失败关闭加载：读取
  异常时拒绝覆盖原文件；JSON/UTF-8 损坏时先隔离为
  `companion.json.corrupt*.bak`，再从 `devices.json` 的 phone 投影恢复可恢复的
  令牌记录。保存使用 fsync + 原子替换并带 Windows 重试。Bridge 重启或升级不得
  因空内存状态覆盖已有手机配对令牌。
- **2026-10-21 后续修正：** 手机队列的活跃判定把 `interrupted` 且无
  `completedAt` 的 turn 视为活跃时，必须限制在 `LOG_ACTIVE_SECONDS`（150 秒）
  陈旧窗口内。超过窗口的 turn 视为已死亡并释放队列，否则一次被第二个 writer
  杀死的 turn 会让待发送消息永久卡住，直到用户删除队列项或重启 Bridge。
- **2026-10-21 后续修正：** 新增 UDP 首页计数旁路
  `HOME_COUNTERS(0x1E)`。Bridge 在 UDP 8764 收到设备 presence 后，每 1~2 秒向该
  设备单播一次 `running_count(0x00BC)` / `completed_unread_count(0x00BD)` /
  `failed_unread_count(0x00BE)`，计数变化时立即补发；只单播，不广播。这是新增
  旁路，`system.codex_state(0x0704)`、summary 订阅和 TCP 任务流程保持不变，设备
  首页因此可以不开 CBDP TCP 会话。

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

### 2.1 端口独占（1.4.7 起）

电脑端绑定 `8788`、`8568`、`8764`、`8569` 时使用独占方式，同一台电脑上只会
有一个 CodexBridge 应答设备发现与手机请求；第二个实例在启动时直接给出中文
错误并退出，不会与前一个实例平分连接。

设备侧的 `8764`、`8765` 不受影响。设备仍然应当在监听建立后主动通告一次
（见 3.4），因为桥接端重启后重新绑定 `8764` 最长需要 15 秒退避，只靠电脑端
广播会让设备在这段时间里处于离线状态。

## 3. UDP 发现协议

### 3.1 请求

CodexBridge 向目标地址的 UDP `8764` 发送以下明文请求，不带换行：

```text
CODEXBRIDGE_DISCOVER
```

默认目标是广播地址 `255.255.255.255`。发现超时由 CodexBridge 发起方控制，
`discover()` 的默认单轮超时为 `3.0` 秒；常驻程序的发现循环把单轮压到 `2.5` 秒，已覆盖设备约 `1.0` 秒的 Wi-Fi 监听间隔，平时每 `5` 秒重扫一轮，所有已配对设备都在线时放宽到 `15` 秒。

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

### 3.4 设备主动通告（presence）

设备刚打开 TCP 监听时，可以不等请求，自己向 UDP `8764` 广播一次与 3.2 完全相同的
JSON 应答。CodexBridge 常驻监听该端口，收到后按 3.2 的规则解析，解析成功就立刻拨号，
并和广播发现共用同一条 `sync` 路径。

这不是第二套协议：payload、字段、校验和忽略规则与被动应答一字不差，设备不发通告也能
正常工作，旧固件不受任何影响。

设计原因：处于 Wi-Fi 省电模式的站点只在固定监听间隔醒来，主机这一侧的广播窗口只要
比这个间隔短，就完全可能收不到一个正在监听的设备的回复。实测开机后进入 Codex 页要
等 12 到 21 秒才连上，用户看到的是“未连接”。设备主动通告之后，同一个场景实测
0.44 秒完成连接。

要求与建议：

- 通告目标端口固定为 `8764`，使用子网广播地址，`255.255.255.255` 会被部分主机过滤。
- 通告必须有节奏上限，设备不能把它变成高频心跳。建议监听器建立后的前三次间隔
  `3` 秒，之后放宽到 `15` 秒或更长。
- CodexBridge 按 `device_id + host + port` 在 `5` 秒窗口内去重，重复通告不会重复拨号。
- 无法发送通告的设备（例如未实现广播）不受影响，仍然依靠 3.1 的主动发现。

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
| 心跳间隔 | `5` 秒 | CodexBridge 重复发送 `bridge_ready`（可配置，仅作存活探测） |
| 连接超时 | `15` 秒 | TCP 建连与等待握手应答的超时 |
| 直接拨号超时 | `3` 秒 | 只用于按记住的地址直接连接，该地址可能已失效；失败后 `10` 秒内不对同一设备重复拨号 |
| 通告去重窗口 | `5` 秒 | 同一 `device_id + host + port` 在窗口内只拨号一次 |
| 令牌最短长度 | `16` 字符 | 少于 16 字符的令牌会被拒绝 |

设备可在 `45` 秒未收到 CodexBridge 任何消息时判定连接失效并重新等待连接。
失效期间设备仍在按 3.4 通告自己，所以电脑端重新启动后不需要等满这个
超时就能重新接上。
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

### 5.4 硬件主动注册与批准

硬件通过 UDP `8764` 应答发现请求，或主动通知身份。应答在原有字段上增加注册
字段：

```json
{
  "device": "dotii",
  "device_id": "dotii-a1",
  "name": "Desk",
  "port": 8765,
  "capabilities": ["status", "text", "tasks", "read_ack", "queue"],
  "registration_state": "pending",
  "registration_nonce": "r_7f5c0123456789abcdef"
}
```

规则：

- `registration_state` 缺省时按 `approved` 处理，兼容没有注册字段的旧固件。
- `pending` 必须携带 16 到 64 字符的 `registration_nonce`。
- nonce 绑定 `device_id + 来源地址 + 发现时间`，有效期 10 分钟；过期、地址变化
  或重复使用都必须失败。
- `pending` 设备只出现在待批准列表，不建立 TCP 业务会话，也不接收消息。
- 已批准设备再次被发现时只刷新地址和 `last_seen`，状态保持 `approved`。

批准后 CodexBridge 先建立一次性注册连接：

```json
{"type":"register_offer","registration_nonce":"r_7f5c0123456789abcdef","token":"<新设备令牌>","bridge":"CodexBridge"}
```

设备持久化令牌并返回：

```json
{"type":"register_ok","device_id":"dotii-a1","accepted":true}
```

设备随后关闭该连接；CodexBridge 将状态置为 `approved`，再建立正常
`bridge_auth` 会话。注册失败时设备保留 `pending` 和错误原因，管理页可重新批准。
拒绝返回并保持 `rejected`；撤销会清空令牌并保持 `revoked`，直到设备以新的
nonce 重新发现。

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
control
queue
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
{"type":"codex_state","running":1,"completed_unread":2,"failed_unread":0,"done":2,"failed":0,"request_id":7}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | 固定为 `codex_state` |
| `running` | integer | 是 | 当前处于进行中状态的任务数 |
| `completed_unread` | integer | 是 | 全局未读的完成结果数 |
| `failed_unread` | integer | 是 | 全局未读的失败结果数 |
| `done` | integer | 是 | 已完成的最近结果数 |
| `failed` | integer | 是 | 失败的结果数 |
| `request_id` | integer | 否 | 仅当请求携带时回显 |

`done` / `failed` 是兼容别名，分别与 `completed_unread` / `failed_unread`
相同。计数由 CodexBridge 从统一消息存储实时聚合，不需要设备维护第二套统计。
终态结果按稳定 `message_id` 去重并持久化，进程重启或设备重连不会重复递增；
计数只会在已读水位推进后下降。
设备应自行控制拉取频率（例如前台 15 秒、息屏 60 秒），避免频繁唤醒射频。
设备上线时 CodexBridge 额外主动下发一次快照，避免刚重连时界面空白。
未授权事件返回 `capability_not_enabled`。

同一个请求还带回应前一条 `codex_status_snapshot`，描述 Codex 此刻正在做什么：

```json
{
  "type": "codex_status_snapshot",
  "thread_id": "01a1...",
  "status": "working",
  "current_action": "Running tests",
  "turn_id": "t-1",
  "plan_completed": 1,
  "plan_total": 4,
  "created_at": 1770000000,
  "request_id": 7
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `thread_id` | string | 当前线程，最长 64 字节 |
| `status` | string | `working`、`waiting`、`queued`、`starting` 之一 |
| `current_action` | string | 当前动作；长度按第 7.4 节的设备正文预算裁剪 |
| `turn_id` | string | 当前轮次，最长 40 字节；设备可用它判断"有没有新进展" |
| `plan_completed` / `plan_total` | integer | 计划进度，缺省为 `0` |
| `created_at` | integer | 该状态记录的 Unix 秒 |
| `request_id` | 与请求同 | 仅当请求携带时回显 |

这条记录只在存在进行中状态时发送；没有进行中任务时只回 `codex_state`。
设备应在界面卡住没有新消息时用它做低频自检：正文推送按内容指纹去重，
同一句进展不会重发，因此设备必须能够主动询问，否则界面会一直停在旧状态。
设备侧的处理规则与 `kind == "status"` 的 `message` 完全一致，不需要新代码分支。
### 9.7.1 进行中记录的有效期

running 与 codex_status_snapshot 只统计电脑端仍在刷新的记录。采集器每约 5 秒重新发布一次当前状态，CodexBridge 为每条 status / task 记录盖上到达时间；超过 90 秒没有再次到达的记录视为过期，既不计入 running，也不会作为快照答案返回。
这条规则避免屏幕永远停在等待电脑接收：Codex 线程异常结束、或采集器停止工作时，存储里会留下几行仍然是 working 的旧记录，没有有效期规则的话设备会一直看到 running 大于 0 却等不到任何进展。设备端对应的保护是连续多次轮询得到 running 等于 0 且超过 60 秒没有新消息时，主动结束这一轮并提示重新录音。

### 9.7.2 每台设备只跟随一个会话

桌面上可能同时有多个 Codex 会话在运行。设备一旦选定会话（codex_thread_select，或语音指令由 CodexBridge 新建线程），该设备的 status / task / result 记录就只来自那一个 thread_id：其他会话的进展不会推送给它，codex_status_snapshot 也不会回答别的会话。codex_state 的聚合计数仍是全局的，首页宠物需要知道电脑上一共有多少任务在跑。
尚未选择过会话的客户端（手机伴侣、旧固件）保持原有行为，收到全局推送，但会跳过采集器标记为单会话专用的记录。设备可以在 codex_state_request 里带上 thread_id 主动声明自己在看哪个会话；codex_selected 与 task_queued 也会带上 thread_id，设备据此更新。

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

### 11.4 跨客户端已读（手机与设备互相同步）

手机伴侣与设备会话共用同一个已读存储，因此一次"打开"会让所有客户端同时撤回提醒：

- 设备上报 `read_ack`：其他在线设备收到 `read_state`，手机侧对应线程收到
  `thread_status` 的 `idle` 事件，完成提醒随之撤回。
- 手机读取线程（`POST /api/companion/threads/<id>/read` 或打开线程详情）：
  该线程下所有设备可见的消息按 `message_id` 补记已读，在线设备立刻收到
  `read_state`，重连的设备也不再重放这些消息。

进行中的状态（`kind` 为 `status` 或 `task`，`status` 为 `working`、
`waiting`、`queued`、`starting`）不属于"已消费的通知"：即使手机已经读过，
它仍然会重放给新连接的设备，也仍然能被 `codex_state_request` 拉到。否则设备会
因为别人读过而再也看不到"任务还在跑"。

已读广播只发给声明 `read_ack` 能力的设备；上报者自己不会收到自己造成的广播。

## 11A. 待发送队列

队列属于线程，不属于某台设备，由 CodexBridge 统一持有并持久化到
`%LOCALAPPDATA%\CodexBridge\queues.json`。手机、ESP 和管理页面看到的是同一份 FIFO
队列；线程空闲时 Bridge 立即把队首转成 `turn.start`，线程进行中时保持 `pending`，
turn 结束后再派发下一项。手机不再自行决定 `steer` 或 `start`。

### 11A.1 能力与方法

队列命令要求 `queue` 能力。手机默认启用，硬件必须在 `bridge_auth_ok.capabilities`
中声明后才启用；未声明 `queue` 的旧客户端继续使用 `turn.start` / `mode=steer`。

| 方法 | 必需参数 | 可选参数 |
| --- | --- | --- |
| `queue.list` | `thread_id` | |
| `queue.enqueue` | `thread_id`, `text` | `model`, `effort` |
| `queue.edit` | `queue_id`, `text`, `expected_revision` | `model`, `effort` |
| `queue.delete` | `queue_id`, `expected_revision` | |
| `queue.steer` | `queue_id`, `expected_revision` | |

HTTP 手机使用现有 `POST /api/companion/commands` 信封，TCP 设备使用现有
`{"type":"command",...}` 信封。大结果继续使用 `command_chunk` 分片。

旧式的 `POST /api/companion/threads/<thread_id>/messages` 保持兼容：已协商
`queue` 的手机发送 `mode=start`（或省略 mode）时映射为 `queue.enqueue`，返回
队列项；`mode=steer` 仍是立即引导。未声明 `queue` 的旧手机继续直接调用
`turn.start`。

### 11A.2 队列项

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

### 11A.3 队列事件

手机 SSE 和设备的 `device_event` 共用以下事件：

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
`queue_snapshot`；设备在 `bridge_auth` 完成后的 attach replay 中收到同一快照。

### 11A.4 上限与恢复

- 每个线程最多 `100` 项，单个未完成项最多 `10000` 个字符。
- 每次新增、编辑、删除和状态变化都原子落盘；Bridge 重启后重新载入，并在线程空闲时继续派发。
- 队列使用 `request_id` 幂等缓存和线程级串行派发，避免重复 `turn.start`。

## 12. 错误与边界

### 12.1 协议错误


主机端的每一条错误都同时带机器码和可读句子。设备只渲染 `text`，
不需要为错误码维护自己的映射表，新增错误码时固件无需改动。

```json
{"type":"error","code":"busy_retry","text":"电脑端还在处理上一条请求，请稍后再试"}
```

| `code` | 含义 |
| --- | --- |
| `capability_not_enabled` | 设备发起了未生效能力对应的事件 |
| `codex_catalog_unavailable` | 电脑端没有可用的 Codex，请确认 Codex 已打开 |
| `codex_catalog_failed` | 电脑端读取 Codex 列表失败，请重试 |
| `codex_unavailable` | 电脑端的 Codex 暂时不可用 |
| `busy_retry` | 电脑端还在处理上一条请求（延后执行队列已满） |
| `invalid_request_id` | 请求编号无效 |
| `invalid_event_cursor` | 订阅位置无效 |
| `invalid_command_chunk` | 指令数据损坏 |
| `invalid_request` | 请求格式不正确 |

`text` 是给人看的说明，语言不保证稳定，客户端不得据此分支；分支只看 `code`。

### 12.2 必须忽略的输入

- 未知消息类型。
- 未知能力名称。
- 非 JSON 行、JSON 数组或标量。
- 超过 768 字节的单行。
- 缺少必要字段的消息。
- 设备未授权能力对应的事件。

忽略单条消息不等同于断开连接。鉴权失败、身份不一致或 TCP 关闭会结束会话；
握手完成后的会话不会因为暂时收不到设备消息而被 PC 侧超时断开。

## 13. 统一设备与管理接口

`DeviceRegistry` 是手机和硬件设备身份、注册状态与能力的唯一权威来源。
手机仍通过配对码登记，硬件通过发现与后续的注册批准流程登记；两者进入同一份
`devices.json`（版本 `2`），管理页和内部事件只使用同一份公共设备视图。

### 13.1 统一设备记录与公开视图

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

### 13.2 状态与设备操作

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

### 13.3 语音与常规设置

### 13.4 语音与常规设置

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

### 13.5 Dotii 管理中心快照接口

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
- 硬件明文令牌和手机令牌摘要写入私有注册表，但不得出现在管理 API、事件或日志中。
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
- CBDP 设备的业务事件（`message`、`read_state` 等）一律从 Event Spine
  出站为完整 `DELTA`，携带 `event_id`、`global_seq`、`topic_id`、
  `topic_seq`、`revision` 和非零传输 `seq`。旧 JSON 行协议的兼容翻译只
  适用于旧设备，不得为 CBDP 设备合成缺少这些字段的 `DELTA`。
- 设备注册表同时服务旧 JSON 与 CBDP 设备，`supported`/`enabled` 使用
  CBDP v1 的 9.2 能力名；旧固件的 `quota` 等价于 CBDP 位 `42`
  （`account`）。`protocol = cbdp` 的设备默认启用其声明的全部能力，
  不再用旧型号默认集合裁剪。
- 收到对端主动发来的 `ERROR` 时不得回发新的 `ERROR`，避免错误回环。

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
```

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

- `POST /api/companion/pair` 请求体为 `{"code":"123456","name":"Pixel","client_id":"<安装标识>"}`。`client_id` 是可选字段，手机端应使用安装级稳定标识；旧客户端不传时电脑端保持兼容。配对码在电脑端管理页面生成，`10` 分钟有效，最多尝试 `5` 次，成功使用一次后立即失效。
- 配对成功后返回 `{"ok":true,"phone_id":"phone-xxxxxxxxxxxx","token":"cbp_...","bridge":{...}}`。同一 `client_id` 再次配对时复用原 `phone_id`，轮换令牌并更新名称，因此不会生成重复设备。令牌只在这次响应中返回，电脑端只保存 SHA-256 摘要。
- 轮换令牌不会让已经拿到旧令牌的手机被永久拒绝：同一 `client_id` 重新配对后，电脑端仍会在 `1` 小时内接受该手机此前最多 `4` 个旧令牌，一旦手机用最新令牌成功访问，这些旧令牌立即失效。这样即使配对响应因电脑重启或网络中断丢失，手机也能继续使用，不必让用户手动重新配对。
- 后续请求使用 `Authorization: Bearer <token>`；SSE 允许使用 `?token=`。未配对返回 `401 unauthorized`，电脑端关闭手机连接时返回 `403 companion_disabled`，配对码错误返回 `403 pairing_failed`。
- 网络临时断开或电脑重启导致的 `401` 不应清除手机端已保存的地址、令牌和 `client_id`；客户端恢复到同一地址后继续使用原令牌，只有用户在设置中主动解除配对或电脑端移除设备时才需要重新配对。
- 鉴权不落盘：每个带令牌的请求只在该配对文件的修改时间或大小变化时才重新读取并重新投影设备列表，其余情况直接命中内存状态。同一进程内的重复请求不产生额外磁盘写入，设备端按 `2` 秒级别轮询不会造成电脑端持续占用 CPU。
- 电脑端允许 HTTP/1.1 长连接：同一客户端可复用一条 TCP 连接连续发起多个请求，空闲 `30` 秒后由服务端关闭。手机端应复用连接而不是每次轮询新建连接；`/api/companion/events`（SSE）与 `/cbdp/v1/ws`（WebSocket）仍按各自的流式语义保持独立连接。

### 17.2 接口清单

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
ESP/设备协议；设备只使用后文的通用命令协议。

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

- `GET /api/admin/companion` 返回开关、局域网地址、当前端口、端口健康状态与已配对手机；手机列表由统一设备注册表派生。
- `GET /api/admin/devices` 同时返回手机和硬件；公共字段包含 `kind`、`transport`、`registration_state`、`registration_method`、`online`、`last_address`、`has_token`、`queued_count`，不包含任何令牌或令牌摘要。
- `POST /api/admin/companion` 支持 `{"enabled":true}`、`{"show_intermediate_events":true}`、`{"action":"new_code"}`、`{"action":"remove_phone","phone_id":"phone-xxxxxxxxxxxx"}`、`{"action":"set_port","kind":"companion","port":8568}` 和 `{"action":"set_port","kind":"discovery","port":8569}`。
- `GET /api/admin/codex-status` 返回 `codex.enabled`、采集状态与最后快照时间。
- `POST /api/admin/codex-status` 支持 `{"enabled":false}` 与 `{"enabled":true}`，语义是“空闲采集节奏”而非功能开关。任一时间点只要存在在线设备会话或手机事件流订阅，Bridge 就按原协议的 5 秒节奏采集并推送；`enabled:false` 只把“无任何客户端连接”时的采集降为低速兜底（数据仍持续刷新，不会中断），有客户端出现时立即恢复全速。`enabled:true` 表示空闲时也保持全速。设备连接、心跳、语音、任务列表与 CBDP 线协议行为不因该设置而减少。
- 本机管理接口仍只允许回环地址访问；手机伴侣接口不开放本机管理能力。

### 17.6 读取缓存与新鲜度

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

因此设备端不需要为了“怕看到旧数据”而缩短轮询间隔：状态变化会在下一次校准
（运行时 2 秒、空闲时 15 秒）内被推送，未变化时缓存读取是常数时间。
