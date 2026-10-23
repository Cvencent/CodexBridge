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

    # -- demand-driven polling (the optimisation) --------------------------
    def test_the_poll_runs_when_a_client_is_connected(self):
        # Any connected client restores the full-speed poll, exactly as before.
        self.service._demand = lambda: True
        self.service._codex_enabled = lambda: False
        self.assertTrue(self.service._poll_enabled())

    def test_the_poll_is_skipped_only_when_nobody_is_watching(self):
        # Quiet machine: no client AND the user did not ask for always-on.
        self.service._demand = lambda: False
        self.service._codex_enabled = lambda: False
        self.assertFalse(self.service._poll_enabled())

    def test_always_on_keeps_polling_with_no_client(self):
        self.service._demand = lambda: False
        self.service._codex_enabled = lambda: True
        self.assertTrue(self.service._poll_enabled())

    def test_a_client_arriving_resumes_the_poll(self):
        # This is the property that makes the optimisation safe: the poll is
        # never permanently off, it resumes the moment somebody connects.
        watching = {"on": False}
        self.service._demand = lambda: watching["on"]
        self.service._codex_enabled = lambda: False
        self.assertFalse(self.service._poll_enabled())
        watching["on"] = True
        self.assertTrue(self.service._poll_enabled())

    def test_an_explicit_read_still_works_with_background_off(self):
        # The switch is a cadence knob, never a feature cut: a device or phone
        # that asks for its task list must still be served.
        self.service._codex_enabled = lambda: False
        self.service._demand = lambda: False
        self.assertGreater(self.count(
            lambda: self.service.list_threads(limit=20, refresh=True)), 0)

    def test_an_open_event_stream_counts_as_a_client(self):
        target = self.service.subscribe()
        self.assertTrue(self.service.has_event_subscribers())
        self.service.unsubscribe(target)
        self.assertFalse(self.service.has_event_subscribers())

    def test_a_broken_demand_probe_never_stops_the_poll(self):
        # Skipping the poll is an optimisation, so an unknown must answer
        # "somebody is watching" rather than silently going quiet.
        def boom():
            raise RuntimeError("probe failed")

        self.service._demand = boom
        self.assertTrue(self.service._poll_enabled())

    def test_a_broken_switch_probe_does_not_break_the_poll(self):
        def boom():
            raise RuntimeError("probe failed")

        self.service._codex_enabled = boom
        self.assertTrue(self.service._codex_collection_enabled())
