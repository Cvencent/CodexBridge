import socket
import threading
import unittest
from unittest import mock

from codexbridge.devices.discovery import (
    DISCOVERY_PORT,
    DISCOVERY_REQUEST,
    _broadcast_targets,
    discover,
    parse_reply,
)


class DiscoveryTest(unittest.TestCase):
    def test_parse_unified_reply(self):
        result = parse_reply(
            b'{"device":"dotii","device_id":"a1","name":"Desk","port":8765,'
            b'"capabilities":["status","tasks"]}',
            "10.0.0.5",
        )
        self.assertEqual(
            (result.device_id, result.name, result.port),
            ("a1", "Desk", 8765),
        )
        self.assertEqual(result.capabilities, ["status", "tasks"])

    def test_reply_without_registration_fields_is_approved(self):
        result = parse_reply(
            b'{"device":"dotii","device_id":"old","port":8765}',
            "10.0.0.5",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.registration_state, "approved")
        self.assertIsNone(result.registration_nonce)

    def test_pending_reply_requires_a_valid_nonce(self):
        missing = parse_reply(
            b'{"device":"dotii","device_id":"new","port":8765,'
            b'"registration_state":"pending"}',
            "10.0.0.5",
        )
        too_short = parse_reply(
            b'{"device":"dotii","device_id":"new","port":8765,'
            b'"registration_state":"pending","registration_nonce":"short"}',
            "10.0.0.5",
        )
        pending = parse_reply(
            b'{"device":"dotii","device_id":"new","port":8765,'
            b'"registration_state":"pending","registration_nonce":"r_0123456789abcdef"}',
            "10.0.0.5",
        )

        self.assertIsNone(missing)
        self.assertIsNone(too_short)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.registration_state, "pending")
        self.assertEqual(pending.registration_nonce, "r_0123456789abcdef")

    def test_reply_without_device_id_is_ignored(self):
        self.assertIsNone(
            parse_reply(b'{"device":"dotii","port":8765}', "10.0.0.5")
        )

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_reply(b"not json", "10.0.0.7"))

    def test_default_targets_include_subnet_broadcast(self):
        # Some hosts (ESP-IDF lwIP) drop the limited 255.255.255.255 request,
        # so discovery must also probe the interface subnet broadcast.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            targets = _broadcast_targets(probe)

        self.assertIn("255.255.255.255", targets)
        self.assertTrue(
            any(target.endswith(".255") and target != "255.255.255.255" for target in targets)
        )

    def test_loopback_round_trip(self):
        stop = threading.Event()
        ready = threading.Event()
        requests = []
        responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        responder.bind(("127.0.0.1", 0))
        port = responder.getsockname()[1]
        responder.settimeout(0.2)

        def serve():
            ready.set()
            while not stop.is_set():
                try:
                    payload, source = responder.recvfrom(1024)
                except socket.timeout:
                    continue
                requests.append(payload)
                reply = (
                    b'{"device":"dotii","device_id":"a1","name":"Desk",'
                    b'"port":8765,"capabilities":["status","tasks"]}'
                )
                responder.sendto(reply, source)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1.0))
        try:
            with unittest.mock.patch(
                "codexbridge.devices.discovery.DISCOVERY_PORT", port
            ):
                results = discover(timeout=1.0, targets=("127.0.0.1",))
        finally:
            stop.set()
            thread.join(timeout=1.0)
            responder.close()
        self.assertEqual([result.device_id for result in results], ["a1"])
        self.assertEqual(requests, [DISCOVERY_REQUEST])
