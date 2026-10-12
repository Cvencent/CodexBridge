from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable

from codexbridge import VERSION
from codexbridge.protocol.messages import decode, encode


class DeviceSession:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        protocol: int = 2,
        device_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_online: Callable[[], None] | None = None,
        on_offline: Callable[[], None] | None = None,
        heartbeat_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
    ):
        if len(token) < 16:
            raise ValueError("token must be at least 16 characters")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.host = host
        self.port = port
        self.token = token
        self.protocol = protocol
        self.device_id = device_id
        self.on_event = on_event
        self.on_online = on_online
        self.on_offline = on_offline
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self._send_lock:
            if self._socket is None:
                raise RuntimeError("session is not connected")
            self._socket.sendall(encode(message))

    def run_once(self) -> None:
        online = False
        buffer = bytearray()
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            )
            with self._send_lock:
                self._socket = sock

            hello = {
                "type": "hello",
                "protocol": self.protocol,
                "device_id": self.device_id or "",
                "model": "",
                "name": "",
                "firmware": "",
                "capabilities": [],
            }
            self.send(hello)
            reply, closed = self._next_message(
                sock,
                buffer,
                time.monotonic() + self.timeout_seconds,
            )
            if closed or reply is None or reply.get("type") != "hello_ok":
                return

            self.send({"type": "bridge_auth", "token": self.token})
            reply, closed = self._next_message(
                sock,
                buffer,
                time.monotonic() + self.timeout_seconds,
            )
            if closed or reply is None or reply.get("type") != "bridge_auth_ok":
                return

            online = True
            if self.on_online is not None:
                self.on_online()

            last_received = time.monotonic()
            next_heartbeat = last_received + self.heartbeat_seconds
            while True:
                now = time.monotonic()
                if now - last_received >= self.timeout_seconds:
                    return
                if now >= next_heartbeat:
                    try:
                        self.send({"type": "bridge_ready"})
                    except OSError:
                        return
                    next_heartbeat = now + self.heartbeat_seconds

                wait_until = min(
                    next_heartbeat,
                    last_received + self.timeout_seconds,
                )
                readable, _, _ = select.select(
                    [sock],
                    [],
                    [],
                    max(0.0, wait_until - time.monotonic()),
                )
                if not readable:
                    continue
                try:
                    payload = sock.recv(4096)
                except OSError:
                    return
                if not payload:
                    return
                last_received = time.monotonic()
                buffer.extend(payload)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    message = decode(bytes(line) + b"\n")
                    if message is not None and self.on_event is not None:
                        self.on_event(message)
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
