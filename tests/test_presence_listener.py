"""A device that announces itself must be dialled without a broadcast round."""

from __future__ import annotations

import json
import socket
import time
import unittest

from codexbridge.devices.discovery import (
    DISCOVERY_PORT,
    PRESENCE_DEDUPE_SECONDS,
    PresenceListener,
)


def identity(host="192.168.0.39", device_id="aipassport-1"):
    return json.dumps(
        {
            "device": "FoloOS-AI-Passport",
            "device_id": device_id,
            "name": "AI Passport",
            "port": 8765,
            "capabilities": ["status", "text"],
        }
    ).encode("utf-8")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PresenceListenerTest(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.port = free_port()
        self.listener = PresenceListener(self.results.append, port=self.port)
        self.listener.start()
        self.addCleanup(self.listener.stop)
        # Give the thread a moment to park in select() before the first packet.
        time.sleep(0.2)

    def send(self, payload: bytes) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, ("127.0.0.1", self.port))

    def _wait(self, count: int, seconds: float = 3.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if len(self.results) >= count:
                return True
            time.sleep(0.05)
        return len(self.results) >= count

    def test_announcement_is_delivered_with_the_endpoint(self):
        self.send(identity())
        self.assertTrue(self._wait(1), "no announcement delivered")
        result = self.results[0]
        self.assertEqual("aipassport-1", result.device_id)
        self.assertEqual(8765, result.port)
        self.assertTrue(result.host)

    def test_repeats_collapse_into_one_delivery(self):
        self.send(identity())
        self.assertTrue(self._wait(1))
        self.send(identity())
        self.send(identity())
        time.sleep(0.5)
        self.assertEqual(1, len(self.results))

    def test_garbage_is_ignored(self):
        self.send(b"not json at all")
        self.send(json.dumps({"nope": True}).encode("utf-8"))
        time.sleep(0.5)
        self.assertEqual([], self.results)

    def test_dedupe_window_is_not_zero(self):
        # A window of zero would let every retry datagram start a fresh dial.
        self.assertGreater(PRESENCE_DEDUPE_SECONDS, 0.0)


if __name__ == "__main__":
    unittest.main()
