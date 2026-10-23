"""Remembered endpoints: a paired device must not wait for a broadcast round.

The complaint this guards: opening the Codex app read "not connected" for close
to ten seconds, because the only way the host found the device was a UDP
broadcast whose reply window was shorter than the device's Wi-Fi power-save
listen interval.

Two host-side changes are pinned here: the reply window has to be long enough to
cover that interval, and a paired device with a remembered address is dialled
directly instead of waiting for the next round.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codexbridge.devices.discovery import DISCOVERY_TIMEOUT_SECONDS, DiscoveryResult
from codexbridge.devices.manager import (
    CONNECT_TIMEOUT_SECONDS,
    REDIAL_COOLDOWN_SECONDS,
    DeviceManager,
)
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub


def wait_until(predicate, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class RecordingSession:
    created: list["RecordingSession"] = []
    online = threading.Event()

    def __init__(self, host, port, token, **kwargs):
        self.host = host
        self.port = port
        self.token = token
        self.kwargs = kwargs
        self.stopped = threading.Event()
        RecordingSession.created.append(self)

    def run_once(self) -> None:
        self.kwargs["on_online"]()
        RecordingSession.online.set()
        # A live session is held open; the manager only counts it as online while
        # the thread is still inside run_once.
        self.stopped.wait(30.0)
        self.kwargs["on_offline"]()

    def stop(self) -> None:
        self.stopped.set()


class DiscoveryWindowTest(unittest.TestCase):
    def test_reply_window_covers_a_power_save_listen_interval(self):
        # An access point buffers a unicast for at most one listen interval, and
        # the measured device sleeps on a 1.024 s beacon interval. Anything
        # shorter can lose the reply outright.
        self.assertGreaterEqual(DISCOVERY_TIMEOUT_SECONDS, 2.0)


class EndpointRegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "devices.json"
        self.registry = DeviceRegistry(self.path)

    def _remember(self, **kwargs):
        self.registry.upsert("d1", model="aipassport", **kwargs)
        self.registry.save()

    def test_endpoint_round_trips(self):
        self._remember(host="192.168.0.39", port=8765)
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertEqual("192.168.0.39", reloaded.get("d1").host)
        self.assertEqual(8765, reloaded.get("d1").port)

    def test_absent_endpoint_stays_none(self):
        self._remember()
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertIsNone(reloaded.get("d1").host)
        self.assertIsNone(reloaded.get("d1").port)

    def test_upsert_without_endpoint_keeps_the_memory(self):
        self._remember(host="192.168.0.39", port=8765)
        self.registry.upsert("d1", model="aipassport")
        self.assertEqual("192.168.0.39", self.registry.get("d1").host)
        self.assertEqual(8765, self.registry.get("d1").port)

    def test_bad_port_is_dropped_on_load(self):
        import json

        self._remember(host="h", port=8765)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["devices"]["d1"]["port"] = 70000
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        reloaded = DeviceRegistry(self.path)
        reloaded.load()
        self.assertIsNone(reloaded.get("d1").port)


class PresenceLivenessTest(unittest.TestCase):
    def test_home_only_discovery_counts_as_online_without_tcp_session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        hub = BridgeHub(registry)

        class HomeOnlySession:
            def __init__(self, *args, **kwargs):
                pass

            def run_once(self):
                raise ConnectionRefusedError(10061, "home-only transport")

            def stop(self):
                pass

        manager = DeviceManager(registry, hub, session_factory=HomeOnlySession)
        self.addCleanup(manager.stop)
        registry.mint_token("aipassport-1")

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            manager.sync(
                [
                    DiscoveryResult(
                        host="192.168.0.39",
                        device="FoloOS-AI-Passport",
                        device_id="aipassport-1",
                        name=None,
                        port=8765,
                        capabilities=["status"],
                        protocol="cbdp",
                    )
                ]
            )
            self.assertTrue(
                wait_until(lambda: manager.online_count() == 0, 5.0),
                "the refused TCP session was never cleaned up",
            )

        self.assertIsNotNone(registry.get("aipassport-1").last_seen)
        self.assertIn("aipassport-1", hub.online_device_ids())


class RedialTest(unittest.TestCase):
    def setUp(self):
        RecordingSession.created = []
        RecordingSession.online.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        self.hub = BridgeHub(self.registry)
        self.manager = DeviceManager(
            self.registry, self.hub, session_factory=RecordingSession
        )
        self.addCleanup(self.manager.stop)

    def _pair(self, host="192.168.0.39", port=8765):
        # A discovery reply only becomes a session once the device has a token.
        self.registry.mint_token("aipassport-1")
        self.manager.sync(
            [
                DiscoveryResult(
                    host=host,
                    device="aipassport",
                    device_id="aipassport-1",
                    name=None,
                    port=port,
                    capabilities=["status"],
                )
            ]
        )

    def test_discovery_remembers_the_endpoint(self):
        self._pair()
        self.assertEqual("192.168.0.39", self.registry.get("aipassport-1").host)

    def test_paired_count_only_counts_devices_without_a_session(self):
        self.assertEqual(0, self.manager.paired_count())
        self.registry.upsert("waiting", model="aipassport", host="10.0.0.9")
        self.registry.mint_token("waiting")
        self.assertEqual(1, self.manager.paired_count())
        self._pair()
        self.assertTrue(RecordingSession.online.wait(10.0))
        # aipassport-1 now has a session; the other paired device does not, and
        # one online device must not make a waiting one look connected.
        self.assertEqual(1, self.manager.paired_count())
        self.registry.remove("waiting")
        self.assertEqual(0, self.manager.paired_count())

    def test_redial_dials_the_remembered_endpoint(self):
        self.registry.upsert(
            "aipassport-1",
            model="aipassport",
            host="192.168.0.39",
            port=8765,
        )
        self.registry.mint_token("aipassport-1")
        self.assertEqual(1, self.manager.redial_paired())
        self.assertTrue(RecordingSession.online.wait(10.0))
        self.assertEqual("192.168.0.39", RecordingSession.created[0].host)
        self.assertEqual(8765, RecordingSession.created[0].port)
        self.assertEqual(
            CONNECT_TIMEOUT_SECONDS,
            RecordingSession.created[0].kwargs["timeout_seconds"],
        )

    def test_redial_falls_back_to_the_default_port(self):
        self.registry.upsert("aipassport-1", model="aipassport", host="10.0.0.5")
        self.registry.mint_token("aipassport-1")
        self.assertEqual(1, self.manager.redial_paired())
        self.assertTrue(RecordingSession.online.wait(10.0))
        self.assertEqual(8765, RecordingSession.created[0].port)

    def test_redial_is_cooldown_bounded(self):
        self.registry.upsert("aipassport-1", model="aipassport", host="10.0.0.5")
        self.registry.mint_token("aipassport-1")
        self.assertEqual(1, self.manager.redial_paired())
        # A second call in the same instant must not start a dial storm.
        self.assertEqual(0, self.manager.redial_paired())

    def test_legacy_records_are_not_dialled(self):
        self.registry.upsert("legacy:10.0.0.1", model="old", host="10.0.0.1")
        self.registry.mint_token("legacy:10.0.0.1")
        self.assertEqual(0, self.manager.redial_paired())

    def test_unpaired_device_is_not_dialled(self):
        self.registry.upsert("stranger", model="aipassport", host="10.0.0.7")
        self.assertEqual(0, self.manager.redial_paired())

    def test_stale_endpoint_logs_one_line_not_a_traceback(self):
        class Refusing(RecordingSession):
            def run_once(self):
                raise ConnectionRefusedError(10061, "stale endpoint")

        self.registry.upsert("aipassport-1", model="aipassport", host="10.0.0.8")
        self.registry.mint_token("aipassport-1")
        manager = DeviceManager(self.registry, self.hub, session_factory=Refusing)
        self.addCleanup(manager.stop)

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(1, manager.redial_paired())
            self.assertTrue(
                wait_until(lambda: manager.online_count() == 0, 5.0),
                "the failed session was never cleaned up",
            )
        text = captured.getvalue()
        self.assertIn("dial aipassport-1 failed", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
