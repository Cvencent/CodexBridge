"""CBDP v1 over the Companion HTTP/WebSocket/SSE transport."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    FLAG_REQUEST,
    TAG_CLIENT_NONCE,
    TAG_DEVICE_ID,
    TAG_FIRMWARE,
    TAG_MODEL,
    TAG_PROFILE,
    TAG_REQUEST_ID,
    TAG_SUPPORTED_CAPS,
    TAG_TRANSPORT_SECURITY,
    TAG_VERSION_MINOR,
    TYPE_HELLO,
    TYPE_WELCOME,
)
from codexbridge.cbdp.frame import Frame, decode_frame, encode_frame
from codexbridge.cbdp.http_transport import (
    CbdpHttpBroker,
    decode_ws_frame,
    encode_ws_frame,
    expand_phone_capabilities,
    frame_cursor,
    websocket_accept_key,
)
from codexbridge.cbdp.tlv import u8, u16, u32, u64, utf8
from codexbridge.companion.api import CompanionApi
from codexbridge.companion.service import CompanionService
from codexbridge.companion.store import CompanionStore
from tests.test_companion_service import FakeClient, thread_detail, thread_row


def hello_frame(request_id: int = 1) -> Frame:
    return Frame(
        message_type=TYPE_HELLO,
        flags=FLAG_REQUEST,
        header={
            TAG_VERSION_MINOR: u16(0),
            TAG_DEVICE_ID: utf8("phone-http-1"),
            TAG_MODEL: utf8("CodexBridgeMobile"),
            TAG_FIRMWARE: utf8("0.1"),
            TAG_PROFILE: u8(2),
            TAG_SUPPORTED_CAPS: capability_bitmap(
                ["status", "tasks", "control", "approval", "queue"]
            ).to_bytes(8, "big"),
            TAG_TRANSPORT_SECURITY: u8(0),
            TAG_CLIENT_NONCE: b"\x11" * 16,
            TAG_REQUEST_ID: u32(request_id),
        },
    )


class CbdpHttpTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {"data": [thread_row()], "nextCursor": None}
        self.client.responses["thread/read"] = thread_detail()
        self.store = CompanionStore(Path(self.temp.name) / "companion.json")
        self.store.load()
        self.service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            show_intermediate_events=lambda: self.store.show_intermediate_events,
            isolated=True,
        )
        self.broker = CbdpHttpBroker(
            commands=lambda: self.service.commands,
            capability_for=lambda phone_id: (
                record.capabilities()
                if (record := self.store.get_phone(phone_id)) is not None
                else set()
            ),
        )
        self.api = CompanionApi(
            self.service,
            self.store,
            host="127.0.0.1",
            port=0,
            cbdp_broker=self.broker,
        )
        self.api.start()
        assert self.api._server is not None
        self.port = self.api._server.server_address[1]
        self.api.port = self.port

    def tearDown(self) -> None:
        self.api.stop()
        self.service.stop()
        self.temp.cleanup()

    def pair(self) -> dict:
        code = self.store.pairing_code()["code"]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps({"code": code, "name": "Phone", "client_id": "install-1"}).encode()
        connection.request(
            "POST",
            "/api/companion/pair",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 200, payload)
        return payload

    def test_status_advertises_cbdp_paths_and_auth(self) -> None:
        payload = self.api.status_payload()
        cbdp = payload["cbdp_v1"]
        self.assertEqual(cbdp["version_major"], 1)
        self.assertEqual(cbdp["paths"]["websocket"], "/cbdp/v1/ws")
        self.assertEqual(cbdp["paths"]["events"], "/cbdp/v1/events")
        self.assertEqual(cbdp["paths"]["frame"], "/cbdp/v1/frame")
        self.assertIn("Bearer", cbdp["auth"])
        self.assertNotIn("token", json.dumps(cbdp))

    def test_accept_key_matches_rfc_vector(self) -> None:
        self.assertEqual(
            websocket_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_websocket_frames_round_trip(self) -> None:
        raw = encode_ws_frame(b"abc")
        buffer = bytearray(raw)
        self.assertEqual(decode_ws_frame(buffer), (0x2, b"abc"))
        self.assertEqual(bytes(buffer), b"")

    def test_frame_cursor_reads_delta_global_seq(self) -> None:
        frame = Frame(
            message_type=0x0A,
            header={0x0035: (1234).to_bytes(8, "big")},
        )
        self.assertEqual(frame_cursor(encode_frame(frame)), 1234)

    def test_control_capability_expands_to_cbdp_commands(self) -> None:
        expanded = expand_phone_capabilities({"control"})
        self.assertIn("control", expanded)
        self.assertIn("thread_manage", expanded)
        self.assertNotIn("compression", expanded)

    def test_http_frame_runs_cbdp_handshake_and_queues_welcome(self) -> None:
        paired = self.pair()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/cbdp/v1/frame",
            body=encode_frame(hello_frame()),
            headers={
                "Content-Type": "application/octet-stream",
                "Authorization": "Bearer " + paired["token"],
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 202, payload)

        session = self.broker.get(paired["phone_id"])
        self.assertIsNotNone(session)
        assert session is not None
        welcome = decode_frame(session.queue.get(timeout=2))
        self.assertEqual(welcome.message_type, TYPE_WELCOME)

    def test_frame_route_requires_a_pairing_token(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/cbdp/v1/frame",
            body=encode_frame(hello_frame()),
            headers={"Content-Type": "application/octet-stream"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 401, payload)
        self.assertEqual(payload["error"], "unauthorized")

    def test_companion_port_advertises_cbdp_without_loopback(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/cbdp/v1")
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["version_major"], 1)
        self.assertEqual(payload["paths"]["websocket"], "/cbdp/v1/ws")
        self.assertIn("Bearer", payload["auth"])

    def test_ping_on_companion_port_advertises_cbdp(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/companion/ping")
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["cbdp_v1"]["version_major"], 1)

    def test_broker_passes_active_state_hooks_to_the_gateway(self) -> None:
        recorded = {}

        def setter(device_id, thread_id, turn_id):
            recorded["value"] = (device_id, thread_id, turn_id)
            return {"thread_id": thread_id, "turn_id": turn_id}

        broker = CbdpHttpBroker(
            commands=lambda: self.service.commands,
            capability_for=lambda phone_id: {"tasks"},
            active_state=lambda device_id: {"thread_id": "t1", "turn_id": ""},
            set_active_state=setter,
        )
        gateway = broker._gateway("phone-http-1", "token")
        self.assertIsNotNone(gateway.active_state)
        self.assertIsNotNone(gateway.set_active_state)
        self.assertEqual(gateway.active_state("phone-http-1")["thread_id"], "t1")
        gateway.set_active_state("phone-http-1", "t2", "u2")
        self.assertEqual(recorded["value"][1:], ("t2", "u2"))

    def test_broker_gateway_shares_the_event_spine(self) -> None:
        # Without the shared spine the Companion gateway creates its own empty
        # one, so SUBSCRIBE has nothing to replay and summary events such as
        # system.codex_state never reach a phone.
        from codexbridge.cbdp.events import EventSpine
        from codexbridge.cbdp.subscriptions import SubscriptionManager

        spine = EventSpine(None)
        self.addCleanup(spine.close)
        subscriptions = SubscriptionManager(spine)
        broker = CbdpHttpBroker(
            commands=lambda: self.service.commands,
            capability_for=lambda phone_id: {"tasks"},
            event_spine=spine,
            subscriptions=subscriptions,
        )
        gateway = broker._gateway("phone-http-1", "token")
        self.assertIs(gateway.event_spine, spine)
        self.assertIs(gateway.subscriptions, subscriptions)
        event = spine.append(0x50000003, 0x0704, 1, {})
        result = gateway.subscriptions.subscribe("phone-http-1", 0x50000003, 0, 1, 0)
        self.assertTrue(result.recoverable)
        self.assertIn(event.global_seq, [item.global_seq for item in result.replay])

    def test_ws_route_exists_and_requires_upgrade(self) -> None:
        paired = self.pair()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "GET",
            "/cbdp/v1/ws",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode())
        connection.close()
        self.assertEqual(response.status, 426, payload)
        self.assertEqual(payload["error"], "websocket_upgrade_required")

    def test_sse_route_reports_event_stream(self) -> None:
        paired = self.pair()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "GET",
            "/cbdp/v1/events",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        self.assertEqual(response.getheader("Connection"), "close")
        connection.close()

    def test_sse_heartbeat_does_not_break_the_stream(self) -> None:
        from unittest.mock import patch
        from codexbridge.companion import api as companion_api

        paired = self.pair()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "GET",
            "/cbdp/v1/events",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        with patch.object(companion_api, "SSE_HEARTBEAT_SECONDS", 0.1), patch.object(
            companion_api, "SSE_WAIT_SLICE_SECONDS", 0.05
        ):
            line = response.fp.readline()
        self.assertEqual(line, b": ping\n")
        self.assertEqual(response.fp.readline(), b"\n")
        session = self.broker.get(paired["phone_id"])
        self.assertIsNotNone(session)
        self.assertFalse(session.closed.is_set())
        connection.close()

    def test_closing_one_cbdp_stream_does_not_kill_the_shared_session(self) -> None:
        # The phone may hold SSE and WebSocket streams at the same time. Both
        # share one CbdpHttpSession keyed by device_id, so one stream ending
        # must not close the session underneath the other stream.
        from unittest.mock import patch
        from codexbridge.companion import api as companion_api

        paired = self.pair()
        token = paired["token"]

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        ws = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        ws.sendall(
            (
                "GET /cbdp/v1/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Authorization: Bearer {token}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        handshake = b""
        while b"\r\n\r\n" not in handshake:
            handshake += ws.recv(4096)
        self.assertIn(b"101 Switching Protocols", handshake)

        sse = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        sse.request(
            "GET",
            "/cbdp/v1/events",
            headers={"Authorization": "Bearer " + token},
        )
        response = sse.getresponse()
        self.assertEqual(response.status, 200)

        session = self.broker.get(paired["phone_id"])
        self.assertIsNotNone(session)
        assert session is not None
        self.assertFalse(session.closed.is_set())

        with patch.object(companion_api, "SSE_HEARTBEAT_SECONDS", 0.1), patch.object(
            companion_api, "SSE_WAIT_SLICE_SECONDS", 0.05
        ):
            # Reading a heartbeat proves this SSE stream is live on the same
            # shared session the WebSocket is using.
            self.assertEqual(response.fp.readline(), b": ping\n")
            self.assertEqual(response.fp.readline(), b"\n")

            ws.sendall(encode_ws_frame(b"", opcode=0x8))
            deadline = time.monotonic() + 3.0
            while len(self.api._open_streams) > 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertLessEqual(len(self.api._open_streams), 1)
            # Give a buggy finally block time to close the shared session.
            time.sleep(0.2)
            self.assertFalse(
                session.closed.is_set(),
                "closing the WebSocket stream must not drop the shared CBDP session",
            )
            # The SSE stream must still receive heartbeats after the WS close.
            self.assertEqual(response.fp.readline(), b": ping\n")
            self.assertEqual(response.fp.readline(), b"\n")

        ws.close()
        sse.close()

    def test_per_device_cbdp_stream_limit_rejects_extra_streams(self) -> None:
        # One phone was caught holding hundreds of concurrent SSE streams,
        # which piles up one blocking server thread per stream. Reject the
        # excess with an explicit, retry-able error instead of accepting it.
        from unittest.mock import patch
        from codexbridge.companion import api as companion_api

        paired = self.pair()
        token = paired["token"]
        headers = {"Authorization": "Bearer " + token}

        with patch.object(companion_api, "MAX_CBDP_SSE_PER_DEVICE", 1), patch.object(
            companion_api, "MAX_CBDP_STREAMS_PER_DEVICE", 2
        ), patch.object(companion_api, "MAX_CBDP_STREAMS_TOTAL", 2):
            first = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            first.request("GET", "/cbdp/v1/events", headers=headers)
            first_response = first.getresponse()
            self.assertEqual(first_response.status, 200)
            self.assertEqual(self.api.device_stream_count(paired["phone_id"]), 1)

            second = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            second.request("GET", "/cbdp/v1/events", headers=headers)
            second_response = second.getresponse()
            payload = json.loads(second_response.read().decode())
            self.assertEqual(second_response.status, 429, payload)
            self.assertEqual(payload["error"], "too_many_streams")
            self.assertTrue(second_response.getheader("Retry-After"))
            second.close()
            # The rejected stream must not consume a slot.
            self.assertEqual(self.api.device_stream_count(paired["phone_id"]), 1)

            # Closing the SSE client must release its slot promptly instead of
            # waiting for the next heartbeat write to fail (up to 15 seconds).
            # HTTPConnection.close() only drops the connection object: for a
            # streamed response the socket belongs to the response, so the
            # server never sees a FIN until the response itself is closed.
            first_response.close()
            first.close()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and self.api.device_stream_count(
                paired["phone_id"]
            ) > 0:
                time.sleep(0.05)
            self.assertEqual(
                self.api.device_stream_count(paired["phone_id"]),
                0,
                "an abandoned SSE stream must release its slot promptly",
            )

            third = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            third.request("GET", "/cbdp/v1/events", headers=headers)
            third_response = third.getresponse()
            self.assertEqual(third_response.status, 200)
            third.close()

    def test_stopping_service_closes_cbdp_sessions(self) -> None:
        paired = self.pair()
        session = self.broker.attach(paired["phone_id"], paired["token"])
        self.assertFalse(session.closed.is_set())
        self.api.stop()
        self.assertTrue(session.closed.is_set())


if __name__ == "__main__":
    unittest.main()
