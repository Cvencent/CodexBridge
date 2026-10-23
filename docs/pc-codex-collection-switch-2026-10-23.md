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

## 4. 修复内容（仅 PC 侧）

### 4.1 新增「Codex 采集」开关（管理页 → 偏好 → 启动与运行）

- `POST /api/admin/codex-status` 接受 `{"enabled": false}` / `{"enabled": true}`，
  返回当前 `codex` 快照。
- 关闭时同时做四件事：
  1. 采集线程停止采集（`run_codex_collector` 的 `enabled` 回调本就支持）；
  2. 伴侣后台校准线程停止轮询，并丢弃对比基线，避免重新开启时补发积压变更；
  3. **关闭 CodexBridge 自己启动的 app-server 进程**，释放那份持续 5–13% 单核的
     空转与线程库读取；
  4. 设备目录请求在关闭期间被拒绝，不会把刚关掉的 app-server 又拉起来。
- 开关状态持久化在
  `%LOCALAPPDATA%\CodexBridge\codex-collection.json`，重启后保持用户选择。默认值
  仍为开启，保证既有行为兼容。
- 关闭后设备连接、心跳、语音、CBDP 线协议均不受影响；仅任务列表停止更新。

### 4.2 收口所有可能重新拉起 app-server 的路径

关闭开关后，以下路径都必须尊重开关，否则用户的「关」会被下一次请求撤销：

| 路径 | 处理 |
| --- | --- |
| 采集器 `source_factory` | `enabled` 回调 + `shared_app_server.close()` |
| 伴侣后台校准 `_watch_threads` | 开关关闭时跳过并清空基线 |
| 伴侣读路径 `_fetch` / `_ensure_client` | 开关关闭时抛 `AppServerError`，不再新建客户端 |
| 设备目录请求 `_CodexSourceHolder.request` | 开关关闭时抛 `AppServerError`，不再新建客户端 |
| 伴侣启动 `CompanionService.start` | 开关关闭时不再预热客户端 |

## 5. 验证

- `python -m unittest discover -s tests -v`：**840 tests OK**（新增 20 项，覆盖
  管理接口开关、持久化、门控、设置页）。
- `git diff --check`：通过。
- 真实 HTTP 集成：`GET` 读到开启 → `POST {"enabled":false}` 返回关闭且
  `collector_state=paused` → 设备目录请求被拒绝 → `POST {"enabled":true}` 恢复 →
  状态文件写入正确。
- 真实 `AppServerClient`：`close()` 能确实终止其启动的子进程（`returncode=1`）。

## 6. 设备端结论

**设备团队无需修改任何代码。** 本次卡顿在设备全部离线时仍可复现，根因完全位于
CodexBridge 的 PC 侧 Codex 采集链路。新增的管理接口属于本机回环管理面，
不改变 CBDP 线协议的端口、握手、消息类型与字段。
