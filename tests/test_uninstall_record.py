from __future__ import annotations

import unittest

from codexbridge import uninstall_record


class FakeKey:
    def __init__(self, values=None):
        self.values = {} if values is None else dict(values)


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(self, keys=None, denied=()):
        self.keys = {
            (root, path): FakeKey(values)
            for (root, path), values in (keys or {}).items()
        }
        self.denied = set(denied)
        self.written = []

    def OpenKey(self, root, path, reserved=0, access=0):
        if (root, path) in self.denied:
            raise PermissionError(path)
        if (root, path) not in self.keys:
            raise FileNotFoundError(path)
        return self.keys[(root, path)]

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = value
        self.written.append((name, kind, value))

    def CloseKey(self, key):
        return None


def path_for(suffix="_is1"):
    return uninstall_record.UNINSTALL_ROOT + "\\" + uninstall_record.APP_ID + suffix


class SyncUninstallRecordTest(unittest.TestCase):
    def test_missing_entry_is_left_alone(self):
        winreg = FakeWinreg()

        self.assertEqual(uninstall_record.sync_uninstall_record(winreg=winreg), [])
        self.assertEqual(winreg.written, [])

    def test_matching_version_needs_no_write(self):
        winreg = FakeWinreg({("HKCU", path_for()): {"DisplayVersion": "1.4.1"}})

        self.assertEqual(
            uninstall_record.sync_uninstall_record(version="1.4.1", winreg=winreg),
            [],
        )
        self.assertEqual(winreg.written, [])

    def test_stale_version_is_rewritten(self):
        path = path_for()
        winreg = FakeWinreg(
            {
                ("HKCU", path): {
                    "DisplayVersion": "1.3.7",
                    "InstallDate": "20261014",
                    "VersionMajor": 1,
                    "VersionMinor": 3,
                }
            }
        )

        repaired = uninstall_record.sync_uninstall_record(
            version="1.4.1",
            winreg=winreg,
            today="20261016",
        )

        self.assertEqual(repaired, ["HKCU\\" + path])
        values = winreg.keys[("HKCU", path)].values
        self.assertEqual(values["DisplayVersion"], "1.4.1")
        self.assertEqual(values["InstallDate"], "20261016")
        self.assertEqual(values["VersionMajor"], 1)
        self.assertEqual(values["VersionMinor"], 4)

    def test_missing_display_version_is_filled_in(self):
        path = path_for()
        winreg = FakeWinreg({("HKCU", path): {"DisplayName": "CodexBridge"}})

        repaired = uninstall_record.sync_uninstall_record(
            version="1.4.1",
            winreg=winreg,
            today="20261016",
        )

        self.assertEqual(repaired, ["HKCU\\" + path])
        self.assertEqual(
            winreg.keys[("HKCU", path)].values["DisplayVersion"], "1.4.1"
        )

    def test_per_machine_hive_write_denied_is_ignored(self):
        user_path = path_for()
        machine_path = path_for("")
        winreg = FakeWinreg(
            {
                ("HKCU", user_path): {"DisplayVersion": "1.3.7"},
                ("HKLM", machine_path): {"DisplayVersion": "1.3.7"},
            },
            denied={("HKLM", machine_path)},
        )

        repaired = uninstall_record.sync_uninstall_record(
            version="1.4.1",
            winreg=winreg,
            today="20261016",
        )

        self.assertEqual(repaired, ["HKCU\\" + user_path])
        self.assertEqual(
            winreg.keys[("HKLM", machine_path)].values["DisplayVersion"], "1.3.7"
        )

    def test_legacy_key_without_suffix_is_repaired(self):
        path = path_for("")
        winreg = FakeWinreg({("HKCU", path): {"DisplayVersion": "1.3.7"}})

        repaired = uninstall_record.sync_uninstall_record(
            version="1.4.1",
            winreg=winreg,
            today="20261016",
        )

        self.assertEqual(repaired, ["HKCU\\" + path])
        self.assertEqual(
            winreg.keys[("HKCU", path)].values["DisplayVersion"], "1.4.1"
        )


if __name__ == "__main__":
    unittest.main()