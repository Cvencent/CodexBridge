from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from codexbridge.cbdp.auth import hmac_auth_proof, verify_pairing_token
from codexbridge.cbdp.capabilities import (
    PROFILE_BUDGETS,
    UNIMPLEMENTED_CAPABILITIES,
    capability_bitmap,
    effective_capabilities,
    profile_budget,
)
from codexbridge.cbdp.constants import (
    ERROR_CODES,
    FLAG_REQUEST,
    FLAG_RESPONSE,
    QOS_CRITICAL,
    TAG_AUTH_MODE,
    TAG_AUTH_PROOF,
    TAG_CLIENT_NONCE,
    TAG_DEVICE_ID,
    TAG_DEVICE_REF,
    TAG_EFFECTIVE_CAPS,
    TAG_ENABLED_CAPS,
    TAG_HEARTBEAT_SECONDS,
    TAG_MAX_AUDIO_BYTES,
    TAG_MAX_CHUNK_BYTES,
    TAG_MAX_FRAME_BYTES,
    TAG_MAX_TEXT_BYTES,
    TAG_MODEL,
    TAG_FIRMWARE,
    TAG_MAX_TEXT_CHARS,
    TAG_NAME,
    TAG_METHOD_ID,
    TAG_POLICY_VERSION,
    TAG_PROFILE,
    TAG_QUEUE_DEPTH,
    TAG_REQUEST_ID,
    TAG_REVISION,
    TAG_SESSION_ID,
    TAG_SERVER_NONCE,
    TAG_SERVER_TIME_MS,
    TAG_STATE,
    TAG_SUPPORTED_CAPS,
    TAG_TRANSPORT_ALLOWED,
    TAG_TRANSPORT_SECURITY,
    TAG_CURSOR,
    TAG_EVENT,
    TAG_EVENT_ID,
    TAG_RESYNC_REASON,
    TAG_SCOPE,
    TAG_TOPIC_ID,
    TAG_TOPIC_SEQ,
    TAG_GLOBAL_SEQ,
    TAG_EVENT_TYPE,
    TAG_EVENT_PAYLOAD,
    TAG_PING_ID,
    TAG_ECHO,
    TAG_CODEC,
    TAG_OFFSET,
    TAG_SAMPLE_RATE,
    TAG_CHANNELS,
    TAG_BITS_PER_SAMPLE,
    TAG_TOTAL_BYTES,
    TAG_TRANSCRIPT,
    TAG_VERSION_MINOR,
    TAG_WRITER_LEASE_SECONDS,
    TYPE_ACK,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_AUTH,
    TYPE_AUTH_OK,
    TYPE_COMMAND,
    TYPE_ERROR,
    TYPE_HELLO,
    TYPE_PING,
    TYPE_PONG,
    TYPE_RESULT,
    TYPE_SEND_BEGIN,
    TYPE_SEND_CHUNK,
    TYPE_SEND_END,
    TYPE_SUBSCRIBE,
    TYPE_UNSUBSCRIBE,
    TYPE_WELCOME,
    TYPE_DELTA,
    TYPE_RESYNC,
)
from codexbridge.cbdp.events import EventSpine, event_scope
from codexbridge.cbdp.frame import Frame, encode_frame
from codexbridge.cbdp.leases import LeaseConflictError, LeaseManager
from codexbridge.cbdp.subscriptions import SubscriptionLimitError, SubscriptionManager
from codexbridge.cbdp.tlv import (
    CBDPError,
    decode_bool,
    decode_tlvs,
    decode_i64,
    decode_u8,
    decode_u16,
    decode_u32,
    decode_u64,
    decode_utf8,
    encode_tlvs,
    require_tlv,
    i64,
    u8,
    u16,
    u32,
    u64,
    utf8,
)

METHOD_IDS = {
    0x0001: "status.read",
    0x0002: "threads.list",
    0x0003: "thread.read",
    0x0004: "thread.start",
    0x0005: "turn.start",
    0x0006: "turn.steer",
    0x0007: "turn.interrupt",
    0x0008: "approvals.list",
    0x0009: "approval.resolve",
    0x000A: "thread.claim",
    0x000B: "thread.release",
    0x000C: "models.list",
    0x000D: "projects.list",
    0x0030: "queue.add",
    0x0031: "queue.list",
    0x0032: "queue.update",
    0x0033: "queue.delete",
    0x0040: "guidance.send",
    0x0050: "message.edit",
    0x0051: "message.delete",
    0x00A0: "message.read.update",
    0x00B1: "active_state.read",
    0x00B2: "active_state.set",
    0x00B3: "events.since",
}
METHOD_CAPABILITIES = {
    0x0001: "status",
    0x0002: "tasks",
    0x0003: "tasks",
    0x0004: "control",
    0x0005: "control",
    0x0006: "control",
    0x0007: "control",
    0x0008: "approval",
    0x0009: "approval",
    0x000A: "writer_lease",
    0x000B: "writer_lease",
    0x000C: "tasks",
    0x000D: "tasks",
    0x0030: "queue",
    0x0031: "queue",
    0x0032: "queue_manage",
    0x0033: "queue_manage",
    0x0040: "guidance",
    0x0050: "message_edit",
    0x0051: "message_delete",
    0x00A0: "read_unread",
    0x00B1: "cross_device_active",
    0x00B2: "cross_device_active",
    0x00B3: "events_since",
}
DISPATCH_METHODS = {
    "queue.add": "queue.enqueue",
    "queue.update": "queue.edit",
}


PARAM_TAGS = {
    "thread_id": 0x0022,
    "turn_id": 0x0024,
    "message_id": 0x0026,
    "approval_id": 0x0028,
    "text": 0x0042,
    "body": 0x0042,
    "title": 0x0040,
    "decision": 0x0082,
    "queue_id": 0x0100,
    "queue_item_ref": 0x0100,
    "expected_revision": 0x006B,
    "effort": 0x0124,
    "model": 0x0051,
    "limit": 0x0014,
    "cursor": 0x0033,
    "revision": 0x0036,
    "guidance_text": 0x0109,
}


@dataclass
class SessionState:
    generation: int
    session_id: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    server_nonce: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    device_id: str = ""
    client_nonce: bytes = b"\x00" * 16
    profile: int = 0
    model: str = ""
    name: str = ""
    firmware: str = ""
    supported: int = 0
    enabled: int = 0
    transport_allowed: int = (1 << 64) - 1
    effective: int = 0
    authenticated: bool = False
    ready: bool = False
    max_frame_bytes: int = 0
    max_text_chars: int = 0
    max_text_bytes: int = 0
    heartbeat_seconds: int = 0
    queue_depth: int = 0
    max_chunk_bytes: int = 0
    max_audio_bytes: int = 0
    writer_lease_seconds: int = 0


class CbdpGateway:
    def __init__(
        self,
        commands=None,
        event_spine=None,
        subscriptions=None,
        leases=None,
        pairing_token=None,
        capability_policy=None,
        references=None,
        transport_allowed=None,
        speech=None,
        defer=None,
        active_thread=None,
        auth_mode=1,
        policy_version=1,
    ):
        self.commands = commands
        self.pairing_token = pairing_token or (lambda device_id: "")
        # device_id -> enabled capability names. This is the single policy
        # source used at HELLO time; the live registry may update it later and
        # call refresh_capabilities() without inventing a second state store.
        self.capability_policy = capability_policy or (lambda device_id: set())
        self.references = references
        self.speech = speech
        # Optional ``(device_id, callback)`` hook used to run work that can
        # block on Codex outside the device socket reader thread. Leaving it
        # unset keeps unit tests and embedded use fully synchronous.
        self.defer = defer
        # Optional ``device_id -> thread_id`` resolver. CBDP text replies are
        # sent without an explicit thread_id, so the Bridge maps them to the
        # conversation the device currently selected.
        self.active_thread = active_thread
        self.transport_allowed = transport_allowed or (
            lambda device_id: (1 << 64) - 1
        )
        self.auth_mode = int(auth_mode)
        self.policy_version = int(policy_version)
        self.event_spine = event_spine or EventSpine(None)
        self.subscriptions = subscriptions or SubscriptionManager(self.event_spine)
        self.leases = leases or LeaseManager()
        self.session: SessionState | None = None
        self._generation = 0
        self._send = None
        self._outgoing_seq = 0
        self._transactions: dict[bytes, dict] = {}
        self._lock = threading.RLock()

    def bind_transport(self, send):
        self._send = send

    def begin_session(self):
        with self._lock:
            self._generation += 1
            self.session = SessionState(generation=self._generation)
            return self.session

    def expected_proof(self, device_id, session):
        return hmac_auth_proof(
            self.pairing_token(device_id),
            session.session_id,
            session.client_nonce,
            session.server_nonce,
            device_id,
        )

    def authenticate(self, session_id, proof, device_id):
        session = self.session
        if session is None or session_id != session.session_id:
            return False
        if session.device_id and session.device_id != device_id:
            return False
        if not secrets.compare_digest(
            bytes(proof),
            self.expected_proof(device_id, session),
        ):
            return False
        session.device_id = str(device_id)
        session.authenticated = True
        session.ready = True
        return True

    def authenticate_with_mode(self, auth_mode, session_id, proof, device_id):
        session = self.session
        if auth_mode == 1:
            return self.authenticate(session_id, proof, device_id)
        if auth_mode == 0:
            if session is None or session_id != session.session_id:
                return False
            if session.device_id and session.device_id != device_id:
                return False
            if not verify_pairing_token(
                proof,
                self.pairing_token(device_id),
                session.session_id,
                session.client_nonce,
                session.server_nonce,
                device_id,
            ):
                return False
            session.device_id = str(device_id)
            session.authenticated = True
            session.ready = True
            return True
        return False

    def refresh_capabilities(self):
        """Recompute the session's effective capability set from policy."""

        session = self.session
        if session is None:
            return set()
        enabled = {
            str(name)
            for name in self.capability_policy(session.device_id)
            if str(name) not in UNIMPLEMENTED_CAPABILITIES
        }
        session.enabled = capability_bitmap(enabled)
        session.transport_allowed = int(
            self.transport_allowed(session.device_id)
        )
        session.effective = (
            session.supported & session.enabled & session.transport_allowed
        )
        return self._capabilities()

    def _commands(self):
        return self.commands() if callable(self.commands) else self.commands

    def _emit(self, frame):
        if self._send is None:
            return
        self._send(frame)
        return frame

    def _next_seq(self):
        self._outgoing_seq = (self._outgoing_seq % 0xFFFFFFFF) + 1
        return self._outgoing_seq

    def _delta_frame(self, event):
        header = {
            TAG_EVENT_ID: bytes(event.event_id),
            TAG_GLOBAL_SEQ: u64(event.global_seq),
            TAG_TOPIC_SEQ: u64(event.topic_seq),
            TAG_REVISION: u64(event.revision),
            TAG_EVENT_TYPE: u16(event.event_type),
            TAG_EVENT_PAYLOAD: encode_tlvs(event.payload.items()),
        }
        return Frame(
            message_type=TYPE_DELTA,
            flags=0x0400,
            topic_id=int(event.topic_id),
            seq=self._next_seq(),
            qos=int(event.qos),
            header=header,
        )

    def deliver_event(self, event):
        """Send one EventSpine event to every matching live subscription.

        The subscription manager keeps the per-device cursor and bounded
        pending queue. The gateway only decides whether the event is relevant
        to a scope and serialises it.
        """

        scope = event_scope(int(event.event_type))
        self.subscriptions.deliver(
            int(event.topic_id),
            scope,
            int(event.global_seq),
            int(event.qos),
        )
        if not self.subscriptions.has_subscription(
            int(event.topic_id),
            scope,
        ):
            return None
        frame = self._delta_frame(event)
        # Fragmentation is a transport concern: the session splits any logical
        # frame that overflows the negotiated budget, so an oversized critical
        # event is delivered in pieces instead of being dropped here.
        return self._emit(frame)

    def _error(self, request_id=0, code="INVALID_PARAM", message=""):
        frame = Frame(
            message_type=TYPE_ERROR,
            flags=FLAG_RESPONSE,
            code=ERROR_CODES.get(code, ERROR_CODES["INVALID_PARAM"]),
            header={
                TAG_REQUEST_ID: u32(int(request_id or 0)),
                0x0062: utf8(message or code),
            },
        )
        return self._emit(frame)

    def handle_frame(self, frame):
        if not isinstance(frame, Frame):
            raise TypeError("frame must be a Frame")
        session = self.session
        if frame.message_type == TYPE_HELLO:
            return self._handle_hello(frame)
        if frame.message_type == TYPE_PING:
            return self._handle_ping(frame)
        if frame.message_type == TYPE_PONG:
            return None
        if session is None:
            return self._error(0, "UNAUTHORIZED", "HELLO_REQUIRED")
        if frame.message_type == TYPE_AUTH:
            return self._handle_auth(frame)
        if not session.ready:
            return self._error(self._request_id(frame), "UNAUTHORIZED", "AUTH_REQUIRED")
        if frame.message_type == TYPE_ACK:
            return self._handle_ack(frame)
        if frame.message_type == TYPE_SUBSCRIBE:
            return self._handle_subscribe(frame)
        if frame.message_type == TYPE_UNSUBSCRIBE:
            return self._handle_unsubscribe(frame)
        if frame.message_type == TYPE_COMMAND:
            return self._handle_command(frame)
        if frame.message_type == TYPE_SEND_BEGIN:
            return self._handle_send_begin(frame)
        if frame.message_type == TYPE_SEND_CHUNK:
            return self._handle_send_chunk(frame)
        if frame.message_type == TYPE_SEND_END:
            return self._handle_send_end(frame)
        if frame.message_type == TYPE_AUDIO_BEGIN:
            return self._handle_audio_begin(frame)
        if frame.message_type == TYPE_AUDIO_CHUNK:
            return self._handle_audio_chunk(frame)
        if frame.message_type == TYPE_AUDIO_END:
            return self._handle_audio_end(frame)
        return self._error(self._request_id(frame), "UNSUPPORTED_FEATURE")

    def _handle_ping(self, frame):
        header = {}
        ping_id = frame.header.get(TAG_PING_ID)
        if ping_id is not None:
            if len(ping_id) != 4:
                return self._error(self._request_id(frame), "INVALID_PARAM")
            header[TAG_ECHO] = ping_id
        header[TAG_SERVER_TIME_MS] = i64(int(time.time() * 1000))
        return self._emit(Frame(message_type=TYPE_PONG, header=header))

    @staticmethod
    def _request_id(frame):
        value = frame.header.get(TAG_REQUEST_ID)
        if value is None:
            return 0
        try:
            return decode_u32(value)
        except CBDPError:
            return 0

    def _maybe_defer(self, generation, callback) -> bool:
        """Run ``callback`` on the hub worker instead of the socket reader.

        Returns True when the work was handed off. The callback is guarded by
        the session generation so a late worker cannot answer after the
        device has reconnected with a new session.
        """

        if self.defer is None or self.session is None:
            return False
        device_id = str(self.session.device_id or "")
        if not device_id:
            return False

        def guarded():
            session = self.session
            if session is None or session.generation != generation:
                return
            callback()

        self.defer(device_id, guarded)
        return True

    def _handle_hello(self, frame):
        if not (frame.flags & FLAG_REQUEST):
            return self._error(self._request_id(frame), "INVALID_FRAME")
        try:
            device_id = decode_utf8(require_tlv(frame.header, TAG_DEVICE_ID))
            version_minor = decode_u16(require_tlv(frame.header, TAG_VERSION_MINOR))
            model = decode_utf8(require_tlv(frame.header, TAG_MODEL))
            firmware = decode_utf8(require_tlv(frame.header, TAG_FIRMWARE))
            name = decode_utf8(frame.header.get(TAG_NAME, b""))
            profile = decode_u8(require_tlv(frame.header, TAG_PROFILE))
            supported = decode_u64(require_tlv(frame.header, TAG_SUPPORTED_CAPS))
            transport_security = decode_u8(
                require_tlv(frame.header, TAG_TRANSPORT_SECURITY)
            )
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
            client_nonce = frame.header.get(TAG_CLIENT_NONCE, b"\x00" * 16)
            if (
                version_minor != 0
                or not device_id
                or not model
                or not firmware
                or len(name.encode("utf-8")) > 64
                or profile not in {0, 1, 2}
                or transport_security not in {0, 1}
                or len(client_nonce) != 16
            ):
                raise CBDPError("INVALID_PARAM")
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        session = self.begin_session()
        session.device_id = device_id
        session.profile = profile
        session.model = model
        session.name = name
        session.firmware = firmware
        session.supported = supported
        session.client_nonce = bytes(client_nonce)
        self.refresh_capabilities()
        budget = profile_budget(profile)
        session.max_frame_bytes = budget.max_frame_bytes
        session.max_text_chars = budget.max_text_chars
        session.max_text_bytes = budget.max_text_bytes
        session.heartbeat_seconds = budget.heartbeat_seconds
        session.queue_depth = budget.queue_depth
        session.max_chunk_bytes = budget.max_chunk_bytes
        session.writer_lease_seconds = 300
        session.max_audio_bytes = 1024 * 1024
        welcome = Frame(
            message_type=TYPE_WELCOME,
            flags=FLAG_RESPONSE,
            header={
                TAG_VERSION_MINOR: u16(0),
                TAG_SESSION_ID: session.session_id,
                TAG_SERVER_NONCE: session.server_nonce,
                TAG_AUTH_MODE: u8(self.auth_mode),
                TAG_MAX_FRAME_BYTES: u32(budget.max_frame_bytes),
                TAG_MAX_TEXT_CHARS: u32(budget.max_text_chars),
                TAG_MAX_TEXT_BYTES: u32(budget.max_text_bytes),
                TAG_MAX_AUDIO_BYTES: u32(1024 * 1024),
                TAG_QUEUE_DEPTH: u16(budget.queue_depth),
                TAG_HEARTBEAT_SECONDS: u16(budget.heartbeat_seconds),
                TAG_MAX_CHUNK_BYTES: u16(budget.max_chunk_bytes),
                TAG_WRITER_LEASE_SECONDS: u32(300),
                TAG_PROFILE: u8(profile),
                TAG_ENABLED_CAPS: u64(session.enabled),
                TAG_EFFECTIVE_CAPS: u64(session.effective),
                TAG_TRANSPORT_ALLOWED: u64(session.transport_allowed),
                TAG_POLICY_VERSION: u32(self.policy_version),
                TAG_REQUEST_ID: u32(request_id),
                TAG_SERVER_TIME_MS: i64(int(time.time() * 1000)),
            },
        )
        return self._emit(welcome)

    def _handle_auth(self, frame):
        session = self.session
        try:
            session_id = require_tlv(frame.header, TAG_SESSION_ID)
            proof = require_tlv(frame.header, TAG_AUTH_PROOF)
            device_id = session.device_id or decode_utf8(require_tlv(frame.header, TAG_DEVICE_ID))
            auth_mode = decode_u8(require_tlv(frame.header, TAG_AUTH_MODE))
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        if len(session_id) != 16 or len(proof) != 32:
            return self._error(request_id, "INVALID_PARAM")
        if session.authenticated:
            return self._error(request_id, "INVALID_STATE")
        if auth_mode not in {0, 1} or auth_mode != self.auth_mode:
            return self._error(request_id, "UNSUPPORTED_FEATURE")
        if not self.authenticate_with_mode(
            auth_mode,
            session_id,
            proof,
            device_id,
        ):
            return self._error(request_id, "UNAUTHORIZED")
        references = self.references
        device_ref = (
            int(references.ref_for("device", device_id))
            if references is not None
            else 0
        )
        return self._emit(
            Frame(
                message_type=TYPE_AUTH_OK,
                flags=FLAG_RESPONSE,
                header={
                    TAG_DEVICE_ID: utf8(device_id),
                    TAG_EFFECTIVE_CAPS: u64(session.effective),
                    TAG_POLICY_VERSION: u32(self.policy_version),
                    TAG_DEVICE_REF: u32(device_ref),
                    TAG_REQUEST_ID: u32(request_id),
                },
            )
        )

    def _capabilities(self):
        session = self.session
        if session is None:
            return set()
        return effective_capabilities(
            session.supported,
            session.enabled,
            session.transport_allowed,
        )

    def _handle_subscribe(self, frame):
        try:
            topic_id = decode_u32(require_tlv(frame.header, 0x0030))
            scope = decode_u8(require_tlv(frame.header, 0x0031))
            qos = decode_u8(require_tlv(frame.header, 0x0032))
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
            cursor = decode_u64(frame.header.get(0x0033, u64(0)))
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        try:
            result = self.subscriptions.subscribe(
                self.session.device_id,
                topic_id,
                scope,
                qos,
                cursor,
            )
        except (SubscriptionLimitError, ValueError):
            return self._error(request_id, "BUSY")
        if not result.recoverable:
            self._emit(
                Frame(
                    message_type=TYPE_RESYNC,
                    flags=0x0400,
                    topic_id=int(topic_id),
                    seq=self._next_seq(),
                    header={
                        TAG_RESYNC_REASON: u8(1),
                        TAG_REQUEST_ID: u32(request_id),
                        TAG_TOPIC_ID: u32(topic_id),
                        TAG_SCOPE: u8(scope),
                    },
                )
            )
        for event in result.replay:
            self._emit(self._delta_frame(event))
        return self._result(request_id, 0, {
            TAG_TOPIC_ID: u32(topic_id),
            TAG_SCOPE: u8(scope),
            TAG_CURSOR: u64(result.cursor),
        })

    def _handle_unsubscribe(self, frame):
        try:
            topic_id = decode_u32(require_tlv(frame.header, 0x0030))
            scope = decode_u8(require_tlv(frame.header, 0x0031))
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        self.subscriptions.unsubscribe(self.session.device_id, topic_id, scope)
        return self._result(request_id, 0, {0x0030: u32(topic_id), 0x0031: u8(scope)})

    def _handle_ack(self, frame):
        try:
            if 0x0033 not in frame.header:
                return None
            topic_id = decode_u32(require_tlv(frame.header, 0x0030))
            scope = decode_u8(require_tlv(frame.header, 0x0031))
            cursor = decode_u64(require_tlv(frame.header, 0x0033))
        except CBDPError as error:
            return self._error(0, error.name)
        self.subscriptions.ack(self.session.device_id, topic_id, scope, cursor)
        return None

    def _decode_params(self, fields):
        result = {}
        for name, tag in PARAM_TAGS.items():
            if tag not in fields:
                continue
            if name in result:
                continue
            raw = fields[tag]
            if name in {"expected_revision", "cursor", "limit", "revision", "message_ref", "queue_item_ref"}:
                try:
                    result[name] = decode_u64(raw) if len(raw) == 8 else decode_u32(raw)
                except CBDPError:
                    continue
            elif name == "decision":
                try:
                    result[name] = decode_u8(raw)
                except CBDPError:
                    continue
            else:
                try:
                    result[name] = decode_utf8(raw)
                except CBDPError:
                    continue
        return result

    def _handle_command(self, frame):
        try:
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
            method_id = decode_u16(require_tlv(frame.header, TAG_METHOD_ID))
            params_raw = require_tlv(frame.header, 0x0060)
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        method = METHOD_IDS.get(method_id)
        if method is None:
            return self._error(request_id, "UNSUPPORTED_FEATURE")
        required_capability = METHOD_CAPABILITIES.get(method_id)
        if required_capability and required_capability not in self._capabilities():
            return self._error(request_id, "CAPABILITY_NOT_ENABLED")
        commands = self._commands()
        if commands is None:
            return self._error(request_id, "UPSTREAM_UNAVAILABLE")
        try:
            params = self._decode_params(decode_tlvs(params_raw))
        except CBDPError as error:
            return self._error(request_id, error.name)
        device_id = str(self.session.device_id or "")
        request = {
            "type": "command",
            "request_id": f"cbdp:{device_id}:{request_id}",
            "method": DISPATCH_METHODS.get(method, method),
            "params": self._normalize_params(method, params),
        }
        capabilities = self._capabilities()
        generation = int(self.session.generation)

        def run():
            try:
                response = commands.execute(device_id, capabilities, request)
            except Exception:
                self._error(request_id, "INTERNAL")
                return
            if not isinstance(response, dict) or not response.get("ok"):
                code = (
                    str(response.get("error") or "INVALID_PARAM")
                    if isinstance(response, dict)
                    else "INTERNAL"
                )
                mapping = {
                    "capability_not_enabled": "CAPABILITY_NOT_ENABLED",
                    "request_id_conflict": "IDEMPOTENCY_CONFLICT",
                    "unsupported_method": "UNSUPPORTED_FEATURE",
                    "outcome_unknown": "INTERNAL",
                }
                self._error(request_id, mapping.get(code, "INVALID_PARAM"), code)
                return
            self._result(
                request_id,
                method_id,
                self._result_fields(response.get("result")),
            )

        if self._maybe_defer(generation, run):
            return None
        run()
        return None

    def _normalize_params(self, method, params):
        params = dict(params)
        if method in {"turn.start", "turn.steer", "queue.add", "queue.update"}:
            body = params.pop("body", params.pop("text", None))
            if body is not None:
                params["text"] = body
        if method in {"queue.add", "queue.update"}:
            queue_item_ref = params.pop("queue_item_ref", None)
            if queue_item_ref is not None and "queue_id" not in params:
                params["queue_id"] = str(queue_item_ref)
        if method in {"message.read.update"} and "thread_id" not in params:
            params["thread_id"] = ""
        return params

    def _result_fields(self, result):
        if not isinstance(result, dict):
            return {}
        fields = {}
        thread_id = result.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            fields[0x0022] = utf8(thread_id)
        turn_id = result.get("turn_id") or result.get("id")
        if isinstance(turn_id, str) and turn_id:
            fields[0x0024] = utf8(turn_id)
        if result.get("ok") is True:
            fields[0x0064] = u8(1)
        return fields

    def _result(self, request_id, method_id, fields):
        header = {
            TAG_REQUEST_ID: u32(request_id),
            TAG_METHOD_ID: u16(method_id),
            TAG_STATE: u8(1),
        }
        header.update(fields)
        return self._emit(
            Frame(
                message_type=TYPE_RESULT,
                flags=FLAG_RESPONSE,
                header=header,
            )
        )

    def _handle_send_begin(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
            request_id = tx_id
        except CBDPError as error:
            return self._error(0, error.name)
        if tx_id in self._transactions:
            return self._error(request_id, "IDEMPOTENCY_CONFLICT")
        self._transactions[tx_id] = {"parts": {}, "bytes": 0, "kind": "text"}
        return None

    def _handle_send_chunk(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
            offset = decode_u32(require_tlv(frame.header, 0x0048))
        except CBDPError as error:
            return self._error(0, error.name)
        transaction = self._transactions.get(tx_id)
        if transaction is None or len(frame.payload) > 1_048_576:
            return self._error(tx_id, "INVALID_STATE")
        if offset == transaction["bytes"]:
            transaction["parts"][offset] = bytes(frame.payload)
            transaction["bytes"] += len(frame.payload)
        elif offset not in transaction["parts"]:
            return self._error(tx_id, "INVALID_PARAM")
        return None

    def _handle_send_end(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
        except CBDPError as error:
            return self._error(0, error.name)
        transaction = self._transactions.pop(tx_id, None)
        if transaction is None or transaction.get("kind") != "text":
            return self._error(tx_id, "INVALID_STATE")
        data = b"".join(transaction["parts"][key] for key in sorted(transaction["parts"]))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(tx_id, "INVALID_PARAM")
        commands = self._commands()
        if commands is None:
            return self._error(tx_id, "UPSTREAM_UNAVAILABLE")
        device_id = str(self.session.device_id or "")
        thread_id = ""
        if self.active_thread is not None:
            try:
                thread_id = str(self.active_thread(device_id) or "")
            except Exception:
                thread_id = ""
        if not thread_id:
            # The device must not be left showing "sending to the computer"
            # forever: a text reply with no selected conversation is an
            # explicit protocol error, not a silent drop.
            return self._error(tx_id, "INVALID_STATE", "NO_ACTIVE_THREAD")
        capabilities = self._capabilities() | {"control"}
        generation = int(self.session.generation)

        def run():
            try:
                response = commands.execute(
                    device_id,
                    capabilities,
                    {
                        "type": "command",
                        "request_id": f"cbdp-tx:{device_id}:{tx_id}",
                        "method": "turn.start",
                        "params": {"thread_id": thread_id, "text": text},
                    },
                )
            except Exception:
                self._error(tx_id, "INTERNAL")
                return
            if not isinstance(response, dict) or not response.get("ok"):
                self._error(tx_id, "INVALID_STATE")
                return
            self._result(tx_id, 0x0005, self._result_fields(response.get("result")))

        if self._maybe_defer(generation, run):
            return None
        run()
        return None

    def _handle_audio_begin(self, frame):
        if "audio" not in self._capabilities():
            return self._error(self._request_id(frame), "CAPABILITY_NOT_ENABLED")
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
            request_id = tx_id
            sample_rate = decode_u32(frame.header.get(TAG_SAMPLE_RATE, u32(16000)))
            channels = decode_u8(frame.header.get(TAG_CHANNELS, u8(1)))
            bits = decode_u8(frame.header.get(TAG_BITS_PER_SAMPLE, u8(16)))
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        if tx_id in self._transactions:
            return self._error(request_id, "IDEMPOTENCY_CONFLICT")
        self._transactions[tx_id] = {
            "kind": "audio",
            "parts": {},
            "bytes": 0,
            "sample_rate": int(sample_rate),
            "channels": int(channels),
            "bits": int(bits),
        }
        return None

    def _handle_audio_chunk(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
            offset = decode_u32(require_tlv(frame.header, TAG_OFFSET))
        except CBDPError as error:
            return self._error(0, error.name)
        transaction = self._transactions.get(tx_id)
        limit = int(self.session.max_audio_bytes or 1048576)
        if transaction is None or transaction.get("kind") != "audio":
            return self._error(tx_id, "INVALID_STATE")
        if len(frame.payload) > limit or transaction["bytes"] + len(frame.payload) > limit:
            self._transactions.pop(tx_id, None)
            return self._error(tx_id, "PAYLOAD_TOO_LARGE")
        if offset == transaction["bytes"]:
            transaction["parts"][offset] = bytes(frame.payload)
            transaction["bytes"] += len(frame.payload)
        elif offset not in transaction["parts"]:
            return self._error(tx_id, "INVALID_PARAM")
        return None

    def _handle_audio_end(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
        except CBDPError as error:
            return self._error(0, error.name)
        transaction = self._transactions.pop(tx_id, None)
        if transaction is None or transaction.get("kind") != "audio":
            return self._error(tx_id, "INVALID_STATE")
        try:
            data = b"".join(
                transaction["parts"][key] for key in sorted(transaction["parts"])
            )
        except KeyError:
            return self._error(tx_id, "FRAGMENT_ERROR")
        generation = int(self.session.generation)

        def run():
            transcript = self._dispatch_audio_transcript(transaction, data)
            if transcript is None:
                self._error(tx_id, "UPSTREAM_UNAVAILABLE")
                return
            commands = self._commands()
            if commands is None:
                self._error(tx_id, "UPSTREAM_UNAVAILABLE")
                return
            device_id = str(self.session.device_id or "")
            thread_id = ""
            if self.active_thread is not None:
                try:
                    thread_id = str(self.active_thread(device_id) or "")
                except Exception:
                    thread_id = ""
            if not thread_id:
                self._error(tx_id, "INVALID_STATE", "NO_ACTIVE_THREAD")
                return
            try:
                response = commands.execute(
                    device_id,
                    self._capabilities() | {"control"},
                    {
                        "type": "command",
                        "request_id": f"cbdp-audio:{device_id}:{tx_id}",
                        "method": "turn.start",
                        "params": {"thread_id": thread_id, "text": transcript},
                    },
                )
            except Exception:
                self._error(tx_id, "INTERNAL")
                return
            if not isinstance(response, dict) or not response.get("ok"):
                self._error(tx_id, "INVALID_STATE")
                return
            self._result(
                tx_id,
                0x0013,
                {
                    TAG_TRANSCRIPT: utf8(transcript),
                    **self._result_fields(response.get("result")),
                },
            )

        if self._maybe_defer(generation, run):
            return None
        run()
        return None

    def _dispatch_audio_transcript(self, transaction, data):
        """Run STT for an AUDIO_* transaction, then submit the text.

        Returns the transcribed text, or None when the recognizer cannot be
        used. The caller decides how to report the error on the wire.
        """

        recognizer = self.speech
        if recognizer is None or not hasattr(recognizer, "transcribe_pcm"):
            return None
        try:
            text = recognizer.transcribe_pcm(
                self.session.device_id,
                data,
                sample_rate=int(transaction.get("sample_rate") or 16000),
                channels=int(transaction.get("channels") or 1),
                bits=int(transaction.get("bits") or 16),
            )
        except Exception:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        return text.strip()
