"""The phone status snapshot must not keep re-reading Codex for the same rows.

Three costs showed up on a real machine: every caller fetched its own copy of
the task list, every row the rollout log could not classify pulled a whole
thread body on every poll, and a method the app-server simply refused tore down
and respawned the entire Codex process.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexbridge.codex.app_server import AppServerError, AppServerProtocolError
from codexbridge.companion.service import CompanionService
from tests.test_companion_service import FakeClient, thread_row, thread_detail


def make_service(client: FakeClient, temp: str) -> CompanionService:
    return CompanionService(
        data_dir=Path(temp),
        cwd=Path(temp),
        client_factory=lambda command, cwd: client,
        desktop_state_path=Path(temp) / "missing.json",
        sessions_root=Path(temp) / "sessions",
    )


class SharedListingPageTests(unittest.TestCase):
    """One page of task rows serves every device, however many rows it wants."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {
            "data": [thread_row("t%d" % i, name="task %d" % i)
                     for i in range(6)],
            "nextCursor": None,
        }
        self.client.responses["thread/read"] = thread_detail()
        self.client.responses["account/rateLimits/read"] = {}
        self.client.responses["account/usage/read"] = {}
        self.service = make_service(self.client, self.temp.name)

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def count(self, action) -> int:
        self.client.requests.clear()
        action()
        return len(self.client.requests)

    def test_a_wide_page_and_a_narrow_one_cost_one_fetch(self):
        # The watcher reconciles 80 rows and a phone status wants a dozen. Those
        # used to be two Codex round trips for the same data.
        self.service.list_threads(limit=80)
        self.assertEqual(
            self.count(lambda: self.service.list_threads(limit=12)), 0)

    def test_the_shared_page_is_sliced_per_caller(self):
        self.client.responses["thread/list"] = {
            "data": [thread_row("t%d" % i, name="task %d" % i)
                     for i in range(20)],
            "nextCursor": None,
        }
        self.assertEqual(len(self.service.list_threads(limit=5)["threads"]), 5)
        self.assertEqual(len(self.service.list_threads(limit=20)["threads"]), 20)

    def test_a_search_or_cursor_still_gets_its_own_page(self):
        # Sharing is only safe for the plain first page; a filtered or paged
        # request is genuinely different data.
        self.service.list_threads(limit=10)
        self.assertGreater(
            self.count(lambda: self.service.list_threads(limit=10,
                                                          search="const")), 0)
        self.assertGreater(
            self.count(lambda: self.service.list_threads(limit=10,
                                                          cursor="c1")), 0)


class RowVerdictTests(unittest.TestCase):
    """A row the rollout log cannot classify is decided by one read, once."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {
            "data": [thread_row("t%d" % i, name="task %d" % i)
                     for i in range(4)],
            "nextCursor": None,
        }
        self.client.responses["thread/read"] = thread_detail()
        self.service = make_service(self.client, self.temp.name)

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def read_ids(self) -> set:
        return {params.get("threadId") for method, params in self.client.requests
                if method == "thread/read"}

    def test_rows_are_not_re_read_for_a_verdict_the_log_cannot_give(self):
        # These rows have no rollout log, so the app-server turn read is the only
        # answer -- and it pulls the whole thread body, which for a long task is
        # over 100 KB. Repeating it on every poll was the hot path.
        self.client.requests.clear()
        self.service.list_threads(limit=4)
        self.assertEqual(self.read_ids(), {"t0", "t1", "t2", "t3"})
        self.client.requests.clear()
        self.service.list_threads(limit=4)
        self.assertEqual(self.read_ids(), set())

    def test_a_verdict_survives_a_refreshed_listing(self):
        # The watcher forces a fresh thread/list every cycle; that must not
        # erase what those rows were already settled to be.
        self.service.list_threads(limit=4)
        self.client.requests.clear()
        self.service.list_threads(limit=4, refresh=True)
        self.assertEqual(self.read_ids(), set())

    def test_a_change_to_the_thread_retires_its_verdict(self):
        self.service.list_threads(limit=4)
        self.client.responses["thread/read"] = {"thread": dict(
            thread_row("t1"),
            turns=[{"id": "x", "status": "inProgress"}])}
        self.service.publish({"type": "turn_started", "thread_id": "t1",
                              "turn_id": "x"})
        self.client.requests.clear()
        self.service.list_threads(limit=4, refresh=True)
        self.assertIn("t1", self.read_ids())

    def test_a_thread_the_server_never_loaded_is_not_frozen_as_idle(self):
        # "idle" from a turn read only means no live turn, which is also what
        # the server says about a thread it never loaded. That is not a verdict
        # worth remembering, or a row would stay idle while a turn runs.
        self.client.responses["thread/read"] = {"thread": dict(
            thread_row("t0", status={"type": "notLoaded"}), turns=[])}
        self.service.list_threads(limit=1)
        entry = self.service._row_verdicts.get("t0")
        self.assertIsNotNone(entry)
        self.assertEqual(entry[0], "")


class TransportErrorTests(unittest.TestCase):
    """Only a dead pipe is worth restarting the app-server for."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.service = make_service(self.client, self.temp.name)

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def test_a_refused_method_does_not_restart_the_app_server(self):
        attempts = []

        def refuse(params):
            attempts.append(params)
            raise AppServerProtocolError(
                "App Server 请求失败 account/usage/read")

        self.client.responses["account/usage/read"] = refuse
        with self.assertRaises(AppServerError):
            self.service._fetch("account/usage/read", {})
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.client.closed)
        self.assertIs(self.service._client, self.client)

    def test_a_dead_transport_still_reconnects(self):
        def die(params):
            raise AppServerError("Codex app-server 已退出")

        self.client.responses["thread/list"] = die
        with self.assertRaises(AppServerError):
            self.service._fetch("thread/list", {})
        self.assertTrue(self.client.closed)
