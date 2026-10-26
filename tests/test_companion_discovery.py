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
    def _find_free_udp_port(self) -> int:
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.close()
        return port

    def _query(self, port: int):
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.4)
        try:
            client.sendto(DISCOVERY_REQUEST, ("127.0.0.1", port))
            try:
                return client.recvfrom(1024)[0]
            except (socket.timeout, ConnectionResetError, OSError):
                return None
        finally:
            client.close()

    def test_discovery_reply_carries_stable_bridge_identity(self) -> None:
        store = CompanionStore(Path(tempfile.mkdtemp()) / "companion.json")
        store.load()
        discovery = CompanionDiscovery(store, host="127.0.0.1", port=0)

        reply = discovery._reply()

        self.assertEqual(reply["bridge_instance_id"], store.instance_id)
        self.assertEqual(reply["device_id"], store.device_id)

    def test_set_port_rebinds_the_udp_listener_and_persists_it(self) -> None:
        path = Path(tempfile.mkdtemp()) / "companion.json"
        store = CompanionStore(path)
        store.load()
        discovery = CompanionDiscovery(store, host="127.0.0.1", port=self._find_free_udp_port())
        discovery.start()
        time.sleep(0.1)
        target = self._find_free_udp_port()
        try:
            discovery.set_port(target)
            deadline = time.monotonic() + 2.0
            reply = None
            while time.monotonic() < deadline:
                reply = self._query(target)
                if reply is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(reply)
            self.assertEqual(store.snapshot()["discovery_port"], target)
        finally:
            discovery.stop()

    def test_set_port_failure_keeps_the_old_listener_and_config(self) -> None:
        path = Path(tempfile.mkdtemp()) / "companion.json"
        store = CompanionStore(path)
        store.load()
        original = self._find_free_udp_port()
        store.set_discovery_port(original)
        discovery = CompanionDiscovery(store, host="127.0.0.1", port=original)
        discovery.start()
        time.sleep(0.1)

        try:
            with patch.object(discovery, "_bind", return_value=None):
                with self.assertRaises(OSError):
                    discovery.set_port(self._find_free_udp_port())
            self.assertEqual(store.snapshot()["discovery_port"], original)
            deadline = time.monotonic() + 2.0
            reply = None
            while time.monotonic() < deadline:
                reply = self._query(original)
                if reply is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(reply)
        finally:
            discovery.stop()

    def test_bind_retries_after_address_is_in_use(self) -> None:
        # An upgrade can leave the old process holding the port for a moment.
        # The listener must keep trying; a probe that lands in that gap only
        # owes the caller a reset, not a dead discovery.
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]

        store = CompanionStore(Path(tempfile.mkdtemp()) / "companion.json")
        discovery = CompanionDiscovery(store, host="127.0.0.1", port=port)
        discovery.start()
        time.sleep(0.1)
        self.assertFalse(discovery.running)
        holder.close()

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.5)
        try:
            deadline = time.monotonic() + 5.0
            reply = None
            while time.monotonic() < deadline:
                client.sendto(DISCOVERY_REQUEST, ("127.0.0.1", port))
                try:
                    reply = client.recvfrom(1024)[0]
                    break
                except socket.timeout:
                    continue
                except OSError:
                    # Windows reports an earlier lost probe as a reset here.
                    continue
            self.assertIsNotNone(reply)
            self.assertIn(b"codexbridge-companion", reply or b"")
            self.assertTrue(discovery.running)
        finally:
            client.close()
            discovery.stop()


if __name__ == "__main__":
    unittest.main()
