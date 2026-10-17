"""A phone must never be locked out by a pair response it never received.

Re-pairing used to overwrite the stored secret immediately. If the HTTP
response carrying the new token was lost -- the usual result of a bridge
restart dropping the socket mid-write -- the phone kept presenting a secret
the store no longer knew, every request answered 401, and the only way back
was for a person to notice and pair again by hand.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.companion import store as store_module
from codexbridge.companion.store import CompanionStore


class TokenGraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "companion.json"
        self.store = CompanionStore(self.path, port=8568)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _pair(self, client_id: str) -> dict:
        code = self.store.pairing_code(regenerate=True)["code"]
        return self.store.claim(code, "My Phone", client_id=client_id)

    def test_the_first_token_keeps_working_after_a_repair(self):
        first = self._pair("install-1")
        second = self._pair("install-1")
        self.assertNotEqual(first["token"], second["token"])
        self.assertIsNotNone(self.store.authorize(first["token"]))
        self.assertIsNotNone(self.store.authorize(second["token"]))

    def test_using_the_new_token_retires_the_old_one(self):
        first = self._pair("install-1")
        second = self._pair("install-1")
        self.assertIsNotNone(self.store.authorize(second["token"]))
        self.assertIsNone(self.store.authorize(first["token"]))

    def test_the_old_token_stops_working_once_the_window_closes(self):
        # The allowance is a bridge for one lost response, not a second
        # permanent credential: an old secret must not outlive the window.
        first = self._pair("install-1")
        original = store_module.TOKEN_GRACE_SECONDS
        store_module.TOKEN_GRACE_SECONDS = -1.0
        try:
            self._pair("install-1")
        finally:
            store_module.TOKEN_GRACE_SECONDS = original
        self.assertIsNone(self.store.authorize(first["token"]))

    def test_several_lost_pair_responses_still_leave_a_working_secret(self):
        # A bridge that restarts repeatedly can drop more than one response.
        secrets = [self._pair("install-1")["token"] for _ in range(4)]
        for token in secrets:
            self.assertIsNotNone(self.store.authorize(token), token)

    def test_the_ledger_is_bounded(self):
        # Allowing every token forever would be a growing set of live
        # credentials, so only the most recent few are honoured.
        secrets = [self._pair("install-1")["token"] for _ in range(9)]
        self.assertIsNone(self.store.authorize(secrets[0]))
        self.assertIsNotNone(self.store.authorize(secrets[-1]))

    def test_the_newest_secret_clears_the_whole_allowance(self):
        secrets = [self._pair("install-1")["token"] for _ in range(3)]
        self.assertIsNotNone(self.store.authorize(secrets[-1]))
        for token in secrets[:-1]:
            self.assertIsNone(self.store.authorize(token))

    def test_the_grace_window_is_persisted(self):
        first = self._pair("install-1")
        self._pair("install-1")
        reloaded = CompanionStore(self.path, port=8568)
        reloaded.load()
        self.assertIsNotNone(reloaded.authorize(first["token"]))

    def test_an_unknown_token_is_still_refused(self):
        self._pair("install-1")
        self.assertIsNone(self.store.authorize("cbp_not_a_real_token"))
        self.assertIsNone(self.store.authorize(""))

    def test_a_second_phone_is_not_given_another_phones_grace(self):
        first = self._pair("install-1")
        other = self._pair("install-2")
        self.assertIsNotNone(self.store.authorize(other["token"]))
        # presenting phone 1's secret must resolve to phone 1, not to whoever
        # re-paired last
        record = self.store.authorize(first["token"])
        self.assertIsNotNone(record)
        self.assertEqual(record.client_id, "install-1")

    def test_a_fresh_pair_works_immediately(self):
        claimed = self._pair("install-1")
        self.assertIsNotNone(self.store.authorize(claimed["token"]))

    def test_removing_a_phone_revokes_both_secrets(self):
        first = self._pair("install-1")
        second = self._pair("install-1")
        self.assertTrue(self.store.remove_phone(second["phone_id"]))
        self.assertIsNone(self.store.authorize(first["token"]))
        self.assertIsNone(self.store.authorize(second["token"]))

    def test_a_legacy_record_without_client_id_gets_one_grace(self):
        # Phones paired before installation identity existed present an empty
        # client_id, so a re-pair creates a new record rather than rotating.
        code = self.store.pairing_code(regenerate=True)["code"]
        legacy = self.store.claim(code, "Old Phone")
        fresh = self._pair("install-new")
        self.assertIsNotNone(self.store.authorize(legacy["token"]))
        self.assertIsNotNone(self.store.authorize(fresh["token"]))
