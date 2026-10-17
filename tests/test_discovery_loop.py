"""The discovery loop trades latency for radio wake-ups based on connectivity."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from codexbridge import __main__ as bridge_main
from codexbridge.devices.discovery import DISCOVERY_TIMEOUT_SECONDS


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
    def __init__(self, online: int, paired: int = 0) -> None:
        self.online = online
        self.paired = paired
        self.synced: list[object] = []
        self.redials = 0

    def sync(self, results) -> None:
        self.synced.append(results)

    def redial_paired(self) -> int:
        self.redials += 1
        return 1 if self.paired else 0

    def online_count(self) -> int:
        return self.online

    def paired_count(self) -> int:
        return self.paired


class DiscoveryLoopTest(unittest.TestCase):
    @patch("codexbridge.__main__.discover", return_value=[])
    def test_listens_longer_than_a_power_save_interval(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=0)

        bridge_main._discovery_loop(manager, stop)

        discover.assert_called_once_with(timeout=DISCOVERY_TIMEOUT_SECONDS)
        # An ESP32-C3 station in modem sleep reports a 1.024 s listen interval;
        # a shorter window loses replies from a device that is there and ready.
        self.assertGreaterEqual(DISCOVERY_TIMEOUT_SECONDS, 2.0)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_scans_at_a_quiet_beat_while_nothing_is_connected(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=0)

        bridge_main._discovery_loop(manager, stop)

        self.assertEqual([5.0], stop.waits)
        self.assertEqual(1, manager.redials)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_a_waiting_device_still_gets_a_broadcast_safety_net(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=0, paired=1)

        bridge_main._discovery_loop(manager, stop)

        # The device announces itself, so the loop does not have to poll fast to
        # make the first entry feel instant; five seconds only bounds the case
        # where that datagram is lost.
        self.assertEqual([5.0], stop.waits)
        self.assertEqual(1, manager.redials)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_a_waiting_device_is_not_starved_by_an_online_one(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=1, paired=1)

        bridge_main._discovery_loop(manager, stop)

        # A phone with a healthy session used to push the cadence to 15 s, which
        # starved a passport that had just opened its listener: the user watched
        # "not connected" for a dozen seconds. The waiting device keeps the
        # safety-net beat.
        self.assertEqual([5.0], stop.waits)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_backs_off_once_nothing_is_waiting(self, discover) -> None:
        stop = FakeStop()
        manager = FakeManager(online=1, paired=0)

        bridge_main._discovery_loop(manager, stop)

        self.assertEqual([15.0], stop.waits)

    @patch("codexbridge.__main__.discover", return_value=[])
    def test_remembers_the_endpoint_before_the_broadcast(self, discover) -> None:
        stop = FakeStop()
        order: list[str] = []
        manager = FakeManager(online=0, paired=1)
        manager.redial_paired = lambda: order.append("redial")
        discover.side_effect = lambda **kwargs: order.append("broadcast")

        bridge_main._discovery_loop(manager, stop)

        # The remembered address answers in milliseconds; spending an
        # announcement window first only delays the connect the user is waiting
        # on, and discovery still runs afterwards to catch a new or moved device.
        self.assertEqual(["redial", "broadcast"], order)


if __name__ == "__main__":
    unittest.main()
