"""Tests for the phone companion pairing store."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.companion.store import (
    DEFAULT_PORT,
    LEGACY_PORT,
    PAIRING_CODE_TTL_SECONDS,
    CompanionStore,
)


class CompanionStoreTests(unittest.TestCase):
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
        self.store = CompanionStore(self.path)
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

    def test_saved_payload_never_contains_plain_token(self) -> None:
        code = self.store.pairing_code()["code"]
        claimed = self.store.claim(code, "备机")
        assert claimed is not None
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(claimed["token"], raw)
        payload = json.loads(raw)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["phones"]), 1)

    def test_default_port_is_not_the_reserved_legacy_port(self) -> None:
        self.assertEqual(LEGACY_PORT, 8790)
        self.assertEqual(DEFAULT_PORT, 8791)

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
