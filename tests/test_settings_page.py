import unittest

from codexbridge.settings_page import SETTINGS_HTML


class SettingsPageTest(unittest.TestCase):
    def test_page_defaults_to_simplified_chinese(self):
        self.assertIn("<html lang=\"zh-CN\">", SETTINGS_HTML)
        self.assertIn("CodexBridge 管理中心", SETTINGS_HTML)

    def test_page_has_general_settings(self):
        self.assertIn("常规设置", SETTINGS_HTML)
        self.assertIn("开机自启动", SETTINGS_HTML)
        self.assertIn("打开日志目录", SETTINGS_HTML)
        self.assertIn("/api/admin/settings/general", SETTINGS_HTML)

    def test_page_can_view_and_change_companion_port(self):
        self.assertIn("端口状态", SETTINGS_HTML)
        self.assertIn('id="companion-port"', SETTINGS_HTML)
        self.assertIn('action: "set_port"', SETTINGS_HTML)
        self.assertIn("8790 已保留", SETTINGS_HTML)

    def test_port_rows_have_stable_edit_actions_and_fixed_state_text(self):
        self.assertIn("port-actions-cell", SETTINGS_HTML)
        self.assertIn('fixed: "设备定义"', SETTINGS_HTML)
        self.assertIn("save.addEventListener", SETTINGS_HTML)

    def test_page_can_configure_intermediate_event_visibility(self):
        self.assertIn("显示中间事件", SETTINGS_HTML)
        self.assertIn('id="companion-show-intermediate"', SETTINGS_HTML)
        self.assertIn("show_intermediate_events: toggle.checked", SETTINGS_HTML)

    def test_page_has_device_and_speech_sections(self):
        self.assertIn("设备", SETTINGS_HTML)
        self.assertIn("语音识别", SETTINGS_HTML)
        self.assertIn("API Key", SETTINGS_HTML)
        self.assertIn("/api/admin/settings/speech", SETTINGS_HTML)

    def test_page_has_one_unified_device_center(self):
        self.assertEqual(SETTINGS_HTML.count('id="device-list"'), 1)
        self.assertEqual(SETTINGS_HTML.count('id="companion-phones"'), 0)
        self.assertIn("连接与发现", SETTINGS_HTML)
        self.assertIn("设备中心", SETTINGS_HTML)
        self.assertIn("data-device-filter=\"all\"", SETTINGS_HTML)
        self.assertIn("data-device-filter=\"phone\"", SETTINGS_HTML)
        self.assertIn("data-device-filter=\"hardware\"", SETTINGS_HTML)
        self.assertIn("data-device-filter=\"pending\"", SETTINGS_HTML)

    def test_device_center_has_registration_actions(self):
        self.assertIn("/api/admin/devices/approve", SETTINGS_HTML)
        self.assertIn("/api/admin/devices/reject", SETTINGS_HTML)
        self.assertIn("/api/admin/devices/revoke", SETTINGS_HTML)
        self.assertIn("批准", SETTINGS_HTML)
        self.assertIn("拒绝", SETTINGS_HTML)
        self.assertIn("撤销", SETTINGS_HTML)
        self.assertIn("registration_state", SETTINGS_HTML)

    def test_page_uses_stable_desktop_layout(self):
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1.08fr);", SETTINGS_HTML)
        self.assertIn(".general-panel { grid-column: 1; grid-row: 1; }", SETTINGS_HTML)
        self.assertIn(".companion-panel { grid-column: 1; grid-row: 2; }", SETTINGS_HTML)
        self.assertIn(".speech-panel { grid-column: 1; grid-row: 3; }", SETTINGS_HTML)
        self.assertIn(".devices-panel {", SETTINGS_HTML)
        self.assertIn("grid-column: 2;", SETTINGS_HTML)
        self.assertIn("grid-row: 1 / span 3;", SETTINGS_HTML)

    def test_page_collapses_to_one_column_on_narrow_screens(self):
        self.assertIn(".general-panel { grid-column: 1; grid-row: auto; order: 1; }", SETTINGS_HTML)
        self.assertIn(".companion-panel { grid-column: 1; grid-row: auto; order: 2; }", SETTINGS_HTML)
        self.assertIn(".devices-panel { grid-column: 1; grid-row: auto; align-self: auto; order: 3; }", SETTINGS_HTML)
        self.assertIn(".speech-panel { grid-column: 1; grid-row: auto; order: 4; }", SETTINGS_HTML)
        self.assertIn(".general-list, .source-options { grid-template-columns: 1fr; }", SETTINGS_HTML)
