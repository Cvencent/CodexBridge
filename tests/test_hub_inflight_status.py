"""In-flight Codex state must reach the device through a cheap pull.

Reported on hardware: a second voice turn produced ``task_queued`` and then
nothing for 200 s.  ``status:<thread>:working`` is republished every collector
cycle with an identical fingerprint, ``publish()`` dedupes it (deliberately --
the radio must not wake every few seconds), and the global read marker then
suppresses the same record at replay time.  The device sat on "waiting for the
computer" with no message left to redraw.

The generic answer is pull-based, matching section 9.7 of PROTOCOL.md: the
device asks, the host answers with the *current* live state, and in-flight
records are never treated as consumed notifications.
"""
import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.protocol.messages import MAX_LINE_BYTES
from codexbridge.protocol.messages import wire_messages
from codexbridge.read_state import ReadStateStore


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def record(kind, status, body, message_id=None, thread="t1", data=None,
           created_at=1.0):
    return MessageRecord(
        message_id or f"{kind}:{thread}:{status}",
        thread,
        kind,
        status,
        "标题",
        body,
        created_at,
        data=data,
    )


def live_task(turn="a", action="Running tests", status="working",
              created_at=1.0, thread="t1"):
    return record(
        "status",
        status,
        action,
        message_id=f"status:{thread}:{status}",
        thread=thread,
        created_at=created_at,
        data={"task": {"turn_id": turn, "current_action": action,
                       "plan_completed": 2, "plan_total": 5}},
    )


class InFlightStatusTest(unittest.TestCase):
    def setUp(self):
        self.store = ReadStateStore(None)
        self.hub = BridgeHub(DeviceRegistry(None), read_state=self.store)
        self.hub.registry.upsert(
            "d1",
            model="FoloOS-AI-Passport",
            supported=["status", "tasks", "results", "read_ack"],
        )
        self.session = FakeSession()
        self.hub.attach("d1", self.session)

    def pull(self, device_id="d1", request_id=None):
        self.session.sent.clear()
        event = {"type": "codex_state_request"}
        if request_id is not None:
            event["request_id"] = request_id
        self.hub.handle_device_event(device_id, event)
        self.assertTrue(self.hub.wait_idle(2))
        return list(self.session.sent)

    def test_pull_answers_with_the_live_status_snapshot(self):
        # The collector publishes a status record and a task record per thread;
        # ``running`` is counted from the task record so one task counts once.
        self.hub.publish(live_task())
        self.hub.publish(record("task", "working", "任务一",
                                message_id="task:t1"))
        state = [m for m in self.pull() if m.get("type") == "codex_state"]
        self.assertEqual(1, len(state))
        self.assertEqual(1, state[0]["running"])
        snap = [m for m in self.pull() if m.get("type") == "codex_status_snapshot"]
        self.assertEqual(1, len(snap), "the device must be able to un-stick by asking")

    def test_running_counts_each_thread_once_across_status_and_task_records(self):
        statuses = ["working", "waiting_user", "queued", "starting", "working"]
        for index, status in enumerate(statuses):
            thread = f"t{index + 1}"
            self.hub.publish(live_task(status=status, thread=thread))
            self.hub.publish(record(
                "task",
                status,
                f"任务{index + 1}",
                message_id=f"task:{thread}",
                thread=thread,
            ))

        # Five threads, ten live records (status + task for each). The count is
        # per thread, not per record.
        self.assertEqual(5, self.hub.counts()["running"])

    def test_snapshot_carries_the_renderable_fields(self):
        self.hub.publish(live_task(turn="b", action="Compiling the firmware"))
        snap = [m for m in self.pull() if m.get("type") == "codex_status_snapshot"]
        self.assertEqual(1, len(snap))
        self.assertEqual("working", snap[0]["status"])
        self.assertEqual("Compiling the firmware", snap[0]["current_action"])
        self.assertEqual("b", snap[0]["turn_id"])
        self.assertEqual("t1", snap[0]["thread_id"])
        self.assertEqual(2, snap[0]["plan_completed"])
        self.assertEqual(5, snap[0]["plan_total"])

    def test_waiting_user_snapshot_is_visible_to_the_device(self):
        self.hub.publish(live_task(status="waiting_user", action="等你回话"))
        snap = [m for m in self.pull() if m.get("type") == "codex_status_snapshot"]
        self.assertEqual("waiting", snap[0]["status"])

    def test_device_message_status_uses_the_generic_waiting_name(self):
        self.hub.publish(live_task(status="waiting_user", action="等你回话"))
        messages = [m for m in self.session.sent if m.get("type") == "message"]

        self.assertEqual("waiting", messages[-1]["status"])

    def test_nested_task_list_status_uses_the_generic_waiting_name(self):
        task = record(
            "task",
            "waiting_user",
            "任务一",
            message_id="task:t1",
            data={
                "task": {"thread_id": "t1", "status": "waiting_user"},
                "tasks": [
                    {"thread_id": "t1", "status": "waiting_user"},
                    {"thread_id": "t2", "status": "working"},
                ],
            },
        )
        self.hub.publish(task)

        message = [m for m in self.session.sent if m.get("type") == "message"][-1]
        self.assertEqual("waiting", message["data"]["task"]["status"])
        self.assertEqual(
            ["waiting", "working"],
            [item["status"] for item in message["data"]["tasks"]],
        )

    def test_snapshot_fits_one_wire_line(self):
        long = "很长的动作" * 80
        self.hub.publish(live_task(action=long))
        for message in self.pull():
            for payload in wire_messages(message):
                self.assertLessEqual(len(payload), MAX_LINE_BYTES)

    def test_pull_without_any_status_only_returns_counters(self):
        messages = self.pull()
        self.assertEqual(1, len(messages))
        self.assertEqual("codex_state", messages[0]["type"])
        self.assertEqual(0, messages[0]["running"])

    def test_identical_snapshot_is_not_repushed_every_cycle(self):
        # Power rule stays: the same words must not turn into radio traffic.
        self.hub.publish(live_task())
        self.session.sent.clear()
        self.hub.publish(live_task())
        self.assertEqual([], self.session.sent)

    def test_new_turn_id_pushes_even_with_identical_text(self):
        self.hub.publish(live_task(turn="a"))
        self.session.sent.clear()
        self.hub.publish(live_task(turn="b"))
        pushed = [m for m in self.session.sent if m.get("type") == "message"]
        self.assertEqual(1, len(pushed))

    def test_in_flight_status_survives_a_global_read_marker(self):
        self.hub.publish(live_task())
        self.store.mark_message("status:t1:working")
        other = FakeSession()
        self.hub.registry.upsert(
            "d2",
            model="FoloOS-AI-Passport",
            supported=["status", "tasks", "results", "read_ack"],
        )
        self.hub.attach("d2", other)
        self.hub.replay_state("d2")
        kinds = [m.get("kind") for m in other.sent if m.get("type") == "message"]
        self.assertIn("status", kinds,
                      "live state is not a consumed notification")

    def test_settled_result_is_still_suppressed_once_read(self):
        result = record("result", "completed", "done",
                        message_id="result:t1:5", created_at=5)
        self.hub.publish(result)
        self.store.mark_message("result:t1:5")
        other = FakeSession()
        self.hub.registry.upsert(
            "d2",
            model="FoloOS-AI-Passport",
            supported=["status", "tasks", "results", "read_ack"],
        )
        self.hub.attach("d2", other)
        self.hub.replay_state("d2")
        ids = [m.get("message_id") for m in other.sent if m.get("type") == "message"]
        self.assertNotIn("result:t1:5", ids)

    def test_snapshot_reports_the_most_recent_live_record(self):
        self.hub.publish(live_task(turn="a", action="Old work", created_at=1))
        self.hub.publish(live_task(turn="b", action="New work", created_at=2))
        snap = [m for m in self.pull() if m.get("type") == "codex_status_snapshot"]
        self.assertEqual("New work", snap[0]["current_action"])

    def test_snapshot_clips_long_action_to_the_device_budget(self):
        self.hub.registry.set_max_text_chars("d1", 40)
        self.hub.publish(live_task(action="字" * 200))
        snap = [m for m in self.pull() if m.get("type") == "codex_status_snapshot"]
        self.assertLessEqual(len(snap[0]["current_action"]), 40)

    def test_snapshot_honours_the_status_capability(self):
        self.hub.registry.upsert("d3", model="dotii", supported=["text"])
        session = FakeSession()
        self.hub.attach("d3", session)
        self.hub.handle_device_event("d3", {"type": "codex_state_request"})
        self.assertTrue(self.hub.wait_idle(2))
        codes = [m.get("code") for m in session.sent if m.get("type") == "error"]
        self.assertEqual(["capability_not_enabled"], codes)

    def test_request_id_is_echoed(self):
        self.hub.publish(live_task())
        messages = self.pull(request_id="7")
        for message in messages:
            if message.get("type") in {"codex_state", "codex_status_snapshot"}:
                self.assertEqual("7", message.get("request_id"))


if __name__ == "__main__":
    unittest.main()
