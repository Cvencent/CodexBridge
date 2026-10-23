# CodexBridge

> **文档导航：** **项目说明** · [CBDP v1 协议](docs/CBDP-V1-PROTOCOL.md) · [本机管理与伴侣接口](docs/companion-and-admin-api.md)

[![Release](https://img.shields.io/github/v/release/Cvencent/CodexBridge?display_name=tag&label=Release)](https://github.com/Cvencent/CodexBridge/releases/latest)
[![Windows Release](https://github.com/Cvencent/CodexBridge/actions/workflows/release-windows.yml/badge.svg)](https://github.com/Cvencent/CodexBridge/actions/workflows/release-windows.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4)](#安装与升级)

CodexBridge 是运行在 Windows 电脑上的局域网 Codex 桥接器。它把本机 Codex
桌面端或 Codex CLI 的任务、消息、审批、附件、模型设置和运行状态，统一桥接给
AI Passport、Dotii、Android 手机以及其它实现 CBDP v1 的设备。

简单说：**Codex 在电脑上工作，CodexBridge 负责让局域网设备安全地看见它、控制它、
收到它的完成提醒，并在断线重连后恢复正确状态。**

## 它是什么

CodexBridge 不是设备固件，也不是手机 App，而是 PC 侧的唯一桥接宿主：

- 采集本机 Codex 的线程、turn、消息、审批和任务结果。
- 维护设备发现、配对、鉴权、能力协商、在线状态和事件订阅。
- 通过 CBDP v1 二进制协议连接 ESP32/ESP32-S3，同时保留旧 JSON 行协议兼容网关。
- 为 Android 手机提供 Companion HTTP/SSE/WebSocket 接口。
- 统一处理待发送队列、跨客户端已读、附件上传、桌面 writer owner 路由和语音识别。
- 提供简体中文托盘程序与本地管理页 `127.0.0.1:8788/settings`。

## 支持设备

| 设备或客户端 | 形态 | 接入方式 | 对应仓库 |
|---|---|---|---|
| **AI Passport / FoloToy AI Passport** | ESP32-C3 掌上设备 | CBDP v1、UDP 首页计数 | [Cvencent/one2passport](https://github.com/Cvencent/one2passport) |
| **Dotii 桌面交互屏** | ESP32 桌面设备 | CBDP v1 | [Cvencent/Dotii-Display](https://github.com/Cvencent/Dotii-Display) |
| **CodexBridge Android** | Android 手机 | Companion HTTP、SSE、WebSocket | [Cvencent/CodexBridgeMobile](https://github.com/Cvencent/CodexBridgeMobile) |
| **通用 ESP32 / ESP32-S3 设备** | 自定义硬件 | UDP 发现 + TCP CBDP v1 | [CBDP v1 协议](docs/CBDP-V1-PROTOCOL.md) |
| **Codex Desktop / Codex CLI** | PC 上游执行端 | App Server、Desktop IPC | [openai/codex](https://github.com/openai/codex) |

设备差异通过 `supported` 与 `enabled` 能力协商处理，不为单一型号复制一套协议。
新型号只要实现同一套 UDP 发现、TCP 会话、CBDP 握手和事件模型，就可以接入。

## 核心能力

### Codex 任务桥接

- 读取线程列表、线程详情、任务状态、当前动作和模型目录。
- 手机或设备可以新建任务、继续线程、引导正在运行的 turn、中断任务。
- 桌面端拥有的线程通过 Desktop IPC owner discovery 路由，避免和桌面 Codex
  争抢 writer lock。
- 支持审批请求、审批回复、MCP/结构化输入和附件内容读取。
- 支持线程级模型、推理强度、审批模式和 sandbox 策略同步。

### 统一设备中心

- UDP 自动发现、待批准设备、批准/拒绝/撤销、配对令牌和能力配置都在同一页面。
- 每台设备分别保存支持能力和本机启用策略，不会因为某个新能力自动扩大权限。
- 手机使用配对码；ESP 硬件使用一次性 registration nonce 和 `register_offer`。
- 在线/离线状态采用边沿触发与迟滞，失败重拨不会重复发送假离线事件。
- 已批准 ESP 设备即使停留在首页、暂不建立 CBDP TCP，也会通过 UDP presence 保持在线状态。
- 手机 SSE/WebSocket 心跳会持续刷新在线状态。

### 消息、已读与提醒

- 统一处理任务状态、消息、结果、审批和 Codex 配额信息。
- 完成/失败结果持久化，Bridge 重启后仍恢复未读计数。
- 手机、ESP 和电脑端共享同一份跨设备已读水位。
- `turn.completed` / `turn.failed` / `turn.interrupted` 提供具体任务的终态事件。
- 首页聚合计数同时支持 TCP `system.codex_state(0x0704)` 和 UDP `HOME_COUNTERS(0x1E)`。

### 待发送队列

- 对话正在运行时继续发送的消息进入线程级持久队列，而不是直接制造并发 turn。
- 队列属于线程，不属于设备；手机、ESP 和电脑端看到同一份状态。
- 支持排队、引导、编辑、删除和 revision 冲突保护。
- Bridge 重启后会恢复队列，并从遗留的异常 turn 状态中恢复派发。

### 附件与语音

- 支持文件、图片、音频和二进制附件分片上传、校验、列表、读取与删除。
- 图片附件可转换为 Codex app-server 接受的 `image` 输入。
- Dotii 和 AI Passport 共用一套语音识别配置。
- 默认优先使用内置 Vosk 离线模型，失败时回退 Windows `System.Speech`。
- 可选 OpenAI 兼容 `/v1/audio/transcriptions` 接口；API Key 不通过 GET 返回。

## 协议

CodexBridge 只有一套设备接入协议：

- **[CBDP v1 二进制协议](docs/CBDP-V1-PROTOCOL.md)** —— 所有设备唯一需要实现的协议，
  线格式为 `major = 1`、`minor = 0`。新设备、ESP32/ESP32-S3、面板和手机 CBDP
  客户端一律按它实现，不要在仓库里另找设备协议文档。
- **旧 JSON 行协议**只作为已有 Dotii 和旧固件的兼容网关保留，映射规则写在
  CBDP v1 线协议第 24 节，不再是设备接入的起点。

手机伴侣 API、本机管理 API 和待发送队列属于另一条非设备协议接入面，见
[本机管理与伴侣接口](docs/companion-and-admin-api.md)。

### 默认端口

| 用途 | 默认端口 | 说明 |
|---|---:|---|
| 设备 UDP 发现 / 首页计数 | `8764` | ESP 固定端口；Bridge 监听 presence 和 `0x1E` 计数通道 |
| 设备 TCP 数据连接 | `8765` | Bridge 主动连接设备 TCP 服务端 |
| 手机 Companion HTTP | `8568` | 配对、线程、消息、附件、审批、SSE 和 WebSocket |
| 手机 UDP 发现 | `8569` | 手机自动发现电脑 |
| 本机管理页 | `8788` | 只允许回环地址访问 |

`8790` 已保留，不使用。修改手机连接或发现端口后，手机端必须填写相同值；`8764`
和 `8765` 是设备固件定义的固定端口。

### CBDP v1 会话流程

```text
UDP 发现
  -> TCP 连接设备 8765
  -> HELLO / WELCOME
  -> AUTH / AUTH_OK
  -> 能力协商
  -> SUBSCRIBE / COMMAND / RESULT / DELTA
  -> ACK / cursor replay / resync
```

协议特性包括：

- 定长二进制帧、TLV header/payload、UTF-8 字符串和明确长度上限。
- `critical` 与 `state` QoS；大帧自动分片，接收端重组。
- topic + scope + cursor 的可恢复事件订阅。
- 幂等键固定为 `(device_id, idem)`，设备重启后不会撞上自己的历史请求。
- 中断、重连和事件库损坏后的 resync 与自动恢复。
- CBDP DELTA 必须来自 Event Spine；兼容层不会伪造缺字段的 DELTA。
- 首页计数使用 summary scope 的 `0x0704`，MCU 首页也可使用轻量 UDP `0x1E` 旁路。

## 优势

### 1. 一台电脑统一管理所有设备

Codex 状态、审批、队列、已读和附件只有一份权威状态，不在手机、ESP 和桌面端
各维护一套互相漂移的计数。

### 2. 通用协议而不是型号分支

所有设备复用同一套发现、鉴权、能力协商、命令和事件模型。设备差异通过能力位和
本机策略表达，不需要为每块屏幕复制一套私有握手。

### 3. 对 MCU 友好

- CBDP 使用紧凑二进制帧和 TLV，避免大量 JSON 文本占用 ESP32 内存。
- 首页计数提供 UDP `0x1E` 单播旁路，首页不必长期保留 TCP 会话。
- 文本、数组、附件、音频和请求体都有明确上限。
- 事件按订阅投递，未订阅事件不会占用设备网络与 lwIP 接收缓冲。

### 4. 可恢复、可重放、可重建

- Event Spine 提供全局序号、topic 序号、revision 和稳定 event id。
- `threads`、`messages` 投影可从事件日志重建。
- 设备用 cursor 恢复，不依赖 TCP 传输序号恢复业务状态。
- 持久引用表保证 `thread_ref`、`message_ref`、`approval_ref` 跨重连稳定。

### 5. 多客户端一致性

- 队列修改、已读、审批解决和完成提醒在所有客户端间同步。
- 手机读取结果会同步给设备，桌面已读水位也会回流到 Bridge。
- 审批回复支持简单 decision 和结构化 MCP/权限响应。

### 6. 面向 Windows 的完整交付

- 简体中文安装器、托盘程序、本地管理页和中文 Release 说明。
- 覆盖安装保留 `%LOCALAPPDATA%\CodexBridge` 中的设备、令牌、语音、录音和运行数据。
- 原子写入配置，事件库启动自检并支持损坏恢复。
- 启动时自动修复 Windows 卸载记录的显示版本。

### 7. 职责边界清楚

CodexBridge 只负责 Codex 桥接。Dotii 的 Bambu、显示、配网和烧录仍由 Dotii
管理中心处理；这些能力不会被硬塞进 CodexBridge 的设备协议。

## 快速开始

1. 从 [GitHub Releases](https://github.com/Cvencent/CodexBridge/releases/latest)
   下载 `CodexBridge-Setup-2.1.38-win64.exe`。
2. 安装并启动 CodexBridge，托盘图标会常驻运行。
3. 打开本地管理页：

```text
http://127.0.0.1:8788/settings
```

4. ESP 设备会被 UDP 自动发现；没有令牌的设备先出现在“待批准”，批准后才建立业务会话。
5. 手机在管理页生成配对码，输入后即可查看线程、发送任务、处理审批和同步已读。
6. 保持电脑与设备在同一局域网，并允许 Windows 防火墙的专用网络访问。

便携版 ZIP 内只有一个 `CodexBridge.exe`，功能与安装版相同。

## 安装与升级

系统要求：

- Windows 10/11 x64。
- 本机已安装并登录可用的 Codex Desktop 或 Codex CLI。
- 同一局域网内可访问设备 TCP `8765` 和 UDP `8764`。

安装器使用 Inno Setup 6 构建，默认简体中文。覆盖安装会保留：

```text
%LOCALAPPDATA%\CodexBridge
```

其中包括配对设备、能力配置、语音设置、录音、队列、消息、事件库和已读状态。

本地构建安装器前可安装 Inno Setup：

```powershell
winget install -e --id JRSoftware.InnoSetup
```

## 开发

核心代码要求 Python 3.11 或更高版本，运行时主要使用标准库。开发测试：

```powershell
python -m unittest discover -s tests -v
python -m codexbridge --no-tray
Invoke-RestMethod http://127.0.0.1:8788/api/admin/status
```

安装托盘依赖：

```powershell
pip install -e ".[tray]"
```

构建 Windows 安装包和 portable ZIP：

```powershell
./packaging/build_windows.ps1 -Version 2.1.38
```

使用内置离线语音识别时，将 Vosk 模型放到：

```text
%LOCALAPPDATA%\CodexBridge\models\vosk-model-small-cn-0.22
```

也可以通过 `CODEXBRIDGE_VOSK_MODEL` 指向模型目录。

## 目录结构

| 路径 | 内容 |
|---|---|
| `codexbridge/cbdp/` | CBDP v1 帧、TLV、事件库、订阅、引用、投影和命令网关 |
| `codexbridge/devices/` | UDP 发现、设备注册表、TCP 会话、在线状态和首页计数 |
| `codexbridge/codex/` | Codex app-server、Desktop IPC、采集器和 rollout 状态 |
| `codexbridge/companion/` | 手机 Companion API、配对、队列、附件和线程路由 |
| `codexbridge/protocol/` | 通用命令、能力、消息、分片和旧 JSON 兼容模型 |
| `codexbridge/settings_ui/` | `127.0.0.1:8788/settings` 管理页面 |
| `docs/` | CBDP 协议、跨仓库问题清单和接入说明 |
| `packaging/` | PyInstaller、Inno Setup 和 Windows 发布脚本 |
| `tests/` | 协议、设备、投影、队列、附件、桌面 IPC 和发布测试 |

## 数据与安全

- 管理 API 只允许 `127.0.0.1` 回环访问。
- 手机配对令牌只保存 SHA-256 摘要；硬件注册使用一次性 nonce。
- 设备消息、字符串、数组、附件和音频都有明确上限。
- API Key 不通过管理页 GET 返回；输入框留空表示保留原 Key。
- 配对令牌、API Key、用户配置和录音不会写入日志或提交到仓库。
- 修改持久化格式时必须保持向后兼容，不能在升级时静默丢配置。

## 相关项目

- **AI Passport 固件：** [Cvencent/one2passport](https://github.com/Cvencent/one2passport)
- **Dotii 桌面交互屏：** [Cvencent/Dotii-Display](https://github.com/Cvencent/Dotii-Display)
- **Android 手机端：** [Cvencent/CodexBridgeMobile](https://github.com/Cvencent/CodexBridgeMobile)
- **上游 Codex：** [openai/codex](https://github.com/openai/codex)

## 文档

- [CBDP v1 线协议](docs/CBDP-V1-PROTOCOL.md)
- [本机管理与伴侣接口](docs/companion-and-admin-api.md)
- [手机附件接入说明](docs/CodexBridge-mobile-attachment-api.md)
- [PC 侧卡顿根因与修复：超大线程 JSON（2026-10-23）](docs/pc-json-gil-freeze-2026-10-23.md)
- [设备端建议说明：手机 App 连接管理（2026-10-22）](docs/device-defect-long-lived-connections-2026-10-22.md)
- [CBDP v1 架构重构设计](docs/superpowers/specs/2026-10-17-cbdp-v1-architecture-refactor-design.md)
- [全部版本说明](release-notes/)

## 版本

当前版本：**2.1.38**

发布产物：

- `CodexBridge-Setup-2.1.38-win64.exe`
- `CodexBridge-2.1.38-win64-portable.zip`

最新下载：[GitHub Releases](https://github.com/Cvencent/CodexBridge/releases/latest)
