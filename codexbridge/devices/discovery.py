from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from codexbridge.net_gate import configure_exclusive

DISCOVERY_PORT = 8764

# Repeated announcements from the same endpoint collapse into one dial.
PRESENCE_DEDUPE_SECONDS = 5.0
DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER"
MAX_REPLY_BYTES = 1024

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
    return DiscoveryResult(
        host=host,
        device=device,
        device_id=device_id,
        name=name,
        port=port,
        capabilities=capabilities,
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
) -> list[DiscoveryResult]:
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
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))

        for target in target_hosts:
            try:
                sock.sendto(DISCOVERY_REQUEST, (target, DISCOVERY_PORT))
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
            result = parse_reply(payload, source[0])
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
            result = parse_reply(payload, source[0])
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
