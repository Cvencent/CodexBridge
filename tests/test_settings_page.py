import unittest

from codexbridge.settings_page import SETTINGS_HTML


class SettingsPageTest(unittest.TestCase):
    def test_page_defaults_to_simplified_chinese(self):
        self.assertIn("<html lang=\"zh-CN\">", SETTINGS_HTML)
        self.assertIn("CodexBridge 管理中心", SETTINGS_HTML)

    def test_page_has_device_and_speech_sections(self):
        self.assertIn("设备", SETTINGS_HTML)
        self.assertIn("语音识别", SETTINGS_HTML)
        self.assertIn("API Key", SETTINGS_HTML)
        self.assertIn("/api/admin/settings/speech", SETTINGS_HTML)
