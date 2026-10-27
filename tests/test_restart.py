from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexbridge import __main__ as bridge_main


class RestartLauncherTests(unittest.TestCase):
    def test_restart_resets_pyinstaller_environment(self) -> None:
        calls = []

        def launcher(command, **kwargs):
            calls.append((command, kwargs))

        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            os.environ,
            {
                "_PYI_APPLICATION_HOME_DIR": r"C:\old-temp",
                "_PYI_ARCHIVE_FILE": r"C:\old.exe",
                "_PYI_PARENT_PROCESS_LEVEL": "1",
                "UNCHANGED": "yes",
            },
            clear=False,
        ):
            bridge_main._launch_restarted_process(Path(folder), popen=launcher)

        self.assertEqual(len(calls), 1)
        _, kwargs = calls[0]
        env = kwargs["env"]
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["UNCHANGED"], "yes")
        self.assertFalse(any(key.startswith("_PYI_") for key in env))
        self.assertTrue(kwargs["close_fds"])


if __name__ == "__main__":
    unittest.main()
