"""
A listener must own its port. Two CodexBridge processes sharing 8788 or 8568
split incoming connections between them, so a stale old build left on disk
answers part of the admin API and half of the phone traffic. On Windows
SO_REUSEADDR is what allows that sharing, so these servers have to bind
exclusively instead.
"""

from __future__ import annotations

import http.server
import socket
import unittest

from codexbridge.admin_api import _AdminServer
from codexbridge.net_gate import ExclusiveBindMixin, configure_exclusive, port_is_in_use


def _free_port(kind: int = socket.SOCK_STREAM) -> int:
    # Windows reserves TCP and UDP dynamic ports separately, so a number
    # learned from a TCP probe can be refused for a UDP bind. Ask the
    # protocol under test for its own free port.
    for _attempt in range(8):
        probe = socket.socket(socket.AF_INET, kind)
        try:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
        except OSError:
            continue
        finally:
            probe.close()
    raise AssertionError("no free loopback port")


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class NetGateTest(unittest.TestCase):
    def _serve(self, server_class, port):
        server = server_class(("127.0.0.1", port), _Handler)
        self.addCleanup(server.server_close)
        return server

    def test_a_second_listener_cannot_take_the_admin_port(self):
        port = _free_port()
        owner = self._serve(_AdminServer, port)
        with self.assertRaises(OSError):
            squatter = _AdminServer(("127.0.0.1", port), _Handler)
            squatter.server_close()
        self.assertTrue(port_is_in_use("127.0.0.1", port))
        # (the addCleanup registered by _serve closes the owner)

    def test_the_admin_port_reads_as_in_use_while_it_is_owned(self):
        port = _free_port()
        owner = self._serve(_AdminServer, port)
        self.assertTrue(port_is_in_use("127.0.0.1", port))
        owner.server_close()
        self.assertFalse(port_is_in_use("127.0.0.1", port))

    def test_an_exclusive_datagram_socket_refuses_a_shared_bind(self):
        port = _free_port(socket.SOCK_DGRAM)
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", port))
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(other.close)
        configure_exclusive(other)
        with self.assertRaises(OSError):
            other.bind(("127.0.0.1", port))

    def test_the_owner_still_rebinds_after_it_stops(self):
        # Exclusivity must not cost the quick restart the old reuse flag was
        # there to buy.
        port = _free_port()
        first = self._serve(_AdminServer, port)
        first.server_close()
        second = _AdminServer(("127.0.0.1", port), _Handler)
        self.addCleanup(second.server_close)
        self.assertEqual(second.server_address[1], port)

    def test_the_mixin_does_not_ask_for_a_shared_bind(self):
        self.assertFalse(ExclusiveBindMixin.allow_reuse_address)


if __name__ == "__main__":
    unittest.main()
