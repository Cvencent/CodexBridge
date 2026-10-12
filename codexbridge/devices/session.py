from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable, Iterable

from codexbridge import VERSION
from codexbridge.protocol.messages import decode, encode


class DeviceSession:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        device_id: str,
        on_event: Callable[[dict], None] | None = None,
        on_device_info: Callable[[dict], Iterable[str]] | None = None,
        on_online: Callable[[], None] | None = None,
        on_offline: Callable[[], None] | None = None,
        heartbeat_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
    ):
        if len(token) < 16:
            raise ValueError("token must be at least 16 characters")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.host = host
        self.port = port
        self.token = token
        self.device_id = device_id
        self.on_event = on_event
        self.on_device_info = on_device_info
        self.on_online = on_online
        self.on_offline = on_offline
        self._active_capabilities: set[str] = set()
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stopping = threading.Event()

    def send(self, message: dict) -> None:
        with self._send_lock:
            if self._socket is None:
                raise RuntimeError("session is not connected")
            self._socket.sendall(encode(message))

    def stop(self) -> None:
        self._stopping.set()
        with self._send_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run_once(self) -> None:
        online = False
        buffer = bytearray()
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with self._send_lock:
                self._socket = sock

            self.send({"type": "bridge_auth", "token": self.token})
            reply, closed = self._next_message(
                sock,
                buffer,
                time.monotonic() + self.timeout_seconds,
            )
            if closed or reply is None or reply.get("type") != "bridge_auth_ok":
                return
            if reply.get("device_id") != self.device_id:
                return
            self._active_capabilities = (
                set(self.on_device_info(reply))
                if self.on_device_info is not None
                else set()
            )
            self.send(self._ready_message(self._active_capabilities))

            online = True
            if self.on_online is not None:
                self.on_online()

            last_received = time.monotonic()
            next_heartbeat = last_received + self.heartbeat_seconds
            while True:
                if self._stopping.is_set():
                    return
                self._drain_buffer(buffer, self.on_event)
                now = time.monotonic()
                if now >= next_heartbeat:
                    try:
                        self.send(self._ready_message(self._active_capabilities))
                    except OSError:
                        return
                    next_heartbeat = now + self.heartbeat_seconds

                readable, _, _ = select.select(
                    [sock],
                    [],
                    [],
                    max(0.0, next_heartbeat - time.monotonic()),
                )
                if not readable:
                    continue
                try:
                    payload = sock.recv(4096)
                except OSError:
                    return
                if not payload:
                    return
                buffer.extend(payload)
                self._drain_buffer(buffer, self.on_event)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._send_lock:
                self._socket = None
            if online and self.on_offline is not None:
                self.on_offline()

    @staticmethod
    def _ready_message(capabilities: Iterable[str]) -> dict:
        return {
            "type": "bridge_ready",
            "bridge": "CodexBridge",
            "version": VERSION,
            "capabilities": sorted(capabilities),
        }

    @staticmethod
    def _drain_buffer(
        buffer: bytearray,
        on_event: Callable[[dict], None] | None,
    ) -> None:
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            del buffer[:]
            buffer.extend(remainder)
            message = decode(bytes(line) + b"\n")
            if message is not None and on_event is not None:
                on_event(message)

    @staticmethod
    def _next_message(
        sock: socket.socket,
        buffer: bytearray,
        deadline: float,
    ) -> tuple[dict | None, bool]:
        while True:
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                del buffer[:]
                buffer.extend(remainder)
                message = decode(bytes(line) + b"\n")
                if message is not None:
                    return message, False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, False
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                return None, False
            try:
                payload = sock.recv(4096)
            except OSError:
                return None, True
            if not payload:
                return None, True
            buffer.extend(payload)
