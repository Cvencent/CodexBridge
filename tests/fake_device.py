from __future__ import annotations

import socketserver
import threading
import time
from collections.abc import Callable

from codexbridge.protocol.messages import decode, encode


class _FakeDeviceServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, device):
        self.device = device
        super().__init__(server_address, handler_class)


class _FakeDeviceHandler(socketserver.BaseRequestHandler):
    def handle(self):
        device = self.server.device
        buffer = bytearray()
        authenticated = False
        announced = False

        while not device._stopped.is_set():
            try:
                payload = self.request.recv(4096)
            except OSError:
                return
            if not payload:
                return
            buffer.extend(payload)

            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                message = decode(bytes(line) + b"\n")
                if message is None:
                    continue
                device._append_received(message)

                message_type = message.get("type")
                if message_type == "bridge_auth":
                    if message.get("token") != device._expected_token:
                        return
                    self.request.sendall(encode({"type": "bridge_auth_ok", **device._device_info()}))
                    authenticated = True
                    with device._connection_lock:
                        device._connection = self.request
                elif message_type == "bridge_ready" and authenticated:
                    if not announced:
                        announced = True
                        device._run_after_handshake()
                    if device._stalled:
                        continue
                    return


class FakeDevice:
    def __init__(self, host: str = "127.0.0.1", device_id: str = "fake-device",
                 capabilities: list[str] | None = None,
                 max_text_chars: int | None = None):
        self.host = host
        self.device_id = device_id
        # A device announces its own capabilities and its own body budget; the
        # host must honour both without per-device branches.
        self.model = "dotii"
        self._capabilities = list(capabilities) if capabilities else [
            "status", "text", "read_ack"]
        self.max_text_chars = max_text_chars
        self._expected_token: str | None = None
        self._stalled = False
        self._callbacks: list[Callable[[], None]] = []
        self._connection_lock = threading.Lock()
        self._connection = None
        self._received_lock = threading.Lock()
        self.received: list[dict] = []
        self._stopped = threading.Event()
        self._server = _FakeDeviceServer(
            (host, 0),
            _FakeDeviceHandler,
            self,
        )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stopped.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(2)
            except OSError:
                pass
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)

    def expect_handshake(self, token: str) -> None:
        self._expected_token = token

    def after_handshake(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def stall(self) -> None:
        self._stalled = True

    def send_event(self, event: dict) -> None:
        payload = encode(event)
        with self._connection_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("device is not authenticated")
            connection.sendall(payload)

    def send_raw_split(self, payload: bytes) -> None:
        midpoint = max(1, len(payload) // 2)
        with self._connection_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("device is not authenticated")
            connection.sendall(payload[:midpoint])
            time.sleep(0.01)
            connection.sendall(payload[midpoint:])

    def _append_received(self, message: dict) -> None:
        with self._received_lock:
            self.received.append(message)

    def _run_after_handshake(self) -> None:
        for callback in tuple(self._callbacks):
            callback()

    def _device_info(self) -> dict:
        info = {
            "device_id": self.device_id,
            "model": self.model,
            "name": "Fake Device",
            "firmware": "test",
            "capabilities": list(self._capabilities),
        }
        if self.max_text_chars is not None:
            info["max_text_chars"] = self.max_text_chars
        return info
