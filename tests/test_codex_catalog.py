import json
import tempfile
import unittest
from pathlib import Path

from codexbridge.codex.catalog import CodexCatalogService


class FakeAppServer:
    def __init__(self, rollout_paths=None):
        self.calls = []
        self.rollout_paths = rollout_paths or {}

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "thread/start":
            return {
                "thread": {
                    "id": "thread-new",
                    "name": "新任务",
                    "preview": "新任务",
                    "path": "",
                    "cwd": str(Path(tempfile.gettempdir()) / "repo-a"),
                    "status": {"type": "idle"},
                }
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "gpt-5.1-codex",
                        "model": "gpt-5.1-codex",
                        "displayName": "GPT-5.1 Codex",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [{"effort": "medium"}],
                    },
                    {
                        "id": "gpt-5.1",
                        "model": "gpt-5.1",
                        "displayName": "GPT-5.1",
                        "isDefault": False,
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [{"effort": "high"}],
                    },
                    {"id": "hidden-one", "model": "hidden-one", "hidden": True},
                ]
            }
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "working"}}
        if method == "thread/list":
            return {
                "data": [
                    {
                        "id": "thread-1",
                        "name": "修复登录",
                        "preview": "修复登录",
                        "path": str(self.rollout_paths.get("thread-1") or ""),
                        "cwd": str(Path(tempfile.gettempdir()) / "repo-a"),
                        "status": {"type": "active"},
                    },
                    {
                        "id": "thread-2",
                        "name": "整理文档",
                        "preview": "整理文档",
                        "path": str(self.rollout_paths.get("thread-2") or ""),
                        "cwd": str(Path(tempfile.gettempdir()) / "repo-a"),
                        "status": {"type": "idle"},
                    },
                    {
                        "id": "thread-3",
                        "name": "别的项目",
                        "preview": "别的项目",
                        "path": "",
                        "cwd": str(Path(tempfile.gettempdir()) / "repo-other"),
                        "status": {"type": "idle"},
                    },
                ]
            }
        raise AssertionError(f"unexpected request: {method}")


class CodexCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.desktop_state = self.root / ".codex-global-state.json"
        rollout_one = self.root / "thread-1.jsonl"
        rollout_one.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "登录问题已经修复。",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        rollout_two = self.root / "thread-2.jsonl"
        rollout_two.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "文档已经整理完成。",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.app = FakeAppServer(
            {
                "thread-1": rollout_one,
                "thread-2": rollout_two,
            }
        )

    def _write_desktop_state(self):
        projects = {}
        order = []
        for index in range(9):
            project_id = f"project-{index}"
            order.append(project_id)
            projects[project_id] = {
                "name": f"项目 {index + 1}",
                "rootPaths": [
                    str(
                        Path(tempfile.gettempdir()) / "repo-a"
                        if index == 0
                        else self.root / f"repo-{index}"
                    )
                ],
                "updatedAt": 1_000 - index,
            }
        self.desktop_state.write_text(
            json.dumps(
                {
                    "local-projects": projects,
                    "project-order": order,
                    "thread-project-assignments": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _service(self):
        return CodexCatalogService(
            self.app.request,
            workspace=self.root,
            desktop_state_path=self.desktop_state,
        )

    def test_projects_are_paged_with_short_ids(self):
        self._write_desktop_state()
        service = self._service()

        first = service.handle_device_event(
            "device-1",
            {"type": "codex_catalog_request", "request_id": 1},
        )

        self.assertEqual(
            [message["type"] for message in first],
            ["codex_catalog_begin"]
            + ["codex_catalog_item"] * 8
            + ["codex_catalog_end"],
        )
        self.assertEqual(
            [message["id"] for message in first if message["type"] == "codex_catalog_item"],
            ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "p_more"],
        )
        self.assertEqual(first[1]["title"], "项目 1")
        self.assertEqual(first[-1], {"type": "codex_catalog_end", "scope": "projects"})

        second = service.handle_device_event(
            "device-1",
            {"type": "codex_project_select", "id": "p_more", "request_id": 2},
        )
        items = [message for message in second if message["type"] == "codex_catalog_item"]
        self.assertEqual([message["id"] for message in items], ["p0", "p1", "p_more"])
        self.assertEqual(items[0]["title"], "项目 8")
        self.assertEqual(items[-1]["title"], "回到第1页")


    def test_thread_new_starts_a_real_thread_and_selects_it(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event("device-1", {"type": "codex_catalog_request"})
        service.handle_device_event(
            "device-1", {"type": "codex_project_select", "id": "p0", "request_id": 1}
        )

        created = service.handle_device_event(
            "device-1", {"type": "codex_thread_new", "request_id": 2}
        )

        self.assertTrue(
            any(("thread/start",) == call[0:1] for call in self.app.calls),
            self.app.calls,
        )
        selected = [message for message in created if message["type"] == "codex_selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["title"], "新任务")

        # The new thread is addressable afterwards: the device can push a turn.
        again = service.handle_device_event(
            "device-1", {"type": "codex_thread_select", "id": "t0", "request_id": 3}
        )
        self.assertEqual(again[0]["type"], "codex_selected")

    def test_thread_new_keeps_existing_threads_selectable(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event("device-1", {"type": "codex_catalog_request"})
        service.handle_device_event(
            "device-1", {"type": "codex_project_select", "id": "p0", "request_id": 1}
        )

        created = service.handle_device_event(
            "device-1", {"type": "codex_thread_new", "request_id": 2}
        )

        items = [
            message for message in created if message["type"] == "codex_catalog_item"
        ]
        self.assertTrue(items, f"expected a refreshed thread list: {created}")
        titles = [item["title"] for item in items]
        self.assertIn("新任务", titles)
        self.assertIn("修复登录", titles)

        # Both the new and the pre-existing thread stay addressable.
        for key in ("t0", "t1", "t2"):
            reply = service.handle_device_event(
                "device-1",
                {"type": "codex_thread_select", "id": key, "request_id": 10},
            )
            self.assertEqual(reply[0]["type"], "codex_selected", f"key {key}")

    def test_thread_new_failure_reports_a_readable_error(self):
        self._write_desktop_state()
        service = self._service()

        def failing(method, params=None):
            raise RuntimeError("app-server down")

        service._request = failing
        created = service.handle_device_event(
            "device-1", {"type": "codex_thread_new", "request_id": 1}
        )
        self.assertEqual(created[0]["type"], "error")
        self.assertIn("新建任务失败", created[0]["text"])

    def test_thread_new_passes_selected_model(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event("device-1", {"type": "codex_catalog_request"})
        service.handle_device_event(
            "device-1", {"type": "codex_project_select", "id": "p0", "request_id": 1}
        )
        self.app.calls.clear()
        service.handle_device_event(
            "device-1",
            {"type": "codex_thread_new", "model": "gpt-5.1-codex", "request_id": 2},
        )
        start_call = [call for call in self.app.calls if call[0] == "thread/start"]
        self.assertEqual(len(start_call), 1)
        self.assertEqual(start_call[0][1].get("model"), "gpt-5.1-codex")

    def test_project_and_thread_selection_return_device_messages(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event("device-1", {"type": "codex_catalog_request"})

        threads = service.handle_device_event(
            "device-1",
            {"type": "codex_project_select", "id": "p0", "request_id": 3},
        )

        self.assertEqual(threads[0], {"type": "codex_catalog_begin", "scope": "threads"})
        items = [message for message in threads if message["type"] == "codex_catalog_item"]
        self.assertEqual([message["id"] for message in items], ["t0", "t1"])
        self.assertEqual(items[0]["title"], "修复登录")
        self.assertEqual(threads[-1], {"type": "codex_catalog_end", "scope": "threads"})

        selected = service.handle_device_event(
            "device-1",
            {"type": "codex_thread_select", "id": "t0", "request_id": 4},
        )

        self.assertEqual(
            selected,
            [
                {
                    "type": "codex_selected",
                    "title": "修复登录",
                    "project": "项目 1",
                    "last_message": "登录问题已经修复。",
                    # The id tells the host which thread this device now follows,
                    # so other sessions cannot cut into its screen.
                    "thread_id": "thread-1",
                }
            ],
        )

    def test_model_catalog_uses_short_ids_and_marks_default(self):
        service = self._service()
        catalog = service.handle_device_event(
            "device-1",
            {"type": "codex_catalog_request", "scope": "models", "request_id": 1},
        )
        self.assertEqual(catalog[0], {"type": "codex_catalog_begin", "scope": "models"})
        items = [m for m in catalog if m["type"] == "codex_catalog_item"]
        self.assertEqual([m["id"] for m in items], ["m0", "m1"])
        self.assertEqual(items[0]["title"], "GPT-5.1 Codex（默认）")
        self.assertNotIn("hidden-one", json.dumps(catalog, ensure_ascii=False))
        self.assertEqual(catalog[-1], {"type": "codex_catalog_end", "scope": "models"})

    def test_model_selection_is_remembered_for_new_threads(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event(
            "device-1",
            {"type": "codex_catalog_request", "scope": "models", "request_id": 1},
        )
        chosen = service.handle_device_event(
            "device-1", {"type": "codex_model_select", "id": "m1", "request_id": 2}
        )
        self.assertEqual(chosen[0]["type"], "codex_model_selected")
        self.assertEqual(chosen[0]["model"], "gpt-5.1")

        service.handle_device_event("device-1", {"type": "codex_catalog_request"})
        service.handle_device_event(
            "device-1", {"type": "codex_project_select", "id": "p0", "request_id": 3}
        )
        self.app.calls.clear()
        service.handle_device_event(
            "device-1", {"type": "codex_thread_new", "request_id": 4}
        )
        starts = [c for c in self.app.calls if c[0] == "thread/start"]
        self.assertEqual(starts[0][1].get("model"), "gpt-5.1")

    def test_model_select_without_catalog_refreshes_the_list(self):
        service = self._service()
        reply = service.handle_device_event(
            "device-1", {"type": "codex_model_select", "id": "m0", "request_id": 9}
        )
        self.assertEqual(reply[0]["type"], "codex_catalog_begin")

    def test_voice_command_starts_a_turn_on_the_selected_thread(self):
        self._write_desktop_state()
        service = self._service()
        service.handle_device_event("device-1", {"type": "codex_catalog_request"})
        service.handle_device_event(
            "device-1", {"type": "codex_project_select", "id": "p0", "request_id": 1}
        )
        service.handle_device_event(
            "device-1", {"type": "codex_thread_select", "id": "t0", "request_id": 2}
        )
        self.app.calls.clear()
        reply = service.handle_voice_command("device-1", "帮我把测试跑完")
        self.assertEqual(
            reply,
            [{"type": "task_queued", "text": "指令已发送到电脑端",
             # the device starts following this conversation for its answer
             "thread_id": "thread-1"}],
        )
        turn = [c for c in self.app.calls if c[0] == "turn/start"]
        self.assertEqual(len(turn), 1)
        self.assertEqual(turn[0][1]["threadId"], "thread-1")
        self.assertEqual(turn[0][1]["input"][0]["text"], "帮我把测试跑完")

    def test_voice_command_without_thread_creates_one(self):
        service = self._service()
        reply = service.handle_voice_command("device-1", "新建一个任务")
        self.assertEqual(reply[0]["type"], "task_queued")
        methods = [c[0] for c in self.app.calls]
        self.assertEqual(methods, ["thread/start", "turn/start"])

    def test_voice_command_steers_when_a_turn_is_running(self):
        service = self._service()

        def request(method, params=None):
            if method == "turn/start":
                raise RuntimeError("thread has an active turn")
            return {"thread": {"id": "thread-x"}}

        service._request = request
        reply = service.handle_voice_command("device-1", "补一句话")
        self.assertEqual(reply[0]["type"], "task_queued")

    def test_voice_command_reports_failures_readably(self):
        service = self._service()
        self.assertEqual(service.handle_voice_command("device-1", "  ")[0]["type"], "error")

        def failing(method, params=None):
            raise RuntimeError("app-server down")

        service._request = failing
        self.assertEqual(service.handle_voice_command("device-1", "在吗")[0]["type"], "error")


if __name__ == "__main__":
    unittest.main()
