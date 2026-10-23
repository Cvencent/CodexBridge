# PC 侧根因与修复：Codex 采集开关与独立 app-server 空转（2026-10-23）

- 日期：2026-10-23
- 受影响版本：CodexBridge 2.1.33 及更早
- 现象：所有设备都关闭后，CodexBridge 一启动整机鼠标仍卡顿；关闭 CodexBridge 立刻恢复
- 结论：**与设备无关，是 PC 侧 Codex 采集链路自身的问题。**
  设备团队无需修改任何代码。

---

## 1. 现象与排除过程

用户报告「鼠标一移动就卡」，并进一步确认：把所有设备都关掉、没有任何设备
连上 CodexBridge 时，卡顿**依旧存在**；关闭 CodexBridge 后立即恢复正常。

| 条件 | 结果 |
| --- | --- |
| 全部设备关闭，TCP 8568 `ESTABLISHED` | 0 |
| CodexBridge 运行 | 仍然卡顿 |
| CodexBridge 关闭 | 卡顿消失 |

这是本次修复的决定性证据：卡顿的触发条件是 **CodexBridge 进程本身**，与
设备数量、局域网连接、TCP 会话、UDP 发现都无关。设备端不需要做任何改动。

## 2. 实测：卡顿来自 Codex 采集链路，不是设备链路

在全部设备离线的情况下，按进程统计 5 秒 CPU 增量（单核百分比）：

| 进程 | 说明 | CPU |
| --- | --- | --- |
| `codex app-server --listen stdio`（CodexBridge 自己启动的第二套） | 独立后台 watcher，持续读线程库 | **13.1%** |
| `CodexBridge.exe` | 采集轮询与 IPC | 2.2% |
| Codex 桌面自身进程 | 空闲 | 0.0% |

也就是说，只要 CodexBridge 在运行，它就**额外启动了第二个 Codex app-server
进程**，并让这个进程持续读取本机约 1.31 GB 的
`thread_history_1.sqlite`。设备一台都没连，这份负载照样存在。

进一步用真实 `AppServerClient` 做端到端复现：仅「启动 + 每 5 秒采集一次」
的采集循环，子进程在 12 秒窗口内就消耗了 0.64 秒 CPU（**5.3% 单核**），
而窗口内只完成了 3 次快照。

### 2.1 为什么不能复用桌面自己的 app-server

CodexBridge 本来希望复用桌面 Codex 已经运行的 app-server，代码里也写了
`prefer_shared=True`，但这条路径在本机**恒不可用**：

- Windows 版 Python 没有 `socket.AF_UNIX`（实测 `hasattr` 为 `False`）；
- 桌面 app-server 的控制面是 Unix domain socket
  `%USERPROFILE%\.codex\app-server-control\app-server-control.sock`；
- `codex app-server daemon start` 在 Windows 上会「看起来启动成功」，但它实际
  监听的是 `unix://`；随后的 `proxy` 连接会**挂起无响应**，`daemon version`
  也报 `os error 10061`。

所以 `_desktop_socket()` 必然返回 `None`，CodexBridge 只能退回
`codex app-server --listen stdio://`，即**再起一套**。这是当前架构下无法回避
的既有缺陷，本次通过开关让用户可以直接关掉这条链路。

## 3. 读放大：采集频率与刷新语义

在这条链路之上，还有几处把读放大成周期性突发的地方：

- 采集器默认每 **5 秒**一个快照（`run_codex_collector(interval=5.0)`）。
- 伴侣后台校准「运行中 2 秒、空闲 15 秒」一次
  （`POLL_ACTIVE_SECONDS` / `POLL_IDLE_SECONDS`）。
- 该校准调用 `list_threads(limit=80, refresh=True)`，**强制绕过只读缓存**，
  每个 tick 都打真实请求。
- `refresh_thread()` 对 `working` / `active` 线程每轮**强制重读**
  `thread/read`（`includeTurns=false`）、`thread/turns/list`、
  `thread/items/list`，绕过 10 秒读缓存。

叠加约 1.31 GB 的 `thread_history_1.sqlite`，就形成了「CPU 不高但整机
指针抖动」的软/硬缺页行为。

## 4. 关键实测：进程不是问题，“无人时的轮询”才是

在动手改之前先做了隔离测量，结论直接决定了修复方向：

| 状态 | 独立 app-server 的 CPU |
| --- | --- |
| 刚启动、空闲 | 0.0–0.3% 单核 |
| 被用过一次后完全空闲（连续 6×5 秒窗口） | **0.00%** 单核 |
| CodexBridge 按 5 秒节奏轮询它 | **5–13%** 单核 |

也就是说，**app-server 进程本身几乎不耗 CPU**；让它变成 13% 的是
「没有任何客户端在用，却仍按最快节奏不停轮询它」。因此正确的修复是
**按需轮询（demand-driven polling）**，而不是把功能关掉。

## 5. 修复内容（仅 PC 侧，功能一个都没有减少）

### 5.1 空闲降频 + 客户端出现即恢复全速

- 采集循环新增 `interval_for`：有设备或手机连接时保持原有的 **5 秒**节奏；
  无人连接且用户未要求常开时，降为 **60 秒**的兜底刷新。**数据仍在持续采集**，
  因此一台设备在周期中途出现也不会读到空列表。
- 慢速休眠期间会探测需求，**第一个连上的客户端会立刻打断休眠并马上刷新**，
  不必等满一个空闲周期。
- 伴侣后台校准同样改为按需：没人连接时不发起任何 Codex 请求。

“有人在用”的判定 = 存在在线设备会话 **或** 存在手机事件流订阅
（`hub.online_device_ids()` / `EventStream.subscriber_count()`）。任一条件成立即
恢复全速，对任何真正在线的客户端，行为与修改前完全一致。

### 5.2 「Codex 采集」开关（管理页 → 偏好 → 启动与运行）

- `POST /api/admin/codex-status` 接受 `{"enabled": false}` / `{"enabled": true}`。
- 语义是**空闲节奏**，不是功能开关：
  - **关闭（默认）**：空闲时降频（占用接近 0），有客户端连上立刻恢复全速；
  - **开启**：空闲时也保持全速采集。
- 状态持久化在 `%LOCALAPPDATA%\CodexBridge\codex-collection.json`，重启后保持。
- **任何情况下都不会**：关闭 app-server、拒绝设备/手机的按需读取、停止消息库
  更新、停止任务推送。设备连接、心跳、语音、任务列表、CBDP 线协议全部照常。

### 5.3 明确不做的事

早期一版方案曾让开关“关闭时停掉采集并拒绝按需读取”，那会真的砍掉功能，
已废弃。现在的实现里，开关只影响**无客户端连接时的节奏**。

## 6. 验证

- `python -m unittest discover -s tests -v`：**845 tests OK**。
- `git diff --check`：通过。
- 真实 AppServerClient 实测（决定性）：
  - 无人连接、降频开启：`0.00% / 0.00% / 0.31% / 0.00%` 单核；
  - 客户端连上后：立刻恢复，10 秒内推送 12 条记录，CPU `2.19%` 单核。
- 真实 HTTP 集成（后台采集默认关闭状态下）：默认值、设备能力配置、语音设置、
  通用设置、手机伴侣状态、设备列表、**设备按需读取 Codex 目录**、切回全速常开
  —— 全部通过；唯一未通过项 `/api/admin/queues` 经核对在本次修改前后都不存在
  （HEAD 与工作区均无），前端本就有降级隐藏逻辑，与本次改动无关。

## 7. 设备端结论

**设备团队无需修改任何代码。** 卡顿在设备全部离线时仍可复现，根因完全位于
CodexBridge 的 PC 侧 Codex 采集链路。新增的管理接口属于本机回环管理面，
不改变 CBDP 线协议的端口、握手、消息类型与字段。
