"""Wiring tests: the read cache must not make any device see a stale Codex."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexbridge.companion.service import (
    POLL_ACTIVE_SECONDS,
    POLL_IDLE_SECONDS,
    CompanionService,
)
from tests.test_companion_service import FakeClient, thread_row, thread_detail


class ReadCacheWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.rows = [thread_row("t%d" % i, name="task %d" % i) for i in range(6)]
        self.client.responses["thread/list"] = {"data": self.rows,
                                                "nextCursor": None}
        self.client.responses["thread/read"] = thread_detail()
        self.client.responses["model/list"] = {"data": []}
        self.client.responses["account/rateLimits/read"] = {}
        self.client.responses["account/usage/read"] = {}
        self.service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            isolated=True,
        )

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def count(self, action) -> int:
        self.client.requests.clear()
        action()
        return len(self.client.requests)

    # -- the win ----------------------------------------------------------
    def test_a_second_status_poll_does_not_re_read_codex(self):
        first = self.count(self.service.snapshot)
        second = self.count(self.service.snapshot)
        self.assertGreater(first, 1)
        self.assertEqual(second, 0)

    def test_streaming_events_do_not_empty_the_cache(self):
        # An agent turn emits a delta per token. Invalidating on each one would
        # put every device back to a read per row per poll.
        self.service.snapshot()
        for index in range(40):
            self.service.publish({"type": "agent_delta", "thread_id": "t0",
                                  "delta": "x"})
        self.assertEqual(self.count(self.service.snapshot), 0)

    # -- the safety property ----------------------------------------------
    def test_a_finished_turn_is_refetched_immediately(self):
        self.service.snapshot()
        self.service.publish({"type": "turn_completed", "thread_id": "t2",
                              "turn_id": "x", "status": "completed"})
        self.count(self.service.snapshot)
        read_ids = {params.get("threadId") for method, params in
                    self.client.requests if method == "thread/read"}
        self.assertIn("t2", read_ids)

    def test_a_write_invalidates_even_without_a_thread_id(self):
        self.service.snapshot()
        self.service.publish({"type": "resync_required"})
        self.assertGreater(self.count(self.service.snapshot), 0)

    def test_the_watcher_always_reads_fresh_rows(self):
        # The background watcher is what notices a desktop-initiated turn. If it
        # were served from the cache it would compare its own snapshot with
        # itself and never publish anything.
        self.service.poll_thread_changes()
        self.client.responses["thread/list"] = {"data": [thread_row(
            "t1", turns=[{"id": "brand-new", "status": "completed",
                          "completedAt": 1791000300}])], "nextCursor": None}
        self.count(self.service.poll_thread_changes)
        self.assertIn(("thread/list", {"limit": 80, "sortKey": "recency_at",
                                       "sortDirection": "desc",
                                       "sourceKinds": ["cli", "vscode", "exec",
                                                       "appServer", "unknown"],
                                       "archived": False}),
                      [(m, p) for m, p in self.client.requests])

    def test_a_reset_drops_reads_from_the_old_process(self):
        self.service.snapshot()
        self.service._reset_client()
        self.assertGreater(self.count(self.service.snapshot), 0)

    def test_a_failed_read_leaves_nothing_behind(self):
        # A raised error must not occupy the cache slot, or one transient
        # Codex failure would keep every device blind for the whole TTL.
        from codexbridge.codex.app_server import AppServerError

        def gone(params):
            raise AppServerError("codex gone")

        self.client.responses["thread/list"] = gone
        with self.assertRaises(AppServerError):
            self.service.list_threads(limit=80, refresh=True)
        self.client.responses["thread/list"] = {"data": self.rows,
                                                 "nextCursor": None}
        self.assertGreater(self.count(
            lambda: self.service.list_threads(limit=80)), 0)

    # -- power ------------------------------------------------------------
    def test_an_idle_codex_polls_slowly(self):
        self.service.poll_thread_changes()
        self.assertEqual(self.service._poll_seconds(), POLL_IDLE_SECONDS)

    def test_a_live_turn_polls_fast(self):
        active = {"data": [thread_row(
            "t1", status={"type": "active", "activeFlags": []},
            turns=[{"id": "x", "status": "inProgress"}])], "nextCursor": None}
        self.client.responses["thread/list"] = active
        self.client.responses["thread/read"] = {"thread": {
            "id": "t1", "name": "running", "source": "cli",
            "turns": [{"id": "x", "status": "inProgress"}],
        }}
        self.service.poll_thread_changes()
        self.assertEqual(self.service._poll_seconds(), POLL_ACTIVE_SECONDS)

    def test_a_waiting_approval_polls_fast(self):
        self.service.poll_thread_changes()
        self.service._approvals[1] = {"expires_at": 9_999_999_999,
                                      "thread_id": "t1"}
        self.assertEqual(self.service._poll_seconds(), POLL_ACTIVE_SECONDS)

    def test_before_the_first_poll_the_watcher_keeps_its_fast_beat(self):
        self.assertEqual(self.service._poll_seconds(), POLL_ACTIVE_SECONDS)

    # -- codex collection switch ------------------------------------------
    def test_disabling_collection_stops_the_background_poll(self):
        # With the switch off nothing may touch the app-server: the poll is
        # what kept a private Codex process reading the thread store every few
        # seconds even when no device was connected.
        self.service._codex_enabled = lambda: False
        self.assertEqual(self.count(self.service.poll_thread_changes), 0)

    def test_disabling_collection_drops_the_comparison_baseline(self):
        self.service.poll_thread_changes()
        self.assertIsNotNone(self.service._observed_threads)
        self.service._codex_enabled = lambda: False
        self.service._observed_threads = None
        self.assertIsNone(self.service._observed_threads)

    def test_enabling_collection_polls_again(self):
        flag = {"on": False}
        self.service._codex_enabled = lambda: flag["on"]
        self.assertEqual(self.count(self.service.poll_thread_changes), 0)
        flag["on"] = True
        self.assertGreater(self.count(self.service.poll_thread_changes), 0)

    def test_a_read_cannot_resurrect_a_switched_off_app_server(self):
        # The switch must hold even for an explicit phone request: otherwise
        # the first list poll would spawn the private Codex process again.
        from codexbridge.codex.app_server import AppServerError

        self.service._client = None
        self.service._codex_enabled = lambda: False
        with self.assertRaises(AppServerError):
            self.service.list_threads(limit=20)
        self.assertIsNone(self.service._client)
        self.assertFalse(self.client.started)

    def test_re_enabling_lets_the_next_read_rebuild_the_client(self):
        self.service._codex_enabled = lambda: False
        self.service._client = None
        with self.assertRaises(Exception):
            self.service.list_threads(limit=20)
        self.service._codex_enabled = lambda: True
        self.assertGreater(self.count(
            lambda: self.service.list_threads(limit=20, refresh=True)), 0)
        self.assertIsNotNone(self.service._client)

    def test_a_broken_switch_never_silently_stops_collection(self):
        def boom():
            raise RuntimeError("probe failed")

        self.service._codex_enabled = boom
        self.assertTrue(self.service._codex_collection_enabled())
        self.assertGreater(self.count(self.service.poll_thread_changes), 0)
