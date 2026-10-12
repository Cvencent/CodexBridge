from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from codexbridge import VERSION
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub

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
) -> tuple[int, dict]:
    if not _is_loopback(client_host):
        return 403, {"ok": False, "error": "loopback_required"}

    method = method.upper()
    try:
        if method == "GET" and path == "/api/admin/status":
            return 200, {
                "ok": True,
                "version": VERSION,
                "device_count": len(registry._devices),
            }
        if method == "GET" and path == "/api/admin/codex-status":
            status = codex_status() if codex_status is not None else {}
            return 200, {"ok": True, "codex": status}
        if method == "GET" and path == "/api/admin/devices":
            return 200, {
                "ok": True,
                "devices": [
                    _public_device(record)
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


def _public_device(record) -> dict:
    return {
        "device_id": record.device_id,
        "model": record.model,
        "name": record.name,
        "supported": sorted(record.supported),
        "enabled": sorted(record.enabled),
        "last_seen": record.last_seen,
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
    ):
        self.hub = hub
        self.registry = registry
        self.host = host
        self.port = port
        self.on_device_removed = on_device_removed
        self.codex_status = codex_status
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
                        )
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
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
