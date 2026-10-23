import unittest

from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    EVENT_RATE_LIMITS_CHANGED,
    TAG_EVENT_TYPE,
    TAG_RATE_LIMITS,
    TYPE_SUBSCRIBE,
    FLAG_REQUEST,
    TAG_REQUEST_ID,
    TAG_TOPIC_ID,
    TAG_SCOPE,
    TAG_QOS,
    TAG_CURSOR,
)
from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.frame import Frame
from codexbridge.cbdp.gateway import CbdpGateway
from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
from codexbridge.cbdp.subscriptions import SubscriptionManager
from codexbridge.cbdp.tlv import decode_u16, u8, u32, u64


class _Session:
    def __init__(self, capabilities):
        self.device_id = "dev-1"
        self.generation = 1
        self.ready = True
        self.session_id = b"x" * 16
        self.authenticated = True
        self.profile = 2
        self.supported = capability_bitmap(capabilities)
        self.enabled = capability_bitmap(capabilities)
        self.transport_allowed = capability_bitmap(capabilities)
        self.effective = capability_bitmap(capabilities)


def _gateway(capabilities):
    spine = EventSpine(None)
    subscriptions = SubscriptionManager(spine)
    gateway = CbdpGateway(
        commands=lambda: None,
        event_spine=spine,
        subscriptions=subscriptions,
        capability_policy=lambda device_id: set(capabilities),
    )
    gateway.session = _Session(capabilities)
    gateway.refresh_capabilities()
    emitted = []
    gateway._emit = lambda frame: emitted.append(frame)
    return spine, gateway, emitted


def _subscribe(gateway):
    gateway._handle_subscribe(
        Frame(
            message_type=TYPE_SUBSCRIBE,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(1),
                TAG_TOPIC_ID: u32(SYSTEM_THREAD_SUMMARIES_TOPIC),
                TAG_SCOPE: u8(0),
                TAG_QOS: u8(1),
                TAG_CURSOR: u64(0),
            },
        )
    )


class AccountCapabilityGateTests(unittest.TestCase):
    def test_live_delivery_is_blocked_without_account_capability(self):
        spine, gateway, emitted = _gateway({"status", "text"})
        _subscribe(gateway)
        emitted.clear()

        spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC,
            EVENT_RATE_LIMITS_CHANGED,
            1,
            {},
        )
        gateway.deliver_event(
            spine.latest_event(lambda item: item.event_type == EVENT_RATE_LIMITS_CHANGED)
        )

        self.assertEqual(emitted, [])

    def test_live_delivery_reaches_a_device_with_account_capability(self):
        spine, gateway, emitted = _gateway({"status", "account"})
        _subscribe(gateway)
        emitted.clear()

        event = spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC,
            EVENT_RATE_LIMITS_CHANGED,
            1,
            {},
        )
        gateway.deliver_event(event)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(
            decode_u16(emitted[0].header[TAG_EVENT_TYPE]),
            EVENT_RATE_LIMITS_CHANGED,
        )

    def test_replay_does_not_leak_account_events_without_capability(self):
        spine, gateway, emitted = _gateway({"status", "text"})
        spine.append(SYSTEM_THREAD_SUMMARIES_TOPIC, EVENT_RATE_LIMITS_CHANGED, 1, {})
        emitted.clear()

        _subscribe(gateway)

        types = [
            decode_u16(frame.header[TAG_EVENT_TYPE])
            for frame in emitted
            if TAG_EVENT_TYPE in frame.header
        ]
        self.assertNotIn(EVENT_RATE_LIMITS_CHANGED, types)


if __name__ == "__main__":
    unittest.main()
