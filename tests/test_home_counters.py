import unittest

from codexbridge import __main__ as bridge_main
from codexbridge.cbdp.constants import (
    FLAG_EVENT,
    QOS_STATE,
    TAG_COMPLETED_UNREAD,
    TAG_FAILED_UNREAD,
    TAG_RUNNING_COUNT,
    TYPE_HOME_COUNTERS,
)
from codexbridge.cbdp.frame import decode_frame
from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
from codexbridge.cbdp.tlv import decode_u16
from codexbridge.devices.discovery import DISCOVERY_PORT
from codexbridge.devices.home_counters import (
    HomeCounterSender,
    build_home_counters_frame,
)


class HomeCounterFrameTests(unittest.TestCase):
    def test_frame_carries_the_three_aggregate_counters(self) -> None:
        raw = build_home_counters_frame(
            {"running": 3, "completed_unread": 5, "failed_unread": 1}
        )

        frame = decode_frame(raw)

        self.assertEqual(frame.message_type, TYPE_HOME_COUNTERS)
        self.assertEqual(frame.flags & FLAG_EVENT, FLAG_EVENT)
        self.assertEqual(frame.qos, QOS_STATE)
        self.assertEqual(frame.topic_id, SYSTEM_THREAD_SUMMARIES_TOPIC)
        self.assertEqual(decode_u16(frame.header[TAG_RUNNING_COUNT]), 3)
        self.assertEqual(decode_u16(frame.header[TAG_COMPLETED_UNREAD]), 5)
        self.assertEqual(decode_u16(frame.header[TAG_FAILED_UNREAD]), 1)

    def test_frame_fits_in_a_single_small_datagram(self) -> None:
        raw = build_home_counters_frame(
            {"running": 4, "completed_unread": 9, "failed_unread": 2}
        )
        self.assertLess(len(raw), 256)

    def test_negative_counts_are_clamped(self) -> None:
        frame = decode_frame(
            build_home_counters_frame(
                {"running": -1, "completed_unread": None, "failed_unread": "x"}
            )
        )
        self.assertEqual(decode_u16(frame.header[TAG_RUNNING_COUNT]), 0)
        self.assertEqual(decode_u16(frame.header[TAG_COMPLETED_UNREAD]), 0)
        self.assertEqual(decode_u16(frame.header[TAG_FAILED_UNREAD]), 0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HomeCounterSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counts = {"running": 1, "completed_unread": 0, "failed_unread": 0}
        self.sent = []
        self.clock = FakeClock()
        self.sender = HomeCounterSender(
            lambda: dict(self.counts),
            port=DISCOVERY_PORT,
            interval_seconds=2.0,
            clock=self.clock,
            sendto=lambda frame, endpoint: self.sent.append((frame, endpoint)),
        )

    def test_register_and_push_unicasts_to_the_device(self) -> None:
        self.sender.register("dev-1", "192.168.0.39")

        sent = self.sender.push()

        self.assertEqual(sent, 1)
        frame, endpoint = self.sent[0]
        self.assertEqual(endpoint, ("192.168.0.39", DISCOVERY_PORT))
        self.assertEqual(decode_frame(frame).message_type, TYPE_HOME_COUNTERS)

    def test_broadcast_addresses_are_never_used(self) -> None:
        self.sender.register("dev-1", "192.168.0.39")
        self.sender.register("dev-2", "192.168.0.40")

        self.sender.push()

        for _frame, endpoint in self.sent:
            self.assertNotIn(endpoint[0], {"255.255.255.255", "<broadcast>"})

    def test_unchanged_counts_are_not_resent_until_the_interval(self) -> None:
        self.sender.register("dev-1", "192.168.0.39")
        self.assertEqual(self.sender.push(force=False), 1)

        self.assertEqual(self.sender.push(force=False), 0)

        self.clock.now += 2.5
        self.assertEqual(self.sender.push(force=False), 1)

    def test_changed_counts_push_immediately(self) -> None:
        self.sender.register("dev-1", "192.168.0.39")
        self.sender.push(force=False)
        before = len(self.sent)

        self.counts["running"] = 2

        self.assertEqual(self.sender.push(force=False), 1)
        self.assertEqual(len(self.sent), before + 1)

    def test_unregistered_device_is_not_targeted(self) -> None:
        self.assertEqual(self.sender.push(), 0)

    def test_unregister_stops_delivery(self) -> None:
        self.sender.register("dev-1", "192.168.0.39")
        self.sender.push()
        self.sender.unregister("dev-1")

        self.assertEqual(self.sender.push(), 0)


class DiscoveryLoopIntegrationTests(unittest.TestCase):
    def test_presence_registers_a_home_counter_target(self) -> None:
        import socket
        import time

        from codexbridge.devices.discovery import PresenceListener

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        received = []
        listener = PresenceListener(
            lambda result: received.append(result), port=port
        )
        listener.start()
        self.addCleanup(listener.stop)
        time.sleep(0.2)
        payload = (
            b'{"device":"FoloOS-AI-Passport","device_id":"aipassport-1",'
            b'"name":"AI Passport","port":8765,'
            b'"capabilities":["status"]}'
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, ("127.0.0.1", port))
        deadline = time.time() + 3.0
        while time.time() < deadline and not received:
            time.sleep(0.05)
        self.assertTrue(received)


if __name__ == "__main__":
    unittest.main()
