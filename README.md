# CodexBridge

CodexBridge is the PC-side host for Codex-aware devices on a local network.
It owns Codex collection, a line-delimited JSON device protocol, per-device
capabilities, unified messages, read-state synchronization, and the tray UI.

## Development

Python 3.11 or newer is required. The core uses only the standard library.

```powershell
python -m unittest discover -s tests -v
python -m codexbridge --no-tray
Invoke-RestMethod http://127.0.0.1:8788/api/admin/status
```

Install the optional tray dependencies with `pip install -e ".[tray]"`.
