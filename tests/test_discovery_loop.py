"""The discovery loop trades latency for radio wake-ups based on connectivity."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from codexbridge import __main__ as bridge_main


class FakeStop:
    def __init__(self, loops: int = 1) -> None:
        self.remaining = loops
        self.waits: list[float] = []

    def is_set(self) -> bool:
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


class FakeManager:
    def __init__(self, online: int) -> None:
        self.online = online
        self.synced: list[object] = []

    def sync(self, results) -> None:
        self.synced.append(results)

    def online_count(self) -> int:
        return self.online


class DiscoveryLoopTest(unittest.TestCase):
    @patch("codexbridge.__main__.discover", return_value=[])
    def test_scans_quickly_while_no_device_is_online(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=0)

        bridge_main._discovery_loop(manager, stop)

        discover.assert_called_once_with(timeout=0.75)
        self.assertEqual([5.0], stop.waits)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_backs_off_after_a_device_is_online(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=1)

        bridge_main._discovery_loop(manager, stop)

        discover.assert_called_once_with(timeout=0.75)
        self.assertEqual([15.0], stop.waits)


if __name__ == "__main__":
    unittest.main()