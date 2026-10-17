from __future__ import annotations

from dataclasses import dataclass

from codexbridge.cbdp.capabilities import capability_bitmap, capability_names
from codexbridge.cbdp.constants import (
    TAG_APPROVAL_ID,
    FLAG_COMMAND,
    FLAG_EVENT,
    FLAG_REQUEST,
    FLAG_RESPONSE,
    QOS_CRITICAL,
    QOS_STATE,
    TAG_AUTH_MODE,
    TAG_AUTH_PROOF,
    TAG_BODY,
    TAG_BODY_REF,
    TAG_CAPABILITY_CATALOG,
    TAG_CLIENT_NONCE,
    TAG_CREATED_AT_MS,
    TAG_DECISION,
    TAG_DEVICE_ID,
    TAG_DEVICE_REF,
    TAG_DEVICE_STATE,
    TAG_EFFECTIVE_CAPS,
    TAG_ENABLED_CAPS,
    TAG_ERROR_MESSAGE,
    TAG_EVENT_PAYLOAD,
    TAG_EVENT_TYPE,
    TAG_FIRMWARE,
    TAG_KIND,
    TAG_MAX_FRAME_BYTES,
    TAG_MAX_TEXT_BYTES,
    TAG_MAX_TEXT_CHARS,
    TAG_MESSAGE_ID,
    TAG_MESSAGE_REF,
    TAG_METHOD_ID,
    TAG_MODE,
    TAG_MODEL,
    TAG_NAME,
    TAG_ONLINE,
    TAG_PARAMS,
    TAG_POLICY_VERSION,
    TAG_PROFILE,
    TAG_QUEUE_DEPTH,
    TAG_READ_AT_MS,
    TAG_SENDER,
    TAG_REQUEST_ID,
    TAG_RESULT,
    TAG_SERVER_NONCE,
    TAG_SESSION_ID,
    TAG_STATE,
    TAG_STATUS,
    TAG_SUMMARY,
    TAG_SUPPORTED_CAPS,
    TAG_THREAD_ID,
    TAG_THREAD_REF,
    TAG_TITLE,
    TAG_TURN_ID,
    TAG_TURN_REF,
    TAG_UPDATED_AT_MS,
    TAG_VERSION_MINOR,
    TAG_WRITER_LEASE_SECONDS,
    TYPE_AUTH,
    TYPE_AUTH_OK,
    TYPE_CAPABILITY_UPDATE,
    TYPE_COMMAND,
    TYPE_DEVICE_STATUS,
    TYPE_DELTA,
    TYPE_ERROR,
    TYPE_HELLO,
    TYPE_PING,
    TYPE_RESULT,
    TYPE_WELCOME,
)
from codexbridge.cbdp.frame import Frame
from codexbridge.cbdp.projection import (
    EVENT_MESSAGE_APPENDED,
    EVENT_MESSAGE_UPDATED,
)
from codexbridge.cbdp.tlv import (
    CBDPError,
    decode_tlvs,
    decode_i64,
    decode_u8,
    decode_u16,
    decode_u32,
    decode_u64,
    decode_utf8,
    encode_tlvs,
    i64,
    u8,
    u16,
    u32,
    u64,
    utf8,
)


EVENT_MESSAGE_READ = 0x0303
EVENT_DEVICE_ONLINE = 0x0601
EVENT_DEVICE_OFFLINE = 0x0602
EVENT_DEVICE_POLICY_CHANGED = 0x0603
EVENT_NOTICE_CREATED = 0x0C0F

# Kept in sync with ``gateway.METHOD_IDS``. The gateway is the transport
# authority; this table lets a legacy command be expressed with the same stable
# method number before it reaches the gateway.
METHOD_IDS = {
    "turn.start": 0x0005,
    "turn.steer": 0x0006,
    "turn.interrupt": 0x0007,
    "approval.resolve": 0x0009,
    "message.read.update": 0x00A0,
}
METHOD_NAMES = {value: name for name, value in METHOD_IDS.items()}

KIND_IDS = {
    "user_text": 0,
    "assistant_text": 1,
    "commentary": 2,
    "tool": 3,
    "file_change": 4,
    "error": 5,
    "approval": 6,
    "system": 7,
    "status": 7,
    "task": 2,
    "result": 1,
}
KIND_NAMES = {
    0: "user_text",
    1: "assistant_text",
    2: "commentary",
    3: "tool",
    4: "file_change",
    5: "error",
    6: "approval",
    7: "system",
}
STATUS_IDS = {
    "pending": 0,
    "streaming": 1,
    "completed": 2,
    "failed": 3,
    "interrupted": 4,
    "working": 1,
    "waiting_user": 0,
    "queued": 0,
    "starting": 0,
    "done": 2,
}
STATUS_NAMES = {
    0: "pending",
    1: "streaming",
    2: "completed",
    3: "failed",
    4: "interrupted",
}

LEGACY_EVENT_TYPES = {
    "message": EVENT_MESSAGE_APPENDED,
    "read_state": EVENT_MESSAGE_READ,
    "device_event": 0x0C01,
    "command_result": 0x0503,
    "protocol_catalog": 0x0C0E,
    "capabilities_update": EVENT_DEVICE_POLICY_CHANGED,
    "error": EVENT_DEVICE_POLICY_CHANGED,
}


@dataclass
class CompatContext:
    refs: object
    device_id: str = ""
    envelope: str = ""
    request_id: int = 0
    topic_id: int = 0
    event_type: int = 0


def _refs(context):
    refs = getattr(context, "refs", None)
    if refs is None:
        raise ValueError("compat context requires a reference registry")
    return refs


def _refs_for(context, kind, external_id):
    value = str(external_id or "")
    return 0 if not value else int(_refs(context).ref_for(kind, value))


def _legacy_kind(value):
    return KIND_IDS.get(str(value or "").lower(), KIND_IDS["system"])


def _legacy_status(value):
    return STATUS_IDS.get(str(value or "").lower(), 0)


def _thread_fields(context, value):
    thread_id = str(value.get("thread_id") or "")
    if not thread_id:
        return {}
    return {
        TAG_THREAD_REF: u32(_refs_for(context, "thread", thread_id)),
        TAG_THREAD_ID: utf8(thread_id),
    }


def _message_fields(context, value):
    fields = _thread_fields(context, value)
    turn_id = str(value.get("turn_id") or "")
    if turn_id:
        fields[TAG_TURN_REF] = u32(_refs_for(context, "turn", turn_id))
        fields[TAG_TURN_ID] = utf8(turn_id)
    message_id = str(value.get("message_id") or "")
    if message_id:
        fields[TAG_MESSAGE_REF] = u32(_refs_for(context, "message", message_id))
        fields[TAG_MESSAGE_ID] = utf8(message_id)
    return fields


def _encode_params(values):
    fields = {}
    for key, value in dict(values or {}).items():
        if value is None:
            continue
        tag = _PARAM_TAGS.get(str(key))
        if tag is None:
            continue
        if isinstance(value, bool):
            fields[tag] = u8(1 if value else 0)
        elif isinstance(value, int):
            fields[tag] = u64(int(value))
        else:
            fields[tag] = utf8(str(value))
    return encode_tlvs(fields.items())


_PARAM_TAGS = {
    "thread_id": TAG_THREAD_ID,
    "turn_id": TAG_TURN_ID,
    "message_id": TAG_MESSAGE_ID,
    "approval_id": TAG_APPROVAL_ID,
    "text": TAG_BODY,
    "body": TAG_BODY,
    "title": TAG_TITLE,
    "decision": TAG_DECISION,
    "mode": TAG_MODE,
}


def _command_frame(context, method, params):
    method_id = METHOD_IDS.get(str(method))
    if method_id is None:
        return None
    request_id = int(getattr(context, "request_id", 0) or 0)
    return Frame(
        message_type=TYPE_COMMAND,
        flags=FLAG_REQUEST | FLAG_COMMAND,
        header={
            TAG_REQUEST_ID: u32(request_id),
            TAG_METHOD_ID: u16(method_id),
            TAG_PARAMS: _encode_params(params),
        },
    )


def _legacy_device_command(context, event):
    event_type = str(event.get("type") or "")
    mode = str(event.get("mode") or "")
    if event_type == "voice_command":
        method = "turn.steer" if mode == "steer" else "turn.start"
        return _command_frame(
            context,
            method,
            {
                "thread_id": event.get("thread_id") or "",
                "text": event.get("text") or event.get("body") or "",
            },
        )
    if event_type in {"approval_decision", "mcp_response", "approval_resolve"}:
        decision = event.get("decision")
        if isinstance(decision, str):
            decision = {"approve": 0, "deny": 1, "cancel": 3}.get(decision)
        if decision not in {0, 1, 2, 3}:
            return None
        return _command_frame(
            context,
            "approval.resolve",
            {
                "approval_id": event.get("approval_id") or "",
                "decision": int(decision),
            },
        )
    if event_type in {"cancel_task", "turn_interrupt"}:
        return _command_frame(
            context,
            "turn.interrupt",
            {"thread_id": event.get("thread_id") or ""},
        )
    if event_type in {"command", "device_command"}:
        return _command_frame(
            context,
            str(event.get("method") or ""),
            event.get("params") or {},
        )
    return None


def to_cbdp(message, context):
    """Translate one legacy JSON message into CBDP frame(s).

    Translation keeps no business state of its own; only the persistent
    reference registry in ``context`` is consulted. Unknown legacy events
    become a notice-like event and never mutate domain state.
    """

    if not isinstance(message, dict):
        raise TypeError("legacy message must be a dict")
    message_type = str(message.get("type") or "")
    request_id = int(getattr(context, "request_id", 0) or 0)

    if message_type == "bridge_auth":
        # 24.2: bridge_auth is HELLO + AUTH, not a single envelope.
        device_id = str(
            getattr(context, "device_id", "") or message.get("device_id") or ""
        )
        client_nonce = message.get("client_nonce") or b"\x00" * 16
        if not isinstance(client_nonce, (bytes, bytearray)) or len(client_nonce) != 16:
            client_nonce = b"\x00" * 16
        hello = Frame(
            message_type=TYPE_HELLO,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(request_id),
                TAG_DEVICE_ID: utf8(device_id),
                TAG_MODEL: utf8(str(message.get("model") or "legacy")),
                TAG_FIRMWARE: utf8(str(message.get("firmware") or "legacy")),
                TAG_PROFILE: u8(int(message.get("profile") or 0)),
                TAG_SUPPORTED_CAPS: u64(
                    capability_bitmap(message.get("capabilities") or [])
                ),
                TAG_CLIENT_NONCE: bytes(client_nonce),
                TAG_VERSION_MINOR: u16(0),
            },
        )
        proof = message.get("auth_proof")
        if not isinstance(proof, (bytes, bytearray)) or len(proof) != 32:
            return [hello]
        session_id = message.get("session_id") or b"\x00" * 16
        auth = Frame(
            message_type=TYPE_AUTH,
            flags=FLAG_REQUEST,
            header={
                TAG_REQUEST_ID: u32(request_id + 1),
                TAG_SESSION_ID: bytes(session_id),
                TAG_AUTH_PROOF: bytes(proof),
                TAG_AUTH_MODE: u8(int(message.get("auth_mode") or 1)),
            },
        )
        return [hello, auth]

    if message_type == "bridge_auth_ok":
        # 24.2: bridge_auth_ok is WELCOME + AUTH_OK.
        session_id = bytes(message.get("session_id") or b"\x00" * 16)
        server_nonce = bytes(message.get("server_nonce") or b"\x00" * 16)
        welcome = Frame(
            message_type=TYPE_WELCOME,
            flags=FLAG_RESPONSE,
            header={
                TAG_REQUEST_ID: u32(request_id),
                TAG_VERSION_MINOR: u16(0),
                TAG_SESSION_ID: session_id,
                TAG_SERVER_NONCE: server_nonce,
                TAG_AUTH_MODE: u8(int(message.get("auth_mode") or 1)),
                TAG_MAX_FRAME_BYTES: u32(int(message.get("max_frame_bytes") or 65536)),
                TAG_MAX_TEXT_CHARS: u32(int(message.get("max_text_chars") or 0)),
                TAG_MAX_TEXT_BYTES: u32(int(message.get("max_text_bytes") or 0)),
                TAG_QUEUE_DEPTH: u16(int(message.get("queue_depth") or 32)),
                TAG_WRITER_LEASE_SECONDS: u32(
                    int(message.get("writer_lease_seconds") or 300)
                ),
            },
        )
        device_id = str(message.get("device_id") or "")
        auth_ok = Frame(
            message_type=TYPE_AUTH_OK,
            flags=FLAG_RESPONSE,
            header={
                TAG_REQUEST_ID: u32(request_id),
                TAG_DEVICE_ID: utf8(device_id),
                TAG_EFFECTIVE_CAPS: u64(
                    capability_bitmap(message.get("effective") or [])
                ),
                TAG_POLICY_VERSION: u32(int(message.get("policy_version") or 1)),
                TAG_DEVICE_REF: u32(_refs_for(context, "device", device_id)),
            },
        )
        return [welcome, auth_ok]

    if message_type == "bridge_ready":
        capabilities = message.get("capabilities") or []
        return [
            Frame(
                message_type=TYPE_CAPABILITY_UPDATE,
                header={
                    TAG_EFFECTIVE_CAPS: u64(capability_bitmap(capabilities)),
                    TAG_ENABLED_CAPS: u64(capability_bitmap(capabilities)),
                    TAG_POLICY_VERSION: u32(int(message.get("policy_version") or 1)),
                },
            )
        ]

    if message_type == "capabilities_update":
        capabilities = message.get("enabled") or message.get("capabilities") or []
        return [
            Frame(
                message_type=TYPE_CAPABILITY_UPDATE,
                qos=QOS_CRITICAL,
                header={
                    TAG_EFFECTIVE_CAPS: u64(capability_bitmap(capabilities)),
                    TAG_ENABLED_CAPS: u64(capability_bitmap(capabilities)),
                    TAG_POLICY_VERSION: u32(int(message.get("policy_version") or 1)),
                },
            )
        ]

    if message_type == "message":
        created = int(float(message.get("created_at") or 0) * 1000)
        updated = int(float(message.get("updated_at") or 0) * 1000)
        if not updated:
            updated = created
        message_id = str(message.get("message_id") or "")
        kind = str(message.get("kind") or "")
        sender = {
            "user_text": 0,
            "assistant_text": 1,
            "commentary": 1,
            "tool": 2,
            "file_change": 2,
            "approval": 3,
            "system": 3,
            "status": 3,
            "task": 3,
            "result": 1,
        }.get(kind, 3)
        payload = {
            **_message_fields(context, message),
            TAG_KIND: u8(_legacy_kind(kind)),
            TAG_STATUS: u16(_legacy_status(message.get("status"))),
            TAG_SENDER: u8(sender),
            TAG_SUMMARY: utf8(str(message.get("title") or message.get("body") or "")),
            TAG_BODY_REF: utf8(
                str(message.get("body_ref") or (f"content:{message_id}" if message_id else ""))
            ),
            TAG_BODY: utf8(str(message.get("body") or "")),
            TAG_CREATED_AT_MS: i64(created),
            TAG_UPDATED_AT_MS: i64(updated),
        }
        if message.get("title") is not None:
            payload[TAG_TITLE] = utf8(str(message.get("title") or ""))
        event_type = int(
            getattr(context, "event_type", 0) or EVENT_MESSAGE_APPENDED
        )
        return [
            Frame(
                message_type=TYPE_DELTA,
                flags=FLAG_EVENT,
                qos=QOS_CRITICAL
                if event_type == EVENT_MESSAGE_APPENDED
                else QOS_STATE,
                topic_id=int(getattr(context, "topic_id", 0) or 0),
                header={
                    TAG_EVENT_TYPE: u16(event_type),
                    TAG_EVENT_PAYLOAD: encode_tlvs(payload.items()),
                },
            )
        ]

    if message_type == "read_state":
        # message.read carries one device_ref. A legacy read_by list is expanded
        # to one event per reader instead of overloading a bitmap field.
        message_id = str(message.get("message_id") or "")
        thread_id = str(message.get("thread_id") or "")
        readers = message.get("read_by")
        if not isinstance(readers, list):
            readers = [readers] if readers else []
        if not readers and getattr(context, "device_id", ""):
            readers = [getattr(context, "device_id")]
        frames = []
        for reader in readers:
            payload = {
                **_message_fields(
                    context,
                    {"thread_id": thread_id, "message_id": message_id},
                ),
                TAG_READ_AT_MS: i64(
                    int(float(message.get("read_at") or 0) * 1000)
                ),
                TAG_DEVICE_REF: u32(_refs_for(context, "device", str(reader))),
            }
            frames.append(
                Frame(
                    message_type=TYPE_DELTA,
                    flags=FLAG_EVENT,
                    qos=QOS_STATE,
                    topic_id=int(getattr(context, "topic_id", 0) or 0),
                    header={
                        TAG_EVENT_TYPE: u16(EVENT_MESSAGE_READ),
                        TAG_EVENT_PAYLOAD: encode_tlvs(payload.items()),
                    },
                )
            )
        return frames

    if message_type == "device_event":
        event = message.get("event") if isinstance(message.get("event"), dict) else message
        frame = _legacy_device_command(context, event)
        if frame is not None:
            return [frame]
        return [
            Frame(
                message_type=TYPE_ERROR,
                flags=FLAG_RESPONSE,
                code=0x0006,
                header={
                    TAG_REQUEST_ID: u32(request_id),
                    TAG_ERROR_MESSAGE: utf8("INVALID_PARAM"),
                },
            )
        ]

    if message_type == "command_result":
        method_id = int(
            message.get("method_id")
            or METHOD_IDS.get(str(message.get("method") or ""), 0)
        )
        state = 1 if message.get("ok") is True else 2
        header = {
            TAG_REQUEST_ID: u32(request_id),
            TAG_METHOD_ID: u16(method_id),
            TAG_STATE: u8(state),
        }
        if state == 1:
            header[TAG_RESULT] = encode_tlvs([])
        else:
            header[TAG_ERROR_MESSAGE] = utf8(str(message.get("error") or "INTERNAL"))
        return [Frame(message_type=TYPE_RESULT, flags=FLAG_RESPONSE, header=header)]

    if message_type == "protocol_catalog":
        return [
            Frame(
                message_type=TYPE_DEVICE_STATUS,
                header={
                    TAG_ONLINE: u8(1),
                    TAG_DEVICE_STATE: u8(1),
                    TAG_CAPABILITY_CATALOG: encode_tlvs([]),
                },
            )
        ]

    if message_type == "error":
        return [
            Frame(
                message_type=TYPE_ERROR,
                flags=FLAG_RESPONSE,
                header={
                    TAG_REQUEST_ID: u32(request_id),
                    TAG_ERROR_MESSAGE: utf8(
                        str(message.get("text") or message.get("code") or "ERROR")
                    ),
                },
            )
        ]

    event = message.get("event") if isinstance(message.get("event"), dict) else message
    payload = encode_tlvs(
        {
            TAG_KIND: u8(KIND_IDS["system"]),
            TAG_BODY: utf8(str(event.get("type") or message_type)),
        }.items()
    )
    return [
        Frame(
            message_type=TYPE_DELTA,
            flags=FLAG_EVENT,
            qos=QOS_STATE,
            header={
                TAG_EVENT_TYPE: u16(EVENT_NOTICE_CREATED),
                TAG_EVENT_PAYLOAD: payload,
            },
        )
    ]


def from_cbdp(frame, context):
    """Translate one CBDP frame into a legacy JSON envelope."""

    if not isinstance(frame, Frame):
        raise TypeError("frame must be a Frame")
    request_id = _request_id(frame)

    if frame.message_type == TYPE_AUTH_OK:
        return {
            "type": "bridge_auth_ok",
            "device_id": _text(frame.header, TAG_DEVICE_ID),
        }
    if frame.message_type == TYPE_WELCOME:
        return {
            "type": "bridge_auth_ok",
            "session_id": frame.header.get(TAG_SESSION_ID, b""),
            "server_nonce": frame.header.get(TAG_SERVER_NONCE, b""),
            "max_frame_bytes": _number(frame.header, TAG_MAX_FRAME_BYTES),
            "max_text_chars": _number(frame.header, TAG_MAX_TEXT_CHARS),
            "max_text_bytes": _number(frame.header, TAG_MAX_TEXT_BYTES),
            "queue_depth": _number(frame.header, TAG_QUEUE_DEPTH),
        }
    if frame.message_type == TYPE_PING:
        return {"type": "bridge_ready", "bridge": "CodexBridge"}
    if frame.message_type == TYPE_DELTA:
        payload = _payload(frame)
        event_type = _event_type(frame)
        if event_type in {EVENT_MESSAGE_APPENDED, EVENT_MESSAGE_UPDATED}:
            result = {
                "type": "message",
                "message_id": _text(payload, TAG_MESSAGE_ID),
                "thread_id": _text(payload, TAG_THREAD_ID),
                "turn_id": _text(payload, TAG_TURN_ID),
                "kind": _kind_name(payload),
                "status": _status_name(payload),
                "summary": _text(payload, TAG_SUMMARY),
                "body_ref": _text(payload, TAG_BODY_REF),
                "body": _text(payload, TAG_BODY),
                "created_at": _number(payload, TAG_CREATED_AT_MS, signed=True) / 1000.0,
                "updated_at": _number(payload, TAG_UPDATED_AT_MS, signed=True) / 1000.0,
            }
            if TAG_TITLE in payload:
                result["title"] = _text(payload, TAG_TITLE)
            return result
        if event_type == EVENT_MESSAGE_READ:
            device_ref = _number(payload, TAG_DEVICE_REF)
            read_by = _refs(context).external_id("device", device_ref)
            return {
                "type": "read_state",
                "message_id": _text(payload, TAG_MESSAGE_ID),
                "thread_id": _text(payload, TAG_THREAD_ID),
                "read_at": _number(payload, TAG_READ_AT_MS, signed=True) / 1000.0,
                "read_by": [read_by] if read_by else [],
            }
        if event_type in {EVENT_DEVICE_ONLINE, EVENT_DEVICE_OFFLINE}:
            return {
                "type": "device_event",
                "event": {
                    "type": "online"
                    if event_type == EVENT_DEVICE_ONLINE
                    else "offline",
                    "device_id": _text(payload, TAG_DEVICE_ID),
                },
            }
        return None
    if frame.message_type == TYPE_COMMAND:
        method_id = _number(frame.header, TAG_METHOD_ID)
        return {
            "type": "device_event",
            "event": {
                "type": METHOD_NAMES.get(method_id, "command"),
                "request_id": request_id,
                "params": _decode_params(frame.header.get(TAG_PARAMS)),
            },
        }
    if frame.message_type == TYPE_RESULT:
        state = _number(frame.header, TAG_STATE)
        return {
            "type": "command_result",
            "request_id": request_id,
            "ok": state == 1,
            "error": "" if state == 1 else _text(frame.header, TAG_ERROR_MESSAGE),
        }
    if frame.message_type == TYPE_ERROR:
        return {
            "type": "error",
            "request_id": request_id,
            "text": _text(frame.header, TAG_ERROR_MESSAGE),
        }
    if frame.message_type == TYPE_DEVICE_STATUS:
        return {"type": "protocol_catalog", "request_id": request_id}
    if frame.message_type == TYPE_CAPABILITY_UPDATE:
        return {
            "type": "capabilities_update",
            "enabled": sorted(
                capability_names(_number(frame.header, TAG_EFFECTIVE_CAPS))
            ),
        }
    return None


def _decode_params(raw):
    if raw is None:
        return {}
    try:
        fields = decode_tlvs(raw)
    except CBDPError:
        return {}
    result = {}
    for name, tag in _PARAM_TAGS.items():
        if tag not in fields or name in result:
            continue
        value = fields[tag]
        try:
            if name in {"decision", "mode"}:
                result[name] = decode_u8(value)
            else:
                result[name] = decode_utf8(value)
        except CBDPError:
            continue
    return result


def _payload(frame):
    raw = frame.header.get(TAG_EVENT_PAYLOAD)
    if raw is None:
        return {}
    try:
        return decode_tlvs(raw)
    except CBDPError:
        return {}


def _event_type(frame):
    return _number(frame.header, TAG_EVENT_TYPE)


def _request_id(frame):
    value = frame.header.get(TAG_REQUEST_ID)
    if value is None:
        return 0
    try:
        return decode_u32(value)
    except CBDPError:
        return 0


def _text(payload, tag):
    value = payload.get(tag)
    if value is None:
        return ""
    try:
        return decode_utf8(value)
    except CBDPError:
        return ""


def _number(payload, tag, signed=False):
    value = payload.get(tag)
    if value is None:
        return 0
    decoders = (decode_i64, decode_u64) if signed else (decode_u64, decode_u32, decode_u16, decode_u8)
    for decoder in decoders:
        try:
            return decoder(value)
        except CBDPError:
            continue
    return 0


def _kind_name(payload):
    value = payload.get(TAG_KIND)
    if value is None:
        return "system"
    try:
        return KIND_NAMES.get(decode_u8(value), "system")
    except CBDPError:
        return "system"


def _status_name(payload):
    value = payload.get(TAG_STATUS)
    if value is None:
        return "pending"
    try:
        return STATUS_NAMES.get(decode_u16(value), "unknown")
    except CBDPError:
        return "unknown"
