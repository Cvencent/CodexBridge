import socket
import threading
import unittest

from codexbridge.devices.discovery import (
    DISCOVERY_PORT,
    DISCOVERY_REQUEST,
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

    def test_reply_without_device_id_is_ignored(self):
        self.assertIsNone(
            parse_reply(b'{"device":"dotii","port":8765}', "10.0.0.5")
        )

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_reply(b"not json", "10.0.0.7"))

    def test_loopback_round_trip(self):
        stop = threading.Event()
        ready = threading.Event()
        requests = []

        def responder():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", DISCOVERY_PORT))
            sock.settimeout(0.2)
            ready.set()
            while not stop.is_set():
                try:
                    payload, source = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                requests.append(payload)
                reply = (
                    b'{"device":"dotii","device_id":"a1","name":"Desk",'
                    b'"port":8765,"capabilities":["status","tasks"]}'
                )
                sock.sendto(reply, source)
            sock.close()

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1.0))
        try:
            results = discover(timeout=1.0, targets=("127.0.0.1",))
        finally:
            stop.set()
            thread.join(timeout=1.0)
        self.assertEqual([result.device_id for result in results], ["a1"])
        self.assertEqual(requests, [DISCOVERY_REQUEST])
