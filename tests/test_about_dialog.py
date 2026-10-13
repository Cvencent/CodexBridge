import threading
import unittest
from unittest import mock

from codexbridge import tray


class DialogTest(unittest.TestCase):
    def assert_shown_off_thread(self, show_dialog):
        caller = threading.current_thread()
        calls = []
        release = threading.Event()

        def fake_message_box(*args):
            calls.append(threading.current_thread())
            release.set()

        with mock.patch.object(
            tray.ctypes.windll.user32,
            "MessageBoxW",
            side_effect=fake_message_box,
        ) as message_box:
            show_dialog()
            self.assertTrue(release.wait(2), "dialog was not shown")

        self.assertEqual(message_box.call_count, 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0], caller)

    def test_about_dialog_runs_off_the_menu_callback_thread(self):
        self.assert_shown_off_thread(tray._show_about)
