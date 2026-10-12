import socket
import threading
import unittest

from codexbridge.devices.discovery import (
    DISCOVERY_PORT,
    LEGACY_DISCOVERY_REQUEST,
    discover,
    parse_reply,
)


class DiscoveryTest(unittest.TestCase):
    def test_parse_v2_reply(self):
        result = parse_reply(
            b'{"device":"dotii","device_id":"a1","port":8765,"protocol":2,"capabilities":["status"]}',
            "10.0.0.5",
        )
        self.assertEqual((result.protocol, result.device_id, result.port), (2, "a1", 8765))
        self.assertEqual(result.capabilities, ["status"])

    def test_parse_v1_reply_without_protocol(self):
        result = parse_reply(
            b'{"device":"FoloOS-AI-Passport","port":8765}',
            "10.0.0.6",
        )
        self.assertEqual(result.protocol, 1)
        self.assertEqual(result.capabilities, [])

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_reply(b"not json", "10.0.0.7"))

    def test_loopback_round_trip(self):
        stop = threading.Event()
        requests = []

        def responder():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", DISCOVERY_PORT))
            sock.settimeout(0.2)
            while not stop.is_set():
                try:
                    payload, source = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                requests.append(payload)
                if payload == LEGACY_DISCOVERY_REQUEST:
                    reply = b'{"device":"FoloOS-AI-Passport","port":8765}'
                else:
                    reply = (
                        b'{"device":"dotii","device_id":"a1","port":8765,'
                        b'"protocol":2}'
                    )
                sock.sendto(reply, source)
            sock.close()

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        try:
            results = discover(timeout=1.0, targets=("127.0.0.1",))
        finally:
            stop.set()
            thread.join(timeout=1.0)
        self.assertEqual(
            {result.device_id for result in results},
            {"a1", "legacy:127.0.0.1"},
        )
        self.assertIn(LEGACY_DISCOVERY_REQUEST, requests)
