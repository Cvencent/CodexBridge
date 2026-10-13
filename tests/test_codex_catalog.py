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
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
