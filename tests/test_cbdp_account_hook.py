import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codexbridge.cbdp.constants import (
    EVENT_RATE_LIMITS_CHANGED,
    EVENT_TOKEN_USAGE_CHANGED,
)
from codexbridge.cbdp.events import EventSpine
from codexbridge.messages_store import MessageRecord
from tests.test_cbdp_account_publish import AccountPublishTests


class CollectorHookTests(unittest.TestCase):
    def test_codex_status_record_publishes_account_events(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events = EventSpine(Path(tmp.name) / "events.sqlite3")
        self.addCleanup(events.close)
        from tests.test_cbdp_account_publish import _CbdpSession, _GatewaySubscriptionStub
        from tests.test_cbdp_hub import _Registry
        from codexbridge.hub import BridgeHub

        registry = _Registry()
        record = type("R", (), {})()
        record.supported = {"status", "account"}
        record.enabled = {"status", "account"}
        record.max_text_chars = 4096
        record.profile = 2
        registry.records["d1"] = record
        hub = BridgeHub(registry, event_spine=events)
        session = _CbdpSession()
        session.gateway = _GatewaySubscriptionStub(True, session)
        hub.attach("d1", session)

        hub.publish(
            MessageRecord(
                "codex_status:account",
                "account",
                "codex_status",
                "working",
                "Codex 配额与状态",
                "",
                0.0,
                data={
                    "quota": {
                        "five_hour_remaining_percent": 64,
                        "weekly_remaining_percent": 71,
                        "seven_day_tokens": 12345,
                    }
                },
            )
        )

        types = [
            int(event.event_type)
            for event in (
                events.latest_event(
                    lambda item: int(item.event_type) == EVENT_RATE_LIMITS_CHANGED
                ),
                events.latest_event(
                    lambda item: int(item.event_type) == EVENT_TOKEN_USAGE_CHANGED
                ),
            )
            if event is not None
        ]
        self.assertIn(EVENT_RATE_LIMITS_CHANGED, types)
        self.assertIn(EVENT_TOKEN_USAGE_CHANGED, types)


if __name__ == "__main__":
    unittest.main()
