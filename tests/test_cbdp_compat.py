import tempfile
import unittest
from pathlib import Path

from codexbridge.cbdp.compat import (
    EVENT_MESSAGE_READ,
    CompatContext,
    from_cbdp,
    to_cbdp,
)
from codexbridge.cbdp.constants import (
    TAG_BODY_REF,
    TAG_CREATED_AT_MS,
    TAG_DEVICE_REF,
    TAG_MESSAGE_ID,
    TAG_READ_AT_MS,
    TAG_SENDER,
    TAG_STATUS,
    TAG_SUMMARY,
    TAG_TURN_REF,
    TAG_UPDATED_AT_MS,
    TAG_KIND,
    TAG_METHOD_ID,
    TAG_PARAMS,
    TAG_REQUEST_ID,
    TAG_SESSION_ID,
    TAG_STATE,
    TAG_THREAD_ID,
    TYPE_AUTH,
    TYPE_AUTH_OK,
    TYPE_COMMAND,
    TYPE_DELTA,
    TYPE_HELLO,
    TYPE_RESULT,
    TYPE_WELCOME,
)
from codexbridge.cbdp.refs import ReferenceRegistry
from codexbridge.cbdp.tlv import (
    decode_i64,
    decode_tlvs,
    decode_u8,
    decode_u16,
    decode_u32,
    decode_u64,
    decode_utf8,
)


class CbdpCompatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.refs = ReferenceRegistry(Path(tmp.name) / "refs.sqlite3")
        self.addCleanup(self.refs.close)
        self.context = CompatContext(
            refs=self.refs,
            device_id="dev-1",
            request_id=7,
            topic_id=0x10000001,
        )

    def test_bridge_auth_maps_to_hello_and_auth(self):
        frames = to_cbdp(
            {
                "type": "bridge_auth",
                "model": "panel",
                "firmware": "1.2.3",
                "capabilities": ["status", "text"],
                "auth_proof": b"x" * 32,
                "session_id": b"s" * 16,
                "auth_mode": 1,
            },
            self.context,
        )
        self.assertEqual([frame.message_type for frame in frames], [TYPE_HELLO, TYPE_AUTH])
        self.assertEqual(frames[1].header[TAG_SESSION_ID], b"s" * 16)

    def test_bridge_auth_ok_maps_to_welcome_and_auth_ok(self):
        frames = to_cbdp(
            {
                "type": "bridge_auth_ok",
                "device_id": "dev-1",
                "session_id": b"s" * 16,
                "server_nonce": b"n" * 16,
                "effective": ["status", "text"],
            },
            self.context,
        )
        self.assertEqual(
            [frame.message_type for frame in frames],
            [TYPE_WELCOME, TYPE_AUTH_OK],
        )
        self.assertGreater(decode_u32(frames[1].header[TAG_DEVICE_REF]), 0)

    def test_message_and_read_state_use_cbdp_event_payloads(self):
        message = to_cbdp(
            {
                "type": "message",
                "message_id": "m1",
                "thread_id": "t1",
                "turn_id": "u1",
                "kind": "assistant_text",
                "status": "completed",
                "body": "hi",
            },
            self.context,
        )[0]
        self.assertEqual(message.message_type, TYPE_DELTA)
        payload = decode_tlvs(message.header[0x0038])
        self.assertEqual(decode_utf8(payload[TAG_MESSAGE_ID]), "m1")
        self.assertEqual(decode_utf8(payload[TAG_THREAD_ID]), "t1")
        self.assertEqual(decode_u16(payload[TAG_STATUS]), 2)
        self.assertEqual(decode_u8(payload[TAG_SENDER]), 1)
        self.assertEqual(decode_utf8(payload[TAG_SUMMARY]), "hi")
        self.assertEqual(decode_utf8(payload[TAG_BODY_REF]), "content:m1")
        self.assertEqual(len(payload[TAG_TURN_REF]), 4)
        self.assertEqual(len(payload[TAG_CREATED_AT_MS]), 8)
        self.assertEqual(len(payload[TAG_UPDATED_AT_MS]), 8)

        read = to_cbdp(
            {
                "type": "read_state",
                "message_id": "m1",
                "thread_id": "t1",
                "read_by": ["dev-1"],
            },
            self.context,
        )[0]
        read_payload = decode_tlvs(read.header[0x0038])
        self.assertEqual(read.header[0x0037], (EVENT_MESSAGE_READ).to_bytes(2, "big"))
        self.assertEqual(
            decode_u32(read_payload[TAG_DEVICE_REF]),
            self.refs.ref_for("device", "dev-1"),
        )
        self.assertEqual(len(read_payload[TAG_READ_AT_MS]), 8)
        decode_i64(read_payload[TAG_READ_AT_MS])

    def test_legacy_device_event_maps_to_command_with_params(self):
        command = to_cbdp(
            {
                "type": "device_event",
                "event": {
                    "type": "voice_command",
                    "text": "hello",
                    "thread_id": "t1",
                },
            },
            self.context,
        )[0]
        self.assertEqual(command.message_type, TYPE_COMMAND)
        self.assertEqual(decode_u16(command.header[TAG_METHOD_ID]), 5)
        params = decode_tlvs(command.header[TAG_PARAMS])
        self.assertEqual(decode_utf8(params[TAG_THREAD_ID]), "t1")

    def test_cbdp_read_state_round_trips_through_legacy_envelope(self):
        frame = to_cbdp(
            {
                "type": "read_state",
                "message_id": "m1",
                "thread_id": "t1",
                "read_by": ["dev-1"],
            },
            self.context,
        )[0]
        legacy = from_cbdp(frame, self.context)
        self.assertEqual(legacy["type"], "read_state")
        self.assertEqual(legacy["read_by"], ["dev-1"])

    def test_cbdp_result_maps_back_to_command_result(self):
        from codexbridge.cbdp.frame import Frame

        legacy = from_cbdp(
            Frame(
                message_type=TYPE_RESULT,
                header={
                    TAG_REQUEST_ID: (9).to_bytes(4, "big"),
                    TAG_STATE: b"\x01",
                },
            ),
            self.context,
        )
        self.assertEqual(legacy["type"], "command_result")
        self.assertTrue(legacy["ok"])


if __name__ == "__main__":
    unittest.main()
