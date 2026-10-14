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

    def test_page_has_device_and_speech_sections(self):
        self.assertIn("设备", SETTINGS_HTML)
        self.assertIn("语音识别", SETTINGS_HTML)
        self.assertIn("API Key", SETTINGS_HTML)
        self.assertIn("/api/admin/settings/speech", SETTINGS_HTML)

    def test_page_uses_stable_desktop_layout(self):
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1.08fr);", SETTINGS_HTML)
        self.assertIn(".general-panel { grid-column: 1; grid-row: 1; }", SETTINGS_HTML)
        self.assertIn(".speech-panel { grid-column: 1; grid-row: 2; }", SETTINGS_HTML)
        self.assertIn(".devices-panel {", SETTINGS_HTML)
        self.assertIn("grid-column: 2;", SETTINGS_HTML)
        self.assertIn("grid-row: 1 / span 2;", SETTINGS_HTML)

    def test_page_collapses_to_one_column_on_narrow_screens(self):
        self.assertIn(".general-panel { grid-column: 1; grid-row: auto; order: 1; }", SETTINGS_HTML)
        self.assertIn(".devices-panel { grid-column: 1; grid-row: auto; align-self: auto; order: 2; }", SETTINGS_HTML)
        self.assertIn(".speech-panel { grid-column: 1; grid-row: auto; order: 3; }", SETTINGS_HTML)
        self.assertIn(".general-list, .source-options { grid-template-columns: 1fr; }", SETTINGS_HTML)
