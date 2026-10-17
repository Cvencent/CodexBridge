from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from codexbridge.cbdp.capabilities import capability_names
from codexbridge.cbdp.constants import (
    FLAG_REQUEST,
    FLAG_RESPONSE,
    TAG_CAPABILITY_BITS,
    TAG_DEVICE_ID,
    TAG_DISCOVERY_NONCE,
    TAG_DISCOVERY_SCOPE,
    TAG_FIRMWARE,
    TAG_MODEL,
    TAG_PORT,
    TAG_PROFILE,
    TAG_SUPPORTED_CAPS,
    TAG_TRANSPORT_SECURITY,
    TYPE_DISCOVERY,
    TYPE_DISCOVERY_REPLY,
)
from codexbridge.cbdp.frame import Frame, decode_frame, encode_frame
from codexbridge.cbdp.tlv import CBDPError, decode_u8, decode_u16, decode_utf8, u8, u16, utf8
from codexbridge.net_gate import configure_exclusive

DISCOVERY_PORT = 8764

# Repeated announcements from the same endpoint collapse into one dial.
PRESENCE_DEDUPE_SECONDS = 5.0
DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER"
MAX_REPLY_BYTES = 1024


def build_cbdp_discovery_request(
    nonce: bytes,
    *,
    scope: int = 2,
    profile: int = 0,
) -> bytes:
    if len(nonce) != 16:
        raise ValueError("discovery nonce must be 16 bytes")
    if scope not in {0, 1, 2}:
        raise ValueError("discovery scope must be 0, 1 or 2")
    if profile not in {0, 1, 2}:
        raise ValueError("discovery profile must be 0, 1 or 2")
    return encode_frame(
        Frame(
            message_type=TYPE_DISCOVERY,
            flags=FLAG_REQUEST,
            header={
                TAG_DISCOVERY_NONCE: bytes(nonce),
                TAG_DISCOVERY_SCOPE: u8(scope),
                TAG_PROFILE: u8(profile),
            },
        )
    )


def parse_cbdp_reply(
    payload: bytes,
    host: str,
    *,
    expected_nonce: bytes | None = None,
) -> DiscoveryResult | None:
    if not isinstance(payload, bytes) or len(payload) > MAX_REPLY_BYTES:
        return None
    try:
        frame = decode_frame(payload, max_frame_bytes=MAX_REPLY_BYTES)
        if frame.message_type != TYPE_DISCOVERY_REPLY:
            return None
        if not frame.flags & FLAG_RESPONSE:
            return None
        nonce = frame.header.get(TAG_DISCOVERY_NONCE)
        if nonce is None or len(nonce) != 16:
            return None
        if expected_nonce is not None and nonce != expected_nonce:
            return None
        device_id = decode_utf8(frame.header[TAG_DEVICE_ID])
        model = decode_utf8(frame.header[TAG_MODEL])
        firmware = decode_utf8(frame.header[TAG_FIRMWARE])
        profile = decode_u8(frame.header[TAG_PROFILE])
        port = decode_u16(frame.header[TAG_PORT])
        supported = frame.header.get(TAG_SUPPORTED_CAPS)
        if supported is None or len(supported) != 8:
            return None
        capabilities = sorted(capability_names(int.from_bytes(supported, "big")))
    except (CBDPError, KeyError, ValueError):
        return None
    if not device_id or not model or profile not in {0, 1, 2} or not 1 <= port <= 65535:
        return None
    return DiscoveryResult(
        host=host,
        device=model,
        device_id=device_id,
        name=None,
        port=port,
        capabilities=capabilities,
        protocol="cbdp",
    )

# How long one round waits for replies. A Wi-Fi device in power-save mode
# only wakes for its access point on a fixed interval (an ESP32-C3 station
# here reports 1.024 s), so a window shorter than that interval can simply
# miss a listener that is there and willing to answer. Two intervals plus a
# margin keeps discovery honest without holding the host longer than needed.
DISCOVERY_TIMEOUT_SECONDS = 2.5


@dataclass(frozen=True)
class DiscoveryResult:
    host: str
    device: str
    device_id: str
    name: str | None
    port: int
    capabilities: list[str]
    registration_state: str = "approved"
    registration_nonce: str | None = None
    protocol: str = "legacy_json"


def parse_reply(payload: bytes, host: str) -> DiscoveryResult | None:
    if not isinstance(payload, bytes) or len(payload) > MAX_REPLY_BYTES:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None

    port = value.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None

    device = value.get("device")
    if not isinstance(device, str) or not device:
        return None
    device_id = value.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        return None
    name = value.get("name")
    name = name if isinstance(name, str) and name else None
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    capabilities = [item for item in capabilities if isinstance(item, str)]

    registration_state = value.get("registration_state", "approved")
    if not isinstance(registration_state, str) or registration_state not in {
        "pending",
        "approved",
        "rejected",
        "revoked",
    }:
        return None
    registration_nonce = value.get("registration_nonce")
    if registration_state == "pending":
        if (
            not isinstance(registration_nonce, str)
            or not 16 <= len(registration_nonce) <= 64
        ):
            return None
    elif registration_nonce is not None and (
        not isinstance(registration_nonce, str)
        or not 16 <= len(registration_nonce) <= 64
    ):
        return None

    protocol = value.get("protocol", "legacy_json")
    if protocol not in {"legacy_json", "cbdp"}:
        protocol = "legacy_json"

    return DiscoveryResult(
        host=host,
        device=device,
        device_id=device_id,
        name=name,
        port=port,
        capabilities=capabilities,
        registration_state=registration_state,
        registration_nonce=registration_nonce,
        protocol=protocol,
    )


def _broadcast_targets(sock: socket.socket) -> tuple[str, ...]:
    """Return the limited broadcast plus each interface's subnet broadcast.

    A limited broadcast to 255.255.255.255 is filtered by some hosts (ESP-IDF
    lwIP among them): the reply never leaves the device on that path. Sending
    the same request to the subnet broadcast as well is the portable fix and
    keeps discovery generic across device classes.
    """
    targets: list[str] = ["255.255.255.255"]
    try:
        import ipaddress

        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127."):
                continue
            try:
                network = ipaddress.ip_network(address + "/24", strict=False)
            except ValueError:
                continue
            candidate = str(network.broadcast_address)
            if candidate not in targets:
                targets.append(candidate)
    except OSError:
        pass
    return tuple(targets)


def discover(
    timeout: float = 3.0,
    targets: Iterable[str] | None = None,
    port: int = DISCOVERY_PORT,
) -> list[DiscoveryResult]:
    # ``port`` is overridable for the same reason PresenceListener's is: a
    # live bridge owns the well-known port exclusively, and a test must not
    # have to fight it to exercise the round trip.
    if timeout < 0:
        raise ValueError("timeout must not be negative")

    target_hosts = tuple(targets) if targets is not None else ()
    if targets is None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            target_hosts = _broadcast_targets(probe)
    if not target_hosts:
        return []

    deadline = time.monotonic() + timeout
    results: dict[tuple[str, str], DiscoveryResult] = {}
    nonce = secrets.token_bytes(16)
    cbdp_request = build_cbdp_discovery_request(nonce, scope=1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))

        for target in target_hosts:
            try:
                sock.sendto(DISCOVERY_REQUEST, (target, port))
                sock.sendto(cbdp_request, (target, port))
            except OSError:
                continue

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                break
            try:
                payload, source = sock.recvfrom(MAX_REPLY_BYTES + 1)
            except OSError:
                continue
            result = parse_cbdp_reply(
                payload,
                source[0],
                expected_nonce=nonce,
            ) or parse_reply(payload, source[0])
            if result is not None:
                results[(result.device_id, result.host)] = result

    return list(results.values())


class PresenceListener:
    """Receive unsolicited identity announcements on the discovery port.

    A device that just opened its listener tells the LAN instead of waiting to
    be asked, because a station in power save only answers a broadcast when its
    listen interval happens to line up. The payload is the same discovery reply
    `parse_reply` already accepts, so any device that can answer a broadcast can
    also announce itself with no extra protocol.
    """

    def __init__(
        self,
        on_result,
        logger=print,
        *,
        port: int = DISCOVERY_PORT,
    ) -> None:
        self.on_result = on_result
        self.logger = logger
        # Overridable so a test can bind a free port; two live listeners cannot
        # share the discovery port and keep the datagrams unambiguous.
        self.port = port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="presence-listener", daemon=True)
        self._thread.start()

    def _bind_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            configure_exclusive(sock)
            sock.bind(("", self.port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(1.0)
        return sock

    def _loop(self) -> None:
        # A reboot or an upgrade can leave the port held for a moment. The fast
        # wake path is worth retrying, but quietly: a foreign program that owns
        # the port should not make us shout once a second forever.
        delay = 1.0
        warned = False
        while not self._stop.is_set():
            try:
                sock = self._bind_socket()
            except OSError as exc:
                if not warned:
                    # Another program holds the discovery port. Broadcast discovery
                    # keeps working without this, so the bridge still starts.
                    self.logger(f"CodexBridge: presence listener unavailable: {exc}")
                    warned = True
                self._stop.wait(delay)
                delay = min(delay * 2.0, 15.0)
                continue
            if warned:
                self.logger(f"CodexBridge: presence listener back on {self.port}")
            warned = False
            delay = 1.0
            self._sock = sock
            try:
                self._run(sock)
            finally:
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass

    def _run(self, sock: socket.socket) -> None:
        seen: dict[str, float] = {}
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], 1.0)
                if not readable:
                    continue
                payload, source = sock.recvfrom(MAX_REPLY_BYTES + 1)
            except OSError:
                continue
            result = parse_cbdp_reply(payload, source[0]) or parse_reply(
                payload,
                source[0],
            )
            if result is None:
                continue
            now = time.monotonic()
            key = f"{result.device_id}:{result.host}:{result.port}"
            # Devices repeat announcements to survive a lost datagram; one dial
            # per device per window is enough.
            if now - seen.get(key, 0.0) < PRESENCE_DEDUPE_SECONDS:
                continue
            seen[key] = now
            try:
                self.on_result(result)
            except Exception as exc:
                self.logger(f"CodexBridge: presence handler failed: {exc}")

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None
