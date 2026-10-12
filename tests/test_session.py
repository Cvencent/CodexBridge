import unittest

from codexbridge.devices.session import DeviceSession
from tests.fake_device import FakeDevice


class SessionTest(unittest.TestCase):
    def test_handshake_and_heartbeat(self):
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
            self.assertTrue(any(m["type"] == "bridge_ready" for m in device.received))

    def test_device_event_reaches_callback(self):
        seen = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.after_handshake(
                lambda: device.send_event({"type": "read_ack", "message_id": "m1"})
            )
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                on_event=seen.append,
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(seen[0]["message_id"], "m1")

    def test_stalled_device_goes_offline(self):
        states = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.stall()
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                on_offline=lambda: states.append("offline"),
                heartbeat_seconds=0.1,
                timeout_seconds=0.5,
            )
            session.run_once()
        self.assertIn("offline", states)

    def test_line_split_across_packets_is_reassembled(self):
        seen = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.after_handshake(
                lambda: device.send_raw_split(
                    b'{"type":"read_ack","message_id":"m1"}' + bytes([10])
                )
            )
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                on_event=seen.append,
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(seen[0]["message_id"], "m1")

    def test_v1_authenticates_without_waiting_for_hello(self):
        with FakeDevice(protocol=1) as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                protocol=1,
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(device.received[0]["type"], "bridge_auth")
        self.assertFalse(any(message["type"] == "hello_ok" for message in device.received))
