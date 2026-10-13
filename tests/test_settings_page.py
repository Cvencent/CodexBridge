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

    def test_page_has_device_and_speech_sections(self):
        self.assertIn("设备", SETTINGS_HTML)
        self.assertIn("语音识别", SETTINGS_HTML)
        self.assertIn("API Key", SETTINGS_HTML)
        self.assertIn("/api/admin/settings/speech", SETTINGS_HTML)

    def test_page_uses_stable_desktop_grid_areas(self):
        self.assertIn("grid-template-areas:", SETTINGS_HTML)
        self.assertIn('"general general"', SETTINGS_HTML)
        self.assertIn('"devices speech"', SETTINGS_HTML)
        self.assertIn(".general-panel { grid-area: general; }", SETTINGS_HTML)
        self.assertIn(".devices-panel { grid-area: devices; }", SETTINGS_HTML)
        self.assertIn(".speech-panel { grid-area: speech; }", SETTINGS_HTML)

    def test_page_collapses_to_one_column_on_narrow_screens(self):
        self.assertIn('"general"\n          "devices"\n          "speech";', SETTINGS_HTML)
        self.assertIn(".general-list, .source-options { grid-template-columns: 1fr; }", SETTINGS_HTML)
