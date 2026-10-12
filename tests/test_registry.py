import json
import tempfile
import threading
import unittest
from pathlib import Path

from codexbridge.devices.registry import DeviceRegistry


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "devices.json"
        self.registry = DeviceRegistry(self.path)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        self.registry.upsert("d1", model="dotii", supported=["status", "text"])
        self.registry.save()
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertEqual(reloaded.get("d1").model, "dotii")
        self.assertEqual(reloaded.get("d1").enabled, {"status", "text", "read_ack"})

    def test_enabled_drops_unknown_capability(self):
        self.registry.upsert("d1", model="dotii")
        self.registry.set_enabled("d1", ["status", "nope"])
        self.assertEqual(self.registry.get("d1").enabled, {"status"})

    def test_mint_token_is_long_and_stable(self):
        first = self.registry.mint_token("d1")
        self.assertGreaterEqual(len(first), 16)
        self.assertEqual(first, self.registry.mint_token("d1"))

    def test_pairing_model_defaults_apply_after_discovery(self):
        self.registry.mint_token("d1")
        record = self.registry.upsert(
            "d1",
            model="dotii",
            name="Desk",
            supported=["status", "text", "tasks", "results", "read_ack"],
        )
        self.assertEqual(
            record.enabled,
            {"status", "text", "tasks", "results", "read_ack"},
        )

    def test_migrate_legacy_bridge_json(self):
        legacy = Path(self.tmp.name) / "bridge.json"
        legacy.write_text(json.dumps({"host": "192.168.1.23", "port": 8765, "token": "abcdefghijklmnop"}))
        self.assertTrue(self.registry.migrate_legacy(legacy))
        record = self.registry.get("legacy:192.168.1.23")
        self.assertEqual(record.token, "abcdefghijklmnop")

    def test_concurrent_saves_do_not_raise(self):
        for device_id in range(8):
            self.registry.upsert(
                f"d{device_id}",
                model="dotii",
                supported=["status"],
            )
        barrier = threading.Barrier(8)
        errors = []

        def save_repeatedly():
            barrier.wait()
            try:
                for _ in range(20):
                    self.registry.save()
            except OSError as error:
                errors.append(error)

        threads = [threading.Thread(target=save_repeatedly) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
