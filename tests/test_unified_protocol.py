import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.companion.queue import QueueService
from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.protocol.commands import CommandDispatcher, CommandError
from tests.fake_device import FakeDevice


class _QueueTarget:
    """Minimal CompanionService-shaped owner for a queue integration test."""

    def __init__(self, thread_ids=("t1",)):
        self.thread_ids = set(thread_ids)
        self.active = set()
        self.starts = []
        self.steers = []
        self.events = []
        self.fail_start = False

    def thread_exists(self, thread_id):
        return thread_id in self.thread_ids

    def is_active(self, thread_id):
        return thread_id in self.active

    def start_turn(self, thread_id, text, model="", effort="", client_message_id=""):
        if self.fail_start:
            raise RuntimeError("codex unavailable")
        self.starts.append(
            {
                "thread_id": thread_id,
                "text": text,
                "model": model,
                "effort": effort,
                "client_message_id": client_message_id,
            }
        )
        self.active.add(thread_id)
        return {"thread_id": thread_id, "turn_id": "turn-1", "status": "working"}

    def steer_turn(self, thread_id, text):
        if thread_id not in self.active:
            raise ValueError("no active turn")
        self.steers.append({"thread_id": thread_id, "text": text})
        return {"thread_id": thread_id, "turn_id": "turn-1", "status": "working"}

    def finish_turn(self, thread_id):
        self.active.discard(thread_id)

    def publish(self, event):
        self.events.append(event)


class _QueueServiceFacade:
    """Expose the queue_* methods CommandDispatcher expects."""

    def __init__(self, queue):
        self.queue = queue

    def queue_list(self, thread_id):
        return self.queue.list(thread_id)

    def queue_enqueue(self, thread_id, text, model="", effort=""):
        return self.queue.enqueue(thread_id, text, model=model, effort=effort)

    def queue_edit(self, queue_id, text, model="", effort="", expected_revision=0):
        return self.queue.edit(
            queue_id,
            text,
            model=model,
            effort=effort,
            expected_revision=expected_revision,
        )

    def queue_delete(self, queue_id, expected_revision=0):
        return self.queue.delete(queue_id, expected_revision=expected_revision)

    def queue_steer(self, queue_id, expected_revision=0):
        return self.queue.steer(queue_id, expected_revision=expected_revision)

    def queue_snapshots(self):
        return self.queue.snapshots()


class UnifiedProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.hub = BridgeHub(self.registry)
        self.hub.publish(
            MessageRecord("m1", "t1", "status", "working", "Task", "Body", 0.0)
        )
        self.manager = DeviceManager(self.registry, self.hub)
        self.addCleanup(self.manager.stop)

    def test_unified_device_receives_replay_and_read_state(self):
        self.registry.upsert(
            "fake-device",
            model="dotii",
            token="abcdefghijklmnop",
            supported=["status", "text", "read_ack"],
        )
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.stall()
            self.manager.sync([self._discovery_for(device)])

            self.assertTrue(
                self._wait_for(
                    lambda: any(
                        item.get("type") == "message"
                        and item.get("message_id") == "m1"
                        for item in device.received
                    )
                )
            )
            self.assertEqual(device.received[0]["type"], "bridge_auth")
            self.assertEqual(device.received[1]["type"], "bridge_ready")
            self.assertEqual(
                device.received[1]["capabilities"],
                ["read_ack", "status", "text"],
            )
            self.assertFalse(
                any(
                    item.get("type") in {"hello", "hello_ok"}
                    for item in device.received
                )
            )

    def test_read_ack_broadcasts_to_other_unified_session(self):
        with FakeDevice(device_id="device-a") as first, FakeDevice(
            device_id="device-b"
        ) as second:
            for device in (first, second):
                device.expect_handshake("abcdefghijklmnop")
                device.stall()
                self.registry.upsert(
                    device.device_id,
                    model="dotii",
                    token="abcdefghijklmnop",
                    supported=["status", "text", "read_ack"],
                )
                self.manager.sync([self._discovery_for(device)])

            self.assertTrue(
                self._wait_for(lambda: "m1" in self._received_message_ids(first))
            )
            self.assertTrue(
                self._wait_for(lambda: "m1" in self._received_message_ids(second))
            )

            first.send_event({"type": "read_ack", "message_id": "m1"})

            self.assertTrue(
                self._wait_for(
                    lambda: any(
                        item.get("type") == "read_state"
                        and "device-a" in item.get("read_by", [])
                        for item in second.received
                    )
                )
            )

    def test_two_clients_converge_on_one_queue_revision(self):
        target = _QueueTarget()
        queue = QueueService(
            None,
            start_turn=target.start_turn,
            steer_turn=target.steer_turn,
            thread_exists=target.thread_exists,
            is_active=target.is_active,
            publish=target.publish,
        )
        self.addCleanup(queue.stop)
        commands = CommandDispatcher(_QueueServiceFacade(queue))
        target.active.add("t1")

        first = commands.execute(
            "phone-a",
            {"queue"},
            {
                "request_id": "req-a",
                "method": "queue.enqueue",
                "params": {"thread_id": "t1", "text": "第一台手机"},
            },
        )["result"]["item"]
        second = commands.execute(
            "phone-b",
            {"queue"},
            {
                "request_id": "req-b",
                "method": "queue.enqueue",
                "params": {"thread_id": "t1", "text": "第二台手机"},
            },
        )["result"]["item"]

        snapshot = commands.execute(
            "phone-a",
            {"queue"},
            {
                "request_id": "req-list",
                "method": "queue.list",
                "params": {"thread_id": "t1"},
            },
        )["result"]
        self.assertEqual(
            [item["queue_id"] for item in snapshot["items"]],
            [first["queue_id"], second["queue_id"]],
        )

        # The second client edits through the revision the first client saw.
        edited = commands.execute(
            "phone-b",
            {"queue"},
            {
                "request_id": "req-edit",
                "method": "queue.edit",
                "params": {
                    "queue_id": first["queue_id"],
                    "text": "第一台手机改写",
                    "expected_revision": first["revision"],
                },
            },
        )["result"]["item"]
        self.assertEqual(edited["revision"], first["revision"] + 1)

        # A stale revision must not silently overwrite the new state.
        with self.assertRaisesRegex(CommandError, "queue_conflict"):
            commands.execute(
                "phone-a",
                {"queue"},
                {
                    "request_id": "req-stale",
                    "method": "queue.delete",
                    "params": {
                        "queue_id": first["queue_id"],
                        "expected_revision": first["revision"],
                    },
                },
            )

        converged = commands.execute(
            "phone-b",
            {"queue"},
            {
                "request_id": "req-list-2",
                "method": "queue.list",
                "params": {"thread_id": "t1"},
            },
        )["result"]
        self.assertEqual(
            [item["queue_id"] for item in converged["items"]],
            [first["queue_id"], second["queue_id"]],
        )
        self.assertEqual(converged["items"][0]["text"], "第一台手机改写")

    def test_queue_command_replay_is_idempotent_per_request_id(self):
        target = _QueueTarget()
        queue = QueueService(
            None,
            start_turn=target.start_turn,
            steer_turn=target.steer_turn,
            thread_exists=target.thread_exists,
            is_active=target.is_active,
            publish=target.publish,
        )
        self.addCleanup(queue.stop)
        commands = CommandDispatcher(_QueueServiceFacade(queue))
        target.active.add("t1")

        request = {
            "request_id": "req-once",
            "method": "queue.enqueue",
            "params": {"thread_id": "t1", "text": "只应入队一次"},
        }
        first = commands.execute("phone-a", {"queue"}, request)
        second = commands.execute("phone-a", {"queue"}, request)

        self.assertEqual(first, second)
        self.assertEqual(len(queue.list("t1")["items"]), 1)

    def test_old_phone_without_queue_capability_is_refused(self):
        target = _QueueTarget()
        queue = QueueService(
            None,
            start_turn=target.start_turn,
            steer_turn=target.steer_turn,
            thread_exists=target.thread_exists,
            is_active=target.is_active,
            publish=target.publish,
        )
        self.addCleanup(queue.stop)
        commands = CommandDispatcher(_QueueServiceFacade(queue))
        target.active.add("t1")

        with self.assertRaisesRegex(CommandError, "capability_not_enabled"):
            commands.execute(
                "legacy-phone",
                {"control"},
                {
                    "request_id": "req-legacy",
                    "method": "queue.enqueue",
                    "params": {"thread_id": "t1", "text": "旧手机"},
                },
            )

    def test_startup_redispatch_keeps_busy_threads_waiting(self):
        target = _QueueTarget(thread_ids=("t1", "t2"))
        queue = QueueService(
            None,
            start_turn=target.start_turn,
            steer_turn=target.steer_turn,
            thread_exists=target.thread_exists,
            is_active=target.is_active,
            publish=target.publish,
        )
        self.addCleanup(queue.stop)
        target.active.add("t1")

        queue.enqueue("t1", "进行中的线程")
        queue.enqueue("t2", "空闲线程")
        queue.start()

        self.assertTrue(
            self._wait_for(lambda: len(target.starts) == 1),
            "the idle thread should dispatch on start",
        )
        self.assertEqual(target.starts[0]["thread_id"], "t2")
        self.assertEqual(
            [item["text"] for item in queue.list("t1")["items"]],
            ["进行中的线程"],
        )

    @staticmethod
    def _discovery_for(device: FakeDevice) -> DiscoveryResult:
        return DiscoveryResult(
            host=device.host,
            device="dotii",
            device_id=device.device_id,
            name="Fake Device",
            port=device.port,
            capabilities=["status", "text", "read_ack"],
        )

    @staticmethod
    def _wait_for(predicate) -> bool:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    @staticmethod
    def _received_message_ids(device: FakeDevice) -> list[str]:
        with device._received_lock:
            messages = list(device.received)
        return [
            str(item["message_id"])
            for item in messages
            if item.get("type") == "message" and "message_id" in item
        ]
