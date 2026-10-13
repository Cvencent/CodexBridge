import os
import unittest
from pathlib import Path
from unittest import mock

from codexbridge.management import runtime_paths


class ManagementPathTest(unittest.TestCase):
    def test_windows_runtime_folder_matches_state_display(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}):
            with mock.patch.object(runtime_paths, "current_platform") as current_platform:
                current_platform.return_value.runtime_folder.return_value = Path(
                    r"C:\Users\test\AppData\Local\StateDisplay"
                )
                self.assertEqual(
                    runtime_paths.runtime_folder(),
                    Path(r"C:\Users\test\AppData\Local\StateDisplay"),
                )


if __name__ == "__main__":
    unittest.main()
