import tempfile
import threading
import time
import unittest
from pathlib import Path

from codexbridge.codex.collector import records_from_snapshot, run_codex_collector
from codexbridge.protocol.messages import MAX_LINE_BYTES, wire_messages


def _task(
    thread_id: str,
    status: str,
    *,
    completed_at: int = 0,
    assistant: str = "",
) -> dict:
    return {
        "thread_id": thread_id,
        "turn_id": f"turn-{thread_id}",
        "status": status,
        "title": f"Task {thread_id}",
        "last_user_message": f"Question {thread_id}",
        "last_assistant_message": assistant,
        "codex_messages": [assistant] if assistant else [],
        "current_action": "Working" if status == "working" else "Done",
        "started_at_epoch": 100,
        "last_updated_epoch": 200,
        "completed_at_epoch": completed_at,
    }


SNAPSHOT = {
    "generated_at_epoch": 1_000,
    "account": {
        "weekly_available": True,
        "weekly_remaining_percent": 68,
        "plan_type": "plus",
    },
    "task": _task("active-thread", "working", assistant="not part of status"),
    "tasks": [
        _task("active-thread", "working", assistant="not part of task list"),
        _task("done-thread", "completed", completed_at=900, assistant="finished"),
    ],
    "result": _task(
        "done-thread",
        "completed",
        completed_at=900,
        assistant="finished",
    ),
}


class CodexRecordTests(unittest.TestCase):
    def test_quota_record_stays_within_the_wire_line_limit(self):
        snapshot = {
            "generated_at_epoch": 1_000,
            "account": {
                "five_hour_available": True,
                "five_hour_remaining_percent": 100,
                "five_hour_reset_at_epoch": 9_999_999_999,
                "weekly_available": True,
                "weekly_remaining_percent": 100,
                "weekly_reset_at_epoch": 9_999_999_999,
                "weekly_tokens_available": True,
                "weekly_tokens": 999_999_999,
                "plan_type": "P" * 200,
            },
            "task": {
                "status": "waiting_user",
                "thread_id": "T" * 200,
                "current_action": "工" * 200,
                "token_count": 999_999_999,
                "plan_completed": 65535,
                "plan_total": 65535,
            },
        }
        quota = next(
            record
            for record in records_from_snapshot(snapshot)
            if record.kind == "codex_status"
        )
        payload = {
            "type": "message",
            "message_id": quota.message_id,
            "thread_id": quota.thread_id,
            "kind": quota.kind,
            "status": quota.status,
            "title": quota.title,
            "body": quota.body,
            "created_at": quota.created_at,
            "data": quota.data,
        }
        wires = wire_messages(payload)
        self.assertTrue(wires)
        self.assertLessEqual(max(len(chunk) for chunk in wires), MAX_LINE_BYTES)

    def test_snapshot_records_separate_status_tasks_and_results(self):
        records = records_from_snapshot(SNAPSHOT, now=1_000.0)
        by_kind = {record.kind: record for record in records}

        self.assertEqual(by_kind["status"].status, "working")
        self.assertEqual(
            by_kind["status"].data["account"]["weekly_remaining_percent"],
            68,
        )
        self.assertNotIn("last_assistant_message", by_kind["status"].data["task"])
        self.assertNotIn("codex_messages", by_kind["status"].data["task"])
        self.assertEqual(
            by_kind["task"].data["tasks"][0]["thread_id"],
            "active-thread",
        )
        self.assertEqual(by_kind["result"].data["task"]["thread_id"], "done-thread")
        self.assertEqual(
            by_kind["result"].data["task"]["conversation_text"],
            "finished",
        )

    def test_record_ids_are_stable_for_the_same_logical_state(self):
        first = {
            record.message_id
            for record in records_from_snapshot(SNAPSHOT, now=1.0)
        }
        second = {
            record.message_id
            for record in records_from_snapshot(SNAPSHOT, now=2.0)
        }
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            {
                "status:active-thread:working",
                "task:active-thread",
                "result:done-thread:900",
                "codex_status:active-thread",
            },
        )

    def test_interrupted_snapshot_is_published_as_a_result(self):
        snapshot = {
            "generated_at_epoch": 1_000,
            "result": _task(
                "stopped-thread",
                "interrupted",
                completed_at=950,
                assistant="stopped",
            ),
        }
        result = next(
            record for record in records_from_snapshot(snapshot) if record.kind == "result"
        )
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.body, "stopped")


class FakeSource:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def snapshot(self):
        return self._snapshot

    def close(self):
        self.closed = True


class CodexCollectorTest(unittest.TestCase):
    def test_collector_publishes_records_and_reports_status(self):
        stop = threading.Event()
        published = []
        reports = []
        sources = []

        def factory():
            source = FakeSource(SNAPSHOT)
            sources.append(source)
            return source

        def publish(records):
            published.extend(records)
            stop.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            run_codex_collector(
                runtime_folder=Path(temp_dir),
                cwd=Path(temp_dir),
                interval=0.01,
                stop=stop,
                publish=publish,
                source_factory=factory,
                report=lambda state, detail: reports.append((state, detail)),
            )

        self.assertEqual(
            {item.kind for item in published},
            {"status", "task", "result", "codex_status"},
        )
        quota = next(item for item in published if item.kind == "codex_status")
        self.assertEqual(quota.data["quota"]["weekly_remaining_percent"], 68)
        self.assertEqual(quota.data["quota"]["plan_type"], "plus")
        self.assertEqual(reports[0], ("online", "Codex App Server 已连接"))
        self.assertTrue(sources[0].started)
        self.assertTrue(sources[0].closed)

    def test_collector_registers_and_clears_active_source(self):
        stop = threading.Event()
        source = FakeSource(SNAPSHOT)
        active = []

        def publish(records):
            del records
            stop.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            run_codex_collector(
                runtime_folder=Path(temp_dir),
                cwd=Path(temp_dir),
                interval=0.01,
                stop=stop,
                publish=publish,
                source_factory=lambda: source,
                on_source=lambda current: active.append(current),
            )

        self.assertEqual(active, [source, None])


class CollectorResilienceTest(unittest.TestCase):
    """The collector thread must never die silently."""

    def test_a_publish_failure_does_not_kill_the_thread(self):
        # A sqlite3.DatabaseError from the event spine is neither OSError nor
        # ValueError, so it used to escape the collector loop and kill the
        # thread outright: the running badge froze at its last value with no
        # error anywhere. It must be reported and the loop must survive.
        import sqlite3

        stop = threading.Event()
        reports = []
        attempts = {"n": 0}

        def publish(records):
            del records
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.DatabaseError("database disk image is malformed")
            stop.set()

        source = FakeSource(SNAPSHOT)
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = threading.Thread(
                target=run_codex_collector,
                kwargs=dict(
                    runtime_folder=Path(temp_dir),
                    cwd=Path(temp_dir),
                    interval=1.0,
                    stop=stop,
                    publish=publish,
                    source_factory=lambda: source,
                    report=lambda state, detail: reports.append((state, detail)),
                ),
                daemon=True,
            )
            thread.start()
            self.assertTrue(stop.wait(15.0), "the collector must keep retrying")
            thread.join(timeout=5.0)

        self.assertGreaterEqual(attempts["n"], 2, "the loop must retry after the error")
        self.assertTrue(
            any(state == "error" and "DatabaseError" in detail for state, detail in reports),
            "the failure must be reported instead of vanishing",
        )


class CollectorThrottleTest(unittest.TestCase):
    """The idle throttle must slow the beat without ever stopping collection."""

    def test_slow_interval_still_publishes(self):
        # With the throttle on the collector keeps collecting: the store stays
        # fresh, so a device appearing mid-cycle never reads an empty list.
        stop = threading.Event()
        source = FakeSource(SNAPSHOT)
        published = []

        def publish(records):
            published.extend(records)
            stop.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            run_codex_collector(
                runtime_folder=Path(temp_dir),
                cwd=Path(temp_dir),
                interval=5.0,
                stop=stop,
                publish=publish,
                source_factory=lambda: source,
                interval_for=lambda: 60.0,
                demand=lambda: False,
            )

        self.assertTrue(published, "a throttled collector must still publish")

    def test_a_client_appearing_short_circuits_the_slow_sleep(self):
        # The first client to connect must not wait out the whole idle
        # interval: the slow sleep watches for demand and breaks early.
        stop = threading.Event()
        source = FakeSource(SNAPSHOT)
        demand = {"on": False}
        cycles = []
        first = threading.Event()

        def publish(records):
            del records
            cycles.append(time.monotonic())
            if not first.is_set():
                first.set()
            elif len(cycles) >= 2:
                stop.set()

        def interval_for():
            return 5.0 if demand["on"] else 60.0

        def demand_probe():
            return demand["on"]

        with tempfile.TemporaryDirectory() as temp_dir:
            thread = threading.Thread(
                target=run_codex_collector,
                kwargs=dict(
                    runtime_folder=Path(temp_dir),
                    cwd=Path(temp_dir),
                    interval=5.0,
                    stop=stop,
                    publish=publish,
                    source_factory=lambda: source,
                    interval_for=interval_for,
                    demand=demand_probe,
                ),
                daemon=True,
            )
            thread.start()
            self.assertTrue(first.wait(5.0), "expected an initial collection")
            # A device appears while the loop is in its slow sleep.
            demand["on"] = True
            self.assertTrue(
                stop.wait(6.0),
                "a connected client must resume the fast beat promptly",
            )
            thread.join(timeout=5.0)

        self.assertGreaterEqual(len(cycles), 2)
        gap = cycles[1] - cycles[0]
        self.assertLess(
            gap, 30.0,
            "the second collection must not wait for the full idle interval",
        )


if __name__ == "__main__":
    unittest.main()
