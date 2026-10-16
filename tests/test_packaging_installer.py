from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerScriptTests(unittest.TestCase):
    def test_installer_force_closes_running_bridge_before_copy(self) -> None:
        script = (ROOT / "packaging" / "CodexBridge.iss").read_text(encoding="utf-8-sig")

        self.assertIn("CloseApplications=yes", script)
        self.assertIn("function PrepareToInstall", script)
        self.assertIn("taskkill.exe", script)
        self.assertIn("/F /T /IM CodexBridge.exe", script)

    def test_repair_code_tracks_the_installer_app_id(self) -> None:
        from codexbridge import uninstall_record

        script = (ROOT / "packaging" / "CodexBridge.iss").read_text(encoding="utf-8-sig")

        self.assertIn("AppId={" + uninstall_record.APP_ID, script)


if __name__ == "__main__":
    unittest.main()
