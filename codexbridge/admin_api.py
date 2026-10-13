from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codexbridge import VERSION
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.settings_page import SETTINGS_HTML
from codexbridge.speech_source import (
    load_speech_config,
    prepare_speech_config,
    probe_speech_config,
    public_speech_config,
    save_speech_config,
)

MAX_BODY_BYTES = 8 * 1024


def handle_request(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: bytes,
    client_host: str,
    hub: BridgeHub,
    registry: DeviceRegistry,
    on_device_removed: Callable[[str], None] | None = None,
    codex_status: Callable[[], dict] | None = None,
    speech_config_path: Path | None = None,
    startup_enabled: Callable[[], bool] | None = None,
    set_startup_enabled: Callable[[bool], bool] | None = None,
    open_logs: Callable[[], None] | None = None,
):
    if not _is_loopback(client_host):
        return 403, {"ok": False, "error": "loopback_required"}

    method = method.upper()
    speech_path = speech_config_path or _speech_path(registry)
    try:
        if method == "GET" and path == "/settings":
            return 200, SETTINGS_HTML
        if method == "GET" and path == "/api/admin/status":
            return 200, {
                "ok": True,
                "version": VERSION,
                "device_count": len(registry._devices),
            }
        if method == "GET" and path == "/api/admin/settings/general":
            enabled = (
                startup_enabled()
                if startup_enabled is not None
                else _startup_enabled()
            )
            return 200, {
                "ok": True,
                "general": {"startup_enabled": bool(enabled)},
            }
        if method == "POST" and path == "/api/admin/settings/general":
            payload = _json_body(body)
            if payload.get("action") == "open_logs":
                if open_logs is not None:
                    open_logs()
                return 200, {"ok": True}
            if "startup_enabled" not in payload:
                return 400, {"ok": False, "error": "startup_enabled_required"}
            value = payload.get("startup_enabled")
            if not isinstance(value, bool):
                return 400, {"ok": False, "error": "startup_enabled_invalid"}
            requested = value
            if set_startup_enabled is not None:
                set_startup_enabled(requested)
            else:
                _set_startup_enabled(requested)
            enabled = (
                startup_enabled()
                if startup_enabled is not None
                else _startup_enabled()
            )
            return 200, {
                "ok": True,
                "general": {"startup_enabled": bool(enabled)},
            }
        if method == "GET" and path == "/api/admin/codex-status":
            status = codex_status() if codex_status is not None else {}
            return 200, {"ok": True, "codex": status}
        if method == "GET" and path == "/api/admin/devices":
            sessions = getattr(hub, "_sessions", {})
            return 200, {
                "ok": True,
                "devices": [
                    _public_device(record, record.device_id in sessions)
                    for record in registry._devices.values()
                ],
            }
        if method == "GET" and path == "/api/admin/pairing":
            values = query.get("device_id", [])
            device_id = values[0] if values else ""
            if not device_id:
                return 400, {"ok": False, "error": "device_id_required"}
            token = registry.mint_token(device_id)
            return 200, {
                "ok": True,
                "device_id": device_id,
                "token": token,
                "port": 8765,
            }
        if method == "GET" and path == "/api/admin/settings/speech":
            return 200, {
                "ok": True,
                "speech": public_speech_config(speech_path),
            }
        if method == "POST" and path == "/api/admin/settings/speech":
            payload = _json_body(body)
            try:
                saved = save_speech_config(payload, speech_path)
            except ValueError as error:
                return 400, {
                    "ok": False,
                    "error": str(error),
                    "speech": public_speech_config(speech_path),
                }
            return 200, {
                "ok": True,
                "speech": public_speech_config(speech_path),
                "saved": {
                    "source": saved["source"],
                    "endpoint": saved["endpoint"],
                    "model": saved["model"],
                    "language": saved["language"],
                },
            }
        if method == "POST" and path == "/api/admin/settings/speech/test":
            payload = _json_body(body)
            try:
                config = (
                    prepare_speech_config(payload, speech_path)
                    if payload
                    else load_speech_config(speech_path)
                )
            except ValueError as error:
                return 400, {"ok": False, "error": str(error)}
            ok, message = probe_speech_config(speech_path, config=config)
            if not ok:
                return 400, {"ok": False, "error": message, "message": message}
            return 200, {"ok": True, "message": message}
        if method == "POST" and path == "/api/admin/devices/remove":
            payload = _json_body(body)
            device_id = payload.get("device_id")
            if not isinstance(device_id, str) or not device_id:
                return 400, {"ok": False, "error": "device_id_required"}
            removed = registry.remove(device_id)
            if removed:
                registry.save()
                hub.detach(device_id)
            if on_device_removed is not None:
                on_device_removed(device_id)
            return 200, {"ok": True, "device_id": device_id, "removed": removed}
        if method == "POST" and path == "/api/admin/devices/capabilities":
            payload = _json_body(body)
            device_id = payload.get("device_id")
            enabled = payload.get("enabled")
            if not isinstance(device_id, str) or not device_id:
                return 400, {"ok": False, "error": "device_id_required"}
            if not isinstance(enabled, list):
                return 400, {"ok": False, "error": "enabled_required"}
            registry.set_enabled(device_id, enabled)
            registry.save()
            hub.push_capabilities(device_id)
            return 200, {
                "ok": True,
                "device_id": device_id,
                "enabled": sorted(registry.get(device_id).enabled),
            }
    except KeyError:
        return 404, {"ok": False, "error": "device_not_found"}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return 400, {"ok": False, "error": "invalid_request"}
    return 404, {"ok": False, "error": "not_found"}


def _startup_enabled() -> bool:
    from codexbridge.startup import is_startup_enabled

    return is_startup_enabled()


def _set_startup_enabled(enabled: bool) -> bool:
    from codexbridge.startup import set_startup_enabled

    return set_startup_enabled(enabled)


def _speech_path(registry: DeviceRegistry) -> Path:
    if registry.path is not None:
        return Path(registry.path).parent / "speech-source.json"
    from codexbridge.speech_source import speech_source_path

    return speech_source_path()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _json_body(body: bytes) -> dict:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    if not body:
        return {}
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("body must be an object")
    return value


def _public_device(record, online: bool = False) -> dict:
    return {
        "device_id": record.device_id,
        "model": record.model,
        "name": record.name,
        "supported": sorted(record.supported),
        "enabled": sorted(record.enabled),
        "last_seen": record.last_seen,
        "online": online,
    }


class _AdminServer(ThreadingHTTPServer):
    daemon_threads = True


class AdminApi:
    def __init__(
        self,
        hub: BridgeHub,
        registry: DeviceRegistry,
        host: str = "127.0.0.1",
        port: int = 8788,
        on_device_removed: Callable[[str], None] | None = None,
        codex_status: Callable[[], dict] | None = None,
        speech_config_path: Path | None = None,
        startup_enabled: Callable[[], bool] | None = None,
        set_startup_enabled: Callable[[bool], bool] | None = None,
        open_logs: Callable[[], None] | None = None,
    ):
        self.hub = hub
        self.registry = registry
        self.host = host
        self.port = port
        self.on_device_removed = on_device_removed
        self.codex_status = codex_status
        self.speech_config_path = speech_config_path
        self.startup_enabled = startup_enabled
        self.set_startup_enabled = set_startup_enabled
        self.open_logs = open_logs
        self._server: _AdminServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _dispatch(self, method: str):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                try:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    length = 0
                if length > MAX_BODY_BYTES:
                    status, payload = 413, {"ok": False, "error": "body_too_large"}
                else:
                    body = self.rfile.read(length)
                    content_type = (
                        self.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if method == "POST" and content_type != "application/json":
                        status, payload = 415, {
                            "ok": False,
                            "error": "json_content_type_required",
                        }
                    else:
                        status, payload = handle_request(
                            method,
                            parsed.path,
                            query,
                            body,
                            self.client_address[0],
                            api.hub,
                            api.registry,
                            api.on_device_removed,
                            api.codex_status,
                            api.speech_config_path,
                            api.startup_enabled,
                            api.set_startup_enabled,
                            api.open_logs,
                        )
                if isinstance(payload, str):
                    encoded = payload.encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                else:
                    encoded = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                return

        self._server = _AdminServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._server = None
        self._thread = None
