import tempfile
import unittest
from pathlib import Path

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.tray import build_menu_model, make_tray_menu


class FakeMenuItem:
    def __init__(self, text, action, checked=None, radio=False):
        self.text = text
        self.action = action
        self.checked = checked
        self.radio = radio


class FakeMenu:
    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if len(self._items) == 1 and callable(self._items[0]):
            return tuple(self._items[0]())
        return self._items


class FakePystray:
    Menu = FakeMenu
    MenuItem = FakeMenuItem


class TrayModelTest(unittest.TestCase):
    def test_menu_lists_each_device_with_capability_checks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert("d1", model="dotii", name="桌面屏", supported=["status", "text"])
        model = build_menu_model(registry, BridgeHub(registry))
        device_menu = next(
            item
            for item in model
            if item.kind == "submenu" and "桌面屏" in item.label
        )
        checks = [child for child in device_menu.children if child.kind == "check"]
        self.assertTrue(
            any(
                child.action_id == "toggle_capability:d1:status"
                for child in checks
            )
        )
        self.assertTrue(all(child.checked is not None for child in checks))

    def test_menu_is_rebuilt_when_devices_change(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        menu = make_tray_menu(
            FakePystray,
            registry,
            BridgeHub(registry),
            lambda _action_id: (lambda *_args: None),
        )

        self.assertFalse(
            any("桌面屏" in item.text for item in menu.items)
        )
        registry.upsert(
            "d1",
            model="dotii",
            name="桌面屏",
            supported=["status", "text"],
        )
        self.assertTrue(
            any("桌面屏" in item.text for item in menu.items)
        )
