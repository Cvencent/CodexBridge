"""Port validation and health reporting for the local management page."""

from __future__ import annotations

from codexbridge.companion.store import DEFAULT_PORT, LEGACY_PORT

ADMIN_PORT = 8788
DEVICE_DISCOVERY_PORT = 8764
DEVICE_SESSION_PORT = 8765
COMPANION_DISCOVERY_PORT = 8766
FIXED_PORTS = {
    ADMIN_PORT,
    DEVICE_DISCOVERY_PORT,
    DEVICE_SESSION_PORT,
    COMPANION_DISCOVERY_PORT,
}


def validate_companion_port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("port_invalid")
    if not 1 <= value <= 65535:
        raise ValueError("port_invalid")
    if value == LEGACY_PORT:
        raise ValueError("port_reserved")
    if value in FIXED_PORTS:
        raise ValueError("port_in_use")
    return value


def _status(
    key: str,
    label: str,
    protocol: str,
    port: int,
    state: str,
    purpose: str,
    *,
    editable: bool = False,
) -> dict:
    return {
        "key": key,
        "label": label,
        "protocol": protocol,
        "port": port,
        "state": state,
        "purpose": purpose,
        "editable": editable,
    }


def validate_discovery_port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("port_invalid")
    if not 1 <= value <= 65535:
        raise ValueError("port_invalid")
    if value == LEGACY_PORT:
        raise ValueError("port_reserved")
    if value in {ADMIN_PORT, DEVICE_DISCOVERY_PORT, DEVICE_SESSION_PORT}:
        raise ValueError("port_in_use")
    return value


def build_port_statuses(
    *,
    admin_port: int = ADMIN_PORT,
    companion_port: int = DEFAULT_PORT,
    discovery_port: int = COMPANION_DISCOVERY_PORT,
    companion_enabled: bool = True,
    companion_running: bool = False,
    discovery_running: bool = False,
    admin_running: bool = True,
) -> list[dict]:
    companion_state = "running" if companion_running else ("disabled" if not companion_enabled else "error")
    discovery_state = "running" if discovery_running else ("disabled" if not companion_enabled else "error")
    return [
        _status("device_discovery", "设备发现", "UDP", DEVICE_DISCOVERY_PORT, "fixed", "设备固件规定的发现端口"),
        _status("device_session", "设备会话", "TCP", DEVICE_SESSION_PORT, "fixed", "由设备发现应答上报"),
        _status("admin", "本机管理", "HTTP", admin_port, "running" if admin_running else "error", "仅允许本机访问"),
        _status("companion", "手机连接", "HTTP + SSE", companion_port, companion_state, "手机远程查看和操作 Codex", editable=True),
        _status("discovery", "手机发现", "UDP", discovery_port, discovery_state, "手机扫描电脑", editable=True),
        _status("reserved", "已保留", "TCP", LEGACY_PORT, "reserved", "被其他程序占用，CodexBridge 不会使用"),
    ]
