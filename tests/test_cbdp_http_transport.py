"""CBDP v1 over the Companion HTTP/WebSocket/SSE transport."""

from __future__ import annotations

import http.client
import json
import tempfile
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

    def test_stopping_service_closes_cbdp_sessions(self) -> None:
        paired = self.pair()
        session = self.broker.attach(paired["phone_id"], paired["token"])
        self.assertFalse(session.closed.is_set())
        self.api.stop()
        self.assertTrue(session.closed.is_set())


if __name__ == "__main__":
    unittest.main()
