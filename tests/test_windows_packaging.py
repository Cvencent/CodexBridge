import re
import unittest
from pathlib import Path

import codexbridge


ROOT = Path(__file__).resolve().parents[1]


class WindowsPackagingTest(unittest.TestCase):
    def test_release_pipeline_files_exist(self):
        expected = [
            "packaging/CodexBridge.spec",
            "packaging/CodexBridge.iss",
            "packaging/build_windows.ps1",
            ".github/workflows/release-windows.yml",
        ]
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_installer_and_workflow_use_current_version(self):
        version = codexbridge.VERSION
        for relative in ("packaging/build_windows.ps1", "packaging/CodexBridge.iss"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(version, text, relative)

        workflow = (
            ROOT / ".github/workflows/release-windows.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("CodexBridge-Setup-", workflow)
        self.assertIn("gh release upload", workflow)

    def test_installer_configuration_is_valid(self):
        installer = (ROOT / "packaging/CodexBridge.iss").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("SetupIconFile={#SourceDir}\\CodexBridge.exe", installer)
        self.assertNotIn("--uninstall-stop", installer)

    def test_workflow_installs_inno_setup_before_build(self):
        workflow = (
            ROOT / ".github/workflows/release-windows.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("choco install innosetup", workflow)
    def test_installer_defaults_to_simplified_chinese(self):
        installer = (ROOT / "packaging/CodexBridge.iss").read_text(encoding="utf-8")
        self.assertIn("[Languages]", installer)
        self.assertIn("Name: \"chinesesimp\"", installer)
        self.assertNotIn("Name: \"english\"", installer)
        self.assertIn("创建桌面快捷方式", installer)
        self.assertIn("附加任务", installer)
        self.assertIn("启动 CodexBridge", installer)

    def test_declared_versions_match(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), codexbridge.VERSION)
