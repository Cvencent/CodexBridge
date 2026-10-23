import unittest

from codexbridge.cbdp.account import account_rate_limits_payload
from codexbridge.cbdp.constants import (
    EVENT_RATE_LIMITS_CHANGED,
    EVENT_TOKEN_USAGE_CHANGED,
    QOS_STATE,
    QOS_TELEMETRY,
    TAG_RATE_LIMITS,
)
from codexbridge.cbdp.events import EventSpine, event_scope
from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
from codexbridge.hub import BridgeHub, EVENT_CODEX_STATE
from tests.test_cbdp_hub import _CbdpSession, _GatewaySubscriptionStub, _Registry


class AccountPublishTests(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.events = EventSpine(Path(tmp.name) / "events.sqlite3")
        self.addCleanup(self.events.close)
        self.registry = _Registry()
        self.hub = BridgeHub(self.registry, event_spine=self.events)
        self.hub._append_event = self.hub._append_event

    def _account_device(self, device_id="d1"):
        record = self.registry.records.get(device_id)
        if record is None:
            record = type("R", (), {})()
            record.supported = {"status", "account"}
            record.enabled = {"status", "account"}
            record.max_text_chars = 4096
            record.profile = 2
            self.registry.records[device_id] = record
        return record

    def test_rate_limits_publish_is_summary_state_event(self):
        self._account_device("d1")
        session = _CbdpSession()
        gateway = _GatewaySubscriptionStub(True, session)
        session.gateway = gateway
        self.hub.attach("d1", session)

        published = self.hub.publish_account(
            {
                "five_hour_remaining_percent": 64,
                "five_hour_reset_at": 1770000000,
                "weekly_remaining_percent": 71,
            }
        )

        self.assertTrue(published)
        event = self.events.latest_event(
            lambda item: int(item.event_type) == EVENT_RATE_LIMITS_CHANGED
        )
        self.assertIsNotNone(event)
        self.assertEqual(int(event.qos), QOS_STATE)
        self.assertEqual(int(event.topic_id), SYSTEM_THREAD_SUMMARIES_TOPIC)
        self.assertEqual(event_scope(EVENT_RATE_LIMITS_CHANGED), 0)
        self.assertIn(TAG_RATE_LIMITS, event.payload)

    def test_token_usage_publish_uses_telemetry_qos(self):
        self._account_device("d1")
        session = _CbdpSession()
        gateway = _GatewaySubscriptionStub(True, session)
        session.gateway = gateway
        self.hub.attach("d1", session)

        self.hub.publish_account({"seven_day_tokens": 12345})

        event = self.events.latest_event(
            lambda item: int(item.event_type) == EVENT_TOKEN_USAGE_CHANGED
        )
        self.assertIsNotNone(event)
        self.assertEqual(int(event.qos), QOS_TELEMETRY)

    def test_unchanged_account_snapshot_is_not_republished(self):
        self._account_device("d1")
        session = _CbdpSession()
        gateway = _GatewaySubscriptionStub(True, session)
        session.gateway = gateway
        self.hub.attach("d1", session)

        quota = {"weekly_remaining_percent": 71}
        self.assertTrue(self.hub.publish_account(quota))
        before = self.events.latest_event(
            lambda item: int(item.event_type) == EVENT_RATE_LIMITS_CHANGED
        )
        self.assertFalse(self.hub.publish_account(dict(quota)))
        after = self.events.latest_event(
            lambda item: int(item.event_type) == EVENT_RATE_LIMITS_CHANGED
        )
        self.assertEqual(before.global_seq, after.global_seq)

    def test_empty_snapshot_publishes_nothing(self):
        self.assertFalse(self.hub.publish_account({}))
        self.assertIsNone(
            self.events.latest_event(
                lambda item: int(item.event_type) == EVENT_RATE_LIMITS_CHANGED
            )
        )


if __name__ == "__main__":
    unittest.main()
