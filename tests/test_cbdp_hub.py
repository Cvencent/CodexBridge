import tempfile
import unittest
from pathlib import Path

from codexbridge.cbdp.compat import EVENT_MESSAGE_READ, CompatContext, to_cbdp
from codexbridge.cbdp.constants import (
    QOS_CRITICAL,
    QOS_STATE,
    TAG_APPLIED_POLICY,
    TAG_BODY_REF,
    TAG_CREATED_AT_MS,
    TAG_DEVICE_ID,
    TAG_DEVICE_REF,
    TAG_EFFECTIVE_CAPS,
    TAG_KIND,
    TAG_LAST_SEEN_MS,
    TAG_MESSAGE_REF,
    TAG_POLICY_REASON,
    TAG_POLICY_VERSION,
    TAG_PROFILE,
    TAG_READ_AT_MS,
    TAG_REVISION,
    TAG_SENDER,
    TAG_STATUS,
    TAG_SUMMARY,
    TAG_THREAD_REF,
    TAG_TURN_REF,
    TAG_UPDATED_AT_MS,
    TAG_RUNNING_COUNT,
    TAG_COMPLETED_UNREAD,
    TAG_FAILED_UNREAD,
)
from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.projection import ProjectionStore
from codexbridge.cbdp.refs import ReferenceRegistry, SYSTEM_DEVICES_TOPIC
from codexbridge.cbdp.tlv import (
    decode_i64,
    decode_tlvs,
    decode_u8,
    decode_u16,
    decode_u32,
    decode_u64,
)
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord


class _Registry:
    def __init__(self):
        self.records = {}

    def get(self, device_id):
        return self.records.get(device_id)


class _Record:
    profile = 2
    last_seen = 1.5
    supported = {"status", "text", "results"}
    enabled = {"status", "text", "results"}
    max_text_chars = 4096


class _Session:
    gateway = None
    profile = 1
    is_cbdp = False

    def send(self, message):
        pass


class _CbdpSession(_Session):
    is_cbdp = True

    def __init__(self):
        self.gateway = None
        self.sent_events = []
        self.sent_messages = []

    def send(self, message):
        self.sent_messages.append(message)

    def send_event(self, event):
        self.sent_events.append(event)


class CbdpHubEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.events = EventSpine(self.root / "events.sqlite3")
        self.addCleanup(self.events.close)
        self.refs = ReferenceRegistry(self.root / "refs.sqlite3")
        self.addCleanup(self.refs.close)
        self.projection = ProjectionStore(self.root / "projection.sqlite3")
        self.addCleanup(self.projection.close)
        self.registry = _Registry()
        self.hub = BridgeHub(
            self.registry,
            event_spine=self.events,
            projection=self.projection,
            references=self.refs,
        )

    def _events(self, event_type):
        return [event for event in self.events.events_since(0) if event.event_type == event_type]

    def test_codex_state_is_published_as_a_summary_event(self):
        from codexbridge.cbdp.events import SCOPE_SUMMARY, event_scope
        from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
        from codexbridge.hub import EVENT_CODEX_STATE

        self.hub._broadcast_codex_state()
        events = self._events(EVENT_CODEX_STATE)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.topic_id, SYSTEM_THREAD_SUMMARIES_TOPIC)
        self.assertEqual(event.qos, QOS_STATE)
        self.assertEqual(event_scope(event.event_type), SCOPE_SUMMARY)
        self.assertIn(TAG_RUNNING_COUNT, event.payload)
        self.assertIn(TAG_COMPLETED_UNREAD, event.payload)
        self.assertIn(TAG_FAILED_UNREAD, event.payload)

    def test_unchanged_codex_state_is_not_republished(self):
        from codexbridge.hub import EVENT_CODEX_STATE

        self.hub._broadcast_codex_state()
        self.hub._broadcast_codex_state()
        self.hub._broadcast_codex_state()
        self.assertEqual(len(self._events(EVENT_CODEX_STATE)), 1)

    def test_codex_state_reaches_a_summary_subscriber(self):
        from codexbridge.cbdp.events import SCOPE_SUMMARY
        from codexbridge.cbdp.gateway import CbdpGateway
        from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
        from codexbridge.cbdp.subscriptions import SubscriptionManager
        from codexbridge.hub import EVENT_CODEX_STATE

        subscriptions = SubscriptionManager(self.events)
        gateway = CbdpGateway(
            event_spine=self.events,
            subscriptions=subscriptions,
            references=self.refs,
        )
        gateway.defer = None
        gateway.begin_session()
        gateway.session.device_id = "dev-state"
        gateway.session.ready = True
        sent = []
        gateway.bind_transport(sent.append)
        self.hub.register_cbdp_gateway("dev-state", gateway)
        subscriptions.subscribe(
            "dev-state", SYSTEM_THREAD_SUMMARIES_TOPIC, SCOPE_SUMMARY, QOS_STATE, 0
        )
        self.hub._broadcast_codex_state()
        deltas = [frame for frame in sent if frame.message_type == 0x0A]
        self.assertTrue(
            any(
                frame.header.get(0x0037) == EVENT_CODEX_STATE.to_bytes(2, "big")
                for frame in deltas
            )
        )

    def test_approval_request_is_mirrored_onto_the_event_spine(self):
        from codexbridge.companion.service import CompanionService
        from codexbridge.cbdp.tlv import decode_tlvs

        service = CompanionService(
            self.root,
            self.root,
            client_factory=lambda command, cwd: _NullClient(),
        )
        self.addCleanup(service.stop)
        service._cbdp_event_spine = self.events
        service._cbdp_references = self.refs
        service.publish(
            {
                "type": "approval_request",
                "thread_id": "t1",
                "turn_id": "u1",
                "request_id": "ap-9",
                "kind": "mcp",
                "command": "codegraph_context",
            }
        )
        events = self._events(0x0401)
        self.assertEqual(len(events), 1)
        fields = events[0].payload
        self.assertEqual(fields[0x0087], b"\x04")  # mcp_tool
        self.assertEqual(fields[0x0028], b"ap-9")


class _NullClient:
    def close(self):
        return None

    def test_message_events_share_the_thread_topic_and_scope(self):
        from codexbridge.cbdp.events import event_scope

        # message.appended/updated/deleted all live on the thread topic, so a
        # device subscribed to that thread must receive all of them.
        for event_type in (0x0301, 0x0302, 0x0304):
            self.assertEqual(event_scope(event_type), 1)
        # message.read is a summary-scope marker.
        self.assertEqual(event_scope(0x0303), 0)

    def test_message_event_contains_protocol_required_fields_and_types(self):
        self.hub.publish(
            MessageRecord(
                message_id="m1",
                thread_id="t1",
                kind="assistant_text",
                status="completed",
                title="Answer",
                body="hello",
                created_at=2.0,
                data={"task": {"turn_id": "u1"}},
            )
        )
        event = self._events(0x0301)[0]
        payload = event.payload
        self.assertEqual(decode_u32(payload[TAG_THREAD_REF]), self.refs.ref_for("thread", "t1"))
        self.assertEqual(decode_u32(payload[TAG_TURN_REF]), self.refs.ref_for("turn", "u1"))
        self.assertEqual(decode_u32(payload[TAG_MESSAGE_REF]), self.refs.ref_for("message", "m1"))
        self.assertEqual(decode_u8(payload[TAG_KIND]), 1)
        self.assertEqual(decode_u16(payload[TAG_STATUS]), 2)
        self.assertEqual(decode_u8(payload[TAG_SENDER]), 1)
        self.assertEqual(payload[TAG_SUMMARY], b"Answer")
        self.assertEqual(payload[TAG_BODY_REF], b"content:m1")
        self.assertEqual(decode_i64(payload[TAG_CREATED_AT_MS]), 2000)
        self.assertEqual(decode_i64(payload[TAG_UPDATED_AT_MS]), 2000)

    def test_waiting_task_message_event_uses_running_status_number(self):
        self.hub.publish(
            MessageRecord(
                message_id="task:t1",
                thread_id="t1",
                kind="task",
                status="waiting_user",
                title="Task",
                body="waiting",
                created_at=2.0,
            )
        )

        payload = self._events(0x0301)[0].payload

        self.assertEqual(1, decode_u16(payload[TAG_STATUS]))

    def test_cbdp_session_receives_committed_event_not_compat_delta(self):
        self.registry.records["dev-1"] = _Record()
        session = _CbdpSession()
        self.hub.attach("dev-1", session)
        self.addCleanup(self.hub.detach, "dev-1")

        self.hub.publish(
            MessageRecord(
                message_id="m1",
                thread_id="t1",
                kind="result",
                status="completed",
                title="Answer",
                body="hello",
                created_at=2.0,
            )
        )

        # The CBDP session must get the committed Event Spine record (which
        # carries event_id/global_seq/topic_seq/revision and the real topic),
        # never a synthesised compatibility message.
        self.assertEqual(len(session.sent_events), 1)
        event = session.sent_events[0]
        self.assertEqual(event.topic_id, self.refs.topic_for("t1"))
        self.assertGreater(event.global_seq, 0)
        self.assertGreater(event.topic_seq, 0)
        self.assertGreater(event.revision, 0)
        self.assertEqual(len(event.event_id), 16)
        # No compatibility "message"/"read_state" envelope may be synthesised
        # for a CBDP session; aggregate state messages are still allowed.
        self.assertFalse(
            any(
                item.get("type") in {"message", "read_state"}
                for item in session.sent_messages
            )
        )

    def test_unchanged_message_fingerprint_does_not_append_twice(self):
        record = MessageRecord(
            message_id="m1",
            thread_id="t1",
            kind="result",
            status="completed",
            title="Answer",
            body="hello",
            created_at=2.0,
        )
        self.hub.publish(record)
        self.hub.publish(record)
        self.assertEqual(len(self._events(0x0301)), 1)
        self.assertEqual(len(self._events(0x0302)), 0)

    def test_message_read_uses_read_at_ms_and_device_ref(self):
        self.hub.publish(
            MessageRecord(
                message_id="m1",
                thread_id="t1",
                kind="result",
                status="completed",
                title="Answer",
                body="hello",
                created_at=2.0,
            )
        )
        self.hub._append_read_event("m1", "t1", "dev-1")
        payload = self._events(EVENT_MESSAGE_READ)[0].payload
        self.assertEqual(set(payload), {TAG_THREAD_REF, TAG_MESSAGE_REF, TAG_READ_AT_MS, TAG_DEVICE_REF})
        self.assertEqual(len(payload[TAG_READ_AT_MS]), 8)
        decode_i64(payload[TAG_READ_AT_MS])

    def test_device_online_offline_use_profile_and_last_seen(self):
        self.registry.records["dev-1"] = _Record()
        self.hub.attach("dev-1", _Session())
        self.hub.detach("dev-1")
        for event_type in (0x0601, 0x0602):
            payload = self._events(event_type)[0].payload
            self.assertEqual(payload[TAG_DEVICE_ID], b"dev-1")
            self.assertEqual(decode_u8(payload[TAG_PROFILE]), 2)
            self.assertEqual(len(payload[TAG_LAST_SEEN_MS]), 8)
            self.assertNotIn(TAG_STATUS, payload)

    def test_policy_change_uses_devices_topic_and_applied_policy(self):
        self.registry.records["dev-1"] = _Record()
        self.hub.note_policy_changed("dev-1")
        event = self._events(0x0603)[0]
        self.assertEqual(event.topic_id, SYSTEM_DEVICES_TOPIC)
        payload = event.payload
        applied = decode_tlvs(payload[TAG_APPLIED_POLICY])
        self.assertEqual(decode_u32(applied[TAG_POLICY_VERSION]), 1)
        self.assertEqual(decode_u32(payload[TAG_POLICY_VERSION]), 1)
        self.assertEqual(payload[TAG_POLICY_REASON], b"registry_update")
        self.assertEqual(decode_u64(payload[TAG_REVISION]), event.revision)

    def test_projection_rebuilds_all_protocol_timestamps(self):
        self.hub.publish(
            MessageRecord(
                message_id="m1",
                thread_id="t1",
                kind="result",
                status="completed",
                title="Answer",
                body="hello",
                created_at=2.0,
            )
        )
        rebuilt = ProjectionStore(self.root / "rebuilt.sqlite3")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.events.events_since(0))
        message = rebuilt.get_message("m1")
        self.assertEqual(message.thread_id, "t1")
        self.assertEqual(message.status, 2)
        self.assertEqual(message.body, "hello")


if __name__ == "__main__":
    unittest.main()
