import json
import tempfile
import unittest
from pathlib import Path

from codexbridge.messages_store import MessageRecord, MessageStore


class MessageStoreTest(unittest.TestCase):
    def test_terminal_results_survive_store_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "messages.json"
            store = MessageStore(path=path)
            store.add(
                MessageRecord(
                    "result:t1:10",
                    "t1",
                    "result",
                    "completed",
                    "完成",
                    "正文",
                    10.0,
                )
            )
            store.add(
                MessageRecord(
                    "result:t2:20",
                    "t2",
                    "result",
                    "failed",
                    "失败",
                    "正文",
                    20.0,
                )
            )

            reopened = MessageStore(path=path)

            self.assertEqual(
                ["result:t1:10", "result:t2:20"],
                [record.message_id for record in reopened.records()],
            )
            self.assertEqual(
                ["completed", "failed"],
                [record.status for record in reopened.records()],
            )

    def test_repeated_terminal_result_is_persisted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "messages.json"
            store = MessageStore(path=path)
            record = MessageRecord(
                "result:t1:10",
                "t1",
                "result",
                "completed",
                "完成",
                "正文",
                10.0,
            )
            store.add(record)
            store.add(record)

            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(1, len(payload["messages"]))

    def test_mark_read_records_device(self):
        store = MessageStore()
        store.add(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        store.mark_read("m1", "d1")
        self.assertEqual(store.read_by("m1"), {"d1"})

    def test_pending_filters_by_capability(self):
        store = MessageStore()
        store.add(MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0))
        self.assertEqual(
            [record.message_id for record in store.pending_for("d1", {"results"})],
            ["m1"],
        )
        self.assertEqual(store.pending_for("d1", {"status"}), [])

    def test_oldest_message_is_evicted(self):
        store = MessageStore(max_messages=2)
        for index in range(3):
            store.add(
                MessageRecord(
                    f"m{index}",
                    "t1",
                    "status",
                    "working",
                    "t",
                    "b",
                    float(index),
                )
            )
        self.assertIsNone(store.get("m0"))

    def test_replacing_message_preserves_read_state(self):
        store = MessageStore()
        store.add(MessageRecord("m1", "t1", "result", "completed", "t", "b", 0.0))
        store.mark_read("m1", "d1")
        store.add(MessageRecord("m1", "t1", "result", "completed", "t", "b", 0.0))
        self.assertEqual(store.read_by("m1"), {"d1"})

    def test_message_data_survives_replacement_and_payload(self):
        store = MessageStore()
        store.add(
            MessageRecord(
                "m1",
                "t1",
                "status",
                "working",
                "标题",
                "正文",
                1.0,
                data={"codex": {"task": {"status": "working"}}},
            )
        )
        self.assertEqual(
            store.get("m1").data["codex"]["task"]["status"],
            "working",
        )
