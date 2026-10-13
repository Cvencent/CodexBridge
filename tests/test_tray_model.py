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
        enabled=True,
    ):
        self.text = text
        self.action = action
        self.checked = checked
        self.radio = radio
        self.default = default
        self.visible = visible
        self.enabled = enabled

    def __call__(self, icon):
        return self.action(icon, self)


class FakeMenu:
    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if (
            len(self._items) == 1
            and callable(self._items[0])
            and not isinstance(self._items[0], FakeMenuItem)
        ):
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
    def build_registry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert(
            "d1", model="dotii", name="桌面屏", supported=["status", "text"]
        )
        return registry

    def test_menu_exposes_devices_and_system_actions(self):
        registry = self.build_registry()
        model = build_menu_model(registry, BridgeHub(registry))
        self.assertEqual(
            [item.label for item in model],
            ["设备", "打开设置", "打开管理中心", "重启", "关于", "退出"],
        )
        self.assertEqual(model[0].kind, "submenu")
        self.assertEqual(
            [item.action_id for item in model[1:]],
            ["open_settings", "open_admin", "restart", "about", "exit"],
        )

    def test_device_submenu_lists_each_device_and_capability(self):
        registry = self.build_registry()
        model = build_menu_model(registry, BridgeHub(registry))
        device_menu = model[0]
        self.assertEqual(device_menu.children[0].label, "桌面屏")
        capability_menu = device_menu.children[0].children[0]
        self.assertEqual(capability_menu.label, "能力")
        self.assertEqual(
            [item.label for item in capability_menu.children],
            ["status", "text"],
        )
        self.assertEqual(
            [item.checked for item in capability_menu.children],
            [True, True],
        )
        self.assertEqual(
            capability_menu.children[0].action_id,
            "capability:d1:status",
        )

    def test_tray_menu_exposes_device_submenu_and_system_actions(self):
        registry = self.build_registry()
        menu = make_tray_menu(
            FakePystray,
            registry,
            BridgeHub(registry),
            lambda _action_id: (lambda *_args: None),
        )
        self.assertEqual(
            [item.text for item in menu.visible_items],
            ["设备", "打开设置", "打开管理中心", "重启", "关于", "退出"],
        )
        self.assertEqual(
            [item.text for item in menu.visible_items[0].action.items],
            ["桌面屏"],
        )

    def test_tray_left_click_opens_settings(self):
        registry = self.build_registry()
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

    def test_device_menu_reflects_current_enabled_capabilities(self):
        registry = self.build_registry()
        model = build_menu_model(registry, BridgeHub(registry))
        capability = model[0].children[0].children[0].children[0]
        self.assertTrue(capability.checked)
        registry.set_enabled("d1", ["text"])
        updated = build_menu_model(registry, BridgeHub(registry))
        reversed_capability = updated[0].children[0].children[0].children[0]
        self.assertFalse(reversed_capability.checked)