"""Tests for the companion HTTP API."""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from codexbridge.companion.api import CompanionApi
from codexbridge.companion.service import CompanionService
from codexbridge.companion.store import DEFAULT_PORT, LEGACY_PORT, CompanionStore
from tests.test_companion_service import FakeClient, thread_detail, thread_row


class CompanionApiTests(unittest.TestCase):
    def test_json_responses_keep_the_http_connection_alive(self):
        """A polling phone must be able to reuse one socket for many requests.

        Forcing ``Connection: close`` made every request open a fresh TCP
        connection, and ThreadingHTTPServer answers each connection with a new
        thread. A phone polling several endpoints per second therefore churned
        dozens of threads and starved the desktop of responsiveness.
        """

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            for _ in range(3):
                connection.request("GET", "/api/companion/ping")
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertNotEqual(
                    (response.getheader("Connection") or "").lower(), "close"
                )
                self.assertIn(b"codexbridge-companion", body)
        finally:
            connection.close()

    def test_stopping_the_api_ends_an_open_sse_stream(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "GET",
            "/api/companion/events",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.api.stop()
        # The stream must reach EOF promptly after stop instead of staying
        # half-open until a TCP timeout. The initial hello event is expected.
        remaining = response.read()
        self.assertIn(b"hello", remaining)
        connection.close()

    def test_sse_does_not_keep_old_capabilities_after_negotiation(self):
        code = self.store.pairing_code()['code']
        _, paired = self.request('POST', '/api/companion/pair', {'code': code})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        connection.request('GET', '/api/companion/events', headers={'Authorization': 'Bearer ' + paired['token']})
        response = connection.getresponse()
        while response.fp.readline().strip():
            pass
        self.store.negotiate(paired['phone_id'], ['status'])
        self.service.publish({'type': 'approval_request', 'command': 'secret'})
        self.service.publish({'type': 'turn_completed', 'turn_id': 'done'})
        response.fp.readline()
        payload = json.loads(response.fp.readline().decode()[6:])
        connection.close()
        self.assertEqual(payload['type'], 'turn_completed')

    def test_negotiated_read_only_phone_cannot_use_legacy_write(self):
        code = self.store.pairing_code()['code']
        _, paired = self.request('POST', '/api/companion/pair', {'code': code})
        token = paired['token']
        status, _ = self.request('POST', '/api/companion/capabilities', {'supported': ['status', 'tasks']}, token=token)
        self.assertEqual(status, 200)
        status, result = self.request('POST', '/api/companion/threads/t1/messages', {'text': 'hi'}, token=token)
        self.assertEqual(status, 403)
        self.assertEqual(result['error'], 'capability_not_enabled')

    def test_status_reports_intermediate_event_setting(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        self.store.set_show_intermediate_events(True)

        status, payload = self.request(
            "GET", "/api/companion/status", token=paired["token"]
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["show_intermediate_events"])

    def test_thread_read_uses_store_intermediate_event_setting(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        self.store.set_show_intermediate_events(True)

        status, payload = self.request(
            "GET", "/api/companion/threads/t1", token=paired["token"]
        )

        self.assertEqual(status, 200)
        kinds = [item["kind"] for item in payload["messages"]]
        self.assertIn("command", kinds)

    def test_image_upload_then_message_uses_inline_input_image(self):
        import base64
        import hashlib

        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        token = paired["token"]
        payload = b"\x89PNG\r\n\x1a\nimage"
        digest = hashlib.sha256(payload).hexdigest()

        status, upload = self.request(
            "POST",
            "/api/companion/threads/t1/attachments",
            {
                "kind": "image",
                "byte_size": len(payload),
                "content_sha256": digest,
                "filename": "photo.png",
                "mime_type": "image/png",
            },
            token=token,
        )
        self.assertEqual(status, 201, upload)
        upload_id = upload["upload_id"]

        status, _ = self.request(
            "POST",
            f"/api/companion/uploads/{upload_id}/chunk",
            {"offset": 0, "data": base64.b64encode(payload).decode("ascii")},
            token=token,
        )
        self.assertEqual(status, 200)
        status, record = self.request(
            "POST",
            f"/api/companion/uploads/{upload_id}/end",
            {"byte_size": len(payload)},
            token=token,
        )
        self.assertEqual(status, 200, record)

        status, result = self.request(
            "POST",
            "/api/companion/threads/t1/messages",
            {
                "text": "看一下",
                "input_parts": [
                    {"type": "text", "text": "看一下"},
                    {"type": "image", "attachment_id": record["attachment_id"]},
                ],
            },
            token=token,
        )
        self.assertEqual(status, 200, result)
        self.service.queues.dispatch_ready()
        turn = [call for call in self.client.requests if call[0] == "turn/start"][-1]
        inputs = turn[1]["input"]
        self.assertEqual(inputs[0], {"type": "text", "text": "看一下"})
        self.assertEqual(inputs[1]["type"], "image")
        self.assertTrue(inputs[1]["url"].startswith("data:image/png;base64,"))

        status, listing = self.request(
            "GET", "/api/companion/threads/t1/attachments", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(listing["object_count"], 1)

    def test_desktop_disconnect_returns_structured_error(self):
        from unittest.mock import patch
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        code = self.store.pairing_code()['code']
        _, paired = self.request('POST', '/api/companion/pair', {'code': code})
        with patch.object(self.service, 'start_turn', side_effect=DesktopIpcError('disconnected')):
            status, result = self.request('POST', '/api/companion/commands', {
                'request_id': 'desktop-error', 'method': 'turn.start',
                'params': {'thread_id': 't1', 'text': 'hi'}}, token=paired['token'])
        self.assertEqual(status, 503)
        self.assertEqual(result['error'], 'desktop_unavailable')

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {"data": [thread_row()], "nextCursor": None}
        self.client.responses["thread/read"] = thread_detail()
        self.client.responses["thread/resume"] = {"thread": thread_row()}
        self.client.responses["turn/start"] = {"turn": {"id": "turn-2"}}
        self.client.responses["account/rateLimits/read"] = {"rateLimits": {}}
        self.client.responses["account/usage/read"] = {}
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
        self.api = CompanionApi(self.service, self.store, host="127.0.0.1", port=0)
        self.api.start()
        assert self.api._server is not None
        self.port = self.api._server.server_address[1]
        self.api.port = self.port

    def tearDown(self) -> None:
        self.api.stop()
        self.service.stop()
        self.temp.cleanup()

    def request(self, method: str, path: str, payload=None, token: str | None = None, raw: bool = False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if raw:
            return response.status, data.decode("utf-8")
        return response.status, json.loads(data.decode("utf-8"))

    def _read_sse_data(self, response):
        while True:
            line = response.fp.readline()
            if not line:
                raise AssertionError("SSE stream ended before a data event")
            decoded = line.decode("utf-8").rstrip("\r\n")
            if decoded.startswith("data: "):
                return json.loads(decoded[len("data: "):])

    def test_ping_requires_no_token(self) -> None:
        status, payload = self.request("GET", "/api/companion/ping")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "codexbridge-companion")
        self.assertEqual(payload["bridge_instance_id"], self.store.instance_id)
        self.assertEqual(payload["device_id"], self.store.device_id)

    def test_pair_response_carries_stable_bridge_identity(self) -> None:
        code = self.store.pairing_code()["code"]

        status, payload = self.request("POST", "/api/companion/pair", {"code": code})

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["bridge"]["bridge_instance_id"], self.store.instance_id)
        self.assertEqual(payload["bridge"]["device_id"], self.store.device_id)

    def test_general_command_api_and_deduplicated_control(self):
        code = self.store.pairing_code()['code']
        _, paired = self.request('POST', '/api/companion/pair', {'code': code})
        token = paired['token']
        status, catalog = self.request('GET', '/api/companion/protocol', token=token)
        self.assertEqual(status, 200)
        self.assertIn('turn.start', [row['method'] for row in catalog['methods']])
        request = {'request_id': 'mobile-1', 'method': 'turn.start',
                   'params': {'thread_id': 't1', 'text': 'continue'}}
        status, first = self.request('POST', '/api/companion/commands', request, token=token)
        self.assertEqual(status, 200, first)
        _, second = self.request('POST', '/api/companion/commands', request, token=token)
        self.assertEqual(first, second)
        self.assertEqual(sum(method == 'turn/start' for method, _ in self.client.requests), 1)

    def test_queue_enqueue_command_returns_the_queued_item(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})

        status, payload = self.request(
            "POST",
            "/api/companion/commands",
            {
                "request_id": "mobile-queue-1",
                "method": "queue.enqueue",
                "params": {"thread_id": "t1", "text": "排队继续"},
            },
            token=paired["token"],
        )

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["item"]["text"], "排队继续")
        self.assertEqual(payload["result"]["thread_id"], "t1")

    def test_queue_capable_phone_legacy_start_enqueues_instead_of_starting(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})

        status, payload = self.request(
            "POST",
            "/api/companion/threads/t1/messages",
            {"text": "进行中继续输入"},
            token=paired["token"],
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["item"]["text"], "进行中继续输入")
        self.assertNotIn("turn/start", [method for method, _ in self.client.requests])

    def test_phone_without_queue_capability_keeps_legacy_start_behavior(self):
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        status, negotiated = self.request(
            "POST",
            "/api/companion/capabilities",
            {"supported": ["control"]},
            token=paired["token"],
        )
        self.assertEqual(status, 200, negotiated)
        self.assertEqual(negotiated["enabled"], ["control"])

        status, payload = self.request(
            "POST",
            "/api/companion/threads/t1/messages",
            {"text": "旧手机立即发送"},
            token=paired["token"],
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["turn_id"], "turn-2")
        self.assertEqual(self.client.requests[-1][0], "turn/start")

    def test_sse_sends_initial_queue_snapshot_to_queue_phone(self) -> None:
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        self.store.negotiate(paired["phone_id"], ["queue"])
        queued = self.service.queue_enqueue("t1", "连接前已排队")["item"]
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=1.5
        )
        connection.request(
            "GET",
            "/api/companion/events",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(self._read_sse_data(response)["type"], "hello")
        snapshot = self._read_sse_data(response)
        connection.close()

        self.assertEqual(snapshot["type"], "queue_snapshot")
        self.assertEqual(snapshot["thread_id"], "t1")
        self.assertEqual(
            [item["queue_id"] for item in snapshot["items"]],
            [queued["queue_id"]],
        )

    def test_sse_delivers_queue_events_to_queue_only_phone(self) -> None:
        code = self.store.pairing_code()["code"]
        _, paired = self.request("POST", "/api/companion/pair", {"code": code})
        self.store.negotiate(paired["phone_id"], ["queue"])
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=1.5
        )
        connection.request(
            "GET",
            "/api/companion/events",
            headers={"Authorization": "Bearer " + paired["token"]},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self._read_sse_data(response)
        self.service.publish(
            {
                "type": "queue_item_added",
                "thread_id": "t1",
                "queue_id": "q1",
                "revision": 2,
            }
        )
        payload = self._read_sse_data(response)
        connection.close()
        self.assertEqual(payload["type"], "queue_item_added")
        self.assertEqual(payload["queue_id"], "q1")

    def test_status_requires_token(self) -> None:
        status, payload = self.request("GET", "/api/companion/status")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_pairing_flow_and_thread_access(self) -> None:
        code = self.store.pairing_code()["code"]
        status, payload = self.request(
            "POST",
            "/api/companion/pair",
            {"code": code, "name": "测试手机"},
        )
        self.assertEqual(status, 200, payload)
        token = payload["token"]
        status, threads = self.request("GET", "/api/companion/threads", token=token)
        self.assertEqual(status, 200, threads)
        self.assertEqual(len(threads["threads"]), 1)
        status, detail = self.request("GET", "/api/companion/threads/t1", token=token)
        self.assertEqual(status, 200, detail)
        self.assertTrue(detail["messages"])
        status, sent = self.request(
            "POST",
            "/api/companion/threads/t1/messages",
            {"text": "继续"},
            token=token,
        )
        self.assertEqual(status, 200, sent)
        self.assertEqual(sent["item"]["text"], "继续")
        self.assertEqual(sent["item"]["status"], "pending")

    def test_read_ack_is_shared_by_all_paired_phones(self) -> None:
        code = self.store.pairing_code()["code"]
        _, first = self.request(
            "POST",
            "/api/companion/pair",
            {"code": code, "name": "手机一"},
        )
        first_token = first["token"]
        code = self.store.pairing_code(regenerate=True)["code"]
        _, second = self.request(
            "POST",
            "/api/companion/pair",
            {"code": code, "name": "手机二"},
        )
        second_token = second["token"]

        status, read = self.request(
            "POST",
            "/api/companion/threads/t1/read",
            token=first_token,
        )
        self.assertEqual(status, 200, read)

        status, threads = self.request(
            "GET",
            "/api/companion/threads",
            token=second_token,
        )
        self.assertEqual(status, 200, threads)
        self.assertEqual(threads["threads"][0]["status"], "idle")

    def test_pairing_failure_is_reported(self) -> None:
        status, payload = self.request(
            "POST",
            "/api/companion/pair",
            {"code": "000000", "name": "测试手机"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "pairing_failed")

    def test_repairing_the_same_installation_reuses_the_phone_record(self) -> None:
        code = self.store.pairing_code()["code"]
        status, first = self.request(
            "POST",
            "/api/companion/pair",
            {"code": code, "name": "测试手机", "client_id": "installation-1"},
        )
        self.assertEqual(status, 200, first)

        code = self.store.pairing_code(regenerate=True)["code"]
        status, second = self.request(
            "POST",
            "/api/companion/pair",
            {"code": code, "name": "测试手机", "client_id": "installation-1"},
        )
        self.assertEqual(status, 200, second)

        self.assertEqual(second["phone_id"], first["phone_id"])
        self.assertNotEqual(second["token"], first["token"])
        self.assertEqual(len(self.store.snapshot()["phones"]), 1)
        status, _ = self.request("GET", "/api/companion/status", token=second["token"])
        self.assertEqual(status, 200)

    def test_disabled_store_rejects_requests(self) -> None:
        self.store.set_enabled(False)
        status, payload = self.request("GET", "/api/companion/status")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "companion_disabled")
        code = self.store.pairing_code()["code"]
        status, payload = self.request("POST", "/api/companion/pair", {"code": code})
        self.assertEqual(status, 403)

    def test_removing_phone_revokes_access(self) -> None:
        code = self.store.pairing_code()["code"]
        _, payload = self.request("POST", "/api/companion/pair", {"code": code, "name": "手机"})
        token = payload["token"]
        self.store.remove_phone(payload["phone_id"])
        status, _ = self.request("GET", "/api/companion/status", token=token)
        self.assertEqual(status, 401)

    def test_sse_stream_opens(self) -> None:
        code = self.store.pairing_code()["code"]
        _, payload = self.request("POST", "/api/companion/pair", {"code": code, "name": "手机"})
        token = payload["token"]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", f"/api/companion/events?thread_id=t1", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        line = response.fp.readline().decode("utf-8")
        self.assertTrue(line.startswith("retry:"))
        data_line = response.fp.readline().decode("utf-8")
        self.assertTrue(data_line.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["type"], "hello")
        connection.close()

    def test_start_uses_a_fallback_port_when_preferred_port_is_busy(self) -> None:
        self.api.stop()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy_port = holder.getsockname()[1]
        try:
            recovered = CompanionApi(self.service, self.store, host="127.0.0.1", port=busy_port)
            recovered.start()
            self.assertNotEqual(recovered.port, busy_port)
            self.assertGreater(recovered.port, 0)
            recovered.stop()
        finally:
            holder.close()

    def test_start_never_uses_the_reserved_legacy_port(self) -> None:
        self.api.stop()
        recovered = CompanionApi(self.service, self.store, host="127.0.0.1", port=LEGACY_PORT)
        recovered.start()
        try:
            self.assertNotEqual(recovered.port, LEGACY_PORT)
            self.assertGreater(recovered.port, 0)
        finally:
            recovered.stop()

    def test_set_port_starts_listener_when_api_is_not_running(self) -> None:
        self.api.stop()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        target_port = holder.getsockname()[1]
        holder.close()

        recovered = CompanionApi(self.service, self.store, host="127.0.0.1", port=0)
        self.assertFalse(recovered.running)
        recovered.set_port(target_port)
        try:
            self.assertTrue(recovered.running)
            self.assertEqual(recovered.port, target_port)
            self.assertEqual(self.store.snapshot()["port"], target_port)
        finally:
            recovered.stop()

    def test_set_port_failure_keeps_the_existing_listener_and_config(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy_port = holder.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                self.api.set_port(busy_port)
            self.assertTrue(self.api.running)
            self.assertEqual(self.api.port, self.port)
            self.assertEqual(self.store.snapshot()["port"], self.port)
        finally:
            holder.close()

    def test_fallback_starts_after_the_default_port(self) -> None:
        self.api.stop()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", DEFAULT_PORT))
            holder.listen(1)
            busy_port = DEFAULT_PORT
        except OSError:
            holder.close()
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            busy_port = holder.getsockname()[1]
        try:
            recovered = CompanionApi(self.service, self.store, host="127.0.0.1", port=busy_port)
            recovered.start()
            self.assertNotEqual(recovered.port, busy_port)
            self.assertNotEqual(recovered.port, LEGACY_PORT)
            recovered.stop()
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
