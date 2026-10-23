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



class VoskBundlingTests(unittest.TestCase):
    """Offline Vosk speech needs its native DLLs inside the frozen build.

    ``vosk`` loads ``libvosk.dll`` through cffi at runtime, so PyInstaller's
    static analysis cannot see it. Without an explicit collect the released
    build silently falls back to Windows System.Speech even though the offline
    model is installed.
    """

    def test_build_requirements_pin_vosk(self) -> None:
        requirements = (
            ROOT / "packaging" / "requirements-build.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("vosk", requirements.lower())

    def test_spec_collects_vosk_module_and_native_libraries(self) -> None:
        spec = (ROOT / "packaging" / "CodexBridge.spec").read_text(
            encoding="utf-8"
        )

        # collect_all returns (datas, binaries, hiddenimports); the binaries
        # list is what carries libvosk.dll and the MinGW runtime into the EXE.
        self.assertIn('collect_all("vosk")', spec)
        self.assertIn("binaries=list(vosk_binaries)", spec)
        self.assertIn("vosk_hiddenimports", spec)

    def test_build_fails_when_vosk_native_libraries_are_missing(self) -> None:
        script = (ROOT / "packaging" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )

        for name in ("libvosk.dll", "libstdc++-6.dll", "libgcc_s_seh-1.dll",
                     "libwinpthread-1.dll"):
            self.assertIn(name, script)
        self.assertIn("CArchiveReader", script)
        self.assertIn("missing bundled offline speech file", script)


if __name__ == "__main__":
    unittest.main()
