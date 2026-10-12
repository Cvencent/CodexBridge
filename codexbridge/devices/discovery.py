from __future__ import annotations

import json
import select
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass

DISCOVERY_PORT = 8764
DISCOVERY_REQUEST = b"CODEXBRIDGE_DISCOVER_V2"
LEGACY_DISCOVERY_REQUEST = b"FOLOOS_DISCOVER_V1"
MAX_REPLY_BYTES = 1024


@dataclass(frozen=True)
class DiscoveryResult:
    host: str
    device: str
    device_id: str
    port: int
    protocol: int
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

    protocol = value.get("protocol", 1)
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        protocol = 1

    device = value.get("device")
    device = device if isinstance(device, str) and device else "unknown"
    device_id = value.get("device_id")
    device_id = (
        device_id
        if isinstance(device_id, str) and device_id
        else f"legacy:{host}"
    )
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    capabilities = [item for item in capabilities if isinstance(item, str)]
    return DiscoveryResult(
        host=host,
        device=device,
        device_id=device_id,
        port=port,
        protocol=protocol,
        capabilities=capabilities,
    )


def discover(
    timeout: float = 3.0,
    targets: Iterable[str] = ("255.255.255.255",),
) -> list[DiscoveryResult]:
    if timeout < 0:
        raise ValueError("timeout must not be negative")

    target_hosts = tuple(targets)
    if not target_hosts:
        return []

    deadline = time.monotonic() + timeout
    results: dict[tuple[str, str], DiscoveryResult] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))

        for target in target_hosts:
            for request in (DISCOVERY_REQUEST, LEGACY_DISCOVERY_REQUEST):
                try:
                    sock.sendto(request, (target, DISCOVERY_PORT))
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
