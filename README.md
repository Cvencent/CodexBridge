# CodexBridge

> **文档导航：** **项目说明** · [通用协议](PROTOCOL.md) · [CBDP v1 协议](docs/CBDP-V1-PROTOCOL.md)

CodexBridge is the PC-side host for Codex-aware devices on a local network.
It owns Codex collection, the CBDP v1 binary device protocol, the legacy
JSON compatibility gateway, per-device capabilities, unified messages,
read-state synchronization, shared speech recognition, and the tray UI.

## Settings

Open the local management page from the tray icon or directly at:

```text
http://127.0.0.1:8788/settings
```

The page manages discovered and paired devices, their capability switches and
pairing tokens. Dotii and AI Passport share one speech-recognition
configuration:

- **内置识别** is the default. It prefers a bundled Vosk model and falls back
  to Windows `System.Speech`.
- **OpenAI 兼容接口** accepts an endpoint such as
  `https://example.com/v1/audio/transcriptions`, an API key, a model, and a
  language.

The speech configuration is stored at:

```text
%LOCALAPPDATA%\CodexBridge\speech-source.json
```

The saved API key is never returned to the browser. Leaving the API Key field
empty preserves the previously saved key; use the explicit clear checkbox to
remove it. Remote recognition failures fall back to built-in recognition.

## 手机端

CodexBridge 内置手机伴侣接口：同一局域网内的手机在管理页面
`127.0.0.1:8788/settings` 生成配对码后，即可查看线程、读取消息、远程发送任务、
切换模型并处理审批。

- 手机端默认通过 UDP `8569` 自动发现电脑，也可以手动填写 `http://<电脑 IP>:8568`。
- 设置页会显示各端口的健康状态，并可修改手机连接端口和手机发现端口；如果在电脑端改了发现端口，手机端的“扫描端口”也要改成同一个值。
- `8764` 和 `8765` 是设备固件定义的固定端口；`8790` 已保留，不会使用。
- 伴侣接口默认端口为 `8568`，手机发现端口为 `8569`，两者都可以在电脑端设置页修改；手机端填入对应值即可。
- 伴侣接口默认开启，可在管理页面关闭；配对令牌只保存在手机与电脑本机。
- 接口细节见 [PROTOCOL.md](PROTOCOL.md) 第 17 节。

### 统一设备中心

管理页面里的手机和 ESP 硬件现在是同一份“设备中心”列表，不再是两份：

- 手机通过“生成配对码 + 手机输入配对码”即时批准，登记为
  `registration_state = approved`、`registration_method = pairing_code`。
- 没有令牌的 ESP 会在 UDP 发现应答里声明 `registration_state = pending` 和一次性
  `registration_nonce`，只出现在“待批准”里，不建立业务会话。
- 在管理页点击“批准”后，CodexBridge 才下发 `register_offer`，设备保存令牌并返回
  `register_ok`，下一次连接进入正常的 `bridge_auth`；拒绝、撤销和重新注册都在同一
  张卡片上完成。
- 列表筛选项为 `全部 / 手机 / 硬件 / 待批准`，设备名、来源地址、能力和正文预算
  同列展示，令牌和注册随机值不会出现在页面上。

### 待发送队列

对话进行中继续输入的消息不再由手机直接 `steer`，而是进入由电脑端 CodexBridge
统一持有并持久化（`%LOCALAPPDATA%\CodexBridge\queues.json`）的线程级队列：

- 线程空闲时 Bridge 立即派发队首，线程进行中则保持 `pending`，turn 结束后自动派发下一项。
- 队列属于线程而不是设备：任一客户端加入、编辑、删除，其他在线的手机、ESP 和电脑端
  都会同步到同一份状态。
- 每条待发送项都可以 `引导`（注入当前进行中的 turn）、`编辑` 或确认后 `删除`。
- 队列命令要求 `queue` 能力，手机默认启用，ESP 需要在握手里声明；未声明 `queue` 的旧
  客户端继续使用原有 `turn.start` / `mode=steer` 行为。

Android 手机端应用源码与成品 APK 位于本仓库 [mobile/](mobile/README.md)：

```text
mobile/android/    Kotlin + Jetpack Compose 工程
mobile/release/    CodexBridgeMobile-0.3.6-release.apk 及校验文件
```

## Development

Python 3.11 or newer is required. The core uses only the standard library.

```powershell
python -m unittest discover -s tests -v
python -m codexbridge --no-tray
Invoke-RestMethod http://127.0.0.1:8788/api/admin/status
```

Install the optional tray dependencies with `pip install -e ".[tray]"`.

To use bundled offline recognition, install the optional Vosk runtime and
place the model in `%LOCALAPPDATA%\CodexBridge\models\vosk-model-small-cn-0.22`,
or set `CODEXBRIDGE_VOSK_MODEL` to the model directory.
## Windows release

Download the Windows installer from the GitHub Releases page:

```text
CodexBridge-Setup-2.0.4-win64.exe
```

A portable ZIP contains the same single `CodexBridge.exe`. The release pipeline
is defined in `.github/workflows/release-windows.yml`; local builds use:

```powershell
./packaging/build_windows.ps1 -Version 2.0.4
```

`CodexBridge.exe` stays in the system tray and only handles the Codex side:
Codex collection, the unified LAN device protocol, per-device capabilities and
shared read state. Dotii flashing, BLE provisioning, display and emotion
settings live in the separate `DotiiManagementCenter.exe`, which is started
on demand and does not stay resident.

The installer is Simplified Chinese by default. Installing a newer CodexBridge
version over an older one upgrades the application in place and keeps user data
in `%LOCALAPPDATA%\CodexBridge`, including paired devices and speech settings.
The packaged app also repairs the Windows uninstall record at startup, so the
version listed under Settings > Apps matches the installed build even when an
upgrade wizard is cut short.

The installer is built with Inno Setup 6. Install it locally with:

```powershell
winget install -e --id JRSoftware.InnoSetup
```
