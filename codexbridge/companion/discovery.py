"""UDP discovery responder so phones can find this PC on the LAN."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from codexbridge import VERSION
from codexbridge.companion.store import CompanionStore

DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"CODEXBRIDGE_COMPANION_DISCOVER"
MAX_REPLY_BYTES = 512


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
        self.port = port
        self._logger = logger
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="companion-discovery", daemon=True)
        self._thread.start()

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
            "port": self.store.port,
            "discovery_port": self.port,
            "requires_pairing": True,
        }

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as error:
            self._log(f"companion discovery bind failed: {error}")
            sock.close()
            return
        sock.settimeout(1.0)
        self._socket = sock
        while not self._stop.is_set():
            try:
                data, address = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != DISCOVERY_REQUEST:
                continue
            if not self.store.enabled:
                continue
            try:
                payload = json.dumps(self._reply(), ensure_ascii=False).encode("utf-8")[:MAX_REPLY_BYTES]
                sock.sendto(payload, address)
            except OSError:
                continue
        try:
            sock.close()
        except OSError:
            pass