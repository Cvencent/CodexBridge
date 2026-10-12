import unittest

from codexbridge.devices.session import DeviceSession
from tests.fake_device import FakeDevice


class SessionTest(unittest.TestCase):
    def test_unified_handshake_and_heartbeat(self):
        seen = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                device_id="fake-device",
                on_device_info=lambda info: (
                    seen.append(info) or {"status", "text"}
                ),
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(device.received[0]["type"], "bridge_auth")
        self.assertEqual(device.received[1]["type"], "bridge_ready")
        self.assertEqual(device.received[1]["capabilities"], ["status", "text"])
        self.assertEqual(seen[0]["device_id"], "fake-device")

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
                device_id="fake-device",
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
                device_id="fake-device",
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
                device_id="fake-device",
                on_event=seen.append,
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(seen[0]["message_id"], "m1")

    def test_identity_mismatch_is_rejected(self):
        online = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(
                device.host,
                device.port,
                "abcdefghijklmnop",
                device_id="another-id",
                on_online=lambda: online.append(True),
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(online, [])

    def test_token_mismatch_does_not_go_online(self):
        online = []
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            session = DeviceSession(
                device.host,
                device.port,
                "wrong-token-abcdef",
                device_id="fake-device",
                on_online=lambda: online.append(True),
                heartbeat_seconds=0.2,
                timeout_seconds=1.0,
            )
            session.run_once()
        self.assertEqual(online, [])
        self.assertEqual(device.received[0]["type"], "bridge_auth")
