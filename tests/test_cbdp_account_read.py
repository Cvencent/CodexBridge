import unittest

from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    ERROR_CODES,
    EVENT_ACCOUNT_CHANGED,
    EVENT_RATE_LIMITS_CHANGED,
    EVENT_TOKEN_USAGE_CHANGED,
    FLAG_REQUEST,
    TAG_EVENT_TYPE,
    TAG_METHOD_ID,
    TAG_PARAMS,
    TAG_PLAN_TYPE,
    TAG_RATE_LIMITS,
    TAG_REQUEST_ID,
    TAG_SEVEN_DAY_TOKENS,
    TAG_TOPIC_ID,
    TAG_SCOPE,
    TAG_QOS,
    TAG_CURSOR,
    TYPE_COMMAND,
    TYPE_SUBSCRIBE,
)
from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.frame import Frame
from codexbridge.cbdp.gateway import CbdpGateway
from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
from codexbridge.cbdp.subscriptions import SubscriptionManager
from codexbridge.cbdp.tlv import (
    decode_u16,
    decode_u64,
    encode_tlvs,
    u8,
    u16,
    u32,
    u64,
)


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


class AccountSubscribeSyncTests(unittest.TestCase):
    def test_latest_account_state_is_replayed_after_a_stale_cursor(self):
        spine, gateway, emitted = _gateway({"status", "account"})
        account_event = spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC, EVENT_RATE_LIMITS_CHANGED, 1, {}
        )
        # A cursor that already passed the account event must still receive the
        # current account state, mirroring how codex_state is re-synchronised.
        for _ in range(30):
            spine.append(SYSTEM_THREAD_SUMMARIES_TOPIC, 0x0101, 1, {})
        emitted.clear()

        gateway._handle_subscribe(
            Frame(
                message_type=TYPE_SUBSCRIBE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(1),
                    TAG_TOPIC_ID: u32(SYSTEM_THREAD_SUMMARIES_TOPIC),
                    TAG_SCOPE: u8(0),
                    TAG_QOS: u8(1),
                    TAG_CURSOR: u64(account_event.global_seq),
                },
            )
        )

        types = [
            decode_u16(frame.header[TAG_EVENT_TYPE])
            for frame in emitted
            if TAG_EVENT_TYPE in frame.header
        ]
        self.assertIn(EVENT_RATE_LIMITS_CHANGED, types)


class AccountReadCommandTests(unittest.TestCase):
    def _command(self, gateway, method_id):
        gateway._handle_command(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(9),
                    TAG_METHOD_ID: u16(method_id),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )

    def test_rate_limits_read_returns_the_latest_window(self):
        spine, gateway, emitted = _gateway({"status", "account"})
        spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC,
            EVENT_RATE_LIMITS_CHANGED,
            1,
            {TAG_RATE_LIMITS: b"abc"},
        )
        emitted.clear()

        self._command(gateway, 0x0091)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].message_type, 0x0E)
        self.assertIn(TAG_RATE_LIMITS, emitted[0].header)

    def test_account_read_returns_the_plan(self):
        spine, gateway, emitted = _gateway({"status", "account"})
        spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC,
            EVENT_ACCOUNT_CHANGED,
            1,
            {TAG_PLAN_TYPE: b"plus"},
        )
        emitted.clear()

        self._command(gateway, 0x0090)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(
            emitted[0].header[TAG_PLAN_TYPE].decode("utf-8"), "plus"
        )

    def test_token_usage_read_returns_the_latest_total(self):
        spine, gateway, emitted = _gateway({"status", "account"})
        spine.append(
            SYSTEM_THREAD_SUMMARIES_TOPIC,
            EVENT_TOKEN_USAGE_CHANGED,
            2,
            {TAG_SEVEN_DAY_TOKENS: u64(43210)},
        )
        emitted.clear()

        self._command(gateway, 0x0092)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(decode_u64(emitted[0].header[TAG_SEVEN_DAY_TOKENS]), 43210)

    def test_read_command_requires_account_capability(self):
        _spine, gateway, emitted = _gateway({"status", "text"})

        self._command(gateway, 0x0091)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].code, ERROR_CODES["CAPABILITY_NOT_ENABLED"])


if __name__ == "__main__":
    unittest.main()
