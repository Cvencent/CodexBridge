"""One device's screen belongs to one Codex thread.

Reported on hardware: the device selected its own new task, the host then pushed
``task:01a145dd-"&#42;"`` for a desktop session nobody had chosen, and the screen
jumped to "等你回话" while the device was waiting on its own answer. The next
question was recorded against the wrong thread and the whole flow died.

A client that has selected a thread gets only that thread's live records. A client
that has not selected anything keeps the aggregate feed, so older firmware, the
phone companion, and any new reader behave exactly as before.
"""
import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.read_state import ReadStateStore


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def record(kind, status, body, thread, message_id=None, data=None):
    return MessageRecord(
        message_id or f"{kind}:{thread}:{status}",
        thread,
        kind,
        status,
        "标题",
        body,
        1.0,
        data=data,
    )


class ThreadScopedFeedTest(unittest.TestCase):
    def setUp(self):
        self.hub = BridgeHub(DeviceRegistry(None), read_state=ReadStateStore(None))
        for device_id in ("follower", "legacy"):
            self.hub.registry.upsert(
                device_id,
                model="FoloOS-AI-Passport",
                supported=["status", "tasks", "results", "read_ack", "voice"],
            )
        self.chosen = FakeSession()
        self.anyone = FakeSession()
        self.hub.attach("follower", self.chosen)
        self.hub.attach("legacy", self.anyone)

    def kinds(self, session):
        return [m.get("kind") for m in session.sent if m.get("type") == "message"]

    def test_a_selection_learns_which_thread_the_device_follows(self):
        self.hub.note_device_thread("follower", "mine")
        self.hub.publish(record("status", "working", "正在编译", "someone-else"))
        self.assertEqual([], self.kinds(self.chosen),
                         "another thread must not drive this screen")
        # A client that never selected keeps seeing everything.
        self.assertIn("status", self.kinds(self.anyone))

    def test_the_followed_thread_still_arrives(self):
        self.hub.note_device_thread("follower", "mine")
        self.hub.publish(record("status", "working", "正在编译", "mine"))
        self.assertEqual(["status"], self.kinds(self.chosen))

    def test_results_for_other_threads_are_quiet_too(self):
        self.hub.note_device_thread("follower", "mine")
        self.hub.publish(record("result", "completed", "别人的答案", "someone-else",
                                message_id="result:someone-else:5"))
        self.assertEqual([], self.kinds(self.chosen))

    def test_the_snapshot_answers_about_the_followed_thread(self):
        self.hub.note_device_thread("follower", "mine")
        self.hub.publish(record("status", "working", "别的工作", "someone-else",
                                data={"task": {"current_action": "别的工作"}}))
        self.chosen.sent.clear()
        self.hub.handle_device_event("follower", {"type": "codex_state_request"})
        self.assertTrue(self.hub.wait_idle(2))
        snaps = [m for m in self.chosen.sent if m.get("type") == "codex_status_snapshot"]
        self.assertEqual([], snaps,
                         "a snapshot about another thread is not an answer")
        self.hub.publish(record("status", "working", "我的任务", "mine",
                                data={"task": {"current_action": "我的任务"}}))
        self.chosen.sent.clear()
        self.hub.handle_device_event("follower", {"type": "codex_state_request"})
        self.assertTrue(self.hub.wait_idle(2))
        snaps = [m for m in self.chosen.sent if m.get("type") == "codex_status_snapshot"]
        self.assertEqual(1, len(snaps))
        self.assertEqual("mine", snaps[0]["thread_id"])

    def test_the_home_counters_stay_global(self):
        self.hub.note_device_thread("follower", "mine")
        self.hub.publish(record("task", "working", "另一个会话在跑", "someone-else",
                                message_id="task:someone-else"))
        self.chosen.sent.clear()
        self.hub.handle_device_event("follower", {"type": "codex_state_request"})
        self.assertTrue(self.hub.wait_idle(2))
        state = [m for m in self.chosen.sent if m.get("type") == "codex_state"]
        self.assertEqual(1, state[0]["running"],
                         "the aggregate tally is not thread scoped")
