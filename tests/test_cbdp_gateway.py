import tempfile
import unittest
from pathlib import Path

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
    TAG_NEXT_CURSOR,
    TAG_THREAD_REF,
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
    TYPE_AUTH,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_AUTH_OK,
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
from codexbridge.cbdp.frame import Frame
from codexbridge.cbdp.gateway import CbdpGateway
from codexbridge.cbdp.refs import ReferenceRegistry
from codexbridge.cbdp.tlv import (
    decode_array,
    decode_u16,
    decode_utf8,
    decode_i64,
    decode_tlvs,
    decode_u32,
    decode_u64,
    encode_tlvs,
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

    def _authenticated(self):
        self.gateway.handle_frame(self.hello())
        session = self.gateway.session
        self.gateway.authenticate(
            session_id=session.session_id,
            proof=self.gateway.expected_proof("dev-1", session),
            device_id="dev-1",
        )
        return session

    def test_deliver_event_targets_the_matching_scope(self):
        self._authenticated()
        self.gateway.subscriptions.subscribe("dev-1", 0x10000001, 1, QOS_STATE)
        self.sent.clear()
        event = self.gateway.event_spine.append(0x10000001, 0x0301, 0, {})

        self.gateway.deliver_event(event)

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].message_type, 0x0A)
        self.assertEqual(self.sent[0].header[0x0037], (0x0301).to_bytes(2, "big"))

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
