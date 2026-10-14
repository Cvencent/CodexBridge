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

    def test_ping_requires_no_token(self) -> None:
        status, payload = self.request("GET", "/api/companion/ping")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "codexbridge-companion")

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
        self.assertEqual(sent["turn_id"], "turn-2")

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
