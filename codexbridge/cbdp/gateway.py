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
    TAG_PROJECT_ID,
    TAG_PROJECT_REF,
    TAG_PROFILE,
    TAG_QUEUE_DEPTH,
    TAG_REQUEST_ID,
    TAG_IDEM,
    TAG_THREAD_ID,
    TAG_THREAD_REF,
    TAG_MESSAGE_ID,
    TAG_MESSAGE_REF,
    TAG_KIND,
    TAG_TITLE,
    TAG_SUMMARY,
    TAG_STATUS,
    TAG_REVISION,
    TAG_UPDATED_AT_MS,
    TAG_THREAD_STATE,
    TAG_OWNER_STATE,
    TAG_MODEL_NAME,
    TAG_CREATED_AT_MS,
    TAG_OBJECT,
    TAG_OBJECTS,
    TAG_OBJECT_COUNT,
    TAG_OBJECT_TYPE,
    TAG_NEXT_CURSOR,
    TAG_CWD,
    TAG_PROMPT,
    TAG_EXPIRES_AT_MS,
    TAG_APPROVAL_KIND,
    TAG_APPROVAL_DETAIL,
    TAG_APPROVAL_ID,
    TAG_APPROVAL_REF,
    TAG_DECISION,
    TAG_OPTIONS,
    TAG_ONLINE,
    TAG_MODEL_CAPABILITIES,
    TAG_MODEL_CAPABILITY_KIND,
    TAG_MODEL_CAPABILITY_VALUE,
    TAG_ACTIVE_THREAD_REF,
    TAG_ACTIVE_TURN_REF,
    TAG_ACTIVE_DEVICE_REF,
    TAG_ACTIVE_REVISION,
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
    TAG_ERROR_DETAIL,
    TAG_ERROR_MESSAGE,
    TAG_REASON,
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
    TAG_USE_ACTIVE_THREAD,
    TAG_SHA256,
    TAG_BODY,
    TAG_TURN_REF,
    TAG_VERSION_MINOR,
    TAG_WRITER_LEASE_SECONDS,
    TYPE_ACK,
    TYPE_AUDIO_BEGIN,
    TYPE_AUDIO_CHUNK,
    TYPE_AUDIO_END,
    TYPE_APPROVAL_RESOLVE,
    TYPE_AUTH,
    TYPE_AUTH_OK,
    TYPE_CLOSE,
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
from codexbridge.protocol.commands import CommandError
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
    encode_array,
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
    0x001A: "thread.settings.read",
    0x001B: "thread.settings.update",
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
    0x001A: "thread_settings",
    0x001B: "thread_settings",
}
DISPATCH_METHODS = {
    "queue.add": "queue.enqueue",
    "queue.update": "queue.edit",
}


def _thread_state(status: str) -> int:
    return {
        "idle": 0,
        "working": 1,
        "running": 1,
        "waiting_user": 1,
        "waiting": 1,
        "queued": 1,
        "starting": 1,
        "completed": 2,
        "failed": 3,
        "interrupted": 4,
        "archived": 5,
    }.get(str(status or ""), 6)


def _message_kind(kind: str) -> int:
    return {
        "user": 0,
        "user_text": 0,
        "agent": 1,
        "assistant": 1,
        "assistant_text": 1,
        "commentary": 2,
        "reasoning": 2,
        "command": 3,
        "tool": 3,
        "fileChange": 4,
        "file_change": 4,
        "plan": 4,
        "error": 5,
        "approval": 6,
        "notice": 7,
        "system": 7,
    }.get(str(kind or ""), 7)


def _message_status(status: str) -> int:
    return {
        "pending": 0,
        "working": 1,
        "streaming": 1,
        "waiting_user": 1,
        "waiting": 1,
        "queued": 1,
        "starting": 1,
        "completed": 2,
        "done": 2,
        "failed": 3,
        "interrupted": 4,
    }.get(str(status or ""), 0)


def _decision_name(value: int) -> str:
    """Map the wire decision number to the shared command's decision word."""

    return {
        0: "accept",
        1: "acceptForSession",
        2: "decline",
        3: "cancel",
    }.get(int(value), "")


# Per-field byte budgets for object payloads. A single oversized upstream field
# must never make the whole RESULT unencodable: a TLV value is u16-length
# bounded and the entire frame has to fit the negotiated budget, so long text
# is truncated on a UTF-8 boundary instead of failing the command.
OBJECT_TEXT_LIMIT = 4096
# Head room for the RESULT envelope (request_id, method_id, state, the objects
# TLV wrapper, object_count and next_cursor).
OBJECT_ENVELOPE_BYTES = 128
OBJECT_OBJECTS_BUDGET = 48 * 1024


def _wire_text(value, limit: int = OBJECT_TEXT_LIMIT) -> str:
    text = str(value or "")
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore")


def _pack_objects(objects, budget: int = OBJECT_OBJECTS_BUDGET):
    """Keep only the objects that fit the fixed wire budget.

    Returns ``(packed, dropped)``. Dropping trailing rows is preferable to
    sending a RESULT the peer cannot decode, and ``object_count`` plus
    ``next_cursor`` stay consistent with what was actually sent.
    """

    packed = []
    used = 2
    for encoded in objects:
        size = 4 + len(encoded)
        if used + size > budget:
            break
        packed.append(encoded)
        used += size
    return packed, len(objects) - len(packed)


PARAM_TAGS = {
    "thread_id": 0x0022,
    "thread_ref": 0x0021,
    "turn_id": 0x0024,
    "message_id": 0x0026,
    "message_ref": 0x0025,
    "approval_id": 0x0028,
    "approval_ref": 0x0027,
    "approval_kind": 0x0087,
    "text": 0x0042,
    "body": 0x0042,
    "title": 0x0040,
    "decision": 0x0082,
    "response": 0x0083,
    "queue_id": 0x0100,
    "queue_item_ref": 0x0100,
    "expected_revision": 0x006B,
    "effort": 0x0124,
    "model": 0x0051,
    "history_depth": 0x0014,
    "cursor": 0x0033,
    "revision": 0x0036,
    "read_at_ms": 0x0054,
    "active_thread_ref": 0x0170,
    "active_turn_ref": 0x0171,
    "active_revision": 0x0173,
    "guidance_text": 0x0109,
    "effort": 0x0124,
    "approval_policy": 0x0125,
    "approvals_reviewer": 0x0126,
    "sandbox_policy": 0x0127,
    "personality": 0x0128,
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
        active_state=None,
        set_active_state=None,
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
        # Optional ``device_id -> {thread_id, turn_id, revision}`` reader and
        # writer for active_state.read / active_state.set. Leaving them unset
        # keeps the shared hub as the only source of truth in tests.
        self.active_state = active_state
        self.set_active_state = set_active_state
        self._active_revision = 0
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
        # Highest device_seq accepted without a gap. Responses piggyback it in
        # the fixed header so the peer can clear its retransmit window.
        self._incoming_seq = 0
        self.last_peer_error = None
        self.session_closed = False
        self._transactions: dict[bytes, dict] = {}
        # CBDP carries paging cursors as opaque u64. The upstream app-server
        # returns an opaque string, so keep a reversible per-session table
        # instead of hashing it (a hash would break the next page request).
        self._cursor_map: dict[int, str] = {}
        self._cursor_reverse: dict[str, int] = {}
        self._cursor_seq = 0
        self._ref_external: dict[tuple[str, int], str] = {}
        self._lock = threading.RLock()

    def bind_transport(self, send):
        self._send = send

    def begin_session(self):
        with self._lock:
            self._generation += 1
            self._outgoing_seq = 0
            self._cursor_map.clear()
            self._cursor_reverse.clear()
            self._cursor_seq = 0
            # A new connection cannot resume the previous connection's
            # SEND/AUDIO transactions; leaving them would make the next
            # AUDIO_BEGIN with the same tx_id fail as a conflict forever.
            self._transactions.clear()
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

    def _track_incoming(self, frame) -> None:
        """Advance the cumulative device_seq for frames that occupy one.

        PING/PONG/ACK/CLOSE and discovery frames carry ``seq = 0`` by 13.1 and
        must not move the cumulative acknowledgement.
        """

        seq = int(getattr(frame, "seq", 0) or 0)
        if seq <= 0:
            return
        if seq > self._incoming_seq:
            self._incoming_seq = seq

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

    def delta_frame(self, event):
        """Return the wire DELTA for one committed event.

        Public wrapper used by sessions that must emit a committed event
        outside the subscription replay path (for example the legacy
        compatibility fan-out that predates subscriptions). It advances the
        per-connection transport sequence exactly like ``deliver_event``.
        """

        return self._delta_frame(event)

    def has_subscription(self, topic_id, scope):
        """Whether this gateway's own device subscribed to the topic/scope."""

        session = self.session
        if session is None or not session.device_id:
            return False
        return self.subscriptions.has_subscription_for(
            session.device_id,
            int(topic_id),
            int(scope),
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
        if not self.has_subscription(int(event.topic_id), scope):
            return None
        frame = self._delta_frame(event)
        # Fragmentation is a transport concern: the session splits any logical
        # frame that overflows the negotiated budget, so an oversized critical
        # event is delivered in pieces instead of being dropped here.
        return self._emit(frame)

    def _error(self, request_id=0, code="INVALID_PARAM", message="", detail=None):
        text = _wire_text(message or code, 512)
        header = {
            TAG_REQUEST_ID: u32(int(request_id or 0)),
            TAG_ERROR_MESSAGE: utf8(text),
        }
        if isinstance(detail, dict) and detail:
            fields = {}
            for key, value in detail.items():
                if key == "reason":
                    fields[TAG_REASON] = utf8(_wire_text(value, 128))
                elif key == "http_status":
                    fields[0x01B3] = u16(int(value))
                elif key == "retry_after_ms":
                    fields[0x0067] = u32(int(value))
            if fields:
                header[TAG_ERROR_DETAIL] = encode_tlvs(fields.items())
        frame = Frame(
            message_type=TYPE_ERROR,
            flags=FLAG_RESPONSE,
            seq=self._next_seq(),
            ack_seq=self._incoming_seq,
            code=ERROR_CODES.get(code, ERROR_CODES["INVALID_PARAM"]),
            header=header,
        )
        return self._emit(frame)

    def handle_frame(self, frame):
        if not isinstance(frame, Frame):
            raise TypeError("frame must be a Frame")
        if frame.message_type == TYPE_HELLO:
            # A new connection restarts both directions at seq 1.
            self._incoming_seq = 0
        self._track_incoming(frame)
        try:
            return self._dispatch_frame(frame)
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        except Exception:
            # One malformed or unexpected frame must never kill the socket
            # reader: the device would see a connection reset with no reason.
            return self._error(self._request_id(frame), "INTERNAL")

    def _dispatch_frame(self, frame):
        session = self.session
        if frame.message_type == TYPE_HELLO:
            return self._handle_hello(frame)
        if frame.message_type == TYPE_PING:
            return self._handle_ping(frame)
        if frame.message_type == TYPE_PONG:
            return None
        if frame.message_type == TYPE_ERROR:
            return self._handle_peer_error(frame)
        if frame.message_type == TYPE_CLOSE:
            return self._handle_peer_close(frame)
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
        if frame.message_type == TYPE_APPROVAL_RESOLVE:
            return self._handle_approval_resolve(frame)
        return self._error(self._request_id(frame), "UNSUPPORTED_FEATURE")

    def _handle_peer_error(self, frame):
        """Accept a peer-reported ERROR without echoing another error.

        Protocol 21.3 keeps the connection open for frame/encoding errors.
        Answering the peer's ERROR with UNSUPPORTED_FEATURE would turn normal
        error reporting into a protocol loop and makes the device tear the
        session down. Record it for diagnostics and stay silent.
        """

        self.last_peer_error = (int(frame.code), self._request_id(frame))
        return None

    def _handle_peer_close(self, frame):
        """Handle a peer CLOSE per protocol 23 without replying."""

        self.session_closed = True
        return None

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
            seq=self._next_seq(),
            ack_seq=self._incoming_seq,
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
                seq=self._next_seq(),
                ack_seq=self._incoming_seq,
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
        generation = int(self.session.generation)

        def replay():
            # A long thread history can be hundreds of DELTA frames. Emitting
            # them from the socket reader blocked it until the peer drained
            # its receive window, so an in-flight AUDIO_* upload hit EAGAIN
            # and the device eventually reset the connection. Replay runs on
            # the per-device worker, which keeps the reader draining.
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
            self._result(request_id, 0, {
                TAG_TOPIC_ID: u32(topic_id),
                TAG_SCOPE: u8(scope),
                TAG_CURSOR: u64(result.cursor),
            })

        if self._maybe_defer(generation, replay):
            return None
        replay()
        return None

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
            try:
                if name in {"expected_revision", "cursor", "revision", "message_ref", "queue_item_ref", "active_revision"}:
                    result[name] = decode_u64(raw) if len(raw) == 8 else decode_u32(raw)
                elif name == "history_depth":
                    result[name] = decode_u16(raw)
                elif name in {"thread_ref", "active_thread_ref", "active_turn_ref", "approval_ref"}:
                    result[name] = decode_u32(raw)
                elif name == "read_at_ms":
                    result[name] = decode_i64(raw)
                elif name == "decision":
                    result[name] = decode_u8(raw)
                elif name in {
                    "approval_policy",
                    "approvals_reviewer",
                    "sandbox_policy",
                }:
                    result[name] = decode_u8(raw)
                elif name == "response":
                    # response is a nested TLV; keep it opaque for the caller.
                    result[name] = raw
                else:
                    result[name] = decode_utf8(raw)
            except CBDPError:
                # A malformed known field must be reported as INVALID_PARAM by
                # the command schema, never silently dropped and replaced by a
                # default that changes the request.
                result[name] = ""
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
        params = self._restore_cursor(method, params)
        if method == "active_state.read":
            return self._active_state_result(request_id, device_id)
        if method == "active_state.set":
            return self._handle_active_state_set(request_id, device_id, params)
        try:
            normalized = self._normalize_params(method, params)
        except CBDPError as error:
            return self._error(request_id, error.name)
        except (TypeError, ValueError):
            return self._error(request_id, "INVALID_PARAM")
        request = {
            "type": "command",
            "request_id": f"cbdp:{device_id}:{request_id}",
            "method": DISPATCH_METHODS.get(method, method),
            "params": normalized,
            # Protocol 17.4 keys idempotency on (device_id, idem); request_id
            # is only unique inside one connection.
            "idem": bytes(frame.header.get(TAG_IDEM, b"")),
        }
        capabilities = self._capabilities()
        generation = int(self.session.generation)

        def run():
            try:
                response = commands.execute(device_id, capabilities, request)
            except CommandError as error:
                code = str(error)
                mapping = {
                    "capability_not_enabled": "CAPABILITY_NOT_ENABLED",
                    "invalid_request": "INVALID_PARAM",
                    "invalid_request_id": "INVALID_PARAM",
                    "invalid_params": "INVALID_PARAM",
                    "request_id_conflict": "IDEMPOTENCY_CONFLICT",
                    "unsupported_method": "UNSUPPORTED_FEATURE",
                    "outcome_unknown": "INTERNAL",
                }
                self._error(request_id, mapping.get(code, "INVALID_PARAM"), code)
                return
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
                self._result_fields(response.get("result"), method),
            )

        if self._maybe_defer(generation, run):
            return None
        run()
        return None

    def _normalize_params(self, method, params):
        params = dict(params)
        if method == "thread.settings.update":
            policy_names = {
                "approval_policy": {
                    0: "untrusted",
                    1: "on-failure",
                    2: "on-request",
                    3: "never",
                    4: "granular",
                },
                "approvals_reviewer": {
                    0: "user",
                    1: "auto",
                    2: "guardian",
                },
                "sandbox_policy": {
                    0: "read-only",
                    1: "workspace-write",
                    2: "danger-full-access",
                    3: "custom",
                },
            }
            for field, names in policy_names.items():
                value = params.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    name = names.get(int(value))
                    if name is None:
                        raise CBDPError("INVALID_PARAM")
                    params[field] = name
        if method in {"turn.start", "turn.steer", "queue.add", "queue.update"}:
            body = params.pop("body", params.pop("text", None))
            if body is not None:
                params["text"] = body
        if method in {"queue.add", "queue.update"}:
            queue_item_ref = params.pop("queue_item_ref", None)
            if queue_item_ref is not None and "queue_id" not in params:
                params["queue_id"] = str(queue_item_ref)
        # Protocol selectors accept a compact ref *or* the string id; the
        # upstream command layer only understands the string id. Resolve the
        # compact form first, then validate mutual selectors against the real
        # value instead of manufacturing an empty thread_id that hides a bad
        # request.
        if "thread_ref" in params:
            ref = int(params.pop("thread_ref"))
            resolved = self._thread_from_ref(ref)
            if not resolved:
                raise CBDPError("NOT_FOUND")
            if "thread_id" in params and params["thread_id"] != resolved:
                raise ValueError("thread selectors disagree")
            params["thread_id"] = resolved
        if "message_ref" in params:
            ref = int(params.pop("message_ref"))
            resolved = self._message_from_ref(ref)
            if not resolved:
                raise CBDPError("NOT_FOUND")
            if "message_id" in params and params["message_id"] != resolved:
                raise ValueError("message selectors disagree")
            params["message_id"] = resolved
        if method == "message.read.update":
            if "thread_id" not in params:
                thread_id = self._thread_for_message(params.get("message_id"))
                if thread_id:
                    params["thread_id"] = thread_id
            read_at_ms = params.get("read_at_ms")
            if not isinstance(read_at_ms, int) or isinstance(read_at_ms, bool) or read_at_ms < 0:
                raise ValueError("read_at_ms")
            params["read_at_ms"] = int(read_at_ms)
        if method == "thread.read":
            history_depth = params.pop("history_depth", None)
            if "message_limit" not in params:
                # Omitting the field uses the profile history budget. Explicit
                # zero means metadata only, while a positive value caps the
                # returned message list.
                if history_depth is None:
                    history_depth = profile_budget(int(self.session.profile or 0)).history_depth
                params["message_limit"] = max(0, int(history_depth))
        return params

    def _active_snapshot(self, device_id: str) -> dict:
        """Current active thread/turn for one device.

        ``active_state`` is optional so tests and embedded users can keep the
        existing ``active_thread`` hook as the single source of truth.
        """

        snapshot: dict = {}
        if self.active_state is not None:
            try:
                value = self.active_state(device_id)
            except Exception:
                value = None
            if isinstance(value, dict):
                snapshot = dict(value)
        if not snapshot.get("thread_id") and self.active_thread is not None:
            try:
                snapshot["thread_id"] = str(self.active_thread(device_id) or "")
            except Exception:
                snapshot["thread_id"] = ""
        snapshot.setdefault("turn_id", "")
        snapshot.setdefault("revision", self._active_revision)
        snapshot.setdefault("device_id", device_id)
        return snapshot

    def _active_state_fields(self, snapshot: dict) -> dict:
        thread_ref = self._ref("thread", str(snapshot.get("thread_id") or ""))
        turn_ref = self._ref("turn", str(snapshot.get("turn_id") or ""))
        device_ref = self._ref(
            "device", str(snapshot.get("device_id") or snapshot.get("thread_id") or "")
        )
        return {
            TAG_ACTIVE_THREAD_REF: u32(thread_ref),
            TAG_ACTIVE_TURN_REF: u32(turn_ref),
            TAG_ACTIVE_DEVICE_REF: u32(device_ref),
            TAG_ACTIVE_REVISION: u64(int(snapshot.get("revision") or 0)),
        }

    def _active_state_result(self, request_id, device_id):
        return self._result(
            request_id,
            0x00B1,
            self._active_state_fields(self._active_snapshot(device_id)),
        )

    def _handle_active_state_set(self, request_id, device_id, params):
        thread_id = str(params.get("thread_id") or "")
        if not thread_id:
            ref = params.get("active_thread_ref")
            if ref:
                thread_id = self._thread_from_ref(int(ref))
                if not thread_id:
                    return self._error(request_id, "NOT_FOUND", "thread_ref")
        if not thread_id:
            return self._error(request_id, "INVALID_PARAM", "thread_required")
        if self.set_active_state is None:
            return self._error(request_id, "UNSUPPORTED_FEATURE", "active_state_unavailable")
        turn_id = str(params.get("turn_id") or "")
        if not turn_id:
            ref = params.get("active_turn_ref")
            if ref:
                turn_id = self._turn_from_ref(int(ref))
                if not turn_id:
                    return self._error(request_id, "NOT_FOUND", "turn_ref")
        try:
            snapshot = self.set_active_state(device_id, thread_id, turn_id)
        except Exception:
            return self._error(request_id, "INTERNAL")
        self._active_revision += 1
        payload = dict(snapshot) if isinstance(snapshot, dict) else {}
        payload.setdefault("thread_id", thread_id)
        payload.setdefault("turn_id", turn_id)
        payload.setdefault("device_id", device_id)
        payload.setdefault("revision", self._active_revision)
        return self._result(request_id, 0x00B2, self._active_state_fields(payload))

    def _restore_cursor(self, method, params):
        """Translate a CBDP u64 *command* cursor back to the upstream cursor."""

        if "cursor" not in params or method not in {
            "threads.list", "thread.read", "approvals.list",
            "projects.list", "thread.search", "events.since",
        }:
            return params
        restored = self._decode_cursor(params.get("cursor"))
        if restored is None:
            params.pop("cursor", None)
        else:
            params["cursor"] = restored
        return params

    def _thread_from_ref(self, ref: int) -> str:
        """Resolve a CBDP thread_ref through the persistent reference registry."""

        if not ref:
            return ""
        ref = int(ref)
        if self.references is not None:
            try:
                external = self.references.external_id("thread", ref)
            except Exception:
                external = ""
            if external:
                with self._lock:
                    self._ref_external[("thread", ref)] = str(external)
                return str(external)
        with self._lock:
            return self._ref_external.get(("thread", ref), "")

    def _turn_from_ref(self, ref: int) -> str:
        """Resolve a CBDP turn_ref through the persistent registry."""

        if not ref:
            return ""
        if self.references is not None:
            try:
                return str(self.references.external_id("turn", int(ref)) or "")
            except Exception:
                return ""
        return ""

    def _message_from_ref(self, ref: int) -> str:
        """Resolve a CBDP message_ref through the persistent registry."""

        if not ref:
            return ""
        if self.references is not None:
            try:
                return str(self.references.external_id("message", int(ref)) or "")
            except Exception:
                return ""
        return ""

    def _thread_for_message(self, message_id) -> str:
        """Best-effort thread lookup for a read marker without a thread_id."""

        value = str(message_id or "")
        if not value:
            return ""
        # The event spine supplies the thread when only the message payload
        # knows it. The lookup is bounded by EventSpine.latest_event.
        if self.event_spine is not None:
            event = self.event_spine.latest_event(
                lambda item: item.payload.get(TAG_MESSAGE_ID) == utf8(value)
            )
            if event is not None:
                try:
                    return decode_utf8(event.payload.get(TAG_THREAD_ID, b""))
                except CBDPError:
                    return ""
        return ""

    def _request_thread(self, frame) -> str:
        """Resolve an explicit thread selector carried on a SEND/AUDIO frame."""

        if TAG_THREAD_ID in frame.header:
            try:
                return decode_utf8(frame.header[TAG_THREAD_ID])
            except CBDPError:
                return ""
        if TAG_THREAD_REF in frame.header:
            try:
                ref = decode_u32(frame.header[TAG_THREAD_REF])
            except CBDPError:
                return ""
            return self._thread_from_ref(int(ref))
        return ""

    def _active_thread_id(self, device_id: str) -> str:
        if self.active_thread is None:
            return ""
        try:
            return str(self.active_thread(device_id) or "")
        except Exception:
            return ""

    def _transaction_thread(self, transaction) -> str:
        thread_id = str(transaction.get("thread_id") or "")
        if thread_id:
            return thread_id
        ref = int(transaction.get("thread_ref") or 0)
        if ref:
            return self._thread_from_ref(ref)
        return ""

    def _result_fields(self, result, method: str = ""):
        if not isinstance(result, dict):
            return {}
        fields = {}
        thread_id = result.get("thread_id")
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        if not thread_id and thread:
            thread_id = thread.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            fields[0x0022] = utf8(thread_id)
            thread_ref = self._ref("thread", thread_id)
            if thread_ref:
                fields[TAG_THREAD_REF] = u32(thread_ref)
        status = thread.get("status") if thread else result.get("status")
        if isinstance(status, str) and status:
            fields[TAG_STATUS] = u16(_thread_state(status))
        turn_id = result.get("turn_id") or result.get("id")
        if isinstance(turn_id, str) and turn_id:
            fields[0x0024] = utf8(turn_id)
        if result.get("ok") is True:
            fields[0x0064] = u8(1)
        # 17.1.1 requires status.read to carry `online`; the upstream snapshot
        # reports connection state instead, so project it onto the wire field.
        online = result.get("online")
        if online is None:
            connection = result.get("connection")
            if isinstance(connection, dict):
                transport = str(connection.get("transport") or "")
                online = transport not in {"", "unstarted", "offline"}
        if online is not None:
            fields[TAG_ONLINE] = u8(1 if online else 0)
        objects = self._result_objects(result, method)
        if objects is not None:
            packed, _dropped = _pack_objects(objects, self._objects_budget())
            fields[TAG_OBJECTS] = encode_array(TAG_OBJECT, packed)
            fields[TAG_OBJECT_COUNT] = u16(len(packed))
        cursor = self._encode_cursor(result.get("next_cursor"))
        if cursor is not None:
            fields[TAG_NEXT_CURSOR] = u64(cursor)
        return fields

    def _objects_budget(self) -> int:
        """Bytes available for the ``objects`` value inside one RESULT frame."""

        session = self.session
        limit = int(getattr(session, "max_frame_bytes", 0) or 0)
        if limit <= 0:
            return OBJECT_OBJECTS_BUDGET
        return max(2, min(OBJECT_OBJECTS_BUDGET, limit - OBJECT_ENVELOPE_BYTES))

    def _encode_cursor(self, value) -> int | None:
        """Map an opaque upstream cursor onto a stable per-session u64."""

        if value is None or value == "" or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = int(value)
            return number if number > 0 else None
        text = str(value)
        if not text:
            return None
        if text.isdigit():
            number = int(text)
            return number if number > 0 else None
        with self._lock:
            existing = self._cursor_reverse.get(text)
            if existing is not None:
                return existing
            self._cursor_seq += 1
            token = self._cursor_seq
            self._cursor_map[token] = text
            self._cursor_reverse[text] = token
            return token

    def _decode_cursor(self, value):
        """Turn a CBDP u64 cursor back into the upstream opaque cursor."""

        if value is None:
            return None
        with self._lock:
            return self._cursor_map.get(int(value))

    def _result_objects(self, result, method: str = ""):
        """Encode protocol 8.4 object arrays for the methods that require them.

        The method decides which objects belong to the result. Sniffing the
        response keys is not enough: a `thread.read` reply also carries
        `pending_approvals`, so a key-first search would return the approvals
        list and silently drop every message.
        """

        budget = self._objects_budget()
        text_limit = max(32, min(OBJECT_TEXT_LIMIT, budget // 4))
        objects: list[tuple[int, bytes]] = []

        def threads():
            rows = result.get("threads")
            if not isinstance(rows, list):
                return None
            out = []
            for row in rows:
                encoded = self._encode_thread_object(row, text_limit)
                if encoded is not None:
                    out.append(encoded)
            return out

        def models():
            rows = result.get("models")
            if not isinstance(rows, list):
                return None
            out = []
            for row in rows:
                encoded = self._encode_model_object(row, text_limit)
                if encoded is not None:
                    out.append(encoded)
            return out

        def projects():
            rows = result.get("projects")
            if not isinstance(rows, list):
                return None
            out = []
            for row in rows:
                encoded = self._encode_project_object(row, text_limit)
                if encoded is not None:
                    out.append(encoded)
            return out

        def approvals():
            rows = result.get("pending_approvals")
            if not isinstance(rows, list):
                return None
            out = []
            for row in rows:
                encoded = self._encode_approval_object(row, text_limit)
                if encoded is not None:
                    out.append(encoded)
            return out

        def thread_detail():
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            read_thread_id = str(
                thread.get("thread_id") or result.get("thread_id") or ""
            )
            rows = result.get("messages")
            if not isinstance(rows, list) or not read_thread_id:
                return None
            out = []
            thread_object = self._encode_thread_object(
                thread or {"thread_id": read_thread_id}, text_limit
            )
            if thread_object is not None:
                out.append(thread_object)
            for row in rows:
                encoded = self._encode_message_object(row, read_thread_id, text_limit)
                if encoded is not None:
                    out.append(encoded)
            for row in result.get("pending_approvals") or []:
                encoded = self._encode_approval_object(row, text_limit)
                if encoded is not None:
                    out.append(encoded)
            return out

        method_builders = {
            "threads.list": (threads,),
            "thread.search": (threads,),
            "models.list": (models,),
            "projects.list": (projects,),
            "approvals.list": (approvals,),
            "thread.read": (thread_detail,),
        }
        builders = method_builders.get(method)
        if builders is None:
            # Unknown/legacy caller: fall back to the historical key search.
            builders = (threads, thread_detail, models, projects, approvals)
        for builder in builders:
            encoded = builder()
            if encoded is not None:
                return encoded
        return None

    def _encode_thread_object(self, row, text_limit: int = OBJECT_TEXT_LIMIT):
        if not isinstance(row, dict):
            return None
        thread_id = str(row.get("thread_id") or row.get("id") or "")
        if not thread_id:
            return None
        fields = {
            TAG_OBJECT_TYPE: u8(0),
            TAG_THREAD_REF: u32(self._ref("thread", thread_id)),
            TAG_THREAD_ID: utf8(thread_id),
            # Stable topic for this thread. A panel subscribes to it to receive
            # the thread's DELTA events; without it the device can only guess.
            TAG_TOPIC_ID: u32(self._topic(thread_id)),
            TAG_TITLE: utf8(_wire_text(row.get("title") or row.get("name"), text_limit)),
            TAG_STATUS: u16(_thread_state(str(row.get("status") or ""))),
            TAG_REVISION: u64(int(row.get("revision") or 0)),
            TAG_OWNER_STATE: u8(
                {"unmanaged": 0, "observed": 1, "claimed": 2, "managed": 3}.get(
                    str(row.get("owner_state") or "unmanaged"), 0
                )
            ),
        }
        model = str(row.get("model") or "")
        if model:
            fields[TAG_MODEL_NAME] = utf8(_wire_text(model, min(128, text_limit)))
        cwd = str(row.get("cwd") or "")
        if cwd:
            fields[TAG_CWD] = utf8(_wire_text(cwd, min(1024, text_limit)))
        summary = str(row.get("preview") or row.get("summary") or "")
        if summary:
            fields[TAG_SUMMARY] = utf8(_wire_text(summary, text_limit))
        updated = int(float(row.get("updated_at") or row.get("recency_at") or 0) * 1000)
        if updated:
            fields[TAG_UPDATED_AT_MS] = i64(updated)
            fields[TAG_CREATED_AT_MS] = i64(updated)
        return encode_tlvs(fields.items())

    def _encode_model_object(self, row, text_limit: int = OBJECT_TEXT_LIMIT):
        if not isinstance(row, dict):
            return None
        name = str(row.get("model") or row.get("id") or "")
        if not name:
            return None
        fields = {
            TAG_OBJECT_TYPE: u8(9),
            TAG_MODEL_NAME: utf8(_wire_text(name, min(128, text_limit))),
            TAG_TITLE: utf8(_wire_text(row.get("display_name") or name, min(128, text_limit))),
        }
        efforts = [
            str(item.get("effort") or "")
            for item in row.get("efforts") or []
            if isinstance(item, dict) and str(item.get("effort") or "")
        ]
        capabilities = [
            encode_tlvs(
                {
                    TAG_MODEL_CAPABILITY_KIND: u8(0),
                    TAG_MODEL_CAPABILITY_VALUE: utf8(effort),
                }.items()
            )
            for effort in efforts
        ]
        fields[TAG_MODEL_CAPABILITIES] = encode_array(
            TAG_MODEL_CAPABILITIES, capabilities
        )
        return encode_tlvs(fields.items())

    def _encode_project_object(self, row, text_limit: int = OBJECT_TEXT_LIMIT):
        if not isinstance(row, dict):
            return None
        project_id = str(row.get("project_id") or "")
        name = str(row.get("name") or "")
        if not project_id and not name:
            return None
        fields = {
            TAG_OBJECT_TYPE: u8(10),
            TAG_PROJECT_REF: u32(self._ref("project", project_id or name)),
            TAG_PROJECT_ID: utf8(_wire_text(project_id, min(256, text_limit))),
            TAG_TITLE: utf8(_wire_text(name, min(256, text_limit))),
        }
        cwd = str(row.get("cwd") or "")
        if cwd:
            fields[TAG_CWD] = utf8(_wire_text(cwd, min(1024, text_limit)))
        updated = int(float(row.get("updated_at") or 0) * 1000)
        if updated:
            fields[TAG_UPDATED_AT_MS] = i64(updated)
        return encode_tlvs(fields.items())

    def _encode_message_object(
        self, row, thread_id: str = "", text_limit: int = OBJECT_TEXT_LIMIT
    ):
        if not isinstance(row, dict):
            return None
        message_id = str(row.get("message_id") or row.get("id") or "")
        if not message_id:
            return None
        thread_id = str(row.get("thread_id") or thread_id or "")
        fields = {
            TAG_OBJECT_TYPE: u8(2),
            TAG_MESSAGE_REF: u32(self._ref("message", message_id)),
            TAG_THREAD_REF: u32(self._ref("thread", thread_id)),
            TAG_MESSAGE_ID: utf8(_wire_text(message_id, min(256, text_limit))),
            TAG_KIND: u8(_message_kind(str(row.get("kind") or ""))),
            TAG_STATUS: u16(_message_status(str(row.get("status") or ""))),
        }
        body = str(row.get("text") or "")
        if body:
            fields[TAG_BODY] = utf8(_wire_text(body, text_limit))
        turn_id = str(row.get("turn_id") or "")
        if turn_id:
            fields[TAG_TURN_REF] = u32(self._ref("turn", turn_id))
        created = int(float(row.get("created_at") or 0) * 1000)
        if created:
            fields[TAG_CREATED_AT_MS] = i64(created)
        return encode_tlvs(fields.items())

    def _encode_approval_object(self, row, text_limit: int = OBJECT_TEXT_LIMIT):
        if not isinstance(row, dict):
            return None
        approval_id = row.get("request_id", row.get("approval_id", row.get("id")))
        if approval_id is None:
            return None
        approval_id = str(approval_id)
        thread_id = str(row.get("thread_id") or "")
        turn_id = str(row.get("turn_id") or "")
        fields = {
            TAG_OBJECT_TYPE: u8(3),
            TAG_APPROVAL_REF: u32(self._ref("approval", approval_id)),
            TAG_APPROVAL_ID: utf8(_wire_text(approval_id, min(128, text_limit))),
            TAG_THREAD_REF: u32(self._ref("thread", thread_id)),
            TAG_TURN_REF: u32(self._ref("turn", turn_id)),
            TAG_APPROVAL_KIND: u8(
                # Protocol 20.4: 0=command_execution, 1=file_change,
                # 2=network_access, 3=permission, 4=mcp_tool, 5=user_input,
                # 6=other, 7=mcp_elicitation.
                {
                    "command": 0,
                    "fileChange": 1,
                    "file": 1,
                    "network": 2,
                    "permissions": 3,
                    "permission": 3,
                    "mcp": 4,
                    "input": 5,
                    "mcp_elicitation": 7,
                }.get(str(row.get("kind") or ""), 6)
            ),
            TAG_PROMPT: utf8(_wire_text(row.get("command") or row.get("reason"), text_limit)),
        }
        detail = str(row.get("reason") or "")
        if detail:
            fields[TAG_APPROVAL_DETAIL] = utf8(_wire_text(detail, text_limit))
        cwd = str(row.get("cwd") or "")
        if cwd:
            fields[TAG_CWD] = utf8(_wire_text(cwd, min(1024, text_limit)))
        created = int(float(row.get("created_at") or 0) * 1000)
        if created:
            fields[TAG_CREATED_AT_MS] = i64(created)
        expires = int(float(row.get("expires_at") or 0) * 1000)
        if expires:
            fields[TAG_EXPIRES_AT_MS] = i64(expires)
        options = row.get("options")
        if isinstance(options, list) and options:
            encoded = [
                encode_tlvs(
                    {
                        TAG_DECISION: u8(int(item.get("decision") or 0)),
                        TAG_PROMPT: utf8(str(item.get("prompt") or "")),
                    }.items()
                )
                for item in options
                if isinstance(item, dict)
            ]
            fields[TAG_OPTIONS] = encode_array(TAG_OPTIONS, encoded)
        return encode_tlvs(fields.items())

    def _topic(self, thread_id):
        """Stable topic_id for a thread, or 0 when references are unavailable."""

        if self.references is None or not thread_id:
            return 0
        try:
            return int(self.references.topic_for(thread_id))
        except (AttributeError, ValueError, OverflowError):
            return 0

    def _ref(self, kind, external_id):
        if self.references is None or not external_id:
            return 0
        try:
            ref = int(self.references.ref_for(kind, external_id))
        except (ValueError, OverflowError):
            # An unsupported reference kind must degrade to 0, never raise:
            # a NameError/ValueError here tears down the whole CBDP session
            # instead of returning a decodable RESULT.
            return 0
        if ref:
            with self._lock:
                self._ref_external[(str(kind), ref)] = str(external_id)
        return ref

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
                seq=self._next_seq(),
                ack_seq=self._incoming_seq,
                header=header,
            )
        )

    def _handle_approval_resolve(self, frame):
        """Handle APPROVAL_RESOLVE (0x17) from a device.

        Accepts either the compact ``approval_ref`` or the external
        ``approval_id``, and passes a structured ``response`` through when the
        client sent one (MCP elicitation, user input, permissions). Without a
        response the plain numeric ``decision`` is used.
        """

        try:
            request_id = decode_u32(require_tlv(frame.header, TAG_REQUEST_ID))
        except CBDPError as error:
            return self._error(self._request_id(frame), error.name)
        if "approval" not in self._capabilities():
            return self._error(request_id, "CAPABILITY_NOT_ENABLED")
        # APPROVAL_RESOLVE carries its fields in the message header (the
        # firmware builds it that way), not inside a params TLV.
        params = self._decode_params(frame.header)
        approval_id = str(params.get("approval_id") or "")
        if not approval_id:
            ref = int(params.get("approval_ref") or 0)
            if ref:
                approval_id = self._approval_from_ref(ref)
                if not approval_id:
                    return self._error(request_id, "NOT_FOUND", "approval_ref")
        if not approval_id:
            return self._error(request_id, "INVALID_PARAM", "approval_required")

        response = None
        raw_response = params.get("response")
        if isinstance(raw_response, (bytes, bytearray)) and raw_response:
            try:
                nested = decode_tlvs(raw_response)
                action = decode_utf8(nested.get(0x0141, b""))
                content_raw = decode_utf8(nested.get(0x0142, b"{}")) or "{}"
                response = {"action": action, "content": json.loads(content_raw)}
            except (CBDPError, ValueError, TypeError):
                response = None
        decision = params.get("decision")
        if response is None and decision is None:
            return self._error(request_id, "INVALID_PARAM", "decision_required")

        commands = self._commands()
        if commands is None:
            return self._error(request_id, "UPSTREAM_UNAVAILABLE")
        if response is not None:
            method = "approval.respond"
            call_params = {"approval_id": approval_id, "response": response}
        else:
            method = "approval.resolve"
            call_params = {
                "approval_id": approval_id,
                "decision": _decision_name(int(decision)),
            }
        device_id = str(self.session.device_id or "")
        generation = int(self.session.generation)

        def run():
            try:
                result = commands.execute(
                    device_id,
                    self._capabilities(),
                    {
                        "type": "command",
                        "request_id": f"cbdp-approval:{device_id}:{request_id}",
                        "method": method,
                        "params": call_params,
                    },
                )
            except Exception:
                self._error(request_id, "INTERNAL")
                return
            if not isinstance(result, dict) or not result.get("ok"):
                code = (
                    str(result.get("error") or "INVALID_STATE")
                    if isinstance(result, dict)
                    else "INTERNAL"
                )
                self._error(request_id, "INVALID_STATE", code)
                return
            self._result(
                request_id, 0x0009, self._result_fields(result.get("result"), method)
            )

        if self._maybe_defer(generation, run):
            return None
        run()
        return None

    def _approval_from_ref(self, ref: int) -> str:
        if not ref or self.references is None:
            return ""
        try:
            return str(self.references.external_id("approval", int(ref)) or "")
        except Exception:
            return ""

    def _begin_transaction(self, tx_id, frame) -> bool:
        """Accept a BEGIN, tolerating a retry of an empty in-flight transaction.

        A client that lost the previous response may replay the same
        ``tx_id``/``idem`` pair. Retrying before any payload arrived is safe;
        reusing a ``tx_id`` mid-upload or with a different ``idem`` is not.
        """

        idem = bytes(frame.header.get(0x002B, b""))
        existing = self._transactions.get(tx_id)
        if existing is None:
            return True
        if existing.get("bytes"):
            return False
        if idem and existing.get("idem") != idem:
            return False
        return True

    def _handle_send_begin(self, frame):
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
            request_id = tx_id
        except CBDPError as error:
            return self._error(0, error.name)
        if not self._begin_transaction(tx_id, frame):
            return self._error(request_id, "IDEMPOTENCY_CONFLICT")
        self._transactions[tx_id] = {
            "parts": {},
            "bytes": 0,
            "kind": "text",
            "idem": bytes(frame.header.get(0x002B, b"")),
            "total_bytes": (
                decode_u32(frame.header[TAG_TOTAL_BYTES])
                if TAG_TOTAL_BYTES in frame.header
                else 0
            ),
            "thread_ref": (
                decode_u32(frame.header[TAG_THREAD_REF])
                if TAG_THREAD_REF in frame.header
                else 0
            ),
            "thread_id": (
                decode_utf8(frame.header[TAG_THREAD_ID])
                if TAG_THREAD_ID in frame.header
                else ""
            ),
            "use_active_thread": bool(
                decode_u8(frame.header[TAG_USE_ACTIVE_THREAD])
                if TAG_USE_ACTIVE_THREAD in frame.header
                else 0
            ),
        }
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
        declared = int(transaction.get("total_bytes") or 0)
        if TAG_TOTAL_BYTES in frame.header:
            try:
                declared = decode_u32(frame.header[TAG_TOTAL_BYTES])
            except CBDPError:
                return self._error(tx_id, "INVALID_PARAM")
        if declared and declared != transaction["bytes"]:
            return self._error(tx_id, "INVALID_PARAM")
        data = b"".join(transaction["parts"][key] for key in sorted(transaction["parts"]))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(tx_id, "INVALID_PARAM")
        commands = self._commands()
        if commands is None:
            return self._error(tx_id, "UPSTREAM_UNAVAILABLE")
        device_id = str(self.session.device_id or "")
        thread_id = self._transaction_thread(transaction) or self._request_thread(frame)
        if not thread_id:
            thread_id = self._active_thread_id(device_id)
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
        try:
            tx_id = decode_u32(require_tlv(frame.header, 0x002D))
        except CBDPError as error:
            return self._error(0, error.name)
        if "audio" not in self._capabilities():
            return self._error(tx_id, "CAPABILITY_NOT_ENABLED")
        try:
            sample_rate = decode_u32(frame.header.get(TAG_SAMPLE_RATE, u32(16000)))
            channels = decode_u8(frame.header.get(TAG_CHANNELS, u8(1)))
            bits = decode_u8(frame.header.get(TAG_BITS_PER_SAMPLE, u8(16)))
            thread_ref = (
                decode_u32(frame.header[TAG_THREAD_REF])
                if TAG_THREAD_REF in frame.header
                else 0
            )
            thread_id = (
                decode_utf8(frame.header[TAG_THREAD_ID])
                if TAG_THREAD_ID in frame.header
                else ""
            )
            use_active_thread = bool(
                decode_u8(frame.header[TAG_USE_ACTIVE_THREAD])
                if TAG_USE_ACTIVE_THREAD in frame.header
                else 0
            )
            total_bytes = (
                decode_u32(frame.header[TAG_TOTAL_BYTES])
                if TAG_TOTAL_BYTES in frame.header
                else 0
            )
        except CBDPError as error:
            return self._error(tx_id, error.name)
        if thread_ref:
            # Resolve the compact ref at BEGIN so a stale ref is reported here
            # instead of surfacing as a vague failure after the whole upload.
            resolved = self._thread_from_ref(int(thread_ref))
            if not resolved:
                return self._error(tx_id, "NOT_FOUND", "thread_ref")
            thread_id = resolved
        if not self._begin_transaction(tx_id, frame):
            return self._error(tx_id, "IDEMPOTENCY_CONFLICT")
        self._transactions[tx_id] = {
            "kind": "audio",
            "parts": {},
            "bytes": 0,
            "idem": bytes(frame.header.get(0x002B, b"")),
            "total_bytes": total_bytes,
            "sha256": frame.header.get(TAG_SHA256, b""),
            "thread_ref": int(thread_ref),
            "thread_id": thread_id,
            "use_active_thread": use_active_thread,
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
        declared = int(transaction.get("total_bytes") or 0)
        if TAG_TOTAL_BYTES in frame.header:
            try:
                declared = decode_u32(frame.header[TAG_TOTAL_BYTES])
            except CBDPError:
                return self._error(tx_id, "INVALID_PARAM")
        if declared and declared != transaction["bytes"]:
            return self._error(tx_id, "INVALID_PARAM")
        if TAG_SHA256 in frame.header:
            digest = frame.header[TAG_SHA256]
            if len(digest) != 32:
                return self._error(tx_id, "INVALID_PARAM")
            combined = b"".join(
                transaction["parts"][key] for key in sorted(transaction["parts"])
            )
            if hashlib.sha256(combined).digest() != digest:
                return self._error(tx_id, "INVALID_PARAM")
        try:
            data = b"".join(
                transaction["parts"][key] for key in sorted(transaction["parts"])
            )
        except KeyError:
            return self._error(tx_id, "FRAGMENT_ERROR")
        generation = int(self.session.generation)

        def run():
            transcript, failure = self._dispatch_audio_transcript(transaction, data)
            if transcript is None:
                self._error(
                    tx_id,
                    "UPSTREAM_UNAVAILABLE",
                    failure or "STT_UNAVAILABLE",
                    {"reason": "STT_UNAVAILABLE"},
                )
                return
            commands = self._commands()
            if commands is None:
                self._error(tx_id, "UPSTREAM_UNAVAILABLE")
                return
            device_id = str(self.session.device_id or "")
            thread_id = self._transaction_thread(transaction)
            if not thread_id:
                thread_id = self._active_thread_id(device_id)
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
        """Run STT and return ``(transcript_or_none, failure_reason)``.

        A recognizer exception is user-actionable and must survive to the wire;
        otherwise a remote STT outage is indistinguishable from a missing
        recognizer or an empty transcript.
        """

        recognizer = self.speech
        if recognizer is None or not hasattr(recognizer, "transcribe_pcm"):
            return None, "STT_NOT_CONFIGURED"
        try:
            text = recognizer.transcribe_pcm(
                self.session.device_id,
                data,
                sample_rate=int(transaction.get("sample_rate") or 16000),
                channels=int(transaction.get("channels") or 1),
                bits=int(transaction.get("bits") or 16),
            )
        except Exception as error:
            reason = str(error).strip() or error.__class__.__name__
            return None, _wire_text(reason, 512)
        if not isinstance(text, str) or not text.strip():
            return None, "STT_EMPTY_TRANSCRIPT"
        return text.strip(), ""
