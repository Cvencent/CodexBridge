# CodexBridge

> **文档导航：** **项目说明** · [通用协议](PROTOCOL.md)

CodexBridge is the PC-side host for Codex-aware devices on a local network.
It owns Codex collection, a line-delimited JSON device protocol, per-device
capabilities, unified messages, read-state synchronization, shared speech
recognition, and the tray UI.

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

- 手机端默认通过 UDP `8766` 自动发现电脑，也可以手动填写 `http://<电脑 IP>:8791`。
- 设置页会显示各端口的健康状态，并可修改手机连接端口和手机发现端口；如果在电脑端改了发现端口，手机端的“扫描端口”也要改成同一个值。
- `8764` 和 `8765` 是设备固件定义的固定端口；`8790` 已保留，不会使用。
- 伴侣接口默认开启，可在管理页面关闭；配对令牌只保存在手机与电脑本机。
- 接口细节见 [PROTOCOL.md](PROTOCOL.md) 第 17 节。

Android 手机端应用源码与成品 APK 位于本仓库 [mobile/](mobile/README.md)：

``text
mobile/android/    Kotlin + Jetpack Compose 工程
mobile/release/    CodexBridgeMobile-0.2.5-release.apk 及校验文件
``

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
CodexBridge-Setup-1.3.11-win64.exe
```

A portable ZIP contains the same single `CodexBridge.exe`. The release pipeline
is defined in `.github/workflows/release-windows.yml`; local builds use:

```powershell
./packaging/build_windows.ps1 -Version 1.3.11
```

`CodexBridge.exe` stays in the system tray and only handles the Codex side:
Codex collection, the unified LAN device protocol, per-device capabilities and
shared read state. Dotii flashing, BLE provisioning, display and emotion
settings live in the separate `DotiiManagementCenter.exe`, which is started
on demand and does not stay resident.

The installer is Simplified Chinese by default. Installing a newer CodexBridge
version over an older one upgrades the application in place and keeps user data
in `%LOCALAPPDATA%\CodexBridge`, including paired devices and speech settings.

The installer is built with Inno Setup 6. Install it locally with:

```powershell
winget install -e --id JRSoftware.InnoSetup
```
