"""UDP discovery responder so phones can find this PC on the LAN."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from codexbridge import VERSION
from codexbridge.companion.store import CompanionStore, DEFAULT_DISCOVERY_PORT
from codexbridge.net_gate import configure_exclusive

DISCOVERY_PORT = DEFAULT_DISCOVERY_PORT
DISCOVERY_REQUEST = b"CODEXBRIDGE_COMPANION_DISCOVER"
MAX_REPLY_BYTES = 512
BIND_RETRY_SECONDS = 1.0


class CompanionDiscovery:
    def __init__(
        self,
        store: CompanionStore,
        host: str = "0.0.0.0",
        port: int = DISCOVERY_PORT,
        logger=None,
    ) -> None:
        self.store = store
        self.host = host
        self.port = int(port if port is not None else store.discovery_port)
        self._logger = logger
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def set_port(self, port: int) -> None:
        """Switch the UDP listener to an exact port, preserving the old one on failure."""
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port_invalid")
        with self._lock:
            if port == self.port and self._socket is not None:
                self.store.set_discovery_port(port)
                return
            previous = (self.port, self._socket, self._thread)
            was_running = self._thread is not None
            if was_running:
                self.stop()
            self.port = port
            self._stop = threading.Event()
            probe = self._bind()
            if probe is None:
                self.port, self._socket, self._thread = previous
                if was_running:
                    self._stop = threading.Event()
                    self._thread = threading.Thread(
                        target=self._loop,
                        name="companion-discovery",
                        daemon=True,
                    )
                    self._thread.start()
                raise OSError("port_in_use")
            self._socket = probe
            self.store.set_discovery_port(port)
            self._thread = threading.Thread(
                target=self._serve,
                args=(probe,),
                name="companion-discovery",
                daemon=True,
            )
            self._thread.start()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="companion-discovery", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._socket is not None

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def _reply(self) -> dict[str, Any]:
        return {
            "service": "codexbridge-companion",
            "name": socket.gethostname(),
            "version": VERSION,
            "device_id": self.store.device_id,
            "bridge_instance_id": self.store.instance_id,
            "port": self.store.port,
            "discovery_port": self.port,
            "requires_pairing": True,
        }

    def _bind(self) -> socket.socket | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        configure_exclusive(sock)
        try:
            sock.bind((self.host, self.port))
        except OSError as error:
            sock.close()
            self._log(f"companion discovery bind failed: {error}")
            return None
        sock.settimeout(1.0)
        return sock

    def _serve(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, address = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError as error:
                if self._stop.is_set():
                    break
                self._log(f"companion discovery receive failed: {error}")
                continue
            if data.strip() != DISCOVERY_REQUEST:
                continue
            if not self.store.enabled:
                continue
            try:
                payload = json.dumps(self._reply(), ensure_ascii=False).encode("utf-8")[:MAX_REPLY_BYTES]
                sock.sendto(payload, address)
            except OSError:
                continue

    def _loop(self) -> None:
        # During an upgrade the previous process can still hold this port for a
        # moment. Keep retrying instead of silently disabling phone discovery.
        while not self._stop.is_set():
            sock = self._bind()
            if sock is None:
                self._stop.wait(BIND_RETRY_SECONDS)
                continue
            self._socket = sock
            try:
                self._serve(sock)
            finally:
                self._socket = None
                try:
                    sock.close()
                except OSError:
                    pass
            if not self._stop.is_set():
                self._stop.wait(BIND_RETRY_SECONDS)
