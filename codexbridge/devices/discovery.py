from __future__ import annotations

import json
import select
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass

DISCOVERY_PORT = 8764
DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER"
MAX_REPLY_BYTES = 1024


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
