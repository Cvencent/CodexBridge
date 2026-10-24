"""Windows shared-read helper tests."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import unittest
from pathlib import Path

from codexbridge import win_shared


class SharedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    def test_reads_regular_text_file(self) -> None:
        self.path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(win_shared.read_text_shared(self.path), '{"a": 1}')

    def test_reads_large_file_across_multiple_chunks(self) -> None:
        payload = {"rows": ["x" * 200 for _ in range(2000)]}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(json.loads(win_shared.read_text_shared(self.path)), payload)

    def test_missing_file_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            win_shared.read_text_shared(Path(self.temp.name) / "missing.json")

    @unittest.skipUnless(os.name == "nt", "share modes are Windows-only")
    def test_shared_read_grants_delete_sharing(self) -> None:
        """The shared handle must allow a concurrent delete/replace.

        Codex desktop rewrites ``.codex-global-state.json`` with a
        rename-over; that only succeeds when every open handle on the
        destination allows deletion. A plain ``open(path, "rb")`` does not, so
        Bridge could make the desktop's own write fail with
        ``EPERM ... rename``. This asserts delete sharing deterministically:
        while the shared read handle is open, a second open that requests
        DELETE access must succeed.
        """
        self.path.write_text("{}", encoding="utf-8")
        delete_access = 0x00010000
        share_all = 0x00000007
        open_existing = 3
        invalid = ctypes.c_void_p(-1).value

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        def can_delete() -> bool:
            handle = kernel32.CreateFileW(
                str(self.path),
                delete_access,
                share_all,
                None,
                open_existing,
                0,
                None,
            )
            if handle in (None, 0, invalid):
                return False
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return True

        fd = win_shared._open_shared_fd(self.path)
        try:
            self.assertTrue(can_delete())
        finally:
            os.close(fd)

        # A plain buffered open is the regression this fixes: it must *not*
        # grant delete sharing.
        plain = open(self.path, "rb")
        try:
            self.assertFalse(can_delete())
        finally:
            plain.close()


if __name__ == "__main__":
    unittest.main()
