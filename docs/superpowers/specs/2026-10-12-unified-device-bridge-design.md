# CodexBridge 统一设备桥设计

日期：2026-10-12
状态：唯一协议修订，待评审（2026-10-12）

## 1. 背景

现在有两个 PC 端程序在做同一件事，各自带一个托盘图标：

- Dotii-Display：DotiiManagementCenter.exe（托盘宿主）拉起 DotiiBridge.exe（后台）。设备是 ESP32-S3 圆屏，由设备主动 HTTP 轮询管理中心的 schema_version = 1 快照；Codex 采集在管理中心里。
- FoloOS AI Passport：CodexBridge.exe（托盘）。设备是 AI Passport，设备监听 TCP 8765 与 UDP 8764 发现，PC 主动连接后走换行分隔 JSON；Codex 采集在 bridge 里。

## 2. 目标

1. 一个 EXE、一个托盘：CodexBridge.exe 成为唯一常驻的 PC 端程序，管理中心不再有自己的托盘。
2. 一套通用设备协议：以后任何新设备实现这套协议就能接进来，拿到 Codex 运行状态、任务状态、任务结果、通知和已读同步。
3. 消息互通：一条 Codex 消息广播给所有在线设备；任何一台设备标记已读，其他设备同步。
4. Dotii 改说这套协议（Codex 部分）；Bambu、自定义页、显示设置仍走管理中心。
5. 所有设备只实现、只运行这一套协议；AI Passport 做一次最小固件迁移，Dotii 承担主要接入工作。

## 3. 非目标

- 不改 Dotii 的 Bambu、自定义页、显示设置、BLE 配网与烧录链路。
- 不做云端中转，所有连接都在同一局域网。
- 不重写 AI Passport 的语音、审批和单词熊功能。
- 不在 CodexBridge 内保留任何设备协议版本分支、并行握手或旧设备兼容路径。

## 4. 仓库与目录

- 新仓库 CodexBridge（本仓库）：PC 端全部内容。托盘、通用协议宿主、Codex 采集、统一消息与已读、管理中心后台（网页、Bambu、自定义页、配网、烧录）、打包与发布。
- Dotii-Display：回归设备项目，保留固件、硬件资料和文档。bridge/ 下的管理中心代码迁移到本仓库；迁移完成后该仓库的便携包发布入口改为指向 CodexBridge 的 Release。
- FoloOS AI Passport 社区仓库：只做设备固件的最小改动（见第 10 节）。

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

## 6. 唯一设备协议

CodexBridge、Dotii 和 AI Passport 只实现本节这一套协议。协议内部没有版本字段、版本分支、并行握手或“旧设备优先”逻辑。传输为 TCP 8765、换行分隔 JSON、UTF-8；UDP 8764 只做发现。

### 6.1 发现

- 请求固定为 `CODEXBRIDGE_DISCOVER`，不发送第二套发现请求。
- 应答固定为 `{"device":"dotii","device_id":"<稳定ID>","name":"<名称>","port":8765,"capabilities":[...]}`。
- `device_id`、`device`、`port` 为必填；缺少 `device_id` 的应答不进入设备列表。
- 设备切换 IP 后必须继续上报同一个 `device_id`。

### 6.2 握手

握手只有一条状态机：

1. PC 主动连接设备的 TCP 8765。
2. PC 到设备：`{"type":"bridge_auth","token":"<令牌>"}`。
3. 设备校验令牌并返回：`{"type":"bridge_auth_ok","device_id":"<稳定ID>","model":"<型号>","name":"<名称>","firmware":"<固件版本>","capabilities":[...]}`。
4. PC 到设备：`{"type":"bridge_ready","bridge":"CodexBridge","version":"<PC程序版本>","capabilities":["<生效能力集合>"]}`。
5. 收到 `bridge_ready` 后，会话进入在线状态。

令牌为空且设备支持首次信任配对的型号，可以接受第一个合法令牌并持久保存；这只是一种设备端配对策略，不是第二套协议。Dotii 由管理中心在配网时预置令牌，AI Passport 在固件迁移时实现同一字段和同一校验流程。

### 6.3 心跳与断线

- PC 每 5 秒发送 `bridge_ready`；首次 `bridge_ready` 同时完成能力授予。
- 设备 15 秒未收到任何 PC 消息即判定离线，断线后重新执行同一握手。
- PC 侧每台设备有独立会话和重连退避，一台设备抖动不影响其他设备。

### 6.4 消息

PC 到设备：

| 类型 | 用途 |
| --- | --- |
| `bridge_ready` | 握手完成与心跳，首次带生效能力 |
| `capabilities_update` | 运行时更新该设备允许使用的能力 |
| `message` | 归一消息：`message_id`、`thread_id`、`kind`、`status`、`title`、`body`、`seq`、`total`、`created_at` |
| `read_state` | 已读广播：`message_id`、`read_by[]`、`read_at` |
| `error` | 设备发起了未授予能力的事件 |

设备到 PC：

| 类型 | 用途 |
| --- | --- |
| `read_ack` | 上报 `message_id` 已读 |
| `cancel_task` | 取消当前任务 |
| `approval_decision` | 提交审批决定 |
| `voice_command` | 提交语音命令 |
| `audio_chunk` | 上传语音数据 |

未知消息类型必须忽略，不能断开会话。后续只能增加可选字段或新消息类型，不得在同一协议内建立另一套握手。

### 6.5 能力协商

- 设备在 `bridge_auth_ok.capabilities` 中声明自己实际支持的能力。
- PC 计算 `supported ∩ enabled`，通过首次 `bridge_ready.capabilities` 返回生效能力。
- Dotii 默认开启 `status`、`text`、`tasks`、`results`、`read_ack`；AI Passport 默认只开启 `status`、`text`、`read_ack`。
- PC 只下发设备声明且已经授权的数据；设备上报未授权事件时，PC 返回 `error`。
- 未知能力一律丢弃，不写入设备配置。

### 6.6 长文本

- `message.body` 每片最多 480 字节，消息带 `seq` 与 `total`。
- 设备侧按消息拼接，保留上限 4 KB；超长时截断并给出尾部提示。

### 6.7 协议演进

- 同一协议永远使用同一握手和同一消息语义。
- 新能力通过能力清单增加，不能通过增加协议分支实现。
- 新字段必须可选；不兼容的字段变化在设备固件、CodexBridge 和测试中一次性同步完成。

## 7. 能力与权限模型（可插拔）

需求原话：每台设备能看到什么、能用什么都要可配置；AI Passport 因为性能限制，默认只开很少的能力。因此能力不写死在协议里，而是按设备配置的插件。

- 能力清单（feature id）：`status`、`text`、`tasks`、`results`、`approval`、`audio`、`read_ack`、`voice`、`word_bear`。
- 两层来源：设备在 `bridge_auth_ok` 中声明 supported（它固件支持的）；CodexBridge 保存每台设备的 enabled（用户允许的）。生效能力等于 supported 与 enabled 的交集，只有生效能力对应的消息才会推给这台设备。
- 型号默认模板：按 model 保存一份默认 enabled，新设备首次配对时套用。AI Passport 默认只开最小集合 `status`、`text`、`read_ack`；Dotii 默认开 `status`、`text`、`tasks`、`results`、`read_ack`。
- 运行时变更：用户在托盘改开关后立即下发 capabilities_update，设备据此显示或隐藏页面与菜单，不需要重连。
- 能力与权限分开：能力是这台设备能不能做，权限是允不允许做。被关掉的能力既不下发数据，也不接受它发起的事件，PC 回 error。
- 默认保守：没有模板的新型号只开最小集合；未知能力一律不下发。
- 与现有开关的关系：Dotii 管理中心的模块开关继续管 Bambu、自定义页与显示设置；Codex 相关开关搬到 CodexBridge，避免两处开关互相打架。
- 配置入口：托盘右键的设备子菜单里逐项勾选，另有该型号默认值；同一份数据通过回环管理接口暴露，管理中心网页可以复用。

## 8. 统一消息与已读

- 归一：一个 Codex 任务或一条通知生成一条 message。message_id 由 thread_id 加任务内递增序号组成，跨设备稳定。
- 广播：新消息推给所有在线设备，按能力过滤。
- 已读：设备发 read_ack 后写入该消息的 read_by，并向其他设备推 read_state。
- 补发：设备重连后，PC 先推送未读消息与最新 read_state，避免重连后看不到别人已经读过的状态。
- 不变量：已读是通知级的，按 message_id 记录；不依赖任何设备本地的页面计数或缓存。

### 8.1 看过的定义

- Dotii：任务详情页停留满 2 秒即上报已读（阈值做成常量，便于真机调）。不加额外按键，避免和圆屏的翻页手势冲突。
- AI Passport：用户在该条消息上按 OK 离开时上报已读。
- 备选（仅记录，不实现）：Dotii 改为按 Button A 才算已读。

## 9. Dotii 侧改造

固件（主要工作量，ESP-IDF / C）：

| 文件 | 改动 |
| --- | --- |
| main/codex_bridge_client.c/.h（新增） | UDP 发现应答、TCP 服务端、bridge_auth 与 bridge_auth_ok、行 JSON 解析、心跳、断线重连、read_ack 上报 |
| main/connectivity.c | Codex 字段改由新客户端填充；HTTP 轮询继续填 Bambu、自定义页、显示设置 |
| main/state_ui.c | 详情页停留 2 秒触发已读上报；完成/失败表情由 Codex 状态本地推导 |
| main/device_config.c、main/ble_bridge.c | 新增 codex_bridge_token；管理中心的 URL 与令牌保持不动 |
| sdkconfig.defaults、分区 | 复核新客户端的栈、socket 缓冲与 PSRAM 预算 |

管理中心（原 dotii/bridge/）：

- 删除 Codex 采集：codex_app_server.py、Codex 模块开关、快照里的 Codex 段、管理页 Codex 标签。
- 快照保留 Bambu、自定义页、显示设置、模块开关；Dotii 表情的触发时长与开关留在配置里，表情本身由设备按 Codex 状态本地推导。
- BLE 配网增加两步：设备通过 BLE 上报自己的 device_id；管理中心据此向 CodexBridge 回环接口申领该设备的配对令牌，连同 Wi-Fi 信息一起下发。
- 保留烧录、日志、运行目录与现有配置兼容性。

## 10. AI Passport 侧改造（最小）

- 固件：发现常量改为 `CODEXBRIDGE_DISCOVER`，应答补充稳定的 `device_id`、`name` 和 `capabilities`。
- 固件：`bridge_auth_ok` 补充 `device_id`、`model`、`name`、`firmware`、`capabilities`。
- 固件：接收并保存首次 `bridge_ready.capabilities`，用户按 OK 离开消息时发送 `read_ack`。
- 固件：未知消息类型只忽略，不断开会话。
- 其余不动：语音、审批、单词熊、界面、配网入口。

## 11. 托盘与管理界面

CodexBridge 托盘右键菜单：

- 设备列表：每台设备的名称、型号、在线状态、最后一条消息、最近已读状态。
- 配对设备：广播发现并配对；手动添加 IP 作为发现失败时的兜底；移除设备。
- 能力开关：对选中的设备逐项勾选它能看什么、能用什么（见第 7 节）；另有该型号的默认值。
- 打开管理中心：打开 8787 网页（管理中心由 CodexBridge 拉起，无自己的托盘）。
- Codex 设置：工作目录、语音源等，沿用现有界面。
- 开机自动启动、打开日志目录、退出。

CodexBridge 本机管理接口（仅 127.0.0.1）：

- GET /api/admin/devices：设备列表与状态。
- POST /api/admin/devices/remove：移除设备。
- GET /api/admin/pairing：给管理中心的配网流程取配对令牌。
- GET /api/admin/status：版本与健康状态。

## 12. 存储与路径

- %LOCALAPPDATA%\CodexBridge\：devices.json（device_id、名称、型号、令牌、supported 与 enabled 能力、最后在线时间、已读状态）、capability-defaults.json（按型号的默认能力）、bridge.log、语音源配置。
- 迁移：现有 %LOCALAPPDATA%\FoloOS\bridge.json 里的 host、port、token 自动迁入 devices.json，旧文件保留但不再读取。
- Dotii 的 %LOCALAPPDATA%\StateDisplay 保持不动，Bambu、自定义页与显示配置继续存在那里。

## 13. 安全

- 每台设备一枚令牌（不少于 16 字符），首次配对 TOFU，之后严格校验。
- 管理接口只接受回环地址；设备接口校验令牌。
- 所有字符串、数组、分片都有长度上限；日志不写令牌。
- 8765 只用于局域网，不暴露到公网。

## 14. 迁移

- CodexBridge 不做设备协议兼容分支；接入前，Dotii 与 AI Passport 都必须支持第 6 节这一套协议。
- AI Passport 做一次固件迁移后直接进入唯一协议，不保留旧握手运行路径。
- 旧 `%LOCALAPPDATA%\FoloOS\bridge.json` 只迁移 host、port、token 数据，不迁移旧协议行为。
- 未升级的 Dotii 固件继续只连管理中心，Codex 区域显示为空或离线；这不是 CodexBridge 的第二套协议。

## 15. 测试与验收

单元测试：

- 唯一握手状态机、协议编解码与分片、能力过滤（supported 与 enabled 的交集、运行时变更、越权事件被拒）、消息归一、已读广播、令牌校验、长度上限。

集成测试：

- Dotii 与 AI Passport 假设备使用同一发现、握手、消息与已读状态机跑通全链路。
- 两台设备同时在线的广播与已读同步。

真机验收：

- Dotii 圆屏显示运行状态、任务状态、任务结果；两台 Dotii 的已读同步。
- AI Passport 的已读事件与其他设备同步。
- Dotii 的 Bambu、自定义页、显示设置、BLE 配网、烧录全链路回归。
- FoloOS 的语音、审批、单词熊回归。

## 16. 里程碑

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| M1 | 新仓库骨架、唯一协议核心、设备注册表、托盘设备列表 | 可运行宿主，协议不包含版本分支 |
| M2 | Dotii 固件客户端、双数据源合并、管理中心去掉 Codex | 设备能从 CodexBridge 拿 Codex 数据 |
| M3 | 统一消息与已读、AI Passport read_ack | 多设备已读同步 |
| M4 | 单托盘打包、管理中心子进程、配置迁移 | 一个 EXE、一个托盘 |
| M5 | 真机验收与发布 | 可发布的 Release |

## 17. 风险

- Dotii 固件新增 TCP 服务端并维护双数据源，内存、栈和断线重连是主要风险点。
- 双令牌的配网流程改动面较大，需要真机验证。
- AI Passport 只能在一次固件更新中迁入唯一协议；迁移前不能与 CodexBridge 建立设备会话。
- FoloOS 是社区衍生项目，搬运其 bridge 代码必须保留许可证与出处说明。
- 多台同型号设备依赖稳定的 device_id（建议由 MAC 派生）。
- 发现机制依赖广播；启用访客网络隔离或热点客户端隔离时，需要手动添加 IP 兜底。
