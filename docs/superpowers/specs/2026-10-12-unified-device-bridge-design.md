# CodexBridge 统一设备桥设计

日期：2026-10-12
状态：设计已确认，待评审

## 1. 背景

现在有两个 PC 端程序在做同一件事，各自带一个托盘图标：

- Dotii-Display：DotiiManagementCenter.exe（托盘宿主）拉起 DotiiBridge.exe（后台）。设备是 ESP32-S3 圆屏，由设备主动 HTTP 轮询管理中心的 schema_version = 1 快照；Codex 采集在管理中心里。
- FoloOS AI Passport：CodexBridge.exe（托盘）。设备是 AI Passport，设备监听 TCP 8765 与 UDP 8764 发现，PC 主动连接后走换行分隔 JSON；Codex 采集在 bridge 里。

## 2. 目标

1. 一个 EXE、一个托盘：CodexBridge.exe 成为唯一常驻的 PC 端程序，管理中心不再有自己的托盘。
2. 一套通用设备协议：以后任何新设备实现这套协议就能接进来，拿到 Codex 运行状态、任务状态、任务结果、通知和已读同步。
3. 消息互通：一条 Codex 消息广播给所有在线设备；任何一台设备标记已读，其他设备同步。
4. Dotii 改说这套协议（Codex 部分）；Bambu、自定义页、显示设置仍走管理中心。
5. 尽量多改 Dotii，少改 AI Passport。

## 3. 非目标

- 不改 Dotii 的 Bambu、自定义页、显示设置、BLE 配网与烧录链路。
- 不做云端中转，所有连接都在同一局域网。
- 不重写 AI Passport 的语音、审批和单词熊功能。
- 不为了统一而删除旧的 v1 协议：已刷机的 AI Passport 必须零改动继续可用。

## 4. 仓库与目录

- 新仓库 CodexBridge（本仓库）：PC 端全部内容。托盘、通用协议宿主、Codex 采集、统一消息与已读、管理中心后台（网页、Bambu、自定义页、配网、烧录）、打包与发布。
- Dotii-Display：回归设备项目，保留固件、硬件资料和文档。bridge/ 下的管理中心代码迁移到本仓库；迁移完成后该仓库的便携包发布入口改为指向 CodexBridge 的 Release。
- FoloOS AI Passport 社区仓库：只做设备固件的最小改动（见第 9 节）。

迁移带来的取舍：Dotii-Display 不再自己发布 DotiiManagementCenter 便携包，它的 README 与下载入口改指 CodexBridge。这是有意的，避免两套 PC 端程序长期并存。

## 5. 角色与进程

    CodexBridge.exe（唯一托盘、唯一常驻）
    +-- Codex 采集           沿用 FoloOS windows_codex_backend.py 的能力
    +-- 通用协议宿主         TCP 8765 / UDP 8764，多设备注册表，每设备一条会话
    +-- 统一消息中心         任务与通知归一成 message，含 read_by
    +-- 管理中心子进程       无托盘，网页 8787，Bambu / 自定义页 / 显示设置 / 配网 / 烧录
            |
            +-- Dotii 设备：HTTP 轮询 -> Bambu、自定义页、显示设置、模块开关
            +-- Dotii 设备：TCP -> Codex 状态、任务、结果、已读上报

Dotii 固件是双数据源：Codex 从 CodexBridge 走，其余继续从管理中心走。两条链路独立，互不阻塞。

配对与配网的分工：设备连 Wi-Fi 仍由管理中心的 BLE 配网页完成；配网时管理中心通过本机回环接口向 CodexBridge 取该设备的配对令牌，连同 Wi-Fi 信息一起写进设备。

## 6. 通用设备协议 v2

传输沿用现有设计：TCP 8765、换行分隔 JSON、UTF-8；UDP 8764 做发现。选它是因为设备数量少、局域网内够用，且 AI Passport 固件已经实现了这一套。

### 6.1 发现

- 请求：CODEXBRIDGE_DISCOVER_V2；同时接受旧的 FOLOOS_DISCOVER_V1。
- 应答：{"device":"dotii","device_id":"<稳定ID>","port":8765,"protocol":2,"capabilities":[...]}。
- 旧设备应答 {"device":"FoloOS-AI-Passport","port":8765}（无 protocol 字段）时按 v1 处理。

### 6.2 握手

1. 设备到 PC：{"type":"hello","protocol":2,"device_id":"...","model":"...","name":"...","firmware":"...","capabilities":[...]}
2. PC 到设备：{"type":"hello_ok","protocol":2,"bridge":"CodexBridge","version":"<x.y.z>"}
3. 设备到 PC：{"type":"bridge_auth","token":"<令牌>"}
4. PC 到设备：{"type":"bridge_auth_ok","device_id":"<分配或确认的ID>"}

未配对设备接受首个合法令牌并持久保存（TOFU），之后必须严格匹配。这条沿用现有 CodexBridge 的行为，不新增配对码流程。

### 6.3 心跳与断线

- PC 每 5 秒发 bridge_ready。
- 设备 15 秒收不到任何消息即判定掉线，重连后重新走握手。
- PC 侧每台设备有独立会话与重连退避，一台设备的抖动不影响其他设备。

### 6.4 消息

PC 到设备（星号为 v2 新增）：

| 类型 | 用途 |
| --- | --- |
| task_status | 任务状态变化（沿用） |
| done / text / error | 结果、正文、错误（沿用） |
| approval | 等待用户审批（沿用，仅 approval 能力） |
| codex_catalog_begin/item/end、codex_selected | 任务目录与选择（沿用） |
| speech_start/chunk/end | 语音播放（沿用，仅 audio 能力） |
| message | 归一后的消息：message_id、thread_id、kind、status、title、body、seq、total、created_at |
| read_state | 已读广播：message_id、read_by[]、read_at |

设备到 PC（星号为 v2 新增）：

| 类型 | 用途 |
| --- | --- |
| voice_command / approval_decision / cancel_task | 沿用 |
| record_start / audio_chunk / record_stop / record_cancel | 沿用 |
| codex_catalog_request / codex_project_select / codex_thread_select / codex_thread_new | 沿用 |
| read_ack | 上报某条消息已读：message_id |

### 6.5 能力协商

- 设备在 hello 里声明 capabilities，例如 Dotii 用 ["status","text","read_ack"]，AI Passport 用 ["status","text","audio","approval"]。
- PC 只推设备声明支持的类型；message 与 read_state 是所有设备都有的基础能力。
- 未知消息类型必须被接收方忽略，不能中断会话。

### 6.6 长文本

- 沿用分片（每片 480 字节），message 带 seq 与 total。
- 设备侧按消息拼接，上限 4 KB；超长截断并保留尾部提示。

### 6.7 版本兼容

- protocol = 2 走 v2；无 protocol 字段按 v1 处理，v1 的全部消息类型与语义保持不变。
- 新字段一律可选，旧固件忽略即可；不兼容变更必须抬 protocol 版本号。

## 7. 统一消息与已读

- 归一：一个 Codex 任务或一条通知生成一条 message。message_id 由 thread_id 加任务内递增序号组成，跨设备稳定。
- 广播：新消息推给所有在线设备，按能力过滤。
- 已读：设备发 read_ack 后写入该消息的 read_by，并向其他设备推 read_state。
- 补发：设备重连后，PC 先推送未读消息与最新 read_state，避免重连后看不到别人已经读过的状态。
- 不变量：已读是通知级的，按 message_id 记录；不依赖任何设备本地的页面计数或缓存。

### 7.1 看过的定义

- Dotii：任务详情页停留满 2 秒即上报已读（阈值做成常量，便于真机调）。不加额外按键，避免和圆屏的翻页手势冲突。
- AI Passport：用户在该条消息上按 OK 离开时上报已读。
- 备选（仅记录，不实现）：Dotii 改为按 Button A 才算已读。

## 8. Dotii 侧改造

固件（主要工作量，ESP-IDF / C）：

| 文件 | 改动 |
| --- | --- |
| main/codex_bridge_client.c/.h（新增） | UDP 发现应答、TCP 服务端、hello 与 auth、行 JSON 解析、心跳、断线重连、read_ack 上报 |
| main/connectivity.c | Codex 字段改由新客户端填充；HTTP 轮询继续填 Bambu、自定义页、显示设置 |
| main/state_ui.c | 详情页停留 2 秒触发已读上报；完成/失败表情由 Codex 状态本地推导 |
| main/device_config.c、main/ble_bridge.c | 新增 codex_bridge_token；管理中心的 URL 与令牌保持不动 |
| sdkconfig.defaults、分区 | 复核新客户端的栈、socket 缓冲与 PSRAM 预算 |

管理中心（原 dotii/bridge/）：

- 删除 Codex 采集：codex_app_server.py、Codex 模块开关、快照里的 Codex 段、管理页 Codex 标签。
- 快照保留 Bambu、自定义页、显示设置、模块开关、Dotii 表情（表情改为设备本地推导后，这一段可以只留配置）。
- BLE 配网增加一步：通过 CodexBridge 回环接口取该设备的配对令牌，和 Wi-Fi 信息一起下发。
- 保留烧录、日志、运行目录与现有配置兼容性。

## 9. AI Passport 侧改造（最小）

- 固件：用户按 OK 离开某条消息时发送 {"type":"read_ack","message_id":"..."}。
- 固件：确认未知消息类型被忽略，保证 v2 新消息不会打断 v1 逻辑。
- 其余不动：语音、审批、单词熊、界面、配网。

## 10. 托盘与管理界面

CodexBridge 托盘右键菜单：

- 设备列表：每台设备的名称、型号、在线状态、最后一条消息、最近已读状态。
- 配对设备：广播发现并配对；手动添加 IP 作为发现失败时的兜底；移除设备。
- 打开管理中心：打开 8787 网页（管理中心由 CodexBridge 拉起，无自己的托盘）。
- Codex 设置：工作目录、语音源等，沿用现有界面。
- 开机自动启动、打开日志目录、退出。

CodexBridge 本机管理接口（仅 127.0.0.1）：

- GET /api/v1/admin/devices：设备列表与状态。
- POST /api/v1/admin/devices/remove：移除设备。
- GET /api/v1/admin/pairing：给管理中心的配网流程取配对令牌。
- GET /api/v1/admin/status：版本与健康状态。

## 11. 存储与路径

- %LOCALAPPDATA%\CodexBridge\：devices.json（device_id、名称、型号、令牌、能力、最后在线时间、已读状态）、bridge.log、语音源配置。
- 迁移：现有 %LOCALAPPDATA%\FoloOS\bridge.json 里的 host、port、token 自动迁入 devices.json，旧文件保留但不再读取。
- Dotii 的 %LOCALAPPDATA%\StateDisplay 保持不动，Bambu、自定义页与显示配置继续存在那里。

## 12. 安全

- 每台设备一枚令牌（不少于 16 字符），首次配对 TOFU，之后严格校验。
- 管理接口只接受回环地址；设备接口校验令牌。
- 所有字符串、数组、分片都有长度上限；日志不写令牌。
- 8765 只用于局域网，不暴露到公网。

## 13. 兼容与迁移

- 未升级的 Dotii 固件继续只连管理中心：Codex 显示为空或离线，不报错。
- 已刷机的 AI Passport 走 v1 分支，零改动。
- 回滚路径：CodexBridge 保留 v1 代码路径，必要时可退回现有 FoloOS bridge 行为。

## 14. 测试与验收

单元测试：

- 协议编解码与分片、能力过滤、消息归一、已读广播、TOFU 与令牌校验、长度上限。

集成测试：

- 假设备（分别模拟 Dotii 与 AI Passport）跑通发现、握手、消息、已读全链路。
- 两台设备同时在线的广播与已读同步。

真机验收：

- Dotii 圆屏显示运行状态、任务状态、任务结果；两台 Dotii 的已读同步。
- AI Passport 的已读事件与其他设备同步。
- Dotii 的 Bambu、自定义页、显示设置、BLE 配网、烧录全链路回归。
- FoloOS 的语音、审批、单词熊回归。

## 15. 里程碑

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M1 | 新仓库骨架、协议核心、设备注册表、托盘设备列表 | 不依赖设备改动的可运行宿主 |
| M2 | Dotii 固件客户端、双数据源合并、管理中心去掉 Codex | 设备能从 CodexBridge 拿 Codex 数据 |
| M3 | 统一消息与已读、AI Passport read_ack | 多设备已读同步 |
| M4 | 单托盘打包、管理中心子进程、配置迁移 | 一个 EXE、一个托盘 |
| M5 | 真机验收与发布 | 可发布的 Release |

## 16. 风险

- Dotii 固件新增 TCP 服务端并维护双数据源，内存、栈和断线重连是主要风险点。
- 双令牌的配网流程改动面较大，需要真机验证。
- FoloOS 是社区衍生项目，搬运其 bridge 代码必须保留许可证与出处说明。
- 多台同型号设备依赖稳定的 device_id（建议由 MAC 派生）。
- 发现机制依赖广播；启用访客网络隔离或热点客户端隔离时，需要手动添加 IP 兜底。
