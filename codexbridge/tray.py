from __future__ import annotations

import ctypes
import os
import webbrowser
from dataclasses import dataclass, field

from codexbridge import VERSION
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub

REPOSITORY_URL = "https://github.com/Cvencent/CodexBridge"
SETTINGS_URL = "http://127.0.0.1:8788/settings"
ADMIN_URL = "http://127.0.0.1:8787/"


@dataclass
class MenuItem:
    label: str
    kind: str
    action_id: str | None = None
    checked: bool | None = None
    children: list["MenuItem"] = field(default_factory=list)


def _device_label(record) -> str:
    name = record.name or record.device_id
    return name


def _device_menu(record, hub: BridgeHub) -> MenuItem:
    capabilities = sorted(record.supported)
    enabled = set(record.enabled)
    capability_items = [
        MenuItem(
            label=feature,
            kind="check",
            action_id=f"capability:{record.device_id}:{feature}",
            checked=feature in enabled,
        )
        for feature in capabilities
    ]
    children: list[MenuItem] = []
    if capability_items:
        children.append(MenuItem("能力", "submenu", children=capability_items))
    else:
        children.append(MenuItem("能力（设备未上报）", "info"))
    children.append(
        MenuItem("移除设备", "action", f"remove:{record.device_id}")
    )
    return MenuItem(_device_label(record), "submenu", children=children)


def build_menu_model(
    registry: DeviceRegistry | None = None,
    hub: BridgeHub | None = None,
) -> list[MenuItem]:
    registry = registry or DeviceRegistry(None)
    hub = hub or BridgeHub(registry)
    devices = [
        _device_menu(record, hub)
        for record in registry._devices.values()
    ]
    device_menu = MenuItem("设备", "submenu", children=devices)
    return [
        device_menu,
        MenuItem("打开设置", "action", "open_settings"),
        MenuItem("打开管理中心", "action", "open_admin"),
        MenuItem("重启", "action", "restart"),
        MenuItem("关于", "action", "about"),
        MenuItem("退出", "action", "exit"),
    ]


def make_tray_menu(
    pystray,
    registry: DeviceRegistry,
    hub: BridgeHub,
    make_dispatch,
):
    def menu_items(items):
        for item in items:
            if item.kind == "info":
                yield pystray.MenuItem(
                    item.label,
                    lambda *_args: None,
                    enabled=False,
                    visible=True,
                )
                continue
            if item.kind == "submenu":
                yield pystray.MenuItem(
                    item.label,
                    pystray.Menu(lambda children=item.children: tuple(menu_items(children))),
                    checked=None,
                    radio=False,
                    default=False,
                )
                continue
            yield pystray.MenuItem(
                item.label,
                make_dispatch(item.action_id),
                checked=(lambda _item, value=item.checked: value) if item.kind == "check" else None,
                radio=False,
                default=item.action_id == "open_settings",
            )

    def entries():
        return tuple(menu_items(build_menu_model(registry, hub)))

    return pystray.Menu(entries)

def about_text() -> str:
    return (
        "CodexBridge\n"
        f"版本 {VERSION}\n\n"
        f"{REPOSITORY_URL}"
    )


def _show_about() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            about_text(),
            "关于 CodexBridge",
            0x00000040,
        )
    except Exception:
        return


def _show_management_error() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "管理中心子进程多次异常退出，请重启 CodexBridge 后重试。",
            "管理中心不可用",
            0x00000010,
        )
    except Exception:
        return


def run(
    hub: BridgeHub,
    registry: DeviceRegistry,
    manager=None,
    on_restart=None,
    management_url: str | None = None,
    management_failed=None,
) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            'Tray dependencies are missing; install with pip install -e ".[tray]".'
        ) from error

    del manager
    image = Image.new("RGB", (64, 64), "#1f2933")
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 14, 50, 50), fill="#f4c95d")

    def make_dispatch(action_id: str | None):
        def dispatch(icon, _item) -> None:
            if action_id == "restart":
                if on_restart is not None:
                    on_restart()
                icon.stop()
            elif action_id == "about":
                _show_about()
            elif action_id == "open_settings":
                webbrowser.open(SETTINGS_URL)
            elif action_id == "open_admin":
                if management_failed is not None and management_failed():
                    _show_management_error()
                    return
                webbrowser.open(management_url or ADMIN_URL)
            elif action_id == "exit":
                icon.stop()
            elif action_id and action_id.startswith("capability:"):
                _, device_id, feature = action_id.split(":", 2)
                record = registry.get(device_id)
                if record is None:
                    return
                enabled = set(record.enabled)
                if feature in enabled:
                    enabled.discard(feature)
                else:
                    enabled.add(feature)
                registry.set_enabled(device_id, enabled)
                registry.save()
                hub.push_capabilities(device_id)
            elif action_id and action_id.startswith("remove:"):
                device_id = action_id.split(":", 1)[1]
                if registry.remove(device_id):
                    registry.save()
                    hub.detach(device_id)

        return dispatch

    icon = pystray.Icon(
        "CodexBridge",
        image,
        "CodexBridge",
        make_tray_menu(
            pystray,
            registry,
            hub,
            make_dispatch,
        ),
    )
    icon.run()
