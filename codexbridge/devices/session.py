from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable, Iterable

from codexbridge import VERSION
from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    HEADER_SIZE,
    MAGIC,
    TYPE_DELTA,
    TYPE_PING,
    TYPE_PONG,
)
from codexbridge.cbdp.tlv import CBDPError
from codexbridge.cbdp.compat import CompatContext, to_cbdp
from codexbridge.cbdp.events import event_scope
from codexbridge.cbdp.frame import (
    Frame,
    FrameReassembler,
    decode_frame,
    encode_fragments,
)
from codexbridge.protocol.messages import decode, encode


CBDP_MAGIC = b"\xCB\x01"


def detect_wire_protocol(buffer: bytes | bytearray) -> str:
    """Classify the first bytes on a shared device TCP port."""
    data = bytes(buffer)
    if not data:
        return "unknown"
    if data[:1] == b"{":
        return "legacy_json"
    if data.startswith(CBDP_MAGIC):
        return "cbdp"
    if data[:1] == CBDP_MAGIC[:1]:
        return "unknown"
    return "unknown"



def send_register_offer(
    host: str,
    port: int,
    token: str,
    *,
    device_id: str,
    registration_nonce: str,
    timeout_seconds: float = 15.0,
) -> bool:
    """Store a hardware token over the one-shot registration connection."""
    if len(token) < 16:
        raise ValueError("token must be at least 16 characters")
    if not device_id:
        raise ValueError("device_id must not be empty")
    if not 16 <= len(registration_nonce) <= 64:
        raise ValueError("registration_nonce must be 16 to 64 characters")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    buffer = bytearray()
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout_seconds)
        sock.sendall(
            encode(
                {
                    "type": "register_offer",
                    "registration_nonce": registration_nonce,
                    "token": token,
                    "bridge": "CodexBridge",
                }
            )
        )
        reply, closed = DeviceSession._next_message(
            sock,
            buffer,
            time.monotonic() + timeout_seconds,
        )
        return (
            not closed
            and reply is not None
            and reply.get("type") == "register_ok"
            and reply.get("device_id") == device_id
            and reply.get("accepted") is True
        )
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class CbdpDeviceSession:
    """CBDP v1 session driver used after a CBDP discovery result."""

    # The hub uses this marker to deliver committed events through the Event
    # Spine instead of the legacy JSON compatibility translator.
    is_cbdp = True

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        device_id: str,
        gateway=None,
        on_event: Callable[[dict], None] | None = None,
        on_device_info: Callable[[dict], Iterable[str]] | None = None,
        on_online: Callable[[], None] | None = None,
        on_offline: Callable[[], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
        heartbeat_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
        registration_nonce: str | None = None,
        protocol: str = "cbdp",
        **_: object,
    ) -> None:
        del registration_nonce, protocol
        if not device_id:
            raise ValueError("device_id must not be empty")
        self.host = host
        self.port = port
        self.token = token
        self.device_id = device_id
        self.gateway = gateway
        self.on_event = on_event
        self.on_device_info = on_device_info
        self.on_online = on_online
        self.on_offline = on_offline
        self.on_heartbeat = on_heartbeat
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stopping = threading.Event()
        self._next_request_id = 1
        self._next_ping_id = 1
        self._max_frame_bytes = 65536
        self._heartbeat_seconds = heartbeat_seconds
        self._reassembler = FrameReassembler(self._max_frame_bytes)

    def send_event(self, event) -> bool:
        """Send one committed Event Spine event as a protocol-valid DELTA.

        Using the gateway's own encoder guarantees the frame carries
        ``event_id``/``global_seq``/``topic_seq``/``revision``, the real
        ``topic_id`` and a non-zero transport ``seq``. The compatibility
        translator must not synthesise DELTA frames for CBDP sessions.

        A committed event may leave the Bridge only while the device owns an
        active subscription for its topic/scope. Returning ``False`` makes the
        non-delivery explicit for attach-time replay callers.
        """

        if self.gateway is None:
            raise RuntimeError("CBDP gateway not configured")
        has_subscription = getattr(self.gateway, "has_subscription", None)
        if not callable(has_subscription) or not has_subscription(
            int(event.topic_id), event_scope(int(event.event_type))
        ):
            return False
        self._send_frame(self.gateway.delta_frame(event))
        return True

    def send(self, message: dict) -> None:
        """Send a legacy-shaped message through the CBDP compatibility path.

        The bridge never opens a second JSON channel on a CBDP session. Legacy
        call sites can keep their message shape, but the bytes on the wire are
        always CBDP v1 frames.
        """

        context = CompatContext(
            refs=getattr(self.gateway, "references", None),
            device_id=self.device_id,
            request_id=self._take_request_id(),
        )
        for frame in to_cbdp(message, context):
            if frame.message_type == TYPE_DELTA:
                # CBDP DELTA frames are committed Event Spine events. The
                # compatibility translator is stateless and cannot supply the
                # required event envelope, so it must never put a DELTA on the
                # wire. Business events reach this session via send_event().
                continue
            self._send_frame(frame)

    def stop(self) -> None:
        self._stopping.set()
        with self._send_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run_once(self) -> None:
        if self.gateway is None:
            raise RuntimeError("CBDP gateway not configured")
        online = False
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with self._send_lock:
                self._socket = sock
            self.gateway.bind_transport(lambda frame: self._send_frame(frame))
            self.gateway.begin_session()
            buffer = bytearray()
            hello_deadline = time.monotonic() + 10.0
            auth_deadline = None
            last_activity = time.monotonic()
            next_ping = None
            missed_pongs = 0
            while not self._stopping.is_set():
                now = time.monotonic()
                if not online and self.gateway.session is not None and not self.gateway.session.ready:
                    if auth_deadline is None and self.gateway.session.authenticated:
                        auth_deadline = now + 10.0
                    deadline = auth_deadline or hello_deadline
                    if now > deadline:
                        return
                try:
                    readable, _, _ = select.select([sock], [], [], 0.25)
                except (OSError, TypeError):
                    # Test doubles and a few wrapper sockets do not expose a
                    # pollable descriptor; fall back to the blocking recv path.
                    readable = [sock]
                if not readable:
                    if online and next_ping is not None and now >= next_ping:
                        if missed_pongs >= 3:
                            return
                        self._send_ping()
                        missed_pongs += 1
                        next_ping = now + self._heartbeat_seconds
                    continue
                try:
                    payload = sock.recv(self._max_frame_bytes)
                except OSError:
                    return
                if not payload:
                    return
                last_activity = time.monotonic()
                buffer.extend(payload)
                if len(buffer) > self._max_frame_bytes:
                    return
                while True:
                    if len(buffer) < HEADER_SIZE:
                        break
                    header_len = int.from_bytes(buffer[6:8], "big")
                    payload_len = int.from_bytes(buffer[8:12], "big")
                    if (
                        header_len < HEADER_SIZE
                        or header_len + payload_len > self._max_frame_bytes
                    ):
                        return
                    frame_size = header_len + payload_len
                    if len(buffer) < frame_size:
                        break
                    frame = decode_frame(
                        bytes(buffer[:frame_size]),
                        max_frame_bytes=self._max_frame_bytes,
                    )
                    del buffer[:frame_size]
                    try:
                        logical = self._reassembler.accept(frame)
                    except CBDPError:
                        return
                    if logical is None:
                        continue
                    frame = logical
                    if frame.message_type == TYPE_PONG:
                        missed_pongs = 0
                        next_ping = time.monotonic() + self._heartbeat_seconds
                    self.gateway.handle_frame(frame)
                session = self.gateway.session
                if session is not None and session.ready and not online:
                    online = True
                    self._max_frame_bytes = int(session.max_frame_bytes or 65536)
                    self._reassembler.max_frame_bytes = self._max_frame_bytes
                    self._heartbeat_seconds = float(
                        session.heartbeat_seconds or self.heartbeat_seconds
                    )
                    next_ping = time.monotonic() + self._heartbeat_seconds
                    info = {
                        "device_id": session.device_id or self.device_id,
                        "model": session.model,
                        "name": session.name,
                        "firmware": session.firmware,
                        "profile": session.profile,
                        "max_text_chars": session.max_text_chars,
                        "capabilities": sorted(self.gateway._capabilities()),
                    }
                    capabilities = (
                        self.on_device_info(info)
                        if self.on_device_info is not None
                        else None
                    )
                    if capabilities is not None:
                        session.enabled = capability_bitmap(capabilities)
                        session.effective = (
                            session.supported & session.enabled & session.transport_allowed
                        )
                    if self.on_online is not None:
                        self.on_online()
            return
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._send_lock:
                self._socket = None
            subscriptions = getattr(self.gateway, "subscriptions", None)
            if subscriptions is not None:
                final_device_id = str(
                    getattr(getattr(self.gateway, "session", None), "device_id", "")
                    or self.device_id
                )
                subscriptions.drop_device(final_device_id)
            if online and self.on_offline is not None:
                self.on_offline()

    def _take_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 2
        if self._next_request_id > 0xFFFFFFFF:
            self._next_request_id = 1
        return request_id

    def _send_ping(self) -> None:
        ping_id = self._next_ping_id.to_bytes(4, "big")
        self._next_ping_id += 1
        if self._next_ping_id > 0xFFFFFFFF:
            self._next_ping_id = 1
        self._send_frame(
            Frame(
                message_type=TYPE_PING,
                header={0x0097: ping_id},
            )
        )

    def _send_frame(self, frame) -> None:
        # Protocol 14.1: a logical frame that overflows the negotiated
        # max_frame_bytes is split into transport fragments instead of being
        # dropped or written as one oversized TCP frame.
        payloads = encode_fragments(frame, self._max_frame_bytes)
        with self._send_lock:
            if self._socket is None:
                raise RuntimeError("session is not connected")
            for payload in payloads:
                self._socket.sendall(payload)

class DeviceSession:
    # Legacy UTF-8 JSON line protocol: the hub keeps sending it legacy shapes.
    is_cbdp = False

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        device_id: str,
        on_event: Callable[[dict], None] | None = None,
        on_device_info: Callable[[dict], Iterable[str]] | None = None,
        on_online: Callable[[], None] | None = None,
        on_offline: Callable[[], None] | None = None,
        on_heartbeat: Callable[[], None] | None = None,
        heartbeat_seconds: float = 5.0,
        timeout_seconds: float = 15.0,
        registration_nonce: str | None = None,
    ):

        if len(token) < 16:
            raise ValueError("token must be at least 16 characters")
        if not device_id:
            raise ValueError("device_id must not be empty")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.host = host
        self.port = port
        self.token = token
        self.device_id = device_id
        self.on_event = on_event
        self.on_device_info = on_device_info
        self.on_online = on_online
        self.on_offline = on_offline
        self.on_heartbeat = on_heartbeat
        self.registration_nonce = registration_nonce
        self._active_capabilities: set[str] = set()
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stopping = threading.Event()

    def send(self, message: dict) -> None:
        with self._send_lock:
            if self._socket is None:
                raise RuntimeError("session is not connected")
            self._socket.sendall(encode(message))

    def stop(self) -> None:
        self._stopping.set()
        with self._send_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run_once(self) -> None:
        if self.registration_nonce is not None and not send_register_offer(
            self.host,
            self.port,
            self.token,
            device_id=self.device_id,
            registration_nonce=self.registration_nonce,
            timeout_seconds=self.timeout_seconds,
        ):
            return

        online = False
        buffer = bytearray()
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with self._send_lock:
                self._socket = sock

            self.send({"type": "bridge_auth", "token": self.token})
            reply, closed = self._next_message(
                sock,
                buffer,
                time.monotonic() + self.timeout_seconds,
            )
            if closed or reply is None or reply.get("type") != "bridge_auth_ok":
                return
            if reply.get("device_id") != self.device_id:
                return
            self._active_capabilities = (
                set(self.on_device_info(reply))
                if self.on_device_info is not None
                else set()
            )
            self.send(self._ready_message(self._active_capabilities))

            online = True
            if self.on_online is not None:
                self.on_online()

            last_received = time.monotonic()
            next_heartbeat = last_received + self.heartbeat_seconds
            while True:
                if self._stopping.is_set():
                    return
                self._drain_buffer(buffer, self.on_event)
                now = time.monotonic()
                if now >= next_heartbeat:
                    try:
                        self.send(self._ready_message(self._active_capabilities))
                    except OSError:
                        return
                    if self.on_heartbeat is not None:
                        self.on_heartbeat()
                    next_heartbeat = now + self.heartbeat_seconds

                readable, _, _ = select.select(
                    [sock],
                    [],
                    [],
                    max(0.0, next_heartbeat - time.monotonic()),
                )
                if not readable:
                    continue
                try:
                    payload = sock.recv(4096)
                except OSError:
                    return
                if not payload:
                    return
                buffer.extend(payload)
                self._drain_buffer(buffer, self.on_event)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._send_lock:
                self._socket = None
            if online and self.on_offline is not None:
                self.on_offline()

    @staticmethod
    def _ready_message(capabilities: Iterable[str]) -> dict:
        return {
            "type": "bridge_ready",
            "bridge": "CodexBridge",
            "version": VERSION,
            "capabilities": sorted(capabilities),
        }

    @staticmethod
    def _drain_buffer(
        buffer: bytearray,
        on_event: Callable[[dict], None] | None,
    ) -> None:
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            del buffer[:]
            buffer.extend(remainder)
            message = decode(bytes(line) + b"\n")
            if message is not None and on_event is not None:
                on_event(message)

    @staticmethod
    def _next_message(
        sock: socket.socket,
        buffer: bytearray,
        deadline: float,
    ) -> tuple[dict | None, bool]:
        while True:
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                del buffer[:]
                buffer.extend(remainder)
                message = decode(bytes(line) + b"\n")
                if message is not None:
                    return message, False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, False
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                return None, False
            try:
                payload = sock.recv(4096)
            except OSError:
                return None, True
            if not payload:
                return None, True
            buffer.extend(payload)
