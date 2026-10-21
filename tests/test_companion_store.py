"""Tests for the phone companion pairing store."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.companion.store import (
    CompanionStoreError,
    DEFAULT_PORT,
    LEGACY_PORT,
    PAIRING_CODE_TTL_SECONDS,
    CompanionStore,
)
from codexbridge.devices.registry import DeviceRegistry


class CompanionStoreTests(unittest.TestCase):
    def test_corrupt_companion_file_is_quarantined_not_overwritten(self) -> None:
        self.path.write_text('{"phones": [', encoding="utf-8")
        broken = self.path.read_text(encoding="utf-8")
        store = CompanionStore(self.path)

        store.load()

        backups = list(self.path.parent.glob("companion.json.corrupt*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), broken)
        self.assertTrue(self.path.exists())
        self.assertIn("not valid JSON", store.load_warning)

    def test_unloaded_store_refuses_to_overwrite_disk(self) -> None:
        self.store.claim(self.store.pairing_code()["code"], "keep-me")
        raw = self.path.read_text(encoding="utf-8")
        fresh = CompanionStore(self.path)

        with self.assertRaises(CompanionStoreError):
            fresh.save()

        self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_missing_companion_file_recovers_phone_from_registry(self) -> None:
        self.registry.upsert_phone(
            "phone-registry",
            name="Recovered",
            token_hash="d" * 64,
            enabled=["status", "attachment"],
            last_seen=123.0,
            client_id="install-recovered",
        )
        self.registry.save()
        self.path.unlink(missing_ok=True)

        restored = CompanionStore(self.path, registry=self.registry)
        restored.load()

        record = restored.phones["phone-registry"]
        self.assertEqual(record.token_hash, "d" * 64)
        self.assertEqual(record.capabilities(), {"status", "attachment"})
        self.assertEqual(record.client_id, "install-recovered")
        self.assertTrue(self.path.exists())

    def test_phone_negotiates_optional_capabilities_and_persists_them(self):
        paired = self.store.claim(self.store.pairing_code()['code'], 'phone')
        phone_id = paired['phone_id']
        self.assertEqual(self.store.negotiate(phone_id, ['status', 'tasks', 'unknown']), ['status', 'tasks'])
        loaded = CompanionStore(self.path)
        loaded.load()
        self.assertEqual(loaded.phones[phone_id].capabilities(), {'status', 'tasks'})

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "companion.json"
        self.devices_path = Path(self.temp.name) / "devices.json"
        self.registry = DeviceRegistry(self.devices_path)
        self.registry.load()
        self.store = CompanionStore(self.path, registry=self.registry)
        self.store.load()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_pairing_code_is_six_digits(self) -> None:
        pairing = self.store.pairing_code()
        self.assertRegex(pairing["code"], r"^\d{6}$")
        self.assertAlmostEqual(
            pairing["expires_at"],
            time.time() + PAIRING_CODE_TTL_SECONDS,
            delta=5,
        )

    def test_paired_phone_is_synchronized_into_the_device_registry(self) -> None:
        claimed = self.store.claim(
            self.store.pairing_code()["code"],
            "Pixel",
            client_id="installation-1",
        )
        assert claimed is not None

        record = self.registry.get(claimed["phone_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.kind, "phone")
        self.assertEqual(record.transport, "companion_http")
        self.assertEqual(record.registration_state, "approved")
        self.assertEqual(record.registration_method, "pairing_code")
        self.assertEqual(record.client_id, "installation-1")
        self.assertEqual(record.token_hash, self.store.phones[claimed["phone_id"]].token_hash)
        self.assertNotIn(claimed["token"], self.devices_path.read_text(encoding="utf-8"))

    def test_capability_negotiation_and_touch_update_registry(self) -> None:
        claimed = self.store.claim(
            self.store.pairing_code()["code"], "Pixel"
        )
        assert claimed is not None
        self.assertEqual(
            self.store.negotiate(claimed["phone_id"], ["status", "queue"]),
            ["queue", "status"],
        )
        self.store.phones[claimed["phone_id"]].last_seen = 0.0
        self.store.touch(claimed["phone_id"])

        record = self.registry.get(claimed["phone_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.enabled, {"status", "queue"})
        self.assertGreater(record.last_seen or 0.0, 0.0)

    def test_removing_phone_removes_unified_registry_record(self) -> None:
        claimed = self.store.claim(
            self.store.pairing_code()["code"], "Pixel"
        )
        assert claimed is not None

        self.assertTrue(self.store.remove_phone(claimed["phone_id"]))

        self.assertIsNone(self.registry.get(claimed["phone_id"]))

    def test_load_merges_legacy_phone_records_into_registry(self) -> None:
        payload = {
            "version": 1,
            "enabled": True,
            "phones": [
                {
                    "phone_id": "phone-legacy",
                    "name": "Legacy",
                    "token_hash": "c" * 64,
                    "created_at": 1.0,
                    "last_seen": 2.0,
                    "enabled": ["status", "queue"],
                    "client_id": "old-install",
                }
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        store = CompanionStore(self.path, registry=self.registry)

        store.load()

        record = self.registry.get("phone-legacy")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.kind, "phone")
        self.assertEqual(record.enabled, {"status", "queue"})
        self.assertEqual(record.token_hash, "c" * 64)

    def test_same_installation_reuses_phone_id(self) -> None:
        first = self.store.claim(
            self.store.pairing_code()["code"], "旧名字", client_id="installation-1"
        )
        assert first is not None
        second = self.store.claim(
            self.store.pairing_code(regenerate=True)["code"],
            "新名字",
            client_id="installation-1",
        )
        assert second is not None
        self.assertEqual(second["phone_id"], first["phone_id"])
        self.assertNotEqual(second["token"], first["token"])

    def test_claim_with_correct_code_returns_token(self) -> None:
        code = self.store.pairing_code()["code"]
        claimed = self.store.claim(code, "小手机")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertTrue(claimed["token"].startswith("cbp_"))
        self.assertEqual(claimed["name"], "小手机")
        record = self.store.authorize(claimed["token"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.phone_id, claimed["phone_id"])
        self.assertIsNone(self.store.authorize(claimed["token"] + "x"))
        # A code is single use.
        self.assertIsNone(self.store.claim(code, "第二台"))

    def test_wrong_code_fails_and_locks_after_attempts(self) -> None:
        code = self.store.pairing_code()["code"]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertIsNone(self.store.claim(wrong, "x"))
        self.assertIsNone(self.store.claim(code, "x"))

    def test_records_persist_across_reload(self) -> None:
        code = self.store.pairing_code()["code"]
        claimed = self.store.claim(code, "备机")
        assert claimed is not None
        reloaded = CompanionStore(self.path)
        reloaded.load()
        record = reloaded.authorize(claimed["token"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.name, "备机")
        self.assertEqual(reloaded.snapshot()["phones"][0]["phone_id"], claimed["phone_id"])

    def test_remove_phone_revokes_token(self) -> None:
        code = self.store.pairing_code()["code"]
        claimed = self.store.claim(code, "备机")
        assert claimed is not None
        self.assertTrue(self.store.remove_phone(claimed["phone_id"]))
        self.assertIsNone(self.store.authorize(claimed["token"]))
        self.assertFalse(self.store.remove_phone(claimed["phone_id"]))

    def test_stale_instance_does_not_erase_newer_disk_phone(self) -> None:
        stale = CompanionStore(self.path)
        stale.load()
        code = stale.pairing_code()["code"]
        claimed = stale.claim(code, "新手机")
        assert claimed is not None
        stale.save()

        # Simulate a second instance that loaded the file before that claim.
        disk_empty = json.loads(self.path.read_text(encoding="utf-8"))
        disk_empty["phones"] = []
        self.path.write_text(json.dumps(disk_empty, ensure_ascii=False), encoding="utf-8")
        stale.touch(claimed["phone_id"])

        reloaded = CompanionStore(self.path)
        reloaded.load()
        self.assertIsNotNone(reloaded.authorize(claimed["token"]))

    def test_stale_instance_authorizes_phone_paired_by_newer_instance(self) -> None:
        stale = CompanionStore(self.path)
        stale.load()
        fresh = CompanionStore(self.path)
        fresh.load()
        claimed = fresh.claim(fresh.pairing_code()["code"], "新手机")
        assert claimed is not None

        authorized = stale.authorize(claimed["token"])

        self.assertIsNotNone(authorized)
        assert authorized is not None
        self.assertEqual(authorized.phone_id, claimed["phone_id"])

    def test_saved_payload_never_contains_plain_token(self) -> None:
        code = self.store.pairing_code()["code"]
        claimed = self.store.claim(code, "备机")
        assert claimed is not None
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(claimed["token"], raw)
        payload = json.loads(raw)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["phones"]), 1)

    def test_show_intermediate_events_defaults_off_and_persists(self) -> None:
        self.assertFalse(self.store.show_intermediate_events)
        self.assertFalse(self.store.snapshot()["show_intermediate_events"])

        self.store.set_show_intermediate_events(True)

        reloaded = CompanionStore(self.path)
        reloaded.load()
        self.assertTrue(reloaded.show_intermediate_events)
        self.assertTrue(reloaded.snapshot()["show_intermediate_events"])

    def test_default_port_is_not_the_reserved_legacy_port(self) -> None:
        self.assertEqual(LEGACY_PORT, 8790)
        self.assertEqual(DEFAULT_PORT, 8568)

    def test_legacy_port_is_migrated_and_persisted(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"version": 1}
        payload["port"] = LEGACY_PORT
        payload["phones"] = payload.get("phones") or []
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        migrated = CompanionStore(self.path)
        migrated.load()

        self.assertEqual(migrated.port, DEFAULT_PORT)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["port"], DEFAULT_PORT)

    def test_set_port_rejects_the_reserved_legacy_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "port_reserved"):
            self.store.set_port(LEGACY_PORT)

    def test_malformed_saved_capabilities_fail_closed_without_crashing(self):
        for enabled in [None, 'status', [{'tasks': True}], 3]:
            record = self.store._record_from({'phone_id': 'p', 'token_hash': 'a' * 64,
                                             'enabled': enabled})
            self.assertEqual(record.capabilities(), set())


if __name__ == "__main__":
    unittest.main()
