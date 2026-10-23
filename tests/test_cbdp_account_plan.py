import unittest

from codexbridge.cbdp.account import account_plan_payload
from codexbridge.cbdp.constants import EVENT_ACCOUNT_CHANGED, TAG_PLAN_TYPE


class AccountPlanPayloadTests(unittest.TestCase):
    def test_account_plan_is_emitted_when_present(self):
        payload = account_plan_payload({"plan_type": "plus"})
        self.assertEqual(payload[TAG_PLAN_TYPE].decode("utf-8"), "plus")

    def test_missing_or_bad_plan_is_absent(self):
        self.assertEqual(account_plan_payload({}), {})
        self.assertEqual(account_plan_payload({"plan_type": 7}), {})

    def test_account_changed_event_number_is_fixed(self):
        self.assertEqual(EVENT_ACCOUNT_CHANGED, 0x0E01)


if __name__ == "__main__":
    unittest.main()
