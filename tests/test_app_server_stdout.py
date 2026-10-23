"""Regression tests for the app-server stdout drain."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexbridge.codex import app_server as app_server_module
from codexbridge.codex.app_server import AppServerClient


class StdoutLimitTests(unittest.TestCase):
    def test_oversized_line_is_dropped_without_json_parsing(self) -> None:
        client = AppServerClient(["codex"], Path("."))
        oversized = "x" * (client.MAX_STDOUT_LINE_CHARS + 128) + "\n"
        small = json.dumps({"method": "test/notification", "params": {"ok": True}}) + "\n"
        client.process = SimpleNamespace(stdout=io.StringIO(oversized + small))

        with mock.patch.object(
            app_server_module.json, "loads", wraps=json.loads
        ) as loads:
            client._read_stdout()

        self.assertEqual(1, loads.call_count)
        self.assertEqual("test/notification", client.notifications.get_nowait()["method"])
        self.assertGreater(client.oversized_stdout_lines, 0)


class ItemPageCacheTest(unittest.TestCase):
    """Bounded item pages must be shared, not re-fetched per caller.

    A single active thread costs two ``thread/items/list`` round trips (turn
    head + tail). They used to be the only bounded reads left out of the cache,
    so the 5 s collector beat and every phone request each paid for them again.
    """

    def test_item_pages_are_cacheable(self):
        self.assertIn("thread/items/list", AppServerClient.READ_CACHE_METHODS)

    def test_item_pages_use_a_short_ttl(self):
        # A running turn appends to the tail, so the item window is deliberately
        # shorter than whole-thread reads.
        self.assertLess(
            AppServerClient.READ_CACHE_TTL["thread/items/list"],
            AppServerClient.READ_CACHE_SECONDS,
        )

    def test_the_cached_entry_is_reused_then_expires(self):
        client = AppServerClient(["codex"], Path.cwd())
        key = client._read_cache_key("thread/items/list", {"threadId": "t1"})
        client._read_cache[key] = (9999999999.0, [{"id": "item1"}])
        hit, value = client._cached_read(key)
        self.assertTrue(hit)
        self.assertEqual(value, [{"id": "item1"}])
        # An expired entry must be dropped rather than replayed forever.
        client._read_cache[key] = (0.0, [{"id": "stale"}])
        hit, _ = client._cached_read(key)
        self.assertFalse(hit)

    def test_a_write_clears_the_item_cache(self):
        client = AppServerClient(["codex"], Path.cwd())
        key = client._read_cache_key("thread/items/list", {"threadId": "t1"})
        client._read_cache[key] = (9999999999.0, [{"id": "item1"}])
        client._clear_read_cache()
        hit, _ = client._cached_read(key)
        self.assertFalse(hit)


if __name__ == "__main__":
    unittest.main()
