"""Regression tests for the per-device deferred event worker.

The device session thread is the only thread that reads that device's audio
chunks and answers its heartbeats. Codex round-trips can each wait for their
whole 15 s timeout, so they must never run on that thread.
"""

from __future__ import annotations

import threading
import time
import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import WORKER_QUEUE_LIMIT, BridgeHub


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self._lock:
            self.sent.append(message)

    def types(self) -> list[str]:
        with self._lock:
            return [str(message.get("type")) for message in self.sent]


class BlockingCatalog:
    """Catalog service that only answers once the test releases it."""

    def __init__(self, release_timeout: float = 5.0) -> None:
        self.events: list[str] = []
        self.release = threading.Event()
        self.release_timeout = release_timeout
        self.started = threading.Event()

    def handle_device_event(self, device_id: str, event: dict) -> list[dict]:
        del device_id
        self.started.set()
        self.events.append(str(event.get("type")))
        self.release.wait(self.release_timeout)
        return [{"type": "codex_catalog_end", "scope": str(event.get("type"))}]


class SlowCatalog(BlockingCatalog):
    def handle_device_event(self, device_id: str, event: dict) -> list[dict]:
        del device_id
        self.started.set()
        self.events.append(str(event.get("type")))
        time.sleep(0.4)
        return [{"type": "codex_catalog_end", "scope": str(event.get("type"))}]


class DeferredWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = BridgeHub(DeviceRegistry(None))
        self.hub.registry.upsert(
            "d1",
            model="dotii",
            supported=[
                "status",
                "text",
                "tasks",
                "results",
                "read_ack",
                "audio",
                "voice",
                "approval",
            ],
        )
        self.session = FakeSession()
        self.hub.attach("d1", self.session)

    def tearDown(self) -> None:
        self.hub.detach("d1")

    def test_blocking_event_does_not_hold_the_calling_thread(self) -> None:
        catalog = BlockingCatalog()
        self.addCleanup(catalog.release.set)
        self.hub.set_codex_catalog(catalog)

        started = time.monotonic()
        self.hub.handle_device_event("d1", {"type": "codex_catalog_request"})
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertTrue(catalog.started.wait(1.0))

    def test_the_session_keeps_answering_cheap_events_while_codex_is_stuck(self) -> None:
        catalog = BlockingCatalog()
        self.addCleanup(catalog.release.set)
        self.hub.set_codex_catalog(catalog)
        self.hub.handle_device_event("d1", {"type": "codex_catalog_request"})
        self.assertTrue(catalog.started.wait(1.0))

        self.hub.handle_device_event("d1", {"type": "codex_state_request"})

        self.assertIn("codex_state", self.session.types())

    def test_events_for_one_device_run_in_the_order_they_arrived(self) -> None:
        catalog = BlockingCatalog()
        self.addCleanup(catalog.release.set)
        self.hub.set_codex_catalog(catalog)

        order = [
            "codex_project_select",
            "codex_catalog_cancel",
            "codex_selection_confirm",
            "codex_thread_select",
        ]
        for event_type in order:
            self.hub.handle_device_event("d1", {"type": event_type})

        self.assertTrue(catalog.started.wait(1.0))
        self.assertEqual(catalog.events, order[:1])

        catalog.release.set()
        self.assertTrue(self.hub.wait_idle(5.0))
        self.assertEqual(catalog.events, order)

    def test_a_device_that_outruns_the_worker_is_told_to_retry(self) -> None:
        catalog = BlockingCatalog()
        self.addCleanup(catalog.release.set)
        self.hub.set_codex_catalog(catalog)

        accepted = 0
        for _ in range(WORKER_QUEUE_LIMIT + 6):
            before = self.session.types()
            self.hub.handle_device_event("d1", {"type": "codex_catalog_request"})
            after = self.session.types()
            if "error" in after[len(before):]:
                break
            accepted += 1

        self.assertEqual(accepted, WORKER_QUEUE_LIMIT)
        self.assertEqual(self.session.types()[-1], "error")
        self.assertEqual(self.session.sent[-1]["code"], "busy_retry")

        catalog.release.set()
        self.assertTrue(self.hub.wait_idle(5.0))
        self.assertEqual(len(catalog.events), WORKER_QUEUE_LIMIT)

    def test_slow_events_on_one_device_do_not_delay_another_device(self) -> None:
        catalog = SlowCatalog()
        self.addCleanup(catalog.release.set)
        self.hub.set_codex_catalog(catalog)
        other = FakeSession()
        self.hub.registry.upsert("d2", model="dotii", supported=["status", "tasks"])
        self.hub.attach("d2", other)
        self.addCleanup(self.hub.detach, "d2")

        self.hub.handle_device_event("d1", {"type": "codex_catalog_request"})
        started = time.monotonic()
        self.hub.handle_device_event("d2", {"type": "codex_catalog_request"})
        self.assertTrue(other.types().count("codex_catalog_end") >= 1
                        or time.monotonic() - started < 0.9)
        self.assertTrue(self.hub.wait_idle(5.0))
        self.assertEqual(catalog.events, ["codex_catalog_request",
                                          "codex_catalog_request"])


if __name__ == "__main__":
    unittest.main()
