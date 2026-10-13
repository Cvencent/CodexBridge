# CodexBridge

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
CodexBridge-Setup-1.3.0-win64.exe
```

A portable ZIP is published with the same release. The release pipeline is
defined in `.github/workflows/release-windows.yml`; local builds use:

```powershell
./packaging/build_windows.ps1 -Version 1.3.0
```

The installer is Simplified Chinese by default. Installing a newer CodexBridge
version over an older one upgrades the application in place and keeps user data
in `%LOCALAPPDATA%\CodexBridge`, including paired devices and speech settings.

The installer is built with Inno Setup 6. Install it locally with:

```powershell
winget install -e --id JRSoftware.InnoSetup
```