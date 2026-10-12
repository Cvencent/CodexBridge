from __future__ import annotations

import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.protocol.capabilities import FEATURES


@dataclass
class MenuItem:
    label: str
    kind: str
    action_id: str | None = None
    checked: bool | None = None
    children: list[MenuItem] = field(default_factory=list)


def build_menu_model(registry: DeviceRegistry, hub: BridgeHub) -> list[MenuItem]:
    model = [
        MenuItem("打开管理中心", "action", "open_admin"),
        MenuItem("打开日志目录", "action", "open_logs"),
    ]
    sessions = getattr(hub, "_sessions", {})
    records = sorted(
        registry._devices.values(),
        key=lambda record: record.device_id,
    )
    for record in records:
        title = record.name or record.device_id
        online = record.device_id in sessions
        children = [
            MenuItem("配对设备...", "action", f"pair_device:{record.device_id}"),
            MenuItem("手动添加 IP...", "action", f"manual_add:{record.device_id}"),
        ]
        for feature in FEATURES:
            children.append(
                MenuItem(
                    feature,
                    "check",
                    f"toggle_capability:{record.device_id}:{feature}",
                    feature in record.enabled,
                )
            )
        children.append(
            MenuItem("移除设备", "action", f"remove_device:{record.device_id}")
        )
        state = "在线" if online else "离线"
        model.append(
            MenuItem(
                f"{title} ({record.model}) - {state}",
                "submenu",
                children=children,
            )
        )
    model.append(MenuItem("退出", "action", "exit"))
    return model


def run(hub: BridgeHub, registry: DeviceRegistry) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            'Tray dependencies are missing; install with pip install -e ".[tray]".'
        ) from error

    image = Image.new("RGB", (64, 64), "#1f2933")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 14, 50, 50), fill="#f4c95d")

    def toggle(action_id: str) -> None:
        _, device_id, feature = action_id.split(":", 2)
        record = registry.get(device_id)
        if record is None:
            return
        enabled = set(record.enabled)
        if feature in enabled:
            enabled.remove(feature)
        else:
            enabled.add(feature)
        registry.set_enabled(device_id, enabled)
        registry.save()
        hub.push_capabilities(device_id)

    def remove(action_id: str) -> None:
        device_id = action_id.split(":", 1)[1]
        if registry.remove(device_id):
            registry.save()
            hub.detach(device_id)

    def make_dispatch(action_id: str | None):
        def dispatch(icon, _item) -> None:
            if action_id is None:
                return
            if action_id.startswith("toggle_capability:"):
                toggle(action_id)
            elif action_id.startswith("remove_device:"):
                remove(action_id)
            elif action_id == "open_admin":
                webbrowser.open("http://127.0.0.1:8787/")
            elif action_id == "open_logs":
                data_dir = Path(
                    os.environ.get("LOCALAPPDATA", str(Path.home()))
                ) / "CodexBridge"
                data_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(data_dir)
            elif action_id == "exit":
                icon.stop()

        return dispatch

    def make_menu(items: list[MenuItem]) -> pystray.Menu:
        entries = []
        for item in items:
            if item.kind == "submenu":
                entries.append(
                    pystray.MenuItem(
                        item.label,
                        make_menu(item.children),
                    )
                )
                continue
            checked = None
            if item.kind == "check":
                checked = lambda _item, action_id=item.action_id: (
                    _capability_checked(registry, action_id)
                )
            entries.append(
                pystray.MenuItem(
                    item.label,
                    make_dispatch(item.action_id),
                    checked=checked,
                    radio=False,
                )
            )
        return pystray.Menu(*entries)

    icon = pystray.Icon(
        "CodexBridge",
        image,
        "CodexBridge",
        make_menu(build_menu_model(registry, hub)),
    )
    icon.run()


def _capability_checked(registry: DeviceRegistry, action_id: str | None) -> bool:
    if action_id is None:
        return False
    _, device_id, feature = action_id.split(":", 2)
    record = registry.get(device_id)
    return record is not None and feature in record.enabled
