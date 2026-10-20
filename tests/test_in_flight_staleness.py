"""A live record stops proving that Codex is working after a while.

Reported on hardware: after a second voice turn the screen sat on "waiting for the
computer" for minutes while the host answered every poll with
``{"type":"codex_state","running":4}``. The collector had stopped republishing
those threads, so four old ``task`` records and one old ``status`` record were still
sitting in the store looking in-flight forever.

The generic rule: an in-flight record is only answerable while the host keeps
seeing it. ``MessageStore.add`` stamps ``seen_at`` on every arrival, and
``is_fresh`` retires a record once that stamp is older than
``IN_FLIGHT_STALE_AFTER_SECONDS``. Nothing here knows about a device model.
"""
import time
import unittest

from codexbridge.codex.collector import records_from_snapshot
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import (
    IN_FLIGHT_STALE_AFTER_SECONDS,
    MessageRecord,
    MessageStore,
    is_fresh,
    is_in_flight,
)


def task_record(status="working", thread="t1", message_id=None, stamp=1.0):
    return MessageRecord(
        message_id or f"task:{thread}",
        thread,
        "task",
        status,
        "任务",
        "正文",
        stamp,
    )


def status_record(status="working", thread="t1", stamp=1.0, action="Running tests"):
    return MessageRecord(
        f"status:{thread}:{status}",
        thread,
        "status",
        status,
        "状态",
        action,
        stamp,
        data={"task": {"turn_id": "a", "current_action": action}},
    )


class IsFreshTest(unittest.TestCase):
    def test_store_stamps_arrival_for_live_kinds_only(self):
        store = MessageStore()
        live = store.add(task_record())
        result = store.add(MessageRecord("result:t1:1", "t1", "result",
                                        "completed", "结果", "正文", 1.0))
        self.assertGreater(live.seen_at, 0.0)
        self.assertEqual(0.0, result.seen_at)

    def test_republish_moves_the_stamp_forward(self):
        store = MessageStore()
        first = store.add(task_record())
        second = store.add(task_record())
        self.assertGreaterEqual(second.seen_at, first.seen_at)

    def test_age_rule_retires_a_record_and_zero_disables_it(self):
        record = MessageRecord("task:t1", "t1", "task", "working",
                               "任务", "正文", 0.0)
        record.seen_at = 100.0
        self.assertTrue(is_fresh(record, now=100.0 + IN_FLIGHT_STALE_AFTER_SECONDS))
        self.assertFalse(is_fresh(record, now=101.0 + IN_FLIGHT_STALE_AFTER_SECONDS))
        self.assertTrue(is_fresh(record, now=10_000.0, max_age_seconds=0))

    def test_record_without_timestamps_is_treated_as_fresh(self):
        record = MessageRecord("task:t1", "t1", "task", "working",
                               "任务", "正文", 0.0)
        self.assertTrue(is_in_flight(record))
        self.assertTrue(is_fresh(record, now=time.time() + 10_000))


class StaleCountsTest(unittest.TestCase):
    def setUp(self):
        self.hub = BridgeHub(DeviceRegistry(None), read_state=None)

    def test_stale_tasks_stop_counting_as_running(self):
        for index in range(4):
            record = task_record(thread=f"t{index}", message_id=f"task:t{index}")
            self.hub.publish(record)
        self.assertEqual(4, self.hub.counts()["running"])
        for record in self.hub.store.records():
            record.seen_at = time.time() - IN_FLIGHT_STALE_AFTER_SECONDS - 1
        self.assertEqual(0, self.hub.counts()["running"])

    def test_starting_task_counts_as_running(self):
        self.hub.publish(task_record(status="starting"))

        self.assertEqual(1, self.hub.counts()["running"])

    def test_terminal_counters_are_not_age_limited(self):
        self.hub.publish(MessageRecord("result:t1:5", "t1", "result",
                                       "completed", "结果", "正文", 5.0))
        self.assertEqual(1, self.hub.counts()["completed_unread"])


class StaleSnapshotTest(unittest.TestCase):
    def test_a_stale_status_record_is_not_an_answer(self):
        self.hub = BridgeHub(DeviceRegistry(None), read_state=None)
        self.hub.publish(status_record())
        self.assertIsNotNone(self.hub.store.live_status())
        for record in self.hub.store.records():
            record.seen_at = time.time() - IN_FLIGHT_STALE_AFTER_SECONDS - 1
        self.assertIsNone(self.hub.store.live_status())

    def test_a_fresh_task_answers_when_only_the_status_is_stale(self):
        store = MessageStore()
        stale = store.add(status_record())
        stale.seen_at = time.time() - IN_FLIGHT_STALE_AFTER_SECONDS - 1
        store.add(task_record())
        live = store.live_status()
        self.assertIsNotNone(live)
        self.assertEqual("task", live.kind)

    def test_the_freshest_record_wins(self):
        store = MessageStore()
        now = time.time()
        old = store.add(task_record(thread="t1", message_id="task:t1"))
        fresh = store.add(task_record(thread="t2", message_id="task:t2"))
        old.seen_at = now - 5
        fresh.seen_at = now
        self.assertEqual("task:t2", store.live_status().message_id)


class CollectorSnapshotTest(unittest.TestCase):
    def test_the_collector_line_survives_a_republish_and_expires_afterwards(self):
        base = time.time() - 10
        snapshot = {
            "generated_at_epoch": base,
            "task": {"status": "working", "thread_id": "t1",
                     "current_action": "Running tests"},
            "tasks": [],
        }
        first = records_from_snapshot(snapshot)[0]
        store = MessageStore()
        store.add(first)
        self.assertIsNotNone(store.live_status())
        store.add(records_from_snapshot({**snapshot, "generated_at_epoch": base + 5})[0])
        live = store.live_status()
        self.assertEqual("status:t1:working", live.message_id)
        live.seen_at = time.time() - IN_FLIGHT_STALE_AFTER_SECONDS - 1
        self.assertIsNone(store.live_status())


if __name__ == "__main__":
    unittest.main()
