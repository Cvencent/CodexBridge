"""Tests for the companion app-server service."""

from __future__ import annotations

import json
import queue
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codexbridge.codex.app_server import AppServerError
from codexbridge.companion.service import CompanionService
from codexbridge.read_state import ReadStateStore


class FakeClient:
    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.requests: list[tuple[str, dict]] = []
        self.responses_sent: list[tuple[int, dict]] = []
        self.notifications: queue.Queue = queue.Queue()
        self.server_requests: queue.Queue = queue.Queue()
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def request(self, method: str, params: dict | None = None):
        self.requests.append((method, params or {}))
        handler = self.responses.get(method)
        if handler is None:
            raise AppServerError(f"unsupported: {method}")
        return handler(params or {}) if callable(handler) else handler

    def respond(self, request_id: int, result: dict) -> None:
        self.responses_sent.append((request_id, result))


def thread_row(thread_id: str = "t1", **overrides):
    row = {
        "id": thread_id,
        "name": "桌面任务",
        "preview": "帮我看一下构建结果",
        "model": "gpt-5.1-codex",
        "cwd": r"D:\work",
        "source": "vscode",
        "createdAt": 1791000000,
        "updatedAt": 1791000200,
        "recencyAt": 1791000200,
        "status": {"type": "notLoaded"},
        "turns": [],
    }
    row.update(overrides)
    return row


def thread_detail(thread_id: str = "t1"):
    return {
        "thread": {
            **thread_row(thread_id),
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 1791000000,
                    "completedAt": 1791000100,
                    "items": [
                        {
                            "id": "item-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "帮我看一下构建结果"}],
                        },
                        {
                            "id": "item-2",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "先检查日志",
                        },
                        {
                            "id": "item-3",
                            "type": "agentMessage",
                            "phase": "final",
                            "text": "构建通过",
                        },
                        {
                            "id": "item-4",
                            "type": "commandExecution",
                            "command": "gradle build",
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "BUILD SUCCESSFUL",
                        },
                    ],
                }
            ],
        }
    }


class CompanionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {
            "data": [thread_row(), thread_row("t2", name="第二个任务")],
            "nextCursor": None,
        }
        self.client.responses["thread/read"] = thread_detail()
        self.client.responses["model/list"] = {
            "data": [
                {
                    "id": "gpt-5.1-codex",
                    "model": "gpt-5.1-codex",
                    "displayName": "GPT-5.1 Codex",
                    "description": "默认模型",
                    "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [{"effort": "medium", "description": "平衡"}],
                    "inputModalities": ["text"],
                }
            ]
        }
        self.client.responses["account/rateLimits/read"] = {"rateLimits": {}}
        self.client.responses["account/usage/read"] = {}
        self.client.responses["thread/resume"] = {"thread": thread_row()}
        self.client.responses["turn/start"] = {"turn": {"id": "turn-2", "status": "inProgress"}}
        self.client.responses["turn/interrupt"] = {}
        self.service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def _write_rollout(self, thread_id: str, kind: str, seconds_ago: float = 0.0) -> None:
        day = datetime.now()
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        record = {
            "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": kind},
        }
        path = folder / f"rollout-{thread_id}.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_list_threads_reports_workspace_name(self) -> None:
        result = self.service.list_threads(limit=10)
        self.assertEqual(result["threads"][0]["workspace"], "work")
        self.assertEqual(result["threads"][0]["cwd"], r"D:\work")

    def test_list_threads_marks_active_rollout_first(self) -> None:
        # t2 is older by recency, but the desktop is still writing its rollout.
        self._write_rollout("t2", "task_started")
        result = self.service.list_threads(limit=10)
        self.assertEqual(result["threads"][0]["thread_id"], "t2")
        self.assertEqual(result["threads"][0]["status"], "working")
        self.assertEqual(result["threads"][1]["status"], "idle")

    def test_list_threads_marks_finished_rollout_completed(self) -> None:
        self._write_rollout("t2", "task_complete", seconds_ago=30)
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "completed")

    def test_read_thread_uses_live_rollout_status_for_desktop_turn(self) -> None:
        detail = thread_detail("t2")
        detail["thread"]["status"] = {"type": "notLoaded"}
        detail["thread"]["turns"][-1]["status"] = "interrupted"
        self.client.responses["thread/read"] = detail
        self._write_rollout("t2", "task_started", seconds_ago=1)

        result = self.service.read_thread("t2")

        self.assertEqual(result["thread"]["status"], "working")
        self.assertEqual(result["task"]["status"], "working")

    def test_list_threads_marks_read_completion_idle(self) -> None:
        self._write_rollout("t2", "task_complete", seconds_ago=30)
        read_state = ReadStateStore(None)
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            read_state=read_state,
        )
        try:
            service.list_threads(limit=10)  # populate the log cache
            read_state.mark_thread("t2", int(self.service._thread_log("t2")[2]))
            result = service.list_threads(limit=10)
        finally:
            service.stop()
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "idle")

    def test_list_threads_ignores_stale_rollout(self) -> None:
        self._write_rollout("t2", "task_started", seconds_ago=600)
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "idle")

    def test_list_threads_maps_rows(self) -> None:
        result = self.service.list_threads(limit=10)
        self.assertEqual(len(result["threads"]), 2)
        first = result["threads"][0]
        self.assertEqual(first["thread_id"], "t1")
        self.assertEqual(first["title"], "桌面任务")
        self.assertEqual(first["model"], "gpt-5.1-codex")
        self.assertIn(first["status"], {"idle", "completed", "working"})
        self.assertEqual(self.client.requests[0][0], "thread/list")

    def test_read_thread_maps_messages(self) -> None:
        detail = self.service.read_thread("t1")
        kinds = [item["kind"] for item in detail["messages"]]
        self.assertEqual(kinds, ["user", "commentary", "agent", "command"])
        self.assertEqual(detail["messages"][0]["text"], "帮我看一下构建结果")
        self.assertEqual(detail["messages"][3]["detail"]["exit_code"], 0)
        self.assertEqual(detail["messages"][3]["detail"]["output"], "BUILD SUCCESSFUL")
        self.assertEqual(detail["thread"]["completed_at"], 1791000100)

    def test_start_turn_publishes_event(self) -> None:
        target = self.service.subscribe()
        result = self.service.start_turn("t1", "继续修复", model="gpt-5.1-codex")
        self.assertEqual(result["turn_id"], "turn-2")
        event = target.get(timeout=1.0)
        self.assertEqual(event["type"], "turn_started")
        self.assertEqual(event["thread_id"], "t1")
        method, params = self.client.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"][0]["text"], "继续修复")
        self.assertEqual(params["model"], "gpt-5.1-codex")

    def test_start_turn_forks_when_thread_is_locked_by_another_writer(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}

        def resume(_params):
            calls["resume"] += 1
            raise AppServerError("thread t1 already has an active writer")

        def fork(params):
            calls["fork"] += 1
            self.assertEqual(params["threadId"], "t1")
            return {"thread": thread_row("t1-fork")}

        def start(params):
            calls["start"] += 1
            # turn runs on the fork we own, not the locked original
            self.assertEqual(params["threadId"], "t1-fork")
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        result = self.service.start_turn("t1", "继续这个任务", model="gpt-5.1-codex")

        self.assertEqual(result["status"], "working")
        self.assertEqual(result["continued_as"], "t1-fork")
        self.assertEqual(calls, {"resume": 1, "fork": 1, "start": 1})

    def test_start_turn_reuses_fork_on_next_reply(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        forks = {"n": 0}

        def fork(_params):
            forks["n"] += 1
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "第一次")
        result = self.service.start_turn("t1", "第二次")

        self.assertEqual(forks["n"], 1)
        self.assertEqual(result["continued_as"], "t1-fork")

    def test_fork_is_named_so_the_desktop_can_tell_it_apart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            self.assertEqual(params["threadId"], "t1-fork")
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        names: list[dict] = []

        def name_set(params):
            names.append(params)
            return {}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = name_set

        self.service.start_turn("t1", "继续这个任务")

        self.assertEqual(len(names), 1)
        self.assertEqual(names[0]["threadId"], "t1-fork")
        self.assertIn("手机继续", names[0]["name"])
        self.assertIn("桌面任务", names[0]["name"])

    def test_renaming_the_fork_keeps_the_phone_task_title(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            if params["threadId"] == "t1-fork":
                # Once renamed, the stored fork no longer carries the old name.
                return {"thread": thread_row("t1-fork", name="手机继续 · 桌面任务")}
            return thread_detail()

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = {}

        self.service.start_turn("t1", "继续这个任务")

        listing = self.service.list_threads()
        row = next(item for item in listing["threads"] if item["thread_id"] == "t1")
        self.assertEqual(row["title"], "桌面任务")

        detail = self.service.read_thread("t1")
        self.assertEqual(detail["thread"]["title"], "桌面任务")

    def test_fork_titles_survive_restart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            if params["threadId"] == "t1-fork":
                return {"thread": thread_row("t1-fork", name="手机继续 · 桌面任务")}
            return thread_detail()

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = {}

        self.service.start_turn("t1", "继续这个任务")

        restarted = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            detail = restarted.read_thread("t1")
            self.assertEqual(detail["thread"]["title"], "桌面任务")
        finally:
            restarted.stop()

    def test_fork_naming_failure_does_not_break_the_reply(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        def name_set(_params):
            raise AppServerError("thread metadata update failed")

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/name/set"] = name_set

        result = self.service.start_turn("t1", "继续这个任务")

        self.assertEqual(result["continued_as"], "t1-fork")

    def test_fork_publishes_thread_forked_event(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        subscriber = self.service.subscribe()
        try:
            self.service.start_turn("t1", "继续")
            events = []
            while not subscriber.empty():
                events.append(subscriber.get_nowait())
        finally:
            self.service.unsubscribe(subscriber)

        forked = [event for event in events if event.get("type") == "thread_forked"]
        self.assertEqual(len(forked), 1)
        self.assertEqual(forked[0]["thread_id"], "t1")
        self.assertEqual(forked[0]["continued_as"], "t1-fork")

    def test_read_thread_reports_continuation_fork(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "继续")

        detail = self.service.read_thread("t1")
        self.assertEqual(detail["continued_as"], "t1-fork")

    def test_read_thread_reports_no_continuation_for_free_thread(self) -> None:
        detail = self.service.read_thread("t1")
        self.assertEqual(detail["continued_as"], "")

    def test_thread_summary_surfaces_provider_failure_reason(self) -> None:
        payload = json.dumps(
            {
                "error": {
                    "message": "[workbuddy] upstream client (http 400): "
                    + json.dumps({"code": 11129, "msg": "invalid function call parameters"})
                }
            }
        )
        detail = thread_detail("t1")
        detail["thread"]["turns"][0]["status"] = "failed"
        detail["thread"]["turns"][0]["error"] = {"message": payload}
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(
            result["thread"]["error_message"],
            "[workbuddy] upstream client (http 400): invalid function call parameters",
        )

    def test_turn_completed_event_carries_provider_error(self) -> None:
        payload = {
            "threadId": "t1",
            "turn": {
                "id": "turn-9",
                "status": "failed",
                "completedAt": 1791000300,
                "error": {"message": json.dumps({"error": {"message": "provider exploded"}})},
            },
        }

        event = self.service._event_payload({"method": "turn/completed", "params": payload})

        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_message"], "provider exploded")

    def test_fork_rename_does_not_retitle_the_phone_thread(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "继续")

        event = self.service._event_payload(
            {"method": "thread/name/updated", "params": {"threadId": "t1-fork", "name": "手机继续 · 桌面任务"}}
        )
        self.assertIsNone(event)

    def test_start_turn_reports_locked_thread_when_fork_fails(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            raise AppServerError("fork unsupported")

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        with self.assertRaises(AppServerError) as ctx:
            self.service.start_turn("t1", "继续")

        self.assertIn("电脑端", str(ctx.exception))

    def test_forked_reply_is_transparent_in_list_and_read(self) -> None:
        # Lock t1, continue it, then make sure the phone still sees one task
        # named t1 whose history comes from the fork.
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        detail = thread_detail("t1-fork")
        detail["thread"]["turns"][0]["status"] = "completed"
        self.client.responses["thread/read"] = detail
        self.client.responses["thread/list"] = {
            "data": [thread_row("t1-fork"), thread_row("t2", name="第二个任务")],
            "nextCursor": None,
        }

        self.service.start_turn("t1", "继续这个任务")

        listing = self.service.list_threads(limit=10)
        visible = [item["thread_id"] for item in listing["threads"]]
        self.assertIn("t1", visible)
        self.assertNotIn("t1-fork", visible)

        read = self.service.read_thread("t1")
        self.assertEqual(read["thread"]["thread_id"], "t1")
        self.assertEqual(read["task"]["thread_id"], "t1")

    def test_fork_completion_flips_to_idle_after_read(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        detail = thread_detail("t1-fork")
        self.client.responses["thread/read"] = detail
        self.client.responses["thread/list"] = {"data": [thread_row("t1-fork")], "nextCursor": None}

        self.service.start_turn("t1", "继续")
        self._write_rollout("t1-fork", "task_complete", seconds_ago=20)

        before = self.service.list_threads(limit=10)["threads"][0]
        self.assertEqual(before["status"], "completed")

        self.service.mark_thread_read("t1")

        after = self.service.list_threads(limit=10)["threads"][0]
        self.assertEqual(after["thread_id"], "t1")
        self.assertEqual(after["status"], "idle")

    def test_fork_events_report_origin_thread_id(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.service.start_turn("t1", "继续")

        payload = self.service._event_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "t1-fork", "turnId": "turn-2", "itemId": "i1", "delta": "好"},
            }
        )
        self.assertEqual(payload["thread_id"], "t1")

    def test_fork_mapping_survives_restart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.service.start_turn("t1", "继续")
        self.service.stop()

        restored = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            forks = {"n": 0}

            def fork_again(_params):
                forks["n"] += 1
                return {"thread": thread_row("t1-fork2")}

            self.client.responses["thread/fork"] = fork_again
            result = restored.start_turn("t1", "第二次")
        finally:
            restored.stop()
        # The persisted mapping is reused, so no second fork is created.
        self.assertEqual(forks["n"], 0)
        self.assertEqual(result["continued_as"], "t1-fork")

    def test_start_turn_tolerates_active_writer_during_resume(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}

        def resume(_params):
            calls["resume"] += 1
            raise AppServerError("thread already has an active writer")

        def fork(_params):
            calls["fork"] += 1
            return {"thread": thread_row("t1-fork")}

        def start(_params):
            calls["start"] += 1
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        result = self.service.start_turn("t1", "继续这个旧任务")

        self.assertEqual(result["status"], "working")
        self.assertEqual(calls, {"resume": 1, "fork": 1, "start": 1})

    def test_turn_completed_event_uses_desktop_completion_timestamp(self) -> None:
        event = self.service._event_payload(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t1",
                    "turn": {
                        "id": "turn-2",
                        "status": "completed",
                        "completedAt": 1791000100,
                        "durationMs": 1200,
                    },
                },
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["completed_at"], 1791000100)

    def test_steer_turn_requires_an_active_turn(self) -> None:
        # A resident thread with no in-progress turn cannot be steered; the
        # app-server requires expectedTurnId, so surface a clear error instead
        # of sending an invalid steer.
        calls = {"steer": 0}

        def steer(_params):
            calls["steer"] += 1
            return {"turn": {"id": "turn-3", "status": "inProgress"}}

        self.client.responses["turn/steer"] = steer

        with self.assertRaises(AppServerError):
            self.service.steer_turn("t1", "换个方向")

        self.assertEqual(calls["steer"], 0)

    def test_steer_turn_sends_expected_turn_id(self) -> None:
        detail = thread_detail()
        detail["thread"]["turns"].append(
            {
                "id": "turn-9",
                "status": "inProgress",
                "startedAt": 1791000200,
                "items": [],
            }
        )
        self.client.responses["thread/read"] = detail
        seen = {}

        def steer(params):
            seen.update(params)
            return {"turn": {"id": "turn-9", "status": "inProgress"}}

        self.client.responses["turn/steer"] = steer
        self.service.steer_turn("t1", "换个方向")

        self.assertEqual(seen["expectedTurnId"], "turn-9")
        self.assertEqual(seen["threadId"], "t1")
    def test_start_turn_resumes_disk_thread_first(self) -> None:
        self.service.start_turn("t1", "继续修复", model="gpt-5.1-codex")
        methods = [method for method, _ in self.client.requests]
        self.assertIn("thread/resume", methods)
        self.assertLess(methods.index("thread/resume"), methods.index("turn/start"))
        resume_params = next(
            params for method, params in self.client.requests if method == "thread/resume"
        )
        self.assertEqual(resume_params["threadId"], "t1")
        self.assertTrue(resume_params["excludeTurns"])

    def test_start_turn_skips_repeat_resume_for_same_thread(self) -> None:
        self.service.start_turn("t1", "第一次")
        self.service.start_turn("t1", "第二次")
        resumes = [method for method, _ in self.client.requests if method == "thread/resume"]
        self.assertEqual(len(resumes), 1)
        turns = [method for method, _ in self.client.requests if method == "turn/start"]
        self.assertEqual(len(turns), 2)

    def test_steer_turn_resumes_thread(self) -> None:
        detail = thread_detail()
        detail["thread"]["turns"].append(
            {"id": "turn-9", "status": "inProgress", "startedAt": 1791000200, "items": []}
        )
        self.client.responses["thread/read"] = detail
        self.client.responses["turn/steer"] = {"turn": {"id": "turn-9", "status": "inProgress"}}
        self.service.steer_turn("t1", "改一下方向")
        methods = [method for method, _ in self.client.requests]
        self.assertLess(methods.index("thread/resume"), methods.index("turn/steer"))

    def test_resume_cleared_after_client_reset(self) -> None:
        clients = []

        def factory(command, cwd):
            client = FakeClient()
            clients.append(client)
            client.responses["thread/resume"] = {"thread": thread_row()}
            client.responses["turn/start"] = {"turn": {"id": "turn-2", "status": "inProgress"}}
            return client

        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=factory,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            service.start_turn("t1", "第一次")
            service._reset_client()
            service.start_turn("t1", "第二次")
        finally:
            service.stop()
        self.assertGreaterEqual(len(clients), 2)
        self.assertEqual(
            [method for method, _ in clients[0].requests if method == "thread/resume"],
            ["thread/resume"],
        )
        self.assertEqual(
            [method for method, _ in clients[1].requests if method == "thread/resume"],
            ["thread/resume"],
        )

    def test_completed_thread_shows_idle_once_read(self) -> None:
        read_state = ReadStateStore(None)
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            read_state=read_state,
        )
        try:
            self.assertEqual(service.read_thread("t1")["thread"]["status"], "completed")
            read_state.mark_thread("t1", 1791000100)
            self.assertEqual(service.read_thread("t1")["thread"]["status"], "idle")
        finally:
            service.stop()

    def test_mark_thread_read_publishes_idle_for_other_clients(self) -> None:
        target = self.service.subscribe()

        result = self.service.mark_thread_read("t1")

        self.assertEqual(result["status"], "idle")
        event = target.get(timeout=1.0)
        self.assertEqual(event["type"], "thread_status")
        self.assertEqual(event["thread_id"], "t1")
        self.assertEqual(event["status"], "idle")

    def test_notification_pump_forwards_agent_delta(self) -> None:
        self.service.start()
        target = self.service.subscribe()
        self.client.notifications.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "t1", "turnId": "turn-2", "itemId": "i1", "delta": "你好"},
            }
        )
        event = target.get(timeout=2.0)
        self.assertEqual(event["type"], "agent_delta")
        self.assertEqual(event["delta"], "你好")

    def test_approval_request_can_be_resolved(self) -> None:
        self.service.start()
        target = self.service.subscribe()
        self.client.server_requests.put(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn-2",
                    "itemId": "i9",
                    "command": "rm -rf build",
                    "reason": "需要删除构建目录",
                },
            }
        )
        event = target.get(timeout=2.0)
        self.assertEqual(event["type"], "approval_request")
        self.assertEqual(event["request_id"], 7)
        self.assertEqual(event["command"], "rm -rf build")
        pending = self.service.pending_approvals()
        self.assertEqual(len(pending), 1)
        self.assertTrue(self.service.resolve_approval(7, "accept"))
        self.assertEqual(self.client.responses_sent[-1], (7, {"decision": "accept"}))
        self.assertEqual(self.service.pending_approvals(), [])

    def test_models_and_projects(self) -> None:
        models = self.service.list_models()["models"]
        self.assertEqual(models[0]["display_name"], "GPT-5.1 Codex")
        self.assertTrue(models[0]["is_default"])
        projects = self.service.list_projects()
        self.assertIsInstance(projects["projects"], list)

    def test_snapshot_includes_account_and_tasks(self) -> None:
        snapshot = self.service.snapshot()
        self.assertIn("account", snapshot)
        self.assertIn("tasks", snapshot)
        self.assertIn("pending_approvals", snapshot)

    def test_interrupt_uses_latest_turn(self) -> None:
        result = self.service.interrupt("t1")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(self.client.requests[-1][0], "turn/interrupt")


if __name__ == "__main__":
    unittest.main()
