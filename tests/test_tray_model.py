import tempfile
import unittest
from pathlib import Path

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.tray import build_menu_model, make_tray_menu


class FakeMenuItem:
    def __init__(
        self,
        text,
        action,
        checked=None,
        radio=False,
        default=False,
        visible=True,
    ):
        self.text = text
        self.action = action
        self.checked = checked
        self.radio = radio
        self.default = default
        self.visible = visible

    def __call__(self, icon):
        return self.action(icon, self)


class FakeMenu:
    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if len(self._items) == 1 and callable(self._items[0]):
            return tuple(self._items[0]())
        return self._items

    @property
    def visible_items(self):
        return tuple(item for item in self.items if item.visible)

    def __call__(self, icon):
        for item in self.items:
            if item.default:
                return item(icon)
        return None


class FakePystray:
    Menu = FakeMenu
    MenuItem = FakeMenuItem


class TrayModelTest(unittest.TestCase):
    def test_menu_has_only_restart_about_and_exit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert(
            "d1", model="dotii", name="桌面屏", supported=["status", "text"]
        )
        model = build_menu_model(registry, BridgeHub(registry))
        self.assertEqual([item.label for item in model], ["重启", "关于", "退出"])
        self.assertEqual(
            [item.action_id for item in model],
            ["restart", "about", "exit"],
        )

    def test_tray_has_no_device_submenus(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert(
            "d1", model="dotii", name="桌面屏", supported=["status", "text"]
        )
        model = build_menu_model(registry, BridgeHub(registry))
        self.assertFalse(any(item.kind == "submenu" for item in model))

    def test_tray_menu_exposes_three_items(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        menu = make_tray_menu(
            FakePystray,
            registry,
            BridgeHub(registry),
            lambda _action_id: (lambda *_args: None),
        )
        self.assertEqual(
            [item.text for item in menu.visible_items],
            ["重启", "关于", "退出"],
        )

    def test_tray_left_click_opens_settings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        actions = []
        menu = make_tray_menu(
            FakePystray,
            registry,
            BridgeHub(registry),
            lambda action_id: (
                lambda *_args, action_id=action_id: actions.append(action_id)
            ),
        )
        menu(object())
        self.assertEqual(actions, ["open_settings"])
