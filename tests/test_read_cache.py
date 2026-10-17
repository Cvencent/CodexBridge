"""The read cache keeps one protocol responsive for every device at once."""

from __future__ import annotations

import threading
import unittest

from codexbridge.companion.read_cache import (
    CACHEABLE_METHODS,
    ReadCache,
    cache_key,
    thread_tag,
)


class ReadCacheTests(unittest.TestCase):
    def test_a_repeated_read_fetches_once(self):
        cache = ReadCache()
        calls = []

        def fetch():
            calls.append(1)
            return {"data": []}

        for _ in range(5):
            cache.call("thread/list", {"limit": 12}, fetch)
        self.assertEqual(len(calls), 1)

    def test_different_params_are_different_entries(self):
        cache = ReadCache()
        calls = []
        cache.call("thread/read", {"threadId": "a"}, lambda: calls.append(1))
        cache.call("thread/read", {"threadId": "b"}, lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_refresh_bypasses_the_cache_and_refills_it(self):
        cache = ReadCache()
        calls = []
        cache.call("thread/list", {"limit": 12}, lambda: calls.append(1))
        cache.call("thread/list", {"limit": 12}, lambda: calls.append(1))
        cache.call("thread/list", {"limit": 12}, lambda: calls.append(1),
                   refresh=True)
        cache.call("thread/list", {"limit": 12}, lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_only_the_changed_thread_is_dropped(self):
        cache = ReadCache()
        cache.call("thread/read", {"threadId": "a"}, lambda: "A")
        cache.call("thread/read", {"threadId": "b"}, lambda: "B")
        cache.invalidate_thread("a")
        self.assertEqual(cache.call("thread/read", {"threadId": "a"},
                                    lambda: "A2"), "A2")
        self.assertEqual(cache.call("thread/read", {"threadId": "b"},
                                    lambda: "B2"), "B")

    def test_invalidating_a_thread_retires_the_shared_listing(self):
        cache = ReadCache()
        cache.call("thread/list", {"limit": 12}, lambda: "L1")
        cache.invalidate_thread("a")
        self.assertEqual(cache.call("thread/list", {"limit": 12}, lambda: "L2"),
                         "L2")

    def test_a_failed_read_is_never_replayed(self):
        cache = ReadCache()

        def boom():
            raise RuntimeError("codex unavailable")

        with self.assertRaises(RuntimeError):
            cache.call("thread/read", {"threadId": "a"}, boom)
        self.assertEqual(cache.call("thread/read", {"threadId": "a"},
                                    lambda: "ok"), "ok")

    def test_an_entry_expires(self):
        cache = ReadCache(ttl=0.01)
        calls = []
        cache.call("thread/read", {"threadId": "a"}, lambda: calls.append(1))
        threading.Event().wait(0.05)
        cache.call("thread/read", {"threadId": "a"}, lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_an_explicit_ttl_overrides_the_default(self):
        cache = ReadCache(ttl=0.01)
        cache.call("thread/read", {"threadId": "a"}, lambda: "A", ttl=30.0)
        threading.Event().wait(0.05)
        self.assertEqual(cache.call("thread/read", {"threadId": "a"},
                                    lambda: "B"), "A")

    def test_the_cache_is_bounded(self):
        cache = ReadCache()
        for index in range(600):
            cache.set("thread/read|%d" % index, index, 30.0)
        self.assertLessEqual(len(cache._entries), 400)

    def test_state_changing_methods_are_not_cacheable(self):
        for method in ("turn/start", "turn/steer", "thread/start",
                       "thread/name/set", "account/read"):
            self.assertNotIn(method, CACHEABLE_METHODS)

    def test_thread_tag_reads_the_ids_the_callers_use(self):
        self.assertEqual(thread_tag({"threadId": "a"}), "a")
        self.assertEqual(thread_tag({"thread_id": "b"}), "b")
        self.assertEqual(thread_tag(None), "")
        self.assertEqual(thread_tag({"limit": 12}), "")

    def test_keys_are_stable_regardless_of_param_order(self):
        self.assertEqual(cache_key("thread/read", {"a": 1, "b": 2}),
                         cache_key("thread/read", {"b": 2, "a": 1}))


class ReadCacheConcurrencyTests(unittest.TestCase):
    def test_the_fetch_runs_outside_the_lock(self):
        # Sixty phone connections plus two devices read through here. A lock
        # held across the round trip would serialise every device against one
        # Codex request, which is the stall this cache exists to remove.
        cache = ReadCache()
        started = threading.Event()
        release = threading.Event()

        def slow_fetch():
            started.set()
            release.wait(2.0)
            return "value"

        holder = threading.Thread(target=lambda: cache.call(
            "thread/read", {"threadId": "slow"}, slow_fetch))
        holder.start()
        self.assertTrue(started.wait(1.0))
        done = threading.Event()

        def other():
            cache.call("thread/read", {"threadId": "other"}, lambda: "fast")
            done.set()

        second = threading.Thread(target=other)
        second.start()
        self.assertTrue(done.wait(1.0), "a second reader was blocked by the lock")
        release.set()
        holder.join(timeout=2.0)
