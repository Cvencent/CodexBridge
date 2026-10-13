import unittest

from codexbridge import startup


class FakeKey:
    def __init__(self, values=None):
        self.values = {} if values is None else dict(values)


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values=None):
        self.key = FakeKey(values)
        self.deleted = []
        self.opened = 0
        self.created = 0

    def OpenKey(self, root, path, reserved=0, access=0):
        self.opened += 1
        return self.key

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        self.created += 1
        return self.key

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = value

    def DeleteValue(self, key, name):
        self.deleted.append(name)
        key.values.pop(name, None)

    def CloseKey(self, key):
        return None


class StartupTest(unittest.TestCase):
    def test_reports_disabled_when_value_is_missing(self):
        winreg = FakeWinreg()
        self.assertFalse(startup.is_startup_enabled(winreg))

    def test_enabling_writes_current_user_run_entry(self):
        winreg = FakeWinreg()
        startup.set_startup_enabled(True, winreg)
        self.assertTrue(startup.is_startup_enabled(winreg))
        command = winreg.key.values[startup.RUN_VALUE_NAME]
        self.assertIn("codexbridge", command.lower())
        self.assertGreater(winreg.created, 0)

    def test_disabling_removes_run_entry(self):
        winreg = FakeWinreg({startup.RUN_VALUE_NAME: '"old.exe"'})
        startup.set_startup_enabled(False, winreg)
        self.assertFalse(startup.is_startup_enabled(winreg))
        self.assertIn(startup.RUN_VALUE_NAME, winreg.deleted)
