"""Read files that another process replaces atomically.

Codex desktop rewrites ``~/.codex/.codex-global-state.json`` by writing a
sibling temporary file and renaming it over the destination. On Windows a
rename-over needs every open handle on the destination to allow deletion, and a
plain ``open(path, "rb")`` does not. A reader as short-lived as a stat+read can
therefore make the desktop's own write fail with
``EPERM: operation not permitted, rename ...``.

Reading through CreateFileW with read/write/delete sharing keeps the desktop's
atomic replace working whether or not Bridge is reading the file at that
instant.
"""

from __future__ import annotations

import ctypes
import io
import os
from pathlib import Path

_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x00000007  # FILE_SHARE_READ | WRITE | DELETE
_OPEN_EXISTING = 3
_INVALID = ctypes.c_void_p(-1).value


def _open_shared_fd(path: Path) -> int:
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
    handle = kernel32.CreateFileW(
        str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, 0, None
    )
    if handle in (None, 0, _INVALID):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))
    import msvcrt

    return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)


def read_bytes_shared(path: Path | str) -> bytes:
    """Read *path* without blocking another process's atomic replace."""

    target = Path(path)
    if os.name != "nt":
        return target.read_bytes()
    fd = _open_shared_fd(target)
    with io.open(fd, "rb", closefd=True) as handle:
        return handle.read()


def read_text_shared(path: Path | str, encoding: str = "utf-8") -> str:
    """Read *path* as text without blocking another process's replace."""

    return read_bytes_shared(path).decode(encoding)
