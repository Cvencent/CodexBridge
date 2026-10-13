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


@dataclass
class MenuItem:
    label: str
    kind: str
    action_id: str | None = None
    checked: bool | None = None
    children: list[MenuItem] = field(default_factory=list)


def build_menu_model(
    registry: DeviceRegistry | None = None,
    hub: BridgeHub | None = None,
) -> list[MenuItem]:
    del registry, hub
    return [
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
    def entries():
        for item in build_menu_model(registry, hub):
            yield pystray.MenuItem(
                item.label,
                make_dispatch(item.action_id),
                checked=None,
                radio=False,
                default=False,
            )
        yield pystray.MenuItem(
            "打开设置",
            make_dispatch("open_settings"),
            visible=False,
            default=True,
        )

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


def run(
    hub: BridgeHub,
    registry: DeviceRegistry,
    manager=None,
    on_restart=None,
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
            elif action_id == "exit":
                icon.stop()

        return dispatch

    icon = pystray.Icon(
        "CodexBridge",
        image,
        "CodexBridge",
        make_tray_menu(pystray, registry, hub, make_dispatch),
    )
    icon.run()
