import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class FakeCatalog:
    def __init__(self):
        self.events = []

    def handle_device_event(self, device_id, event):
        del device_id
        self.events.append(event)
        return [
            {"type": "codex_catalog_begin", "scope": "projects"},
            {
                "type": "codex_catalog_item",
                "scope": "projects",
                "id": "p0",
                "title": "项目一",
                "detail": "项目 1/1",
            },
            {"type": "codex_catalog_end", "scope": "projects"},
        ]


class VoiceCatalog:
    def __init__(self, reply=None, error=None):
        self.calls = []
        self.reply = reply if reply is not None else [
            {"type": "task_queued", "text": "指令已发送到电脑端"}
        ]
        self.error = error

    def handle_voice_command(self, device_id, text):
        self.calls.append((device_id, text))
        if self.error is not None:
            raise self.error
        return self.reply

    def handle_device_event(self, device_id, event):
        return FakeCatalog().handle_device_event(device_id, event)


class HubTest(unittest.TestCase):
    def setUp(self):
        self.hub = BridgeHub(DeviceRegistry(None))
        self.hub.registry.upsert(
            "d1",
            model="dotii",
            supported=["status", "text", "tasks", "results", "read_ack"],
        )
        self.hub.registry.upsert(
            "d2",
            model="FoloOS-AI-Passport",
            supported=["status", "text", "read_ack"],
        )
        self.hub.registry.upsert(
            "d3",
            model="dotii",
            supported=["status"],
        )
        self.hub.registry.upsert(
            "d4",
            model="FoloOS-AI-Passport",
            supported=["status", "text", "read_ack", "tasks", "results",
                       "audio", "voice", "approval"],
        )
        (self.s1, self.s2, self.s3, self.s4) = (
            FakeSession(), FakeSession(), FakeSession(), FakeSession())
        self.hub.attach("d1", self.s1)
        self.hub.attach("d2", self.s2)
        self.hub.attach("d3", self.s3)
        self.hub.attach("d4", self.s4)

    def test_publish_only_reaches_capable_devices(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.assertEqual([message["type"] for message in self.s1.sent], ["message"])
        self.assertEqual(self.s2.sent, [])

    def test_read_ack_broadcasts_read_state(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.s1.sent.clear()
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})
        self.assertEqual(self.s2.sent[-1]["type"], "read_state")
        self.assertEqual(self.s2.sent[-1]["read_by"], ["d1"])

    def test_read_ack_suppresses_unread_message_for_other_devices(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})

        state = self.hub.state_for("d2")

        self.assertEqual(state["messages"], [])

    def test_read_state_is_filtered_by_read_ack_capability(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.s2.sent.clear()
        self.s3.sent.clear()
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})
        self.assertEqual(self.s2.sent[-1]["type"], "read_state")
        self.assertEqual(self.s3.sent, [])

    def test_capability_less_event_is_rejected(self):
        self.hub.handle_device_event("d2", {"type": "voice_command", "text": "hi"})
        self.assertEqual(self.s2.sent[-1]["type"], "error")

    def test_voice_command_reaches_codex(self):
        catalog = VoiceCatalog()
        self.hub.set_codex_catalog(catalog)
        self.hub.handle_device_event("d4", {"type": "voice_command", "text": "跑一下测试"})
        self.assertTrue(self.hub.wait_idle(2.0))
        self.assertEqual(catalog.calls, [("d4", "跑一下测试")])
        self.assertEqual(self.s4.sent[-1],
                         {"type": "task_queued", "text": "指令已发送到电脑端"})

    def test_voice_command_without_text_is_rejected(self):
        catalog = VoiceCatalog()
        self.hub.set_codex_catalog(catalog)
        self.hub.handle_device_event("d4", {"type": "voice_command", "text": "   "})
        self.assertEqual(catalog.calls, [])
        self.assertEqual(self.s4.sent[-1]["type"], "error")

    def test_voice_command_without_catalog_reports_unavailable(self):
        self.hub.handle_device_event("d4", {"type": "voice_command", "text": "在吗"})
        self.assertEqual(self.s4.sent[-1],
                         {"type": "error", "code": "codex_catalog_unavailable"})

    def test_voice_command_failure_is_reported(self):
        self.hub.set_codex_catalog(VoiceCatalog(error=RuntimeError("boom")))
        self.hub.handle_device_event("d4", {"type": "voice_command", "text": "在吗"})
        self.assertTrue(self.hub.wait_idle(2.0))
        self.assertEqual(self.s4.sent[-1], {"type": "error", "code": "codex_catalog_failed"})

    def test_reconnect_state_includes_unread_messages(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        state = self.hub.state_for("d1")
        self.assertEqual([message["message_id"] for message in state["messages"]], ["m1"])

    def test_structured_data_is_sent_only_to_capable_devices(self):
        record = MessageRecord(
            "m-data",
            "t1",
            "result",
            "completed",
            "标题",
            "正文",
            1.0,
            data={"codex": {"task": {"status": "working"}}},
        )
        self.hub.publish(record)
        self.assertEqual(self.s1.sent[-1]["data"], record.data)
        self.assertEqual(self.s2.sent, [])

    def test_body_is_clipped_to_declared_device_budget(self):
        # One protocol, many devices: an ESP32 declares 64 chars and gets a
        # clipped body, while a desktop declares nothing and gets full text.
        self.hub.registry.get("d1").max_text_chars = 64
        body = "很" * 500
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", body, 0.0)
        )
        sent = self.s1.sent[-1]
        self.assertLessEqual(len(sent["body"]), 64)
        self.assertTrue(sent["body"].endswith("..."))

    def test_unlimited_device_receives_full_body(self):
        # A device that declares no budget gets every chunk, so the wire
        # payload still reassembles into the complete body.
        self.hub.registry.get("d1").max_text_chars = None
        body = "很" * 500
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", body, 0.0)
        )
        sent = [message for message in self.s1.sent if message["type"] == "message"]
        self.assertGreater(len(sent), 1)
        self.assertEqual("".join(message["body"] for message in sent), body)

    def test_catalog_request_uses_service_and_echoes_request_id(self):
        catalog = FakeCatalog()
        self.hub.set_codex_catalog(catalog)

        self.hub.handle_device_event(
            "d1",
            {"type": "codex_catalog_request", "request_id": 7},
        )
        self.assertTrue(self.hub.wait_idle(2.0))

        self.assertEqual(len(catalog.events), 1)
        self.assertEqual(catalog.events[0]["request_id"], 7)
        self.assertEqual(
            [message["type"] for message in self.s1.sent],
            ["codex_catalog_begin", "codex_catalog_item", "codex_catalog_end"],
        )
        self.assertTrue(all(message["request_id"] == 7 for message in self.s1.sent))

    def test_catalog_selection_and_cancel_are_routed_to_service(self):
        catalog = FakeCatalog()
        self.hub.set_codex_catalog(catalog)

        self.hub.handle_device_event(
            "d1",
            {"type": "codex_project_select", "id": "p0", "request_id": 8},
        )
        self.hub.handle_device_event(
            "d1",
            {"type": "codex_catalog_cancel"},
        )
        self.hub.handle_device_event(
            "d1",
            {"type": "codex_selection_confirm", "request_id": 8},
        )
        self.assertTrue(self.hub.wait_idle(2.0))

        self.assertEqual(
            [event["type"] for event in catalog.events],
            [
                "codex_project_select",
                "codex_catalog_cancel",
                "codex_selection_confirm",
            ],
        )

    def test_replay_state_prime_catalog_for_task_devices(self):
        catalog = FakeCatalog()
        self.hub.set_codex_catalog(catalog)

        self.hub.replay_state("d1")
        self.hub.replay_state("d2")

        # codex_state comes first (attach-time snapshot), then the catalog.
        self.assertEqual(
            [message["type"] for message in self.s1.sent],
            [
                "codex_state",
                "codex_catalog_begin",
                "codex_catalog_item",
                "codex_catalog_end",
            ],
        )
        self.assertNotIn("request_id", self.s1.sent[1])
        # d2 has status but not tasks: it gets the state snapshot and no catalog.
        self.assertEqual(
            self.s2.sent,
            [{"type": "codex_state", "running": 0, "done": 0, "failed": 0}],
        )

    def test_codex_state_request_returns_task_counters(self):
        self.hub.publish(
            MessageRecord("task:t1", "t1", "task", "working", "任务", "正文", 0.0)
        )
        self.hub.publish(
            MessageRecord("result:t2:1", "t2", "result", "completed", "结果", "正文", 1.0)
        )
        self.hub.publish(
            MessageRecord("result:t3:2", "t3", "result", "failed", "结果", "正文", 2.0)
        )
        self.hub.handle_device_event("d1", {"type": "codex_state_request", "request_id": 7})
        self.assertEqual(
            self.s1.sent[-1],
            {
                "type": "codex_state",
                "running": 1,
                "done": 1,
                "failed": 1,
                "request_id": 7,
            },
        )

    def test_replay_state_pushes_codex_state_to_status_devices(self):
        self.hub.publish(
            MessageRecord("task:t1", "t1", "task", "working", "任务", "正文", 0.0)
        )
        self.s1.sent.clear()
        self.hub.replay_state("d1")
        self.assertIn(
            {"type": "codex_state", "running": 1, "done": 0, "failed": 0},
            self.s1.sent,
        )

    def test_replay_state_skips_codex_state_without_status_capability(self):
        self.hub.registry.set_enabled("d3", ["tasks"])
        self.s3.sent.clear()
        self.hub.replay_state("d3")
        self.assertNotIn(
            "codex_state",
            [message.get("type") for message in self.s3.sent],
        )

    def test_codex_state_request_requires_status_capability(self):
        self.hub.registry.set_enabled("d3", ["tasks"])
        self.hub.handle_device_event("d3", {"type": "codex_state_request"})
        self.assertEqual(
            self.s3.sent[-1],
            {"type": "error", "code": "capability_not_enabled"},
        )





    def test_elapsed_only_change_does_not_reach_devices(self):
        first = MessageRecord(
            "s1", "t1", "status", "working", "任务", "在写代码", 1.0,
            data={"generated_at_epoch": 1.0,
                  "task": {"status": "working", "current_action": "在写代码",
                           "duration_seconds": 3, "token_count": 10}},
        )
        self.hub.publish(first)
        self.s1.sent.clear()
        second = MessageRecord(
            "s1", "t1", "status", "working", "任务", "在写代码", 6.0,
            data={"generated_at_epoch": 6.0,
                  "task": {"status": "working", "current_action": "在写代码",
                           "duration_seconds": 8, "token_count": 90}},
        )
        self.hub.publish(second)
        self.assertEqual(self.s1.sent, [])
        # The refreshed record is still stored for late readers and counts.
        self.assertEqual(self.hub.store.get("s1").data["task"]["duration_seconds"], 8)

    def test_new_current_action_is_delivered(self):
        self.hub.publish(
            MessageRecord("s1", "t1", "status", "working", "任务", "第一步", 1.0,
                          data={"task": {"current_action": "第一步"}})
        )
        self.s1.sent.clear()
        self.hub.publish(
            MessageRecord("s1", "t1", "status", "working", "任务", "第二步", 2.0,
                          data={"task": {"current_action": "第二步"}})
        )
        self.assertEqual([message["type"] for message in self.s1.sent], ["message"])
    def test_unchanged_republish_does_not_reach_devices(self):
        data = {"generated_at_epoch": 1.0, "task": {"status": "completed"}}
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 1.0, data=data)
        )
        self.s1.sent.clear()
        # The collector re-reads the same snapshot every few seconds. Only the
        # timing fields move, so nothing should go over the radio.
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 6.0,
                          data={"generated_at_epoch": 6.0, "task": {"status": "completed"}})
        )
        self.assertEqual(self.s1.sent, [])
        # The refreshed record is still stored, so late readers see current data.
        self.assertEqual(self.hub.store.get("m1").created_at, 6.0)

    def test_changed_republish_is_delivered(self):
        self.hub.publish(MessageRecord("m1", "t1", "status", "working", "任务", "第一步", 1.0))
        self.s1.sent.clear()
        self.hub.publish(MessageRecord("m1", "t1", "status", "working", "任务", "第二步", 2.0))
        self.assertEqual([message["type"] for message in self.s1.sent], ["message"])

    def test_status_change_same_body_is_delivered(self):
        self.hub.publish(MessageRecord("m1", "t1", "status", "working", "任务", "", 1.0))
        self.s1.sent.clear()
        self.hub.publish(MessageRecord("m1", "t1", "status", "completed", "任务", "", 2.0))
        self.assertEqual([message["type"] for message in self.s1.sent], ["message"])

class FakeSpeechJob:
    def poll(self):
        return 0

    def wait(self, timeout=None):
        return True

    def terminate(self):
        pass

    def kill(self):
        pass


class FakeSpeechRecognizer:
    last_error = ""
    recognition_timeout_seconds = 5.0

    def start(self, wav_path):
        return FakeSpeechJob()

    def result(self, process):
        return True, "统一语音结果"


class HubSpeechTest(unittest.TestCase):
    def test_audio_events_use_the_shared_transcription_service(self):
        import base64
        import tempfile
        from pathlib import Path
        from codexbridge.speech import SpeechService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        registry.upsert(
            "d1",
            model="dotii",
            supported=["audio"],
        )
        registry.set_enabled("d1", ["audio"])
        speech = SpeechService(
            recognizer=FakeSpeechRecognizer(),
            output_dir=Path(tmp.name),
        )
        hub = BridgeHub(registry, speech=speech)
        session = FakeSession()
        hub.attach("d1", session)

        hub.handle_device_event("d1", {"type": "record_start"})
        pcm = b"\x00\x00" * 16000
        hub.handle_device_event(
            "d1",
            {"type": "audio_chunk", "data": base64.b64encode(pcm).decode()},
        )
        hub.handle_device_event("d1", {"type": "record_stop", "bytes": len(pcm)})
        hub.poll_speech()

        self.assertEqual(
            session.sent[-1],
            {"type": "transcript", "text": "统一语音结果"},
        )
