"""Short-lived cache for read-only Codex queries shared by every device.

The phone list and the status snapshot each used to fan out into one
thread/read per row on every request, and the background thread watcher did it
again every two seconds. Those serial round trips queue on the single
app-server request lock, so a phone polling status could hold the lock long
enough to make an ESP32 session look disconnected and stall the local admin
API. Caching the reads briefly, with event-driven invalidation, keeps one
protocol responsive for every device at once.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

# Read-only methods worth caching. Anything that changes state never goes
# through this path, so a stale entry can only be a fraction of a second late.
CACHEABLE_METHODS = frozenset({
    "thread/list",
    "thread/read",
    "thread/loaded",
    "model/list",
})

DEFAULT_TTL_SECONDS = 2.0
# Kept as the old name so existing imports keep working.
READ_TTL_SECONDS = DEFAULT_TTL_SECONDS
# A thread whose turn reached a terminal state cannot change again until a
# new turn starts, so its body is worth holding much longer than a live one.
# A thread whose turn reached a terminal state cannot change again on its
# own, so its reads are worth holding longer than a live one. The window
# stays small because the desktop can start a new turn without telling this
# process, and only the rollout log would notice.
TERMINAL_READ_TTL_SECONDS = 30.0
LONG_TTL_METHODS = {"model/list": 60.0}
MAX_ENTRIES = 400


def cache_key(method: str, params: dict[str, Any] | None) -> str:
    if not params:
        return method
    try:
        encoded = json.dumps(params, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = repr(sorted(params.items(), key=lambda item: str(item[0])))
    return method + "|" + encoded[:300]


def thread_tag(params: dict[str, Any] | None) -> str:
    """Return the thread a cached read belongs to, for targeted invalidation."""
    if not isinstance(params, dict):
        return ""
    value = params.get("threadId") or params.get("thread_id") or ""
    return str(value)


class ReadCache:
    """TTL cache with one lock held only around the map, never the fetch."""

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        if ttl <= 0:
            raise ValueError("ttl")
        self.ttl = float(ttl)
        self._entries: dict[str, tuple[float, Any]] = {}
        self._threads: dict[str, str] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def ttl_for(self, method: str) -> float:
        return LONG_TTL_METHODS.get(method, self.ttl)

    def get(self, key: str) -> tuple[bool, Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or now >= entry[0]:
                if entry is not None:
                    self._entries.pop(key, None)
                self.misses += 1
                return False, None
            self.hits += 1
            return True, entry[1]

    def set(self, key: str, value: Any, ttl: float | None = None,
            thread_id: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            self._entries[key] = (now + (ttl if ttl is not None else self.ttl), value)
            if thread_id:
                self._threads[key] = thread_id
            if len(self._entries) > MAX_ENTRIES:
                self._prune_locked(now)

    def call(self, method: str, params: dict[str, Any] | None,
             factory: Callable[[], Any], ttl: float | None = None,
             refresh: bool = False) -> Any:
        """Return a cached value, or fetch once and store it.

        ``refresh`` is for the caller that decides whether a change is worth
        publishing at all: a reconciliation poll served from the cache would
        keep reporting the state it stored itself and never notice anything.
        """
        key = cache_key(method, params)
        if not refresh:
            found, value = self.get(key)
            if found:
                return value
        value = factory()
        # Only successful reads are stored: a raised AppServerError must not be
        # replayed from cache for the life of the TTL.
        self.set(key, value,
                 ttl if ttl is not None else self.ttl_for(method),
                 thread_tag(params))
        return value

    def invalidate_thread(self, thread_id: str) -> None:
        if not thread_id:
            return
        with self._lock:
            for key in [key for key, tag in self._threads.items() if tag == thread_id]:
                self._entries.pop(key, None)
                self._threads.pop(key, None)
            # thread/list rows contain the id in their params only when a cursor
            # was used, so drop the listings too: their rows carry per-thread
            # status that just changed.
            for key in [key for key in self._entries if key.startswith("thread/list")]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._threads.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, (expiry, _value) in self._entries.items()
                   if now >= expiry]
        for key in expired:
            self._entries.pop(key, None)
            self._threads.pop(key, None)
        if len(self._entries) > MAX_ENTRIES:
            for key in sorted(self._entries, key=lambda item: self._entries[item][0])[:64]:
                self._entries.pop(key, None)
                self._threads.pop(key, None)
