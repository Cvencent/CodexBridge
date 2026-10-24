"""Import the desktop Codex read state into the shared read marker.

The desktop keeps two facts about every local thread in
`~/.codex/.codex-global-state.json`:

* `electron-persisted-atom-state.thread-user-activity-times-v1` -- when the
  user last interacted with the thread in the desktop UI, as epoch
  milliseconds keyed by `["local","<thread_id>"]`.
* `electron-thread-read-state-v1.unreadByIdentity` -- the threads the desktop
  still shows as unread, grouped by signed-in identity and host.

A thread the desktop has tracked and that is absent from its unread set has
been opened and read on the computer. That is the same event a device reports
with `read_ack` and the phone reports through
`/api/companion/threads/<id>/read`, so it must advance the one shared read
marker. Without this import the desktop was the only client that could read a
task without retiring the badge, and the phone plus both ESPs kept showing a
completed task the user had already opened.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import unquote

from codexbridge.messages_store import MessageStore
from codexbridge.read_state import ReadStateStore
from codexbridge.win_shared import read_text_shared

# One JSON parse per changed file, not one per poll tick: the desktop state is
# large (hundreds of kilobytes) and is polled several times a second.
_state_cache: dict[
    str, tuple[tuple[int, int], tuple[tuple[str, int], ...]]
] = {}
_cache_lock = threading.Lock()


def desktop_state_path() -> Path:
    """Default location of the desktop Codex global state file."""

    return Path.home() / ".codex" / ".codex-global-state.json"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(read_text_shared(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _activity_thread_id(key: object) -> str:
    """Decode an activity map key such as `["local","<thread_id>"]`."""

    if isinstance(key, (list, tuple)):
        parts = list(key)
    elif isinstance(key, str):
        try:
            decoded = json.loads(key)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        parts = decoded if isinstance(decoded, (list, tuple)) else []
    else:
        return ""
    if len(parts) < 2:
        return ""
    if str(parts[0] or "").strip() != "local":
        return ""
    return str(parts[-1] or "").strip()


def _activity_seconds(activity: object) -> dict[str, int]:
    """Return `thread_id -> last desktop user activity` in whole seconds."""

    result: dict[str, int] = {}
    if not isinstance(activity, dict):
        return result
    for key, value in activity.items():
        thread_id = _activity_thread_id(key)
        if not thread_id:
            continue
        try:
            millis = int(value)
        except (TypeError, ValueError):
            continue
        if millis <= 0:
            continue
        seconds = millis // 1000
        if seconds > result.get(thread_id, 0):
            result[thread_id] = seconds
    return result


def _desktop_unread(state: dict) -> set[str]:
    """Threads any signed-in desktop identity still reports as unread."""

    read = state.get("electron-thread-read-state-v1")
    if not isinstance(read, dict):
        return set()
    by_identity = read.get("unreadByIdentity")
    unread: set[str] = set()
    if isinstance(by_identity, dict):
        for hosts in by_identity.values():
            if not isinstance(hosts, dict):
                continue
            for thread_ids in hosts.values():
                if not isinstance(thread_ids, list):
                    continue
                for thread_id in thread_ids:
                    value = str(thread_id or "").strip()
                    if value:
                        unread.add(value)
    legacy = read.get("legacyMigration")
    # The per-identity map is authoritative once present. The legacy snapshot
    # can contain threads that were already read and would otherwise pin them
    # as unread forever.
    if (
        (not isinstance(by_identity, dict) or not by_identity)
        and isinstance(legacy, dict)
    ):
        hosts = legacy.get("unreadThreadIdsByHostId")
        if isinstance(hosts, dict):
            for thread_ids in hosts.values():
                if not isinstance(thread_ids, list):
                    continue
                for thread_id in thread_ids:
                    value = str(thread_id or "").strip()
                    if value:
                        unread.add(value)
    return unread


def _desktop_read_state_available(state: dict) -> bool:
    read = state.get("electron-thread-read-state-v1")
    if not isinstance(read, dict):
        return False
    return isinstance(read.get("unreadByIdentity"), dict) or isinstance(
        read.get("legacyMigration"), dict
    )


def _desktop_known_threads(state: dict) -> set[str]:
    """Thread ids the desktop has explicitly written into its local UI state.

    Newer desktop builds often create a thread and remove it from
    ``unreadByIdentity`` without writing a legacy
    ``thread-user-activity-times-v1`` entry. These keys still prove the desktop
    knows the thread, so they let the shared read marker follow the visible
    blue-dot state instead of getting stuck forever.
    """

    atom = state.get("electron-persisted-atom-state")
    if not isinstance(atom, dict):
        return set()
    known: set[str] = set()
    prefixes = (
        "thread-reference-capability:",
        "thread-tab-routes-v1:",
        "thread-workspace-state-v1:",
    )
    for raw_key in atom:
        key = str(raw_key or "")
        for prefix in prefixes:
            if key.startswith(prefix):
                thread_id = key[len(prefix):].strip()
                if thread_id:
                    known.add(thread_id)
                break
        marker = "thread-client-id-v1:local%3A"
        if key.startswith(marker):
            thread_id = unquote(key[len(marker):]).strip()
            if thread_id:
                known.add(thread_id)
    return known


def _terminal_times(store: MessageStore | None) -> dict[str, int]:
    """Newest completed/failed result timestamp for each known thread."""

    if store is None:
        return {}
    result: dict[str, int] = {}
    for record in store.records():
        if record.kind != "result" or record.status not in {"completed", "failed"}:
            continue
        thread_id = str(record.thread_id or "")
        if not thread_id:
            continue
        stamp = int(record.created_at or 0)
        if stamp > result.get(thread_id, 0):
            result[thread_id] = stamp
    return result


def sync_desktop_read_state(
    read_state: ReadStateStore,
    path: Path | str | None = None,
    store: MessageStore | None = None,
) -> int:
    """Advance the shared marker for threads already read on the desktop.

    Returns the number of threads whose watermark moved. Safe to call on every
    poll tick: a file that has not changed since the previous call is skipped
    without parsing it again.

    A thread counts as desktop-read only when the desktop recorded user
    activity for it and it is no longer in the desktop unread set. Phone-only
    threads the desktop has never tracked stay untouched. The desktop state
    file must also be newer than the newest terminal result Bridge knows for
    that thread; otherwise the desktop has not classified the result yet and
    clearing it early would hide a notification the user has not seen.
    """

    state_path = Path(path) if path is not None else desktop_state_path()
    try:
        stat = state_path.stat()
    except OSError:
        return 0
    key = str(state_path)
    marker = (int(stat.st_mtime_ns), int(stat.st_size))
    terminal = _terminal_times(store)
    terminal_marker = tuple(sorted(terminal.items()))
    with _cache_lock:
        if _state_cache.get(key) == (marker, terminal_marker):
            return 0

    state = _read_json(state_path)
    if not state:
        # Remember the failed read as well, so a broken file is not reparsed on
        # every tick; a later write changes the mtime and retries.
        with _cache_lock:
            _state_cache[key] = (marker, terminal_marker)
        return 0

    activity = _activity_seconds(
        (state.get("electron-persisted-atom-state") or {}).get(
            "thread-user-activity-times-v1"
        )
        if isinstance(state.get("electron-persisted-atom-state"), dict)
        else None
    )
    unread = _desktop_unread(state)
    read_state_available = _desktop_read_state_available(state)
    known_threads = _desktop_known_threads(state)
    desktop_updated = float(stat.st_mtime)
    changed = 0
    for thread_id, latest in terminal.items():
        seconds = int(activity.get(thread_id, 0) or 0)
        desktop_tracked = bool(seconds) or thread_id in known_threads
        if not desktop_tracked or thread_id in unread:
            continue
        if not seconds and not read_state_available:
            # Without an explicit desktop read-state map, an absent activity
            # entry is not enough evidence to retire a phone-only thread.
            continue
        if latest and desktop_updated + 1.0 < latest:
            # The desktop has not written a decision for this result yet.
            continue
        watermark = max(seconds, latest)
        if read_state.thread_read_at(thread_id) >= watermark:
            continue
        read_state.mark_thread(thread_id, watermark, source="desktop")
        changed += 1

    with _cache_lock:
        _state_cache[key] = (marker, terminal_marker)
    return changed
