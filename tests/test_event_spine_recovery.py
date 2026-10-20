import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexbridge.cbdp.events import EventSpine


class EventSpineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "events.sqlite3"

    def _append(self, count=4, topic_id=0x50000003):
        spine = EventSpine(self.path)
        for index in range(count):
            spine.append(topic_id, 0x0704 + index, 1, {})
        spine.close()

    def test_healthy_store_is_not_recovered_or_backed_up(self):
        self._append()
        before = self.path.read_bytes()
        messages = []

        spine = EventSpine(self.path, logger=messages.append)
        self.addCleanup(spine.close)

        self.assertFalse(spine.recovered)
        self.assertEqual(spine.recovery_mode, "healthy")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(messages, [])
        self.assertFalse(list(self.root.glob("*.corrupt-*")))

    def test_corrupt_store_is_vacuumed_and_keeps_rows(self):
        self._append(count=19)
        messages = []

        # Real index-page corruption cannot be produced portably by a SQL
        # statement. Simulate the corruption signal once, then exercise the
        # real VACUUM INTO path and validate the resulting store.
        with mock.patch.object(
            EventSpine,
            "_quick_check",
            side_effect=["simulated corruption", "ok"],
        ):
            spine = EventSpine(self.path, logger=messages.append)
        self.addCleanup(spine.close)

        self.assertTrue(spine.recovered)
        self.assertEqual(spine.recovery_mode, "vacuum")
        self.assertIsNotNone(spine.recovery_backup)
        self.assertTrue(spine.recovery_backup.exists())
        self.assertEqual(EventSpine._quick_check(self.path), "ok")
        self.assertEqual(len(spine.events_latest(topic_id=0x50000003, limit=100)), 19)
        self.assertTrue(any("mode=vacuum" in message for message in messages))

    def test_failed_vacuum_keeps_backup_and_opens_clean_store(self):
        self._append()

        def fail_vacuum(_self, _source, _destination):
            raise sqlite3.DatabaseError("simulated vacuum failure")

        with mock.patch.object(EventSpine, "_quick_check", return_value="simulated corruption"), mock.patch.object(
            EventSpine,
            "_vacuum_copy",
            fail_vacuum,
        ):
            spine = EventSpine(self.path)
            self.addCleanup(spine.close)

        self.assertTrue(spine.recovered)
        self.assertEqual(spine.recovery_mode, "fresh")
        self.assertIsNotNone(spine.recovery_backup)
        self.assertTrue(spine.recovery_backup.exists())
        self.assertEqual(EventSpine._quick_check(self.path), "ok")
        self.assertEqual(spine.events_latest(topic_id=0x50000003, limit=10), [])

    def test_recovery_that_loses_rows_is_not_installed(self):
        self._append(count=6)

        def lose_rows(_self, source, destination):
            Path(destination).write_bytes(b"")
            connection = sqlite3.connect(str(destination))
            EventSpine._create_schema(connection)
            connection.execute(
                "INSERT INTO events(event_id, topic_id, topic_seq, revision, "
                "event_type, qos, payload, created_at_ms) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("a" * 32, 0x50000003, 1, 1, 0x0704, 1, b"", 1),
            )
            connection.commit()
            connection.close()

        def fail_rescue(_self, _source, _destination):
            raise sqlite3.DatabaseError("simulated row rescue failure")

        with mock.patch.object(
            EventSpine,
            "_quick_check",
            side_effect=["simulated corruption", "ok"],
        ), mock.patch.object(
            EventSpine,
            "_vacuum_copy",
            lose_rows,
        ), mock.patch.object(
            EventSpine,
            "_rescue_copy",
            fail_rescue,
        ):
            spine = EventSpine(self.path)
            self.addCleanup(spine.close)

        self.assertEqual(spine.recovery_mode, "fresh")
        self.assertIn("row rescue", spine.recovery_error)
        self.assertIsNotNone(spine.recovery_backup)
        self.assertTrue(spine.recovery_backup.exists())
        self.assertEqual(spine.events_latest(topic_id=0x50000003, limit=10), [])

    def test_row_rescue_preserves_history_when_vacuum_fails(self):
        self._append(count=9)

        def fail_vacuum(_self, _source, _destination):
            raise sqlite3.DatabaseError("simulated vacuum failure")

        with mock.patch.object(
            EventSpine,
            "_quick_check",
            side_effect=["simulated corruption", "ok"],
        ), mock.patch.object(EventSpine, "_vacuum_copy", fail_vacuum):
            spine = EventSpine(self.path)
            self.addCleanup(spine.close)

        self.assertEqual(spine.recovery_mode, "row_rescue")
        self.assertEqual(len(spine.events_latest(topic_id=0x50000003, limit=100)), 9)
        self.assertEqual(EventSpine._quick_check(self.path), "ok")

    def test_vacuum_that_loses_rows_falls_back_to_row_rescue(self):
        self._append(count=9)

        def lose_rows(_self, _source, destination):
            Path(destination).write_bytes(b"")
            connection = sqlite3.connect(str(destination))
            EventSpine._create_schema(connection)
            connection.execute(
                "INSERT INTO events(event_id, topic_id, topic_seq, revision, "
                "event_type, qos, payload, created_at_ms) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("a" * 32, 0x50000003, 1, 1, 0x0704, 1, b"", 1),
            )
            connection.commit()
            connection.close()

        with mock.patch.object(
            EventSpine,
            "_quick_check",
            side_effect=["simulated corruption", "ok", "ok"],
        ), mock.patch.object(EventSpine, "_vacuum_copy", lose_rows):
            spine = EventSpine(self.path)
            self.addCleanup(spine.close)

        self.assertEqual(spine.recovery_mode, "row_rescue")
        self.assertEqual(len(spine.events_latest(topic_id=0x50000003, limit=100)), 9)

    def test_replaced_store_removes_stale_wal_and_shm(self):
        self._append()
        stale_wal = b"stale wal"
        stale_shm = b"stale shm"
        wal_path = Path(str(self.path) + "-wal")
        shm_path = Path(str(self.path) + "-shm")
        wal_path.write_bytes(stale_wal)
        shm_path.write_bytes(stale_shm)

        with mock.patch.object(
            EventSpine,
            "_quick_check",
            side_effect=["simulated corruption", "ok"],
        ):
            spine = EventSpine(self.path)
        self.addCleanup(spine.close)

        self.assertEqual(spine.recovery_mode, "vacuum")
        # A replacement database may legitimately create a new WAL. What must
        # not survive is the old bytes being replayed into the recovered store.
        self.assertNotEqual(wal_path.read_bytes(), stale_wal)
        self.assertNotEqual(shm_path.read_bytes(), stale_shm)
        self.assertEqual(
            Path(str(spine.recovery_backup) + "-wal").read_bytes(),
            stale_wal,
        )
        self.assertEqual(
            Path(str(spine.recovery_backup) + "-shm").read_bytes(),
            stale_shm,
        )


if __name__ == "__main__":
    unittest.main()
