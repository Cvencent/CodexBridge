"""Turn liveness read straight from the rollout log Codex is appending to.

The desktop owns its threads, and a second app-server process cannot see their
in-flight state: ``status.type`` stays ``notLoaded`` and ``recencyAt`` is frozen
at the moment the turn started, so a long turn looks dead from the outside while
it is working. Both the phone companion and the device collector need the same
answer to one question -- "is a turn really running right now?" -- so they read
it from the same place: the rollout log the desktop itself writes.

Two rules learned the hard way and kept here:

* ``thread_settings_applied`` records are appended by a resume or a read-only
  ``thread/read``. They carry a fresh timestamp and no turn work, so counting
  them flickers long-finished tasks into "working".
* The file mtime is *not* required to agree with the record timestamps. On
  Windows a rollout that is still growing can report a frozen mtime, and gating
  on it hid live turns for hours.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# How much of the tail to read. Turn events live at the end, and a rollout for a
# long conversation can pass 100 MB, so the tail is both enough and cheap.
LOG_TAIL_BYTES = 131072
# A turn counts as running while the log shows real work this recently. The
# desktop appends on every item, so this only has to cover one tool call.
LOG_ACTIVE_SECONDS = 150.0
# Rollout tails are read for every listed thread on every collector pass; the
# cache keeps that off the hot path without making the answer stale.
LOG_CACHE_SECONDS = 8.0
# How many days of rollout folders to search when a listing carries no path.
ROLLOUT_SCAN_DAYS = 3

# Rollout records the app-server appends when it merely loads or resumes a
# thread: a fresh timestamp that describes no turn progress.
_BOOKKEEPING_EVENT_TYPES = {"thread_settings_applied"}
_TURN_EVENT_TYPES = {"task_started", "task_complete", "turn_aborted"}

_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
_ROLLOUT_STEM_RE = re.compile(
    r"^rollout-(?:\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-)?(.+)$"
)


def parse_stamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def line_stamp(line: str) -> float | None:
    """Read the record ``timestamp`` without parsing the whole payload."""
    match = _TIMESTAMP_RE.search(line)
    if match is None:
        return None
    return parse_stamp(match.group(1))


def is_bookkeeping_line(line: str) -> bool:
    """Return True for a rollout record that only refreshes thread settings."""
    if '"event_msg"' not in line:
        return False
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return str(payload.get("type") or "") in _BOOKKEEPING_EVENT_TYPES


def log_activity(line: str) -> tuple[float, str] | None:
    """Return ``(stamp, kind)`` for a turn-level event line, else None."""
    if '"event_msg"' not in line:
        return None
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    kind = str(payload.get("type") or "")
    if kind not in _TURN_EVENT_TYPES:
        return None
    stamp = parse_stamp(str(record.get("timestamp") or ""))
    if stamp is None:
        return None
    if kind == "task_complete" and payload.get("error"):
        kind = "task_failed"
    return stamp, kind


def rollout_thread_id(filename: str) -> str:
    """Extract the thread id from a ``rollout-*.jsonl`` filename."""
    name = str(filename or "")
    if not name.endswith(".jsonl"):
        return ""
    match = _ROLLOUT_STEM_RE.match(name[:-len(".jsonl")])
    return match.group(1) if match else ""


def read_file_tail(path: Path, size: int) -> bytes:
    """Read the last ``LOG_TAIL_BYTES`` of a file another process is writing.

    Codex holds its rollout open with sharing flags that reject a plain
    ``open(path, "rb")`` on Windows, which used to make readers see no activity
    at all for a thread being written to right then.
    """
    start = size - LOG_TAIL_BYTES if size > LOG_TAIL_BYTES else 0
    if os.name != "nt":
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
            return handle.read()
    import msvcrt

    try:
        descriptor = msvcrt.open_osfhandle(
            win32_open_shared(str(path), start),
            os.O_RDONLY | os.O_BINARY,
        )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            return stream.read()
    except OSError:
        return b""


def win32_open_shared(path: str, offset: int) -> int:
    """Open a file for shared reading on Windows and return a raw HANDLE."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(
        wintypes.LPCWSTR(path),
        wintypes.DWORD(GENERIC_READ),
        wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(0),
        wintypes.HANDLE(None),
    )
    invalid = wintypes.HANDLE(-1).value
    if not handle or handle == invalid:
        raise OSError(ctypes.get_last_error(), "cannot open rollout for shared read", path)
    if offset and kernel32.SetFilePointer(
        wintypes.HANDLE(handle), wintypes.LONG(offset), None, wintypes.DWORD(0)
    ) == 0xFFFFFFFF:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise OSError(ctypes.get_last_error(), "cannot seek rollout", path)
    return handle


def read_log_tail(path: Path) -> tuple[float, str, float, float, float] | None:
    """Return ``(newest, last_turn_event, event_stamp, written_at, real_activity)``.

    ``real_activity`` is the newest stamp from a record that reflects actual turn
    work, so callers can tell "a turn is running" apart from "the file was
    merely touched by a resume". Returns None when the file is unreadable or the
    tail carries no timestamp at all.
    """
    try:
        stat = path.stat()
        size = stat.st_size
        blob = read_file_tail(path, size)
    except OSError:
        return None
    # The log records its own timestamps, but a stale tail can carry
    # fresh-looking stamps long after the writer stopped. The file mtime is a
    # fallback for that case, never the primary gate.
    written_at = float(stat.st_mtime)
    newest = 0.0
    real_activity = 0.0
    kind = ""
    kind_stamp = 0.0
    with_timestamps = 0
    for raw in blob.splitlines():
        line = raw.decode("utf-8", "replace")
        if '"timestamp"' not in line:
            continue
        activity = log_activity(line)
        if activity is not None:
            stamp, kind = activity
            kind_stamp = stamp
        else:
            stamp = line_stamp(line)
        if stamp is None:
            continue
        with_timestamps += 1
        if stamp > newest:
            newest = stamp
        if not is_bookkeeping_line(line) and stamp > real_activity:
            real_activity = stamp
    if with_timestamps == 0:
        return None
    return newest, kind, kind_stamp, written_at, real_activity


def status_from_activity(
    activity: tuple[float, str, float, float, float] | None,
    now: float | None = None,
) -> str:
    """Return "working" while the log shows recent real turn work, else "".

    A finished turn keeps its outcome record, so ``task_complete``/
    ``turn_aborted`` newer than the last real activity settles the thread down.
    """
    if activity is None:
        return ""
    newest, kind, kind_stamp, written_at, real_activity = activity
    if newest <= 0:
        return ""
    reference = time.time() if now is None else float(now)
    if kind == "task_complete" and kind_stamp >= real_activity - 1.0:
        return "completed"
    if kind == "turn_aborted" and kind_stamp >= real_activity - 1.0:
        return "idle"
    if real_activity <= 0:
        return "working" if reference - written_at <= LOG_ACTIVE_SECONDS else ""
    return "working" if reference - real_activity <= LOG_ACTIVE_SECONDS else ""


class RolloutActivity:
    """Cached rollout-log reader shared by the phone API and the device collector.

    Lookups prefer the path the listing already carries; the day-folder scan is
    only the fallback, so a caller that has a path never walks the session tree.
    """

    def __init__(self, sessions_root: Path | str | None = None) -> None:
        self._sessions_root = (
            Path(sessions_root)
            if sessions_root is not None
            else Path.home() / ".codex" / "sessions"
        )
        self._cache: dict[str, tuple[float, tuple[float, str, float, float, float] | None]] = {}
        self._lock = threading.Lock()

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def find_path(self, thread_id: str) -> Path | None:
        key = str(thread_id or "")
        if not key:
            return None
        for offset in range(ROLLOUT_SCAN_DAYS):
            day = datetime.now() - timedelta(days=offset)
            folder = self._sessions_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            try:
                entries = list(folder.glob(f"*{key}.jsonl"))
            except OSError:
                continue
            if entries:
                try:
                    return max(entries, key=lambda item: item.stat().st_mtime)
                except OSError:
                    return entries[0]
        return None

    def activity_for_thread(self, thread_id: str) -> tuple[float, str, float, float, float] | None:
        return self._activity(str(thread_id or ""), None)

    def activity_for_path(self, path: Any) -> tuple[float, str, float, float, float] | None:
        text = str(path or "")
        if not text:
            return None
        return self._activity(f"path:{text}", text)

    def activity(
        self, item: dict[str, Any] | None
    ) -> tuple[float, str, float, float, float] | None:
        """Activity for one ``thread/list`` / ``thread/read`` entry."""
        if not isinstance(item, dict):
            return None
        thread_id = str(item.get("id") or "")
        value = item.get("path")
        if isinstance(value, (str, Path)) and str(value):
            found = self._activity(f"path:{value}", str(value))
            if found is not None:
                return found
        return self._activity(thread_id, None)

    def is_working(self, item: dict[str, Any] | None, now: float | None = None) -> bool:
        return status_from_activity(self.activity(item), now) == "working"

    def _activity(
        self, key: str, explicit_path: str | None
    ) -> tuple[float, str, float, float, float] | None:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] <= LOG_CACHE_SECONDS:
                return cached[1]
        if explicit_path is not None:
            path = Path(explicit_path)
        else:
            path = self.find_path(key)
        found = read_log_tail(path) if path is not None else None
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = (now, found)
        return found
