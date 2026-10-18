"""LAN HTTP and SSE API for paired phones."""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from codexbridge import VERSION
from codexbridge.codex.app_server import AppServerError
from codexbridge.companion.service import CompanionService
from codexbridge.companion.store import DEFAULT_PORT, LEGACY_PORT, CompanionStore
from codexbridge.ports import validate_companion_port
from codexbridge.net_gate import ExclusiveBindMixin
from codexbridge.protocol.commands import CommandError
from codexbridge.codex.desktop_ipc import DesktopIpcError

MAX_BODY_BYTES = 64 * 1024
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 60 * 60
PAIR_WINDOW_SECONDS = 600
PAIR_MAX_PER_WINDOW = 20
FALLBACK_PORT_ATTEMPTS = 20


class CompanionApi:
    """Threading HTTP server exposing Codex control to the local network."""

    def __init__(
        self,
        service: CompanionService,
        store: CompanionStore,
        host: str = "0.0.0.0",
        port: int | None = None,
        logger=None,
    ) -> None:
        self.service = service
        self.store = store
        self.host = host
        self.port = int(port if port is not None else store.port)
        if self.port == LEGACY_PORT:
            self.port = DEFAULT_PORT
        self._logger = logger
        self._server: _CompanionServer | None = None
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._pair_attempts: dict[str, list[float]] = {}
        self._pair_lock = threading.Lock()

    @property
    def url(self) -> str:
        host = self.host if self.host not in {"0.0.0.0", "::"} else "127.0.0.1"
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._server is not None:
                return
            server = self._bind_server()
            server.app = self
            self._server = server
            self.port = int(server.server_address[1])
            self.store.set_port(self.port)
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="companion-api",
                daemon=True,
            )
            self._thread.start()

    def _bind_server(self) -> "_CompanionServer":
        candidates = [self.port]
        if self.port:
            candidates.extend(self.port + offset for offset in range(1, FALLBACK_PORT_ATTEMPTS + 1))
        last_error: OSError | None = None
        for port in candidates:
            if not 1 <= port <= 65535:
                continue
            if port == LEGACY_PORT:
                continue
            try:
                return _CompanionServer((self.host, port), _CompanionHandler)
            except OSError as error:
                last_error = error
        # Port zero has no deterministic fallback; retrying zero asks the OS for
        # any currently free port and keeps the API available on locked-down hosts.
        try:
            return _CompanionServer((self.host, 0), _CompanionHandler)
        except OSError:
            raise last_error or OSError("no available companion port")

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return self._server is not None

    def set_port(self, port: int) -> int:
        """Switch to an exact port without dropping the current connection.

        The new listener is bound before the old one is closed, so a busy or
        invalid target leaves the working companion API untouched.
        """
        requested = validate_companion_port(port)
        with self._lifecycle_lock:
            if self._server is None:
                server = _CompanionServer((self.host, requested), _CompanionHandler)
                server.app = self
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="companion-api",
                    daemon=True,
                )
                self._server = server
                self._thread = thread
                self.port = requested
                self.store.set_port(requested)
                thread.start()
                return requested
            if requested == self.port:
                self.store.set_port(requested)
                return requested
            server = _CompanionServer((self.host, requested), _CompanionHandler)
            server.app = self
            thread = threading.Thread(
                target=server.serve_forever,
                name="companion-api",
                daemon=True,
            )
            old_server = self._server
            old_thread = self._thread
            self._server = server
            self._thread = thread
            self.port = requested
            self.store.set_port(requested)
            thread.start()
            if old_server is not None:
                old_server.shutdown()
                old_server.server_close()
            if old_thread is not None:
                old_thread.join(timeout=2.0)
            return requested

    def stop(self) -> None:
        with self._lifecycle_lock:
            server = self._server
            self._server = None
            if server is not None:
                server.shutdown()
                server.server_close()
            thread = self._thread
            self._thread = None
            if thread is not None:
                thread.join(timeout=2.0)

    def log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def status_payload(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        addresses = local_addresses()
        return {
            "enabled": bool(snapshot.get("enabled")),
            "show_intermediate_events": bool(snapshot.get("show_intermediate_events")),
            "port": snapshot.get("port"),
            "discovery_port": snapshot.get("discovery_port"),
            "host": self.host,
            "urls": [f"http://{address}:{snapshot.get('port')}" for address in addresses],
            "phones": snapshot.get("phones") or [],
            "pairing_expires_at": snapshot.get("pairing_expires_at") or 0,
        }

    def allow_pair_attempt(self, client_host: str) -> bool:
        now = time.time()
        with self._pair_lock:
            window = [stamp for stamp in self._pair_attempts.get(client_host, []) if now - stamp < PAIR_WINDOW_SECONDS]
            window.append(now)
            self._pair_attempts[client_host] = window
            return len(window) <= PAIR_MAX_PER_WINDOW


def local_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    return addresses
class _CompanionHandler(BaseHTTPRequestHandler):
    server_version = "CodexBridgeCompanion/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        try:
            self.server.app.log("companion: " + (fmt % args))
        except Exception:  # noqa: BLE001
            pass

    @property
    def app(self) -> CompanionApi:
        return self.server.app

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _error(self, status: int, code: str, message: str = "") -> None:
        payload: dict[str, Any] = {"ok": False, "error": code}
        if message:
            payload["message"] = message
        self._send_json(status, payload)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _token(self, query: dict[str, list[str]]) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        values = query.get("token") or []
        return values[0].strip() if values else ""

    def _authorize(self, query: dict[str, list[str]]):
        if not self.app.store.enabled:
            self._error(403, "companion_disabled", "手机连接已在电脑端关闭")
            return None
        record = self.app.store.authorize(self._token(query))
        if record is None:
            self._error(401, "unauthorized", "配对令牌无效，请重新配对")
            return None
        self.app.store.touch(record.phone_id)
        return record

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/companion/ping":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "codexbridge-companion",
                    "name": socket.gethostname(),
                    "version": VERSION,
                    "requires_pairing": True,
                    "port": self.app.store.port,
                    "discovery_port": self.app.store.discovery_port,
                },
            )
            return
        if path == "/api/companion/events":
            device = self._authorize(query)
            if device is None:
                return
            self._stream_events(query, device)
            return
        device = self._authorize(query)
        if device is None:
            return
        required = ('status' if path == '/api/companion/status' else
                    'tasks' if path not in {'/api/companion/protocol'} else None)
        if required and required not in device.capabilities():
            self._error(403, 'capability_not_enabled')
            return
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        try:
            if path == '/api/companion/protocol':
                self._send_json(200, {'ok': True, **self.app.service.commands.catalog()})
                return
            if path == "/api/companion/status":
                result = self.app.service.snapshot()
                if 'approval' not in device.capabilities():
                    result['pending_approvals'] = []
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/companion/threads":
                limit = int((query.get("limit") or ["40"])[0])
                search = (query.get("search") or [""])[0]
                cursor = (query.get("cursor") or [""])[0]
                result = self.app.service.list_threads(limit=limit, search=search, cursor=cursor)
                self._send_json(200, {"ok": True, **result})
                return
            if path == "/api/companion/models":
                self._send_json(200, {"ok": True, **self.app.service.list_models()})
                return
            if path == "/api/companion/projects":
                self._send_json(200, {"ok": True, **self.app.service.list_projects()})
                return
            if path == "/api/companion/log":
                self._send_json(200, {"ok": True, "entries": self.app.service.event_log()})
                return
            if len(parts) == 4 and parts[:3] == ["api", "companion", "threads"]:
                detail = self.app.service.read_thread(parts[3])
                if 'approval' not in device.capabilities():
                    detail['pending_approvals'] = []
                self._send_json(200, {"ok": True, **detail})
                return
        except ValueError:
            self._error(400, "invalid_request")
            return
        except AppServerError as error:
            self._error(503, "codex_unavailable", str(error))
            return
        except OSError as error:
            self._error(500, "transport_error", str(error))
            return
        self._error(404, "not_found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        if path == "/api/companion/pair":
            self._pair(query)
            return
        device = self._authorize(query)
        if device is None:
            return
        required = ('control' if path.startswith('/api/companion/threads') else
                    'approval' if path.startswith('/api/companion/approvals') else None)
        if required and required not in device.capabilities():
            self._error(403, 'capability_not_enabled')
            return
        try:
            payload = self._body()
        except ValueError:
            self._error(400, "invalid_body")
            return
        try:
            if path == '/api/companion/capabilities':
                enabled = self.app.store.negotiate(device.phone_id, payload.get('supported'))
                self._send_json(200, {'ok': True, 'enabled': enabled})
                return
            if path == '/api/companion/commands':
                result = self.app.service.commands.execute(
                    device.phone_id, device.capabilities(), payload)
                self._send_json(200, result)
                return
            if path == "/api/companion/threads":
                result = self.app.service.start_thread(
                    str(payload.get("cwd") or ""),
                    str(payload.get("model") or ""),
                )
                self._send_json(200, {"ok": True, **result})
                return
            if len(parts) == 5 and parts[:3] == ["api", "companion", "threads"] and parts[4] == "read":
                result = self.app.service.mark_thread_read(parts[3])
                self._send_json(200, {"ok": True, **result})
                return
            if len(parts) == 5 and parts[:3] == ["api", "companion", "threads"] and parts[4] == "messages":
                text = str(payload.get("text") or "")
                model = str(payload.get("model") or "")
                effort = str(payload.get("effort") or "")
                mode = str(payload.get("mode") or "start")
                if mode == "steer":
                    result = self.app.service.steer_turn(parts[3], text)
                elif "queue" in device.capabilities():
                    result = self.app.service.queue_enqueue(
                        parts[3], text, model=model, effort=effort
                    )
                    result["request_id"] = str(payload.get("client_message_id") or "")
                else:
                    result = self.app.service.start_turn(
                        parts[3],
                        text,
                        model=model,
                        effort=effort,
                        client_message_id=str(payload.get("client_message_id") or ""),
                    )
                self._send_json(200, {"ok": True, **result})
                return
            if len(parts) == 5 and parts[:3] == ["api", "companion", "threads"] and parts[4] == "interrupt":
                result = self.app.service.interrupt(parts[3], str(payload.get("turn_id") or ""))
                self._send_json(200, {"ok": True, **result})
                return
            if len(parts) == 5 and parts[:3] == ["api", "companion", "approvals"]:
                request_id = int(parts[4])
                resolved = self.app.service.resolve_approval(request_id, str(payload.get("decision") or ""))
                self._send_json(200, {"ok": True, "resolved": resolved})
                return
        except CommandError as error:
            code = str(error)
            self._error(409 if code in {'request_id_conflict', 'outcome_unknown'} else 400, code)
            return
        except DesktopIpcError as error:
            self._error(503, 'desktop_unavailable', str(error))
            return
        except ValueError as error:
            self._error(400, str(error) or "invalid_request")
            return
        except AppServerError as error:
            self._error(503, "codex_unavailable", str(error))
            return
        except OSError as error:
            self._error(500, "transport_error", str(error))
            return
        self._error(404, "not_found")
    def _pair(self, query: dict[str, list[str]]) -> None:
        if not self.app.store.enabled:
            self._error(403, "companion_disabled", "手机连接已在电脑端关闭")
            return
        client_host = self.client_address[0] if self.client_address else "?"
        if not self.app.allow_pair_attempt(client_host):
            self._error(429, "too_many_attempts", "配对尝试过于频繁，请稍后再试")
            return
        try:
            payload = self._body()
        except ValueError:
            self._error(400, "invalid_body")
            return
        code = str(payload.get("code") or "")
        name = str(payload.get("name") or "")
        client_id = str(payload.get("client_id") or "")
        claimed = self.app.store.claim(code, name, client_id=client_id)
        if claimed is None:
            self._error(403, "pairing_failed", "配对码不正确或已过期")
            return
        self.app.log(f"companion: paired {claimed.get('phone_id')} from {client_host}")
        self._send_json(
            200,
            {
                "ok": True,
                "phone_id": claimed["phone_id"],
                "token": claimed["token"],
                "bridge": {
                    "name": socket.gethostname(),
                    "version": VERSION,
                    "port": self.app.store.port,
                },
            },
        )

    def _body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_header)
        except ValueError as error:
            raise ValueError("content-length") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("content-length")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("json") from error
        if not isinstance(payload, dict):
            raise ValueError("json")
        return payload

    def _stream_events(self, query: dict[str, list[str]], device=None) -> None:
        thread_filter = (query.get("thread_id") or [""])[0]
        after = self.headers.get('Last-Event-ID') or (query.get('after') or [''])[0]
        target = self.app.service.subscribe(after)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        deadline = time.monotonic() + SSE_MAX_SECONDS
        try:
            hello = {"type": "hello", "version": VERSION, "server_time": time.time()}
            self.wfile.write(
                ("retry: 3000\ndata: " + json.dumps(hello, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            current = (
                self.app.store.authorize(self._token(query))
                if device is not None
                else None
            )
            if current is not None and "queue" in current.capabilities():
                for snapshot in self.app.service.queue_snapshots():
                    if thread_filter and snapshot.get("thread_id") != thread_filter:
                        continue
                    data = json.dumps({"type": "queue_snapshot", **snapshot}, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            while time.monotonic() < deadline:
                try:
                    payload = target.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                thread_id = str(payload.get("thread_id") or "")
                if device is not None:
                    current = self.app.store.authorize(self._token(query))
                    if current is None or not self.app.store.enabled:
                        break
                    kind = payload.get('type', '')
                    required = ('queue' if kind.startswith('queue_') else
                                'approval' if kind.startswith('approval_') else
                                'status' if kind.startswith('turn_') or kind == 'resync_required' else 'tasks')
                    if required not in current.capabilities():
                        continue
                if thread_filter and thread_id and thread_id != thread_filter:
                    continue
                data = json.dumps(payload, ensure_ascii=False)
                event_id = payload.get('event_id', '')
                self.wfile.write(f"id: {event_id}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            self.app.service.unsubscribe(target)


class _CompanionServer(ExclusiveBindMixin, ThreadingHTTPServer):
    daemon_threads = True
    app: CompanionApi
