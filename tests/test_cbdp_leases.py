import unittest

from codexbridge.cbdp.leases import LeaseConflictError, LeaseManager


class LeaseManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.manager = LeaseManager(
            clock=lambda: self.now[0],
            lease_seconds=30,
        )

    def test_claim_returns_generation_and_expiry(self):
        lease = self.manager.claim("thread-1", "dev-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.thread_id, "thread-1")
        self.assertEqual(lease.owner, "dev-1")
        self.assertEqual(lease.generation, 1)
        self.assertEqual(lease.expires_at, 1030.0)
        self.assertTrue(self.manager.has_writer("thread-1"))

    def test_second_owner_conflicts_without_force(self):
        self.manager.claim("thread-1", "dev-1")
        with self.assertRaises(LeaseConflictError):
            self.manager.claim("thread-1", "dev-2")
        self.assertEqual(self.manager.get("thread-1").owner, "dev-1")

    def test_force_takeover_rotates_generation(self):
        first = self.manager.claim("thread-1", "dev-1")
        second = self.manager.claim("thread-1", "dev-2", force=True)
        self.assertEqual(second.owner, "dev-2")
        self.assertGreater(second.generation, first.generation)
        self.assertFalse(self.manager.release("thread-1", "dev-1", first.generation))
        self.assertEqual(self.manager.get("thread-1").owner, "dev-2")

    def test_expired_lease_can_be_reclaimed(self):
        first = self.manager.claim("thread-1", "dev-1")
        self.now[0] = first.expires_at + 1
        self.assertFalse(self.manager.has_writer("thread-1"))
        second = self.manager.claim("thread-1", "dev-2")
        self.assertEqual(second.owner, "dev-2")
        self.assertGreater(second.generation, first.generation)

    def test_release_requires_matching_generation(self):
        lease = self.manager.claim("thread-1", "dev-1")
        self.assertFalse(self.manager.release("thread-1", "dev-1", lease.generation + 1))
        self.assertTrue(self.manager.release("thread-1", "dev-1", lease.generation))
        self.assertIsNone(self.manager.get("thread-1"))

    def test_refresh_extends_only_current_generation(self):
        lease = self.manager.claim("thread-1", "dev-1")
        self.now[0] = 1010.0
        refreshed = self.manager.refresh("thread-1", "dev-1", lease.generation)
        self.assertEqual(refreshed.expires_at, 1040.0)
        self.assertIsNone(
            self.manager.refresh("thread-1", "dev-1", lease.generation + 1)
        )


if __name__ == "__main__":
    unittest.main()