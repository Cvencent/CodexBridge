import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_version_one_hardware_record_migrates_to_unified_fields(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": {
                        "d1": {
                            "device_id": "d1",
                            "model": "dotii",
                            "token": "hardware-token-abcdef",
                            "supported": ["status", "text"],
                        },
                        "d2": {
                            "device_id": "d2",
                            "model": "dotii",
                            "supported": ["status"],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        self.registry.load()

        approved = self.registry.get("d1")
        self.assertEqual(approved.kind, "hardware")
        self.assertEqual(approved.transport, "device_tcp")
        self.assertEqual(approved.registration_state, "approved")
        self.assertTrue(approved.has_token)
        pending = self.registry.get("d2")
        self.assertEqual(pending.registration_state, "pending")
        self.assertFalse(pending.has_token)

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["version"],
            2,
        )

    def test_phone_and_hardware_public_views_hide_private_tokens(self):
        self.registry.upsert(
            "d1",
            model="dotii",
            token="hardware-token-abcdef",
            supported=["status"],
        )
        self.registry.upsert_phone(
            "phone-1",
            name="Pixel",
            token_hash="a" * 64,
            supported=["status", "queue"],
            enabled=["status", "queue"],
        )

        hardware = self.registry.public("d1")
        phone = self.registry.public("phone-1")

        self.assertEqual(hardware["kind"], "hardware")
        self.assertTrue(hardware["has_token"])
        self.assertEqual(phone["kind"], "phone")
        self.assertEqual(phone["transport"], "companion_http")
        self.assertEqual(phone["registration_method"], "pairing_code")
        self.assertTrue(phone["has_token"])
        rendered = json.dumps([hardware, phone])
        self.assertNotIn("hardware-token-abcdef", rendered)
        self.assertNotIn("a" * 64, rendered)
        self.assertNotIn('"token"', rendered)
        self.assertNotIn('"token_hash"', rendered)

    def test_unified_fields_round_trip_in_version_two(self):
        self.registry.upsert_phone(
            "phone-1",
            name="Pixel",
            token_hash="b" * 64,
            supported=["status", "queue"],
            enabled=["status", "queue"],
            last_seen=123.0,
            client_id="install-1",
        )
        self.registry.save()

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        reloaded = DeviceRegistry(self.path)
        reloaded.load()

        phone = reloaded.get("phone-1")
        self.assertEqual(phone.kind, "phone")
        self.assertEqual(phone.transport, "companion_http")
        self.assertEqual(phone.registration_state, "approved")
        self.assertEqual(phone.registration_method, "pairing_code")
        self.assertEqual(phone.client_id, "install-1")
        self.assertEqual(phone.token_hash, "b" * 64)

    def test_public_all_marks_online_records(self):
        self.registry.upsert("d1", model="dotii")
        self.registry.upsert("d2", model="dotii")

        rows = self.registry.public_all(online_ids={"d2"}, queued_counts={"d2": 3})

        by_id = {row["device_id"]: row for row in rows}
        self.assertFalse(by_id["d1"]["online"])
        self.assertTrue(by_id["d2"]["online"])
        self.assertEqual(by_id["d2"]["queued_count"], 3)

    def test_enabled_drops_unknown_capability(self):
        self.registry.upsert("d1", model="dotii")
        self.registry.set_enabled("d1", ["status", "nope"])
        self.assertEqual(self.registry.get("d1").enabled, {"status"})

    def test_declared_text_budget_round_trips(self):
        self.registry.upsert("d1", model="FoloOS-AI-Passport")
        self.registry.get("d1").max_text_chars = 512
        self.registry.save()
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertEqual(reloaded.get("d1").max_text_chars, 512)

    def test_unlimited_budget_defaults_to_none(self):
        self.registry.upsert("d1", model="dotii")
        self.assertIsNone(self.registry.get("d1").max_text_chars)

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

    def test_cbdp_device_keeps_declared_capabilities_enabled(self):
        record = self.registry.upsert(
            "d1",
            model="dotii",
            protocol="cbdp",
            supported=[
                "status",
                "text",
                "tasks",
                "results",
                "read_ack",
                "subscribe_live",
                "subscribe_summary",
                "multi_page",
                "device_config",
                "account",
                "events_since",
            ],
        )
        self.assertIn("subscribe_live", record.enabled)
        self.assertIn("subscribe_summary", record.enabled)
        self.assertIn("multi_page", record.enabled)
        self.assertIn("device_config", record.enabled)
        self.assertIn("account", record.enabled)
        self.assertIn("events_since", record.enabled)

    def test_legacy_device_keeps_model_defaults(self):
        record = self.registry.upsert(
            "d1",
            model="dotii",
            protocol="legacy_json",
            supported=["status", "text", "tasks", "subscribe_live"],
        )
        self.assertEqual(
            record.enabled,
            {"status", "text", "tasks", "read_ack"},
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

    def test_save_retries_transient_replace_denial(self):
        self.registry.upsert("d1", model="dotii", supported=["status"])
        real_replace = os.replace
        calls = []

        def flaky_replace(source, destination):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(13, "Access is denied")
            return real_replace(source, destination)

        with patch("codexbridge.devices.registry.os.replace", side_effect=flaky_replace):
            self.registry.save()

        self.assertEqual(len(calls), 2)
        self.assertTrue(self.path.exists())


class CapabilityMigrationTest(unittest.TestCase):
    """A device that grows new abilities must not need host-side rework."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.addCleanup(self.tmp.cleanup)

    def test_new_device_capability_is_enabled_when_not_customized(self):
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack"])
        self.assertNotIn("audio", self.registry.get("p1").enabled)
        # Firmware grows a microphone and re-declares it on the next handshake.
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack", "audio", "voice"])
        enabled = self.registry.get("p1").enabled
        self.assertIn("audio", enabled)
        self.assertIn("voice", enabled)

    def test_dropped_capability_is_removed_from_enabled(self):
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack", "audio"])
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack"])
        self.assertNotIn("audio", self.registry.get("p1").enabled)

    def test_manual_choice_survives_a_redeclaration(self):
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack", "audio", "voice"])
        self.registry.set_enabled("p1", ["status", "text"])
        self.assertTrue(self.registry.get("p1").customized)
        self.registry.upsert("p1", model="FoloOS-AI-Passport",
                             supported=["status", "text", "read_ack", "audio", "voice"])
        # The user turned voice off on purpose; the host must not flip it back.
        self.assertEqual(self.registry.get("p1").enabled, {"status", "text"})

    def test_customized_flag_round_trips(self):
        self.registry.upsert("p1", model="FoloOS-AI-Passport")
        self.registry.set_enabled("p1", ["status"])
        self.registry.save()
        reloaded = DeviceRegistry(self.registry.path)
        reloaded.load()
        self.assertTrue(reloaded.get("p1").customized)
