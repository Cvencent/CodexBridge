import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.companion.queue import QueueService
from codexbridge.protocol.commands import CommandError


class FakeTarget:
    def __init__(self):
        self.active = False
        self.starts = []
        self.steers = []
        self.fail_start = False
        self.events = []

    def thread_exists(self, thread_id):
        return thread_id == "thread-1"

    def is_active(self, thread_id):
        return self.active

    def start_turn(self, thread_id, text, model="", effort="", client_message_id=""):
        self.starts.append({
            "thread_id": thread_id,
            "text": text,
            "model": model,
            "effort": effort,
            "client_message_id": client_message_id,
        })
        if self.fail_start:
            raise RuntimeError("codex unavailable")
        self.active = True
        return {"thread_id": thread_id, "turn_id": "turn-1", "status": "working"}

    def steer_turn(self, thread_id, text):
        if not self.active:
            raise ValueError("no active turn")
        self.steers.append({"thread_id": thread_id, "text": text})
        return {"thread_id": thread_id, "turn_id": "turn-1", "status": "working"}

    def publish(self, event):
        self.events.append(event)


class QueueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "queues.json"
        self.target = FakeTarget()
        self.service = self._make()

    def tearDown(self):
        self.service.stop()
        self.temp.cleanup()

    def _make(self):
        return QueueService(
            self.path,
            start_turn=self.target.start_turn,
            steer_turn=self.target.steer_turn,
            thread_exists=self.target.thread_exists,
            is_active=self.target.is_active,
            publish=self.target.publish,
            clock=lambda: 1770000000.0,
        )

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_enqueue_persists_and_reloads_pending_item(self):
        self.target.active = True
        result = self.service.enqueue("thread-1", "继续检查")
        self.assertEqual(result["item"]["status"], "pending")

        self.service.stop()
        reloaded = self._make()
        try:
            snapshot = reloaded.list("thread-1")
            self.assertEqual(snapshot["items"][0]["text"], "继续检查")
            self.assertEqual(snapshot["items"][0]["status"], "pending")
        finally:
            reloaded.stop()

    def test_idle_enqueue_dispatches_once_and_removes_item(self):
        self.service.start()
        result = self.service.enqueue("thread-1", "立即发送")
        self.assertTrue(self.wait_for(lambda: len(self.target.starts) == 1))
        self.assertEqual(self.target.starts[0]["text"], "立即发送")
        self.assertEqual(
            self.target.starts[0]["client_message_id"],
            result["item"]["queue_id"],
        )
        self.assertTrue(self.wait_for(lambda: not self.service.list("thread-1")["items"]))
        self.assertTrue(any(
            event.get("type") == "queue_item_removed"
            and event.get("reason") == "dispatched"
            for event in self.target.events
        ))

    def test_active_thread_keeps_new_item_pending(self):
        self.target.active = True
        self.service.start()
        result = self.service.enqueue("thread-1", "排队消息")
        time.sleep(0.1)
        self.assertEqual(self.target.starts, [])
        self.assertEqual(
            self.service.list("thread-1")["items"][0]["queue_id"],
            result["item"]["queue_id"],
        )

    def test_edit_requires_matching_revision(self):
        self.target.active = True
        item = self.service.enqueue("thread-1", "旧内容")["item"]
        with self.assertRaisesRegex(CommandError, "queue_conflict"):
            self.service.edit(
                item["queue_id"],
                "新内容",
                expected_revision=item["revision"] + 1,
            )
        updated = self.service.edit(
            item["queue_id"],
            "新内容",
            expected_revision=item["revision"],
        )["item"]
        self.assertEqual(updated["text"], "新内容")
        self.assertEqual(updated["revision"], item["revision"] + 1)

    def test_delete_removes_pending_item(self):
        self.target.active = True
        item = self.service.enqueue("thread-1", "删除我")["item"]
        result = self.service.delete(
            item["queue_id"],
            expected_revision=item["revision"],
        )
        self.assertTrue(result["removed"])
        self.assertEqual(self.service.list("thread-1")["items"], [])

    def test_steer_requires_active_turn_and_removes_item(self):
        self.target.active = True
        item = self.service.enqueue("thread-1", "注入当前回合")["item"]
        result = self.service.steer(
            item["queue_id"],
            expected_revision=item["revision"],
        )
        self.assertEqual(result["item"]["status"], "steered")
        self.assertEqual(self.target.steers[0]["text"], "注入当前回合")
        self.assertEqual(self.service.list("thread-1")["items"], [])

    def test_failed_dispatch_pauses_until_edit(self):
        self.target.fail_start = True
        self.service.start()
        item = self.service.enqueue("thread-1", "失败消息")["item"]
        self.assertTrue(self.wait_for(
            lambda: self.service.list("thread-1")["items"][0]["status"] == "failed"
        ))
        self.assertTrue(self.service.list("thread-1")["paused"])

        self.target.fail_start = False
        self.target.active = True
        current = self.service.list("thread-1")["items"][0]
        self.service.edit(
            item["queue_id"],
            "修正后的消息",
            expected_revision=current["revision"],
        )
        self.assertFalse(self.service.list("thread-1")["paused"])

    def test_delete_failed_item_clears_pause_and_new_message_dispatches(self):
        self.target.fail_start = True
        self.service.start()
        item = self.service.enqueue("thread-1", "失败消息")["item"]
        self.assertTrue(self.wait_for(
            lambda: self.service.list("thread-1")["items"][0]["status"] == "failed"
        ))
        self.assertTrue(self.service.list("thread-1")["paused"])

        current = self.service.list("thread-1")["items"][0]
        result = self.service.delete(
            item["queue_id"], expected_revision=current["revision"]
        )
        self.assertFalse(result["paused"])

        self.target.fail_start = False
        self.service.enqueue("thread-1", "重发")
        self.assertTrue(self.wait_for(lambda: len(self.target.starts) == 2))

    def test_enqueue_on_paused_thread_clears_pause(self):
        self.target.fail_start = True
        self.service.start()
        self.service.enqueue("thread-1", "失败消息")
        self.assertTrue(self.wait_for(lambda: self.service.list("thread-1")["paused"]))

        self.target.fail_start = False
        result = self.service.enqueue("thread-1", "重发")
        self.assertFalse(result["paused"])
        self.assertTrue(self.wait_for(lambda: len(self.target.starts) == 2))

    def test_completed_turn_clears_stale_pause(self):
        self.target.fail_start = True
        self.service.start()
        self.service.enqueue("thread-1", "失败消息")
        self.assertTrue(self.wait_for(lambda: self.service.list("thread-1")["paused"]))

        self.target.fail_start = False
        self.target.active = True
        self.service.on_turn_finished("thread-1")
        self.assertFalse(self.service.list("thread-1")["paused"])

    def test_deleting_queued_item_keeps_pause_while_failure_remains(self):
        self.target.active = True
        self.service.enqueue("thread-1", "第一条")
        second = self.service.enqueue("thread-1", "第二条")["item"]
        self.target.fail_start = True
        self.target.active = False
        self.service.start()
        self.assertTrue(self.wait_for(lambda: self.service.list("thread-1")["paused"]))

        # Dispatch order is by (created_at, queue_id); with a frozen clock the
        # failed row may be either item, so delete the row that is still pending.
        current = next(
            value
            for value in self.service.list("thread-1")["items"]
            if value["status"] == "pending"
        )
        result = self.service.delete(
            current["queue_id"], expected_revision=current["revision"]
        )
        self.assertTrue(result["paused"])


if __name__ == "__main__":
    unittest.main()
