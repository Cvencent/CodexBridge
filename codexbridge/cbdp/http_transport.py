"""CBDP v1 over the phone Companion HTTP/WebSocket transport.

The phone transport is authenticated with the same Bearer pairing token as
``/api/companion/*``. That token is held only for the lifetime of the
session and is used as the CBDP HMAC key, so the Bridge never has to persist
a recoverable copy of a phone secret.

One session owns one CbdpGateway, therefore subscriptions, cursors, writer
leases and the reference registry are the same objects the device TCP path
uses. HTTP POST and WebSocket frames feed ``handle_frame``; outbound frames
are either written to the WebSocket or queued for the SSE stream.
"""

from __future__ import annotations

import base64
import hashlib
import queue
import struct
import threading
import time

from codexbridge.cbdp.capabilities import UNIMPLEMENTED_CAPABILITIES
from codexbridge.cbdp.constants import HEADER_SIZE, TAG_GLOBAL_SEQ
from codexbridge.cbdp.frame import FrameReassembler, decode_frame, encode_frame
from codexbridge.cbdp.tlv import CBDPError, decode_tlvs, decode_u64

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_MESSAGE_BYTES = 1024 * 1024
SESSION_TTL_SECONDS = 120.0
SESSION_QUEUE_DEPTH = 256

# The phone Companion capability set is intentionally small. CBDP keeps its
# own capability numbering, so one negotiating phone capability expands to the
# read/subscribe/write bits the same feature needs on the wire. The phone's
# HELLO still intersects this policy with what it actually supports.
PHONE_CAPABILITY_EXPANSION: dict[str, set[str]] = {
    "status": {
        "status", "telemetry", "subscribe_summary", "notice", "account",
        "context_usage", "server_request", "input_capabilities",
    },
    "tasks": {
        "tasks", "results", "history", "multi_page", "subscribe_live",
        "subscribe_thread", "subscribe_summary", "read_unread",
        "project_manage", "thread_search", "model_switch", "item_stream",
        "plan", "reasoning_summary", "tool_events", "mcp", "events_since",
        "file_reference", "image_input", "audio_input", "message_reference",
        "cross_device_active", "resume", "thread_settings", "writer_lease",
    },
    "control": {
        "control", "thread_manage", "thread_lifecycle", "thread_fork",
        "thread_settings", "message_edit", "message_delete", "guidance",
        "draft", "attachment", "model_switch",
    },
    "approval": {"approval", "structured_input", "server_request"},
    "queue": {
        "queue", "queue_manage", "queue_priority", "guidance",
        "message_edit", "message_delete",
    },
}


def expand_phone_capabilities(capabilities) -> set[str]:
    expanded: set[str] = set()
    for name in capabilities or ():
        expanded |= PHONE_CAPABILITY_EXPANSION.get(str(name), set())
    return expanded - UNIMPLEMENTED_CAPABILITIES


class CbdpHttpSession:
    """One CBDP session carried by a phone Bearer token."""

    def __init__(self, device_id: str, token: str, gateway, max_queue: int = SESSION_QUEUE_DEPTH):
        self.device_id = str(device_id)
        self.token = token
        self.gateway = gateway
        self.queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self.created_at = time.monotonic()
        self.last_seen = self.created_at
        self.closed = threading.Event()
        self._reassembler_lock = threading.Lock()
        self._reassembler = FrameReassembler(65536)
        self.gateway.bind_transport(self._outbound)

    def _outbound(self, frame) -> None:
        try:
            self.queue.put_nowait(encode_frame(frame))
        except queue.Full:
            # Mirror the device queue policy: drop the oldest frame and keep
            # the newest state rather than blocking the hub.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(encode_frame(frame))
            except (queue.Full, CBDPError):
                pass

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def accept_bytes(self, raw: bytes) -> None:
        """Decode zero or more complete CBDP frames from one transport message."""

        data = bytes(raw)
        if not data or len(data) > MAX_WS_MESSAGE_BYTES:
            raise CBDPError("PAYLOAD_TOO_LARGE")
        self.touch()
        offset = 0
        while offset < len(data):
            if len(data) - offset < HEADER_SIZE:
                raise CBDPError("INVALID_FRAME")
            header_len = int.from_bytes(data[offset + 6:offset + 8], "big")
            payload_len = int.from_bytes(data[offset + 8:offset + 12], "big")
            if header_len < HEADER_SIZE:
                raise CBDPError("INVALID_FRAME")
            size = header_len + payload_len
            if size <= 0 or offset + size > len(data):
                raise CBDPError("INVALID_FRAME")
            frame = decode_frame(data[offset:offset + size], max_frame_bytes=MAX_WS_MESSAGE_BYTES)
            offset += size
            with self._reassembler_lock:
                logical = self._reassembler.accept(frame)
            if logical is not None:
                self.gateway.handle_frame(logical)

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        subscriptions = getattr(self.gateway, "subscriptions", None)
        if subscriptions is not None:
            session = getattr(self.gateway, "session", None)
            target = str(getattr(session, "device_id", "") or self.device_id)
            subscriptions.drop_device(target)
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class CbdpHttpBroker:
    """Own the CBDP sessions reachable through the Companion HTTP port."""

    def __init__(self, commands, capability_for, hub=None, ttl: float = SESSION_TTL_SECONDS):
        self._commands = commands
        self._capability_for = capability_for
        self._hub = hub
        self._ttl = float(ttl)
        self._sessions: dict[str, CbdpHttpSession] = {}
        self._lock = threading.RLock()

    def attach(self, device_id: str, token: str) -> CbdpHttpSession:
        with self._lock:
            self._expire_locked()
            session = self._sessions.get(device_id)
            if session is None or session.token != token:
                if session is not None:
                    session.close()
                gateway = self._gateway(device_id, token)
                session = CbdpHttpSession(device_id, token, gateway)
                self._sessions[device_id] = session
            session.touch()
        return session

    def get(self, device_id: str) -> CbdpHttpSession | None:
        with self._lock:
            self._expire_locked()
            session = self._sessions.get(device_id)
            if session is not None:
                session.touch()
            return session

    def drop(self, device_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(device_id, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def session_count(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._sessions)

    def _expire_locked(self) -> None:
        now = time.monotonic()
        stale = [
            device_id
            for device_id, session in self._sessions.items()
            if session.closed.is_set() or now - session.last_seen > self._ttl
        ]
        for device_id in stale:
            session = self._sessions.pop(device_id, None)
            if session is not None:
                session.close()

    def _gateway(self, device_id: str, token: str):
        from codexbridge.cbdp.gateway import CbdpGateway

        capabilities = expand_phone_capabilities(self._capability_for(device_id))
        gateway = CbdpGateway(
            commands=self._commands,
            pairing_token=lambda _device_id: token,
            capability_policy=lambda _device_id: capabilities,
            transport_allowed=lambda _device_id: (1 << 64) - 1,
            auth_mode=1,
        )
        if self._hub is not None:
            self._hub.register_cbdp_gateway(device_id, gateway)
            gateway.subscriptions.set_max_subscriptions(device_id, 32)
        return gateway


def websocket_accept_key(key: str) -> str:
    digest = hashlib.sha1((str(key) + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def frame_cursor(raw: bytes) -> int:
    """Return the ``cursor`` an SSE ``id:`` must carry for one CBDP frame.

    Protocol 3.2 requires the SSE id to be the frame cursor. DELTA frames
    carry ``global_seq`` in their header TLV; other frames have no cursor, so
    the fixed-header ``seq`` is used as the transport ordering hint.
    """

    data = bytes(raw)
    if len(data) < HEADER_SIZE:
        return 0
    header_len = int.from_bytes(data[6:8], "big")
    if header_len < HEADER_SIZE or header_len > len(data):
        return 0
    try:
        fields = decode_tlvs(data[HEADER_SIZE:header_len])
    except CBDPError:
        return 0
    value = fields.get(TAG_GLOBAL_SEQ)
    if value is not None:
        try:
            return int(decode_u64(value))
        except CBDPError:
            return 0
    return int.from_bytes(data[16:20], "big")


class WebSocketError(ValueError):
    pass


def encode_ws_frame(payload: bytes, opcode: int = 0x2) -> bytes:
    payload = bytes(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack(">H", size))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", size))
    return bytes(header) + payload


def decode_ws_frame(buffer: bytearray) -> tuple[int, bytes] | None:
    """Return one complete client frame, removing it from ``buffer``."""

    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F
    offset = 2
    if size == 126:
        if len(buffer) < offset + 2:
            return None
        size = struct.unpack_from(">H", buffer, offset)[0]
        offset += 2
    elif size == 127:
        if len(buffer) < offset + 8:
            return None
        size = struct.unpack_from(">Q", buffer, offset)[0]
        offset += 8
    if size > MAX_WS_MESSAGE_BYTES:
        raise WebSocketError("payload_too_large")
    if masked:
        if len(buffer) < offset + 4:
            return None
        mask = bytes(buffer[offset:offset + 4])
        offset += 4
    else:
        mask = b""
    if len(buffer) < offset + size:
        return None
    payload = bytes(buffer[offset:offset + size])
    del buffer[:offset + size]
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload
