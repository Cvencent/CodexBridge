import tempfile
import unittest
from pathlib import Path

from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.subscriptions import SubscriptionManager, SubscriptionLimitError


class SubscriptionManagerTests(unittest.TestCase):
    def make(self, depth=8):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spine = EventSpine(Path(tmp.name) / "events.sqlite3")
        self.addCleanup(spine.close)
        manager = SubscriptionManager(spine)
        manager.set_queue_depth("dev-1", depth)
        return spine, manager

    def test_subscribe_replays_events_after_cursor(self):
        spine, manager = self.make()
        first = spine.append(0x10000001, 0x0101, 1, {})
        second = spine.append(0x10000001, 0x0102, 1, {})
        result = manager.subscribe("dev-1", 0x10000001, 1, 1, cursor=first.global_seq)
        self.assertTrue(result.recoverable)
        self.assertEqual([item.global_seq for item in result.replay], [second.global_seq])
        self.assertEqual(result.cursor, second.global_seq)

    def test_ack_cursor_advances_only_for_its_subscription(self):
        spine, manager = self.make()
        event = spine.append(0x10000001, 0x0101, 1, {})
        manager.subscribe("dev-1", 0x10000001, 1, 1, cursor=event.global_seq)
        manager.subscribe("dev-1", 0x10000001, 2, 1, cursor=event.global_seq)
        manager.ack("dev-1", 0x10000001, 1, event.global_seq)
        self.assertEqual(manager.cursor("dev-1", 0x10000001, 1), event.global_seq)
        self.assertEqual(manager.cursor("dev-1", 0x10000001, 2), event.global_seq)

    def test_unrecoverable_cursor_requires_resync(self):
        spine, manager = self.make()
        for index in range(5):
            spine.append(0x10000001, 0x0101, 1, {})
        spine._connection.execute("DELETE FROM events WHERE global_seq <= 4")
        spine._connection.commit()
        row = spine._connection.execute("SELECT MIN(global_seq) FROM events").fetchone()
        self.assertEqual(int(row[0]), 5)
        result = manager.subscribe("dev-1", 0x10000001, 1, 1, cursor=1)
        self.assertFalse(result.recoverable)
        self.assertEqual(result.replay, [])
        self.assertEqual(result.cursor, 0)

    def test_subscription_limit_is_enforced(self):
        _spine, manager = self.make()
        manager.set_max_subscriptions("dev-1", 1)
        manager.subscribe("dev-1", 0x10000001, 1, 1)
        with self.assertRaises(SubscriptionLimitError):
            manager.subscribe("dev-1", 0x10000002, 1, 1)

    def test_state_queue_coalesces_to_latest(self):
        _spine, manager = self.make(depth=2)
        manager.subscribe("dev-1", 0x10000001, 1, 1)
        manager.deliver(0x10000001, 1, 4, 1)
        manager.deliver(0x10000001, 1, 5, 1)
        manager.deliver(0x10000001, 1, 6, 1)
        self.assertEqual(manager.pending_cursors("dev-1", 0x10000001, 1), [6])


if __name__ == "__main__":
    unittest.main()
