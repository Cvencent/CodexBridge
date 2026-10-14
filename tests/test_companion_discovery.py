from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbridge.companion.discovery import (
    DISCOVERY_REQUEST,
    CompanionDiscovery,
)
from codexbridge.companion.store import CompanionStore


class CompanionDiscoveryTests(unittest.TestCase):
    def test_bind_retries_after_address_is_in_use(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]

        store = CompanionStore(Path(tempfile.mkdtemp()) / "companion.json")
        discovery = CompanionDiscovery(store, host="127.0.0.1", port=port)
        discovery.start()
        time.sleep(0.1)
        holder.close()

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3.0)
        try:
            deadline = time.monotonic() + 3.0
            reply = None
            while time.monotonic() < deadline:
                client.sendto(DISCOVERY_REQUEST, ("127.0.0.1", port))
                try:
                    reply = client.recvfrom(1024)[0]
                    break
                except socket.timeout:
                    continue
            self.assertIsNotNone(reply)
            self.assertIn(b"codexbridge-companion", reply or b"")
        finally:
            client.close()
            discovery.stop()


if __name__ == "__main__":
    unittest.main()