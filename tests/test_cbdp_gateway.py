import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexbridge.cbdp.auth import pairing_token_proof
from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    FLAG_REQUEST,
    FLAG_RESPONSE,
    QOS_STATE,
    TAG_AUTH_PROOF,
    TAG_AUTH_MODE,
    TAG_BODY,
    TAG_CLIENT_NONCE,
    TAG_DEVICE_ID,
    TAG_DEVICE_REF,
    TAG_EFFECTIVE_CAPS,
    TAG_FIRMWARE,
    TAG_MAX_AUDIO_BYTES,
    TAG_MAX_CHUNK_BYTES,
    TAG_MAX_FRAME_BYTES,
    TAG_MAX_TEXT_BYTES,
    TAG_MAX_TEXT_CHARS,
    TAG_METHOD_ID,
    TAG_MODEL,
    TAG_PARAMS,
    TAG_PROFILE,
    TAG_REQUEST_ID,
    TAG_OBJECT,
    TAG_OBJECTS,
    TAG_OBJECT_COUNT,
    TAG_APPROVAL_KIND,
    TAG_DECISION,
    TAG_APPROVAL_REF,
    TAG_NEXT_CURSOR,
    TAG_THREAD_REF,
    TAG_USE_ACTIVE_THREAD,
    TAG_TOPIC_ID,
    TAG_STATUS,
    TAG_ONLINE,
    TAG_ECHO,
    TAG_PING_ID,
    TAG_SERVER_TIME_MS,
    TAG_SERVER_NONCE,
    TAG_SESSION_ID,
    TAG_SUPPORTED_CAPS,
    TAG_TRANSPORT_SECURITY,
    TAG_VERSION_MINOR,
    TYPE_APPROVAL_RESOLVE,
    TYPE_AUTH,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_AUTH_OK,
    TYPE_CLOSE,
    TYPE_COMMAND,
    TYPE_HELLO,
    TYPE_RESULT,
    TYPE_PING,
    TYPE_PONG,
    TYPE_SEND_BEGIN,
    TYPE_SEND_CHUNK,
    TYPE_SEND_END,
    TYPE_ERROR,
    TYPE_SUBSCRIBE,
    TYPE_RESYNC,
    TYPE_WELCOME,
)
from codexbridge.cbdp import gateway
from codexbridge.cbdp.frame import Frame
from codexbridge.cbdp.gateway import CbdpGateway
from codexbridge.cbdp.refs import ReferenceRegistry
from codexbridge.cbdp.tlv import (
    CBDPError,
    decode_array,
    decode_u16,
    decode_utf8,
    decode_i64,
    decode_tlvs,
    decode_u32,
    decode_u64,
    encode_tlvs,
    i64,
    u8,
    u16,
    u32,
    u64,
    utf8,
)


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def execute(self, device_id, capabilities, request):
        self.calls.append((device_id, set(capabilities), dict(request)))
        if request.get("method") == "turn.start" and not request.get("params", {}).get("thread_id"):
            return {
                "type": "command_result",
                "request_id": request["request_id"],
                "method": request["method"],
                "ok": False,
                "error": "thread_required",
            }
        return {
            "type": "command_result",
            "request_id": request["request_id"],
            "method": request["method"],
            "ok": True,
            "result": {"thread_id": "t1", "turn_id": "u1"},
        }


class FakeThreadDispatcher:
    def execute(self, device_id, capabilities, request):
        return {
            "ok": True,
            "result": {
                "threads": [
                    {"thread_id": "t1", "title": "Task", "status": "working"}
                ],
                "next_cursor": 0,
            },
        }


class CbdpGatewayTests(unittest.TestCase):
    def test_summary_subscribe_replays_current_codex_state(self):
        from codexbridge.cbdp.events import EventSpine
        from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
        from codexbridge.cbdp.subscriptions import SubscriptionManager
        from codexbridge.cbdp.constants import TAG_RUNNING_COUNT
        from codexbridge.cbdp.constants import TAG_EVENT, TAG_EVENT_TYPE

        spine = EventSpine(None)
        for index in range(30):
            spine.append(SYSTEM_THREAD_SUMMARIES_TOPIC, 0x0101, 1, {})
        spine.append(SYSTEM_THREAD_SUMMARIES_TOPIC, 0x0704, 1,
                     {TAG_RUNNING_COUNT: u16(3)})
        for index in range(30):
            spine.append(SYSTEM_THREAD_SUMMARIES_TOPIC, 0x0101, 1, {})
        subscriptions = SubscriptionManager(spine)
        subscriptions.set_queue_depth("dev-1", 4)
        gateway = CbdpGateway(commands=lambda: None, event_spine=spine,
                              subscriptions=subscriptions)
        gateway.session = type("S", (), {
            "device_id": "dev-1", "generation": 1, "ready": True,
            "session_id": b"x" * 16, "authenticated": True, "profile": 0,
        })()
        emitted = []
        gateway._emit = lambda frame: emitted.append(frame)

        gateway._handle_subscribe(Frame(
            message_type=TYPE_SUBSCRIBE,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(7),
                0x0030: u32(SYSTEM_THREAD_SUMMARIES_TOPIC),
                0x0031: u8(0),
                0x0032: u8(1),
                0x0033: u64(0),
            },
        ))

        event_types = [
            decode_u16(frame.header[TAG_EVENT_TYPE])
            for frame in emitted
            if TAG_EVENT_TYPE in frame.header
        ]
        self.assertIn(0x0704, event_types)

    def test_threads_list_project_ref_resolves_to_project_id(self):
        from codexbridge.cbdp.gateway import CbdpGateway
        from codexbridge.cbdp.refs import ReferenceRegistry

        class Dispatcher:
            def __init__(self):
                self.requests = []

            def execute(self, device_id, capabilities, request):
                self.requests.append(request)
                return {"ok": True, "result": {"threads": [], "next_cursor": 0}}

        refs = ReferenceRegistry(None)
        gateway = CbdpGateway(commands=lambda: Dispatcher(), references=refs)
        ref = gateway._ref("project", "proj-1")

        params = gateway._normalize_params("threads.list", {"project_ref": ref})

        self.assertEqual(params, {"project_id": "proj-1"})
        with self.assertRaises(CBDPError) as caught:
            gateway._normalize_params("threads.list", {"project_ref": 999999})
        self.assertEqual(caught.exception.name, "NOT_FOUND")

    def test_thread_settings_update_decodes_policy_enums_to_names(self):
        from codexbridge.cbdp.gateway import CbdpGateway

        gateway = CbdpGateway.__new__(CbdpGateway)
        params = gateway._normalize_params(
            "thread.settings.update",
            gateway._decode_params(
                decode_tlvs(
                    encode_tlvs(
                        [
                            (0x0022, utf8("t1")),
                            (0x0124, utf8("high")),
                            (0x0125, u8(3)),
                            (0x0126, u8(1)),
                            (0x0127, u8(2)),
                            (0x0128, utf8("pragmatic")),
                        ]
                    )
                )
            ),
        )

        self.assertEqual("t1", params["thread_id"])
        self.assertEqual("high", params["effort"])
        self.assertEqual("never", params["approval_policy"])
        self.assertEqual("auto", params["approvals_reviewer"])
        self.assertEqual("danger-full-access", params["sandbox_policy"])
        self.assertEqual("pragmatic", params["personality"])

    def test_task_message_status_numbers_match_the_running_set(self):
        from codexbridge.cbdp.gateway import _message_status

        for status in ("working", "waiting", "waiting_user", "queued", "starting"):
            self.assertEqual(1, _message_status(status))

    def setUp(self):
        self.dispatcher = FakeDispatcher()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.refs = ReferenceRegistry(Path(tmp.name) / "refs.sqlite3")
        self.addCleanup(self.refs.close)
        self.gateway = CbdpGateway(
            references=self.refs,
            commands=self.dispatcher,
            active_thread=lambda device_id: "thread-1",
            pairing_token=lambda device_id: "0123456789abcdef0123456789abcdef",
            capability_policy=lambda device_id: {
                "status",
                "text",
                "control",
                "queue",
                "queue_manage",
            },
            auth_mode=1,
        )
        self.sent = []
        self.gateway.bind_transport(self.sent.append)

    def raw(self, frame):
        return frame

    def hello(self, request_id=1, nonce=b"\x01" * 16):
        return Frame(
            message_type=TYPE_HELLO,
            flags=FLAG_REQUEST,
            header={
                TAG_VERSION_MINOR: u16(0),
                TAG_DEVICE_ID: utf8("dev-1"),
                TAG_MODEL: utf8("esp32"),
                TAG_FIRMWARE: utf8("1.0"),
                TAG_PROFILE: u8(1),
                TAG_SUPPORTED_CAPS: capability_bitmap(
                    ["status", "text", "control", "queue", "queue_manage"]
                ).to_bytes(8, "big"),
                TAG_TRANSPORT_SECURITY: u8(0),
                TAG_CLIENT_NONCE: nonce,
                TAG_REQUEST_ID: u32(request_id),
            },
        )

    def test_hello_returns_welcome_with_intersected_capabilities(self):
        self.gateway.handle_frame(self.hello())
        welcome = self.sent[-1]
        self.assertEqual(welcome.message_type, TYPE_WELCOME)
        fields = decode_tlvs(encode_tlvs(welcome.header.items()))
        self.assertIn(TAG_EFFECTIVE_CAPS, fields)
        self.assertEqual(len(fields[TAG_SESSION_ID]), 16)
        self.assertEqual(len(fields[TAG_SERVER_NONCE]), 16)
        self.assertTrue(fields[TAG_MAX_AUDIO_BYTES])
        self.assertTrue(fields[TAG_MAX_TEXT_CHARS])
        self.assertTrue(fields[TAG_MAX_FRAME_BYTES])

    def test_hello_name_and_model_are_limited_to_64_bytes(self):
        hello = self.hello()
        hello.header[0x0005] = utf8("n" * 64)
        hello.header[TAG_MODEL] = utf8("m" * 64)
        self.gateway.handle_frame(hello)
        self.assertEqual(self.sent[-1].message_type, TYPE_WELCOME)

        self.gateway = type(self.gateway)(
            commands=self.dispatcher,
            pairing_token=lambda device_id: "0123456789abcdef0123456789abcdef",
            capability_policy=lambda device_id: set(),
            auth_mode=1,
        )
        self.sent = []
        self.gateway.bind_transport(self.sent.append)
        hello = self.hello()
        hello.header[0x0005] = utf8("n" * 65)
        self.gateway.handle_frame(hello)
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0006)

    def test_auth_ok_uses_reference_registry_for_device_ref(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.sent.clear()
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUTH,
                flags=FLAG_REQUEST,
                header={
                    TAG_AUTH_MODE: u8(1),
                    TAG_DEVICE_ID: utf8("dev-1"),
                    TAG_SESSION_ID: session.session_id,
                    TAG_AUTH_PROOF: self.gateway.expected_proof("dev-1", session),
                    TAG_REQUEST_ID: u32(2),
                },
            )
        )
        auth_ok = self.sent[-1]
        self.assertEqual(auth_ok.message_type, TYPE_AUTH_OK)
        device_ref = decode_u32(auth_ok.header[TAG_DEVICE_REF])
        self.assertGreater(device_ref, 0)
        self.assertEqual(
            self.refs.external_id("device", device_ref),
            "dev-1",
        )

    def test_ping_is_allowed_before_auth_and_echoes_exact_ping_id(self):
        ping_id = b"\x12\x34\x56\x78"
        self.gateway.handle_frame(
            Frame(message_type=TYPE_PING, header={TAG_PING_ID: ping_id})
        )
        pong = self.sent[-1]
        self.assertEqual(pong.message_type, TYPE_PONG)
        self.assertEqual(pong.header[TAG_ECHO], ping_id)
        self.assertEqual(len(pong.header[TAG_ECHO]), 4)
        self.assertIn(TAG_SERVER_TIME_MS, pong.header)
        self.assertEqual(len(pong.header[TAG_SERVER_TIME_MS]), 8)
        decode_i64(pong.header[TAG_SERVER_TIME_MS])

    def test_ping_id_must_be_exactly_four_bytes(self):
        self.gateway.handle_frame(
            Frame(message_type=TYPE_PING, header={TAG_PING_ID: b"\x00\x01"})
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0006)

    def test_method_id_is_u16_on_command_and_result(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(11),
                    TAG_METHOD_ID: u16(1),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        self.assertEqual(self.sent[-1].header[TAG_METHOD_ID], u16(1))

        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(12),
                    TAG_METHOD_ID: u32(1),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0006)

    def test_auth_replay_from_old_session_is_rejected(self):
        first = self.gateway.begin_session()
        second = self.gateway.begin_session()
        self.gateway.handle_frame(self.hello())
        auth = Frame(
            message_type=TYPE_AUTH,
            flags=FLAG_REQUEST,
            header={
                TAG_SESSION_ID: first.session_id,
                TAG_AUTH_PROOF: b"\x00" * 32,
                TAG_AUTH_MODE: u8(1),
                TAG_REQUEST_ID: u32(2),
                TAG_DEVICE_ID: utf8("dev-1"),
            },
        )
        self.gateway.handle_frame(auth)
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0003)
        self.assertEqual(second.generation, first.generation + 1)

    def test_command_is_translated_to_existing_dispatcher(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        auth_ok = self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.assertTrue(auth_ok)
        command = Frame(
            message_type=TYPE_COMMAND,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(9),
                TAG_METHOD_ID: u16(1),
                TAG_PARAMS: encode_tlvs([]),
            },
        )
        self.gateway.handle_frame(command)
        self.assertEqual(self.dispatcher.calls[-1][0], "dev-1")
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)

    def test_hello_rejects_reserved_transport_security(self):
        hello = self.hello()
        hello.header[TAG_TRANSPORT_SECURITY] = u8(2)
        self.gateway.handle_frame(hello)
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0006)

    def test_pairing_token_auth_mode_is_supported(self):
        self.gateway.auth_mode = 0
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        proof = pairing_token_proof(
            "0123456789abcdef0123456789abcdef",
            session.session_id,
            session.client_nonce,
            session.server_nonce,
            "dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUTH,
                flags=FLAG_REQUEST,
                header={
                    TAG_SESSION_ID: session.session_id,
                    TAG_AUTH_PROOF: proof,
                    TAG_AUTH_MODE: u8(0),
                    TAG_REQUEST_ID: u32(2),
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_AUTH_OK)

    def test_unsupported_auth_mode_is_reported(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUTH,
                flags=FLAG_REQUEST,
                header={
                    TAG_SESSION_ID: session.session_id,
                    TAG_AUTH_PROOF: b"\x00" * 32,
                    TAG_AUTH_MODE: u8(0),
                    TAG_REQUEST_ID: u32(2),
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x000E)

    def test_audio_transaction_is_transcribed_and_submitted(self):
        class Recognizer:
            def transcribe_pcm(self, device_id, data, **kwargs):
                assert data == b"\x01\x02" * 8
                return "open the build"

        self.gateway.speech = Recognizer()
        self.gateway.capability_policy = lambda device_id: {"status", "audio", "control"}
        self.gateway.active_thread = lambda device_id: "thread-9"
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(3),
                    0x0073: u8(0),
                    0x0074: u32(16000),
                    0x0075: u8(1),
                    0x0076: u8(16),
                    0x007B: u8(1),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(3), 0x0048: u32(0)},
                payload=b"\x01\x02" * 8,
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(3)},
            )
        )
        self.assertEqual(self.dispatcher.calls[-1][2]["method"], "turn.start")
        self.assertEqual(
            self.dispatcher.calls[-1][2]["params"]["text"], "open the build"
        )
        self.assertTrue(
            any(item.message_type == TYPE_RESULT for item in self.sent)
        )

    def test_audio_begin_explicit_thread_ref_is_used_at_end(self):
        class Recognizer:
            def transcribe_pcm(self, device_id, data, **kwargs):
                return "use the selected thread"

        self.gateway.speech = Recognizer()
        self.gateway.capability_policy = lambda device_id: {"status", "audio", "control"}
        self.gateway.active_thread = lambda device_id: "wrong-thread"
        thread_ref = self.refs.ref_for("thread", "thread-9")
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(4),
                    0x0073: u8(0),
                    0x0074: u32(16000),
                    0x0075: u8(1),
                    0x0076: u8(16),
                    0x0021: u32(thread_ref),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(4), 0x0048: u32(0)},
                payload=b"\x01\x02" * 8,
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(4)},
            )
        )
        self.assertEqual(
            self.dispatcher.calls[-1][2]["params"]["thread_id"], "thread-9"
        )

    def test_audio_end_without_thread_returns_no_active_thread(self):
        class Recognizer:
            def transcribe_pcm(self, device_id, data, **kwargs):
                return "no target"

        self.gateway.speech = Recognizer()
        self.gateway.capability_policy = lambda device_id: {"status", "audio", "control"}
        self.gateway.active_thread = lambda device_id: ""
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(5),
                    0x0073: u8(0),
                    0x0074: u32(16000),
                    0x0075: u8(1),
                    0x0076: u8(16),
                    0x007B: u8(0),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(5), 0x0048: u32(0)},
                payload=b"\x01\x02" * 8,
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(5)},
            )
        )
        error = self.sent[-1]
        self.assertEqual(error.message_type, TYPE_ERROR)
        self.assertEqual(error.code, 0x0007)
        self.assertEqual(error.header[0x0062], utf8("NO_ACTIVE_THREAD"))

    def test_deferred_cbdp_work_runs_off_the_reader_and_updates_state(self):
        deferred = []

        def defer(device_id, callback):
            deferred.append((device_id, callback))

        self.gateway.defer = defer
        self.gateway.active_thread = lambda device_id: "thread-1"
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        command = Frame(
            message_type=TYPE_COMMAND,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(9),
                TAG_METHOD_ID: u16(1),
                TAG_PARAMS: encode_tlvs([]),
            },
        )
        self.gateway.handle_frame(command)
        self.assertEqual(self.dispatcher.calls, [])
        self.assertEqual(len(deferred), 1)
        deferred[0][1]()
        self.assertEqual(self.dispatcher.calls[-1][0], "dev-1")
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)

    def test_thread_list_result_carries_objects_array(self):
        self.gateway.commands = lambda: FakeThreadDispatcher()
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(21),
                    TAG_METHOD_ID: u16(0x0002),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        result = self.sent[-1]
        self.assertEqual(result.message_type, TYPE_RESULT)
        objects = decode_array(result.header[TAG_OBJECTS], TAG_OBJECT)
        self.assertEqual(len(objects), 1)
        thread_fields = decode_tlvs(objects[0][1])
        self.assertEqual(thread_fields[0x003F], u8(0))
        self.assertEqual(decode_u16(thread_fields[0x0044]), 1)
        # The device subscribes to this stable topic to receive thread DELTAs.
        self.assertEqual(
            decode_u32(thread_fields[TAG_TOPIC_ID]),
            self.refs.topic_for("t1"),
        )
        self.assertEqual(decode_u16(result.header[TAG_OBJECT_COUNT]), 1)

    def test_thread_list_result_carries_opaque_cursor_and_roundtrips_it(self):
        class PagingDispatcher(FakeThreadDispatcher):
            def __init__(self):
                self.calls = []

            def execute(self, device_id, capabilities, request):
                self.calls.append(dict(request))
                return {
                    "ok": True,
                    "result": {
                        "threads": [{"thread_id": "t1", "title": "Task"}],
                        "next_cursor": "opaque-page-2",
                    },
                }

        dispatcher = PagingDispatcher()
        self.gateway.commands = lambda: dispatcher
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(31),
                    TAG_METHOD_ID: u16(0x0002),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        cursor = decode_u64(self.sent[-1].header[TAG_NEXT_CURSOR])
        self.assertGreater(cursor, 0)
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(32),
                    TAG_METHOD_ID: u16(0x0002),
                    TAG_PARAMS: encode_tlvs([(0x0033, u64(cursor))]),
                },
            )
        )
        self.assertEqual(dispatcher.calls[-1]["params"]["cursor"], "opaque-page-2")

    def test_thread_object_uses_protocol_audio_and_extended_tags(self):
        from codexbridge.cbdp.constants import (
            TAG_AUDIO_FORMAT,
            TAG_BITS_PER_SAMPLE,
            TAG_CHANNELS,
            TAG_CODEC,
            TAG_DURATION_MS,
            TAG_SAMPLE_RATE,
            TAG_SHA256,
            TAG_TOTAL_BYTES,
            TAG_TRANSCRIPT,
        )

        self.assertEqual(TAG_CODEC, 0x0073)
        self.assertEqual(TAG_SAMPLE_RATE, 0x0074)
        self.assertEqual(TAG_CHANNELS, 0x0075)
        self.assertEqual(TAG_BITS_PER_SAMPLE, 0x0076)
        self.assertEqual(TAG_DURATION_MS, 0x0077)
        self.assertEqual(TAG_TRANSCRIPT, 0x0078)
        self.assertEqual(TAG_SHA256, 0x0079)
        self.assertEqual(TAG_AUDIO_FORMAT, 0x007A)
        self.assertEqual(TAG_TOTAL_BYTES, 0x0047)

    def test_message_objects_are_encoded_from_real_dispatcher_kinds(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        fields = self.gateway._result_fields(
            {
                "thread": {"thread_id": "t1", "title": "Task", "status": "idle"},
                "messages": [
                    {"id": "m1", "turn_id": "u1", "kind": "user", "text": "hi"},
                    {"id": "m2", "turn_id": "u1", "kind": "agent", "text": "hello"},
                    {"id": "m3", "turn_id": "u1", "kind": "tool", "text": "ls"},
                ],
            }
        )
        objects = decode_array(fields[TAG_OBJECTS], TAG_OBJECT)
        kinds = [
            decode_tlvs(value)[0x0045][0]
            for tag, value in objects
            if decode_tlvs(value)[0x003F] == b"\x02"
        ]
        self.assertEqual(kinds, [0, 1, 3])

    def test_oversized_thread_title_does_not_break_the_result(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        fields = self.gateway._result_fields(
            {
                "threads": [
                    {"thread_id": "t1", "title": "x" * 70000, "status": "idle"},
                    {"thread_id": "t2", "title": "ok", "status": "idle"},
                ],
                "next_cursor": "p2",
            }
        )
        objects = decode_array(fields[TAG_OBJECTS], TAG_OBJECT)
        self.assertEqual(len(objects), 2)
        first = decode_tlvs(objects[0][1])
        title = decode_utf8(first[0x0040])
        self.assertLessEqual(len(title.encode("utf-8")), 4096)
        self.assertIn(TAG_NEXT_CURSOR, fields)

    def test_gateway_imports_every_constant_it_references(self):
        # A missing constant shows up as a NameError that tears down a live
        # CBDP session instead of returning an ERROR frame, so assert imports.
        import ast
        from pathlib import Path

        source = Path(gateway.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "codexbridge.cbdp.constants"
            ):
                imported.update(alias.name for alias in node.names)
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id.startswith("TAG_")
        }
        self.assertEqual(sorted(used - imported), [])

    def test_object_page_respects_the_negotiated_frame_budget(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        session.max_frame_bytes = 1024
        fields = self.gateway._result_fields(
            {
                "threads": [
                    {"thread_id": f"t{i}", "title": "t" * 400, "status": "idle"}
                    for i in range(20)
                ]
            }
        )
        self.assertLessEqual(len(fields[TAG_OBJECTS]) + 128, 1024)

    def test_thread_read_keeps_messages_when_approvals_are_present(self):
        # thread.read also carries `pending_approvals`; a key-sniffing encoder
        # returned that (often empty) list and silently dropped every message.
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        fields = self.gateway._result_fields(
            {
                "thread": {"thread_id": "t1", "title": "T", "status": "working"},
                "messages": [
                    {"id": "m1", "kind": "user", "text": "hi"},
                    {"id": "m2", "kind": "agent", "text": "hello"},
                ],
                "pending_approvals": [],
            },
            "thread.read",
        )
        objects = decode_array(fields[TAG_OBJECTS], TAG_OBJECT)
        types = [
            decode_tlvs(value)[0x003F][0]
            for _tag, value in objects
        ]
        self.assertEqual(types.count(2), 2)
        self.assertEqual(types.count(0), 1)

    def test_project_objects_encode_and_unknown_ref_kinds_degrade(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        fields = self.gateway._result_fields(
            {
                "projects": [
                    {"project_id": "p1", "name": "Proj", "cwd": "D:\\p1"},
                    {"project_id": "p2", "name": "Proj2", "cwd": "D:\\p2"},
                ]
            },
            "projects.list",
        )
        objects = decode_array(fields[TAG_OBJECTS], TAG_OBJECT)
        self.assertEqual(len(objects), 2)
        first = decode_tlvs(objects[0][1])
        self.assertEqual(first[0x003F], u8(10))
        self.assertEqual(decode_utf8(first[0x0052]), "D:\\p1")
        # An unsupported kind must yield 0 rather than raising and killing
        # the session.
        self.assertEqual(self.gateway._ref("not-a-kind", "x"), 0)

    def test_result_and_error_carry_nonzero_seq_and_piggyback_ack(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        command = Frame(
            message_type=TYPE_COMMAND,
            flags=FLAG_REQUEST,
            seq=7,
            header={
                TAG_REQUEST_ID: u32(41),
                TAG_METHOD_ID: u16(0x0001),
                TAG_PARAMS: encode_tlvs([]),
            },
        )
        self.gateway.handle_frame(command)
        result = self.sent[-1]
        self.assertEqual(result.message_type, TYPE_RESULT)
        self.assertNotEqual(result.seq, 0)
        self.assertEqual(result.ack_seq, 7)

        self.gateway._error(42, "INVALID_PARAM")
        error = self.sent[-1]
        self.assertEqual(error.message_type, TYPE_ERROR)
        self.assertNotEqual(error.seq, 0)
        self.assertEqual(error.ack_seq, 7)
        self.assertGreater(error.seq, result.seq)

    def test_seq_zero_control_frames_do_not_move_the_ack(self):
        hello = self.hello()
        hello.seq = 5
        self.gateway.handle_frame(hello)
        self.assertEqual(self.gateway._incoming_seq, 5)
        self.gateway.handle_frame(
            Frame(message_type=TYPE_PING, flags=FLAG_REQUEST, seq=0, header={})
        )
        self.assertEqual(self.gateway._incoming_seq, 5)

    def test_handshake_responses_are_numbered(self):
        self.gateway.handle_frame(self.hello())
        welcome = self.sent[-1]
        self.assertEqual(welcome.message_type, TYPE_WELCOME)
        self.assertNotEqual(welcome.seq, 0)
        session = self.gateway.session
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUTH,
                flags=FLAG_REQUEST,
                header={
                    TAG_DEVICE_ID: utf8("dev-1"),
                    TAG_SESSION_ID: session.session_id,
                    TAG_AUTH_PROOF: self.gateway.expected_proof("dev-1", session),
                    TAG_AUTH_MODE: u8(1),
                    TAG_REQUEST_ID: u32(2),
                },
            )
        )
        auth_ok = self.sent[-1]
        self.assertEqual(auth_ok.message_type, TYPE_AUTH_OK)
        self.assertGreater(auth_ok.seq, welcome.seq)

    def test_active_state_read_and_set_are_implemented(self):
        self.gateway.capability_policy = lambda device_id: {
            "status", "tasks", "cross_device_active",
        }
        self.gateway.active_state = lambda device_id: {
            "thread_id": "thread-1", "turn_id": "turn-1",
        }
        recorded = {}

        def setter(device_id, thread_id, turn_id):
            recorded["value"] = (device_id, thread_id, turn_id)
            return {"thread_id": thread_id, "turn_id": turn_id}

        self.gateway.set_active_state = setter
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["cross_device_active"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(51),
                    TAG_METHOD_ID: u16(0x00B1),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        read = self.sent[-1]
        self.assertEqual(read.message_type, TYPE_RESULT)
        self.assertIn(0x0170, read.header)
        self.assertIn(0x0173, read.header)

        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(52),
                    TAG_METHOD_ID: u16(0x00B2),
                    TAG_PARAMS: encode_tlvs(
                        [
                            (0x0170, u32(self.refs.ref_for("thread", "thread-2"))),
                            (0x0171, u32(self.refs.ref_for("turn", "turn-2"))),
                        ]
                    ),
                },
            )
        )
        written = self.sent[-1]
        self.assertEqual(written.message_type, TYPE_RESULT)
        self.assertEqual(recorded["value"][1:], ("thread-2", "turn-2"))

    def test_active_state_set_returns_not_found_for_unknown_ref(self):
        self.gateway.capability_policy = lambda device_id: {
            "status", "tasks", "cross_device_active",
        }
        self.gateway.set_active_state = lambda device_id, thread_id, turn_id: {}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["cross_device_active"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(53),
                    TAG_METHOD_ID: u16(0x00B2),
                    TAG_PARAMS: encode_tlvs([(0x0170, u32(999999))]),
                },
            )
        )
        result = self.sent[-1]
        self.assertEqual(result.message_type, TYPE_ERROR)
        self.assertEqual(result.code, 0x0001)

    def _approval_gateway(self):
        self.gateway.capability_policy = lambda device_id: {"status", "approval"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["approval"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )

    def test_approval_resolve_accepts_approval_ref(self):
        self._approval_gateway()
        ref = self.refs.ref_for("approval", "ap-1")
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_APPROVAL_RESOLVE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(71),
                    TAG_APPROVAL_REF: u32(ref),
                    TAG_DECISION: u8(0),
                },
            )
        )
        # The shared dispatcher has no approval backing in this fixture, so a
        # bridge-level error is acceptable; it must not be UNSUPPORTED_FEATURE
        # and must echo the request id.
        result = self.sent[-1]
        self.assertNotEqual(result.code, 0x000E)
        self.assertEqual(result.header[TAG_REQUEST_ID], u32(71))

    def test_approval_resolve_unknown_ref_returns_not_found(self):
        self._approval_gateway()
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_APPROVAL_RESOLVE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(72),
                    TAG_APPROVAL_REF: u32(999999),
                    TAG_DECISION: u8(0),
                },
            )
        )
        result = self.sent[-1]
        self.assertEqual(result.message_type, TYPE_ERROR)
        self.assertEqual(result.code, 0x0001)

    def test_approval_object_kind_matches_protocol_numbers(self):
        cases = {
            "command": 0,
            "fileChange": 1,
            "permissions": 3,
            "mcp": 4,
            "input": 5,
            "unknown-kind": 6,
        }
        for kind, expected in cases.items():
            encoded = self.gateway._encode_approval_object(
                {"request_id": "ap-1", "kind": kind, "thread_id": "t1"}
            )
            fields = decode_tlvs(encoded)
            self.assertEqual(fields[TAG_APPROVAL_KIND][0], expected, kind)

    def test_status_result_projects_online_field(self):
        fields = self.gateway._result_fields(
            {
                "connection": {"transport": "stdio", "desktop_sync_mode": "polling"},
                "connection_probe": {"transport": "unstarted"},
            }
        )
        self.assertEqual(fields[0x0090], u8(1))
        offline = self.gateway._result_fields(
            {"connection": {"transport": "unstarted"}}
        )
        self.assertEqual(offline[0x0090], u8(0))

    def test_audio_begin_retry_with_same_tx_id_and_idem_is_accepted(self):
        self.gateway.capability_policy = lambda device_id: {"status", "audio"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        begin = Frame(
            message_type=TYPE_AUDIO_BEGIN,
            flags=FLAG_REQUEST,
            header={0x002D: u32(5), 0x002B: b"i" * 16, 0x0074: u32(16000)},
        )
        self.gateway.handle_frame(begin)
        self.gateway.handle_frame(begin)
        self.assertFalse(any(frame.message_type == TYPE_ERROR for frame in self.sent))

    def test_new_connection_clears_stale_transactions(self):
        self.gateway.capability_policy = lambda device_id: {"status", "audio"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        begin = Frame(
            message_type=TYPE_AUDIO_BEGIN,
            flags=FLAG_REQUEST,
            header={0x002D: u32(6), 0x002B: b"i" * 16, 0x0074: u32(16000)},
        )
        self.gateway.handle_frame(begin)
        self.assertTrue(self.gateway._transactions)
        self.gateway.begin_session()
        self.assertEqual(self.gateway._transactions, {})

    def test_audio_end_preserves_stt_failure_reason(self):
        class Recognizer:
            def transcribe_pcm(self, device_id, data, **kwargs):
                raise RuntimeError("STT upstream refused the request")

        self.gateway.speech = Recognizer()
        self.gateway.capability_policy = lambda device_id: {"status", "audio", "control"}
        self.gateway.active_thread = lambda device_id: "thread-1"
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(32),
                    0x0073: u8(0),
                    0x0074: u32(16000),
                    0x0075: u8(1),
                    0x0076: u8(16),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(32), 0x0048: u32(0)},
                payload=b"\x01\x02" * 8,
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(32)},
            )
        )
        error = self.sent[-1]
        self.assertEqual(error.message_type, TYPE_ERROR)
        self.assertEqual(error.code, 0x000B)
        self.assertEqual(
            decode_utf8(error.header[0x0062]),
            "STT upstream refused the request",
        )
        detail = decode_tlvs(error.header[0x0063])
        self.assertEqual(decode_utf8(detail[0x0096]), "STT_UNAVAILABLE")

    def test_thread_read_maps_history_depth_to_message_limit(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [
                (0x0021, u32(self.refs.ref_for("thread", "t1"))),
                (0x0014, u16(2)),
            ]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(30),
                    TAG_METHOD_ID: u16(0x0003),
                    TAG_PARAMS: params,
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)
        request = self.dispatcher.calls[-1][2]
        self.assertEqual(request["method"], "thread.read")
        self.assertEqual(
            request["params"],
            {"thread_id": "t1", "message_limit": 2},
        )

    def test_message_read_update_dispatches_through_shared_command_layer(self):
        self.gateway.capability_policy = lambda device_id: {"status", "read_unread"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["read_unread"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [
                (0x0025, u32(self.refs.ref_for("message", "m1"))),
                (0x0021, u32(self.refs.ref_for("thread", "t1"))),
                (0x0054, i64(1791000000000)),
            ]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(31),
                    TAG_METHOD_ID: u16(0x00A0),
                    TAG_PARAMS: params,
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)
        request = self.dispatcher.calls[-1][2]
        self.assertEqual(request["method"], "message.read.update")
        self.assertEqual(
            request["params"],
            {
                "message_id": "m1",
                "thread_id": "t1",
                "read_at_ms": 1791000000000,
            },
        )

    def test_thread_read_zero_history_depth_requests_metadata_not_a_message(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [
                (0x0021, u32(self.refs.ref_for("thread", "t1"))),
                (0x0014, u16(0)),
            ]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(33),
                    TAG_METHOD_ID: u16(0x0003),
                    TAG_PARAMS: params,
                },
            )
        )
        self.assertEqual(
            self.dispatcher.calls[-1][2]["params"]["message_limit"],
            0,
        )

    def test_thread_read_omitted_history_depth_uses_profile_budget(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [(0x0021, u32(self.refs.ref_for("thread", "t1")))]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(34),
                    TAG_METHOD_ID: u16(0x0003),
                    TAG_PARAMS: params,
                },
            )
        )
        self.assertEqual(
            self.dispatcher.calls[-1][2]["params"]["message_limit"],
            2,
        )

    def test_unknown_message_ref_returns_not_found(self):
        self.gateway.capability_policy = lambda device_id: {"status", "read_unread"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["read_unread"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [
                (0x0025, u32(999999)),
                (0x0054, i64(1791000000000)),
            ]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(35),
                    TAG_METHOD_ID: u16(0x00A0),
                    TAG_PARAMS: params,
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0001)
        self.assertEqual(self.dispatcher.calls, [])

    def test_unknown_command_error_keeps_its_operation_reason(self):
        class RefusingDispatcher:
            def execute(self, device_id, capabilities, request):
                raise gateway.CommandError("invalid_params")

        self.gateway.commands = RefusingDispatcher()
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(36),
                    TAG_METHOD_ID: u16(0x0001),
                    TAG_PARAMS: encode_tlvs([]),
                },
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].code, 0x0006)
        self.assertEqual(decode_utf8(self.sent[-1].header[0x0062]), "invalid_params")

    def test_thread_read_result_carries_top_level_thread_ref_and_status(self):
        self.gateway.capability_policy = lambda device_id: {"status", "tasks"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["tasks"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        fields = self.gateway._result_fields(
            {
                "thread": {
                    "thread_id": "t1",
                    "title": "Task",
                    "status": "working",
                    "model": "gpt",
                    "cwd": "D:\\work",
                },
                "messages": [{"id": "m1", "kind": "user", "text": "hi"}],
            }
        )
        self.assertIn(TAG_THREAD_REF, fields)
        self.assertEqual(decode_u16(fields[TAG_STATUS]), 1)
        self.assertEqual(decode_utf8(fields[0x0022]), "t1")
        thread_object = decode_tlvs(
            decode_array(fields[TAG_OBJECTS], TAG_OBJECT)[0][1]
        )
        self.assertEqual(thread_object[0x003F], u8(0))
        self.assertEqual(decode_utf8(thread_object[0x0052]), "D:\\work")

    def test_queue_update_uses_existing_queue_edit_dispatcher(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        params = encode_tlvs(
            [
                (0x0022, utf8("thread-1")),
                (0x0100, utf8("q_123")),
                (0x0042, utf8("updated")),
                (0x006B, u64(2)),
            ]
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_COMMAND,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(10),
                    TAG_METHOD_ID: u16(0x0032),
                    TAG_PARAMS: params,
                },
            )
        )
        request = self.dispatcher.calls[-1][2]
        self.assertEqual(request["method"], "queue.edit")
        self.assertEqual(request["params"]["thread_id"], "thread-1")
        self.assertEqual(request["params"]["queue_id"], "q_123")
        self.assertEqual(request["params"]["text"], "updated")
        self.assertEqual(request["params"]["expected_revision"], 2)

    def test_text_transaction_reaches_dispatcher_as_turn_start(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_BEGIN,
                flags=FLAG_REQUEST,
                header={0x002D: u32(4), 0x002B: b"i" * 16, 0x0070: u8(0)},
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(4), 0x0048: u32(0)},
                payload="你好".encode(),
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(4)},
            )
        )
        self.assertEqual(self.dispatcher.calls[-1][2]["method"], "turn.start")
        self.assertEqual(self.dispatcher.calls[-1][2]["params"]["text"], "你好")
        self.assertEqual(self.dispatcher.calls[-1][2]["params"]["thread_id"], "thread-1")

    def test_send_end_reports_an_error_when_no_thread_is_selected(self):
        self.gateway.active_thread = lambda device_id: ""
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_BEGIN,
                flags=FLAG_REQUEST,
                header={0x002D: u32(7), 0x002B: b"i" * 16, 0x0070: u8(0)},
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(7), 0x0048: u32(0)},
                payload="hello".encode(),
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(7)},
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.sent[-1].header[TAG_REQUEST_ID], u32(7))

    def test_send_uses_explicit_thread_id_from_begin_frame(self):
        self.gateway.active_thread = lambda device_id: "active-thread"
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(9),
                    0x002B: b"i" * 16,
                    0x0070: u8(0),
                    0x0047: u32(5),
                    0x0022: utf8("explicit-thread"),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(9), 0x0048: u32(0)},
                payload=b"hello",
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(9), 0x0047: u32(5)},
            )
        )
        self.assertEqual(
            self.dispatcher.calls[-1][2]["params"]["thread_id"], "explicit-thread"
        )

    def test_send_end_rejects_mismatched_total_bytes(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_BEGIN,
                flags=FLAG_REQUEST,
                header={0x002D: u32(11), 0x002B: b"i" * 16, 0x0070: u8(0)},
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_CHUNK,
                flags=FLAG_REQUEST,
                header={0x002D: u32(11), 0x0048: u32(0)},
                payload=b"hello",
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SEND_END,
                flags=FLAG_REQUEST,
                header={0x002D: u32(11), 0x0047: u32(999)},
            )
        )
        self.assertEqual(self.sent[-1].message_type, TYPE_ERROR)
        self.assertEqual(self.dispatcher.calls, [])

    def test_subscribe_and_ack_update_cursor(self):
        event = self.gateway.event_spine.append(0x10000001, 0x0101, QOS_STATE, {})
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SUBSCRIBE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(5),
                    0x0030: u32(0x10000001),
                    0x0031: u8(2),
                    0x0032: u8(0),
                    0x0033: u64(0),
                },
            )
        )
        self.gateway.handle_frame(
            Frame(
                message_type=0x0B,
                header={0x0030: u32(0x10000001), 0x0031: u8(2), 0x0033: u64(event.global_seq)},
            )
        )
        self.assertEqual(self.gateway.subscriptions.cursor("dev-1", 0x10000001, 2), event.global_seq)

    def test_subscribe_replays_recoverable_cursor_as_delta(self):
        first = self.gateway.event_spine.append(0x10000001, 0x0101, QOS_STATE, {})
        second = self.gateway.event_spine.append(0x10000001, 0x0102, QOS_STATE, {})
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SUBSCRIBE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(6),
                    0x0030: u32(0x10000001),
                    0x0031: u8(2),
                    0x0032: u8(QOS_STATE),
                    0x0033: u64(first.global_seq),
                },
            )
        )
        delta = self.sent[-2]
        result = self.sent[-1]
        self.assertEqual(delta.message_type, 0x0A)
        self.assertEqual(delta.header[0x0035], u64(second.global_seq))
        self.assertEqual(result.message_type, TYPE_RESULT)
        fields = decode_tlvs(encode_tlvs(result.header.items()))
        self.assertEqual(fields[0x0033], u64(second.global_seq))

    def test_subscribe_replay_is_deferred_off_the_reader_thread(self):
        # A long thread history can be hundreds of DELTAs. Emitting them from
        # the socket reader stalls it until the peer drains its window, which
        # truncated an in-flight AUDIO_* upload.
        self.gateway.event_spine.append(0x10000001, 0x0101, QOS_STATE, {})
        deferred = []
        self.gateway.defer = lambda device_id, callback: deferred.append(callback)
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        before = len(self.sent)
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SUBSCRIBE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(61),
                    0x0030: u32(0x10000001),
                    0x0031: u8(2),
                    0x0032: u8(QOS_STATE),
                },
            )
        )
        self.assertEqual(len(self.sent), before)
        self.assertEqual(len(deferred), 1)
        deferred[0]()
        self.assertTrue(any(frame.message_type == 0x0A for frame in self.sent[before:]))
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)

    def test_audio_begin_reports_unknown_thread_ref_instead_of_dropping(self):
        self.gateway.capability_policy = lambda device_id: {"status", "audio"}
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        session.supported |= capability_bitmap(["audio"])
        self.gateway.refresh_capabilities()
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_AUDIO_BEGIN,
                flags=FLAG_REQUEST,
                header={
                    0x002D: u32(77),
                    TAG_THREAD_REF: u32(999999),
                    TAG_USE_ACTIVE_THREAD: u8(1),
                },
            )
        )
        error = self.sent[-1]
        self.assertEqual(error.message_type, TYPE_ERROR)
        self.assertEqual(error.code, 0x0001)
        self.assertNotIn(77, self.gateway._transactions)

    def test_subscribe_unrecoverable_cursor_emits_resync(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_SUBSCRIBE,
                flags=FLAG_REQUEST,
                header={
                    TAG_REQUEST_ID: u32(7),
                    0x0030: u32(0x10000001),
                    0x0031: u8(2),
                    0x0032: u8(QOS_STATE),
                    0x0033: u64(99),
                },
            )
        )
        self.assertEqual(self.sent[-2].message_type, TYPE_RESYNC)
        self.assertEqual(self.sent[-1].message_type, TYPE_RESULT)

    def test_subscribe_store_error_is_diagnostic_internal(self):
        self._authenticated()
        self.sent.clear()
        with mock.patch.object(
            self.gateway.subscriptions,
            "subscribe",
            side_effect=sqlite3.DatabaseError("database disk image is malformed"),
        ):
            self.gateway.handle_frame(
                Frame(
                    message_type=TYPE_SUBSCRIBE,
                    flags=FLAG_REQUEST,
                    header={
                        TAG_REQUEST_ID: u32(19),
                        0x0030: u32(0x50000003),
                        0x0031: u8(0),
                        0x0032: u8(QOS_STATE),
                        0x0033: u64(0),
                    },
                )
            )

        error = self.sent[-1]
        self.assertEqual(error.message_type, TYPE_ERROR)
        self.assertEqual(error.code, 0x0015)
        self.assertEqual(decode_utf8(error.header[0x0062]), "EVENT_STORE_UNAVAILABLE")
        detail = decode_tlvs(error.header[0x0063])
        self.assertEqual(decode_utf8(detail[0x0096]), "event_store_unavailable")

    def _authenticated(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        return session

    def test_peer_error_is_acknowledged_without_echoing_an_error(self):
        self._authenticated()
        self.sent.clear()
        self.gateway.handle_frame(
            Frame(
                message_type=TYPE_ERROR,
                flags=FLAG_RESPONSE,
                code=0x0005,  # INVALID_FRAME reported by the device
                header={TAG_REQUEST_ID: u32(0)},
            )
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(self.gateway.last_peer_error, (0x0005, 0))

    def test_peer_close_is_not_answered(self):
        self._authenticated()
        self.sent.clear()
        self.gateway.handle_frame(Frame(message_type=TYPE_CLOSE, flags=FLAG_REQUEST))
        self.assertEqual(self.sent, [])
        self.assertTrue(self.gateway.session_closed)

    def test_subscription_check_is_per_device(self):
        self._authenticated()
        self.gateway.subscriptions.subscribe("someone-else", 0x10000001, 1, QOS_STATE)
        self.assertFalse(self.gateway.has_subscription(0x10000001, 1))
        self.gateway.subscriptions.subscribe("dev-1", 0x10000001, 1, QOS_STATE)
        self.assertTrue(self.gateway.has_subscription(0x10000001, 1))

    def test_deliver_event_targets_the_matching_scope(self):
        self._authenticated()
        self.gateway.subscriptions.subscribe("dev-1", 0x10000001, 1, QOS_STATE)
        self.sent.clear()
        event = self.gateway.event_spine.append(0x10000001, 0x0301, 0, {})

        self.gateway.deliver_event(event)

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].message_type, 0x0A)
        self.assertEqual(self.sent[0].header[0x0037], (0x0301).to_bytes(2, "big"))
        # Protocol 15.7: a DELTA must carry the full event envelope and a real
        # transport sequence, not the reduced compatibility shape.
        self.assertEqual(self.sent[0].topic_id, 0x10000001)
        self.assertNotEqual(self.sent[0].seq, 0)
        self.assertEqual(len(self.sent[0].header[0x0029]), 16)
        self.assertEqual(len(self.sent[0].header[0x0034]), 8)
        self.assertEqual(len(self.sent[0].header[0x0035]), 8)
        self.assertEqual(len(self.sent[0].header[0x0036]), 8)

    def test_deliver_event_without_matching_scope_is_not_sent(self):
        self._authenticated()
        # A live subscription must not receive a summary-scope event.
        self.gateway.subscriptions.subscribe("dev-1", 0x10000001, 2, QOS_STATE)
        self.sent.clear()
        event = self.gateway.event_spine.append(0x10000001, 0x0303, 1, {})

        self.gateway.deliver_event(event)

        self.assertEqual(self.sent, [])

    def test_deliver_event_before_ready_is_not_sent(self):
        event = self.gateway.event_spine.append(0x10000001, 0x0301, 0, {})
        self.gateway.deliver_event(event)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
