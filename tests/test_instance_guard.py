"""A second CodexBridge must refuse to start instead of fighting for the devices.

Two hosts dial the same device, and a device holds exactly one TCP session, so
the newer dial tears the older one down mid-transfer. That looked like a Codex
recording that died one second after it started.
"""

from __future__ import annotations

import http.server
import json
import threading
import unittest

from codexbridge.__main__ import _acquire_instance_lock, _another_instance_is_running


class FakeAdmin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "version": "1.4.4", "device_count": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class InstanceGuardTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), FakeAdmin)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # addCleanup runs last-registered-first, so this gives
        # shutdown -> join -> server_close and the selector never wakes on a
        # socket that was already closed under it.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5.0)
        self.addCleanup(self.server.shutdown)

    def test_a_live_admin_api_means_an_instance_is_running(self):
        self.assertTrue(_another_instance_is_running(self.port))

    def test_a_closed_port_is_not_an_instance(self):
        # A port nobody listens on must not look like a running peer, or the
        # bridge would refuse to start on a machine that has none.
        self.server.shutdown()
        self.thread.join(5.0)
        self.server.server_close()
        self.assertFalse(_another_instance_is_running(self.port))

    def test_non_admin_payload_is_not_accepted_as_a_peer(self):
        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": false, "service": "something-else"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        other = http.server.HTTPServer(("127.0.0.1", 0), Other)
        other_thread = threading.Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        self.addCleanup(other.server_close)
        self.addCleanup(other_thread.join, 5.0)
        self.addCleanup(other.shutdown)
        self.assertFalse(_another_instance_is_running(other.server_address[1]))

    def test_acquire_refuses_while_a_peer_answers(self):
        self.assertFalse(_acquire_instance_lock(self.port))


if __name__ == "__main__":
    unittest.main()
