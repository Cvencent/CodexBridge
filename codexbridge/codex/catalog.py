from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

CATALOG_ITEMS_PER_PAGE = 7
CATALOG_TITLE_CHARS = 22
CATALOG_DETAIL_CHARS = 28
LAST_MESSAGE_CHARS = 120
DEFAULT_DESKTOP_STATE_PATH = Path.home() / ".codex" / ".codex-global-state.json"

def _safe_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("`", "").replace("**", "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return " ".join(parts).strip()


def _rollout_path(path: Any, thread_id: str) -> Path | None:
    if path:
        candidate = Path(str(path)).expanduser()
        if candidate.is_file():
            return candidate
    if not thread_id:
        return None
    home = Path.home() / ".codex"
    newest: Path | None = None
    for root in (home / "sessions", home / "archived_sessions"):
        if not root.is_dir():
            continue
        for candidate in root.rglob(f"*{thread_id}*.jsonl"):
            try:
                if newest is None or candidate.stat().st_mtime > newest.stat().st_mtime:
                    newest = candidate
            except OSError:
                continue
    return newest


def _last_assistant_text(
    thread: dict[str, Any],
    limit: int = LAST_MESSAGE_CHARS,
) -> str:
    rollout = _rollout_path(thread.get("path"), str(thread.get("id") or ""))
    if rollout is None:
        return ""
    try:
        size = rollout.stat().st_size
        with rollout.open("rb") as handle:
            if size > 256 * 1024:
                handle.seek(size - 256 * 1024)
                handle.readline()
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    fallback = ""
    for line in reversed(raw.splitlines()):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "event_msg":
            event_type = str(payload.get("type") or "")
            if event_type == "task_complete":
                text = payload.get("last_agent_message")
                if isinstance(text, str) and text.strip():
                    return _safe_text(text, limit)
            elif event_type == "agent_message":
                text = payload.get("message")
                if isinstance(text, str) and text.strip():
                    if str(payload.get("phase") or "").lower() == "final_answer":
                        return _safe_text(text, limit)
                    if not fallback:
                        fallback = text
        elif (
            record.get("type") == "response_item"
            and str(payload.get("type") or "") == "message"
            and str(payload.get("role") or "") == "assistant"
        ):
            text = _message_content_text(payload.get("content"))
            if text and not fallback:
                fallback = text
    return _safe_text(fallback, limit) if fallback else ""


@dataclass
class _CatalogState:
    project_catalog: list[dict[str, Any]] = field(default_factory=list)
    project_page: int = 0
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected_project: dict[str, Any] | None = None
    thread_assignments: dict[str, Any] = field(default_factory=dict)


class CodexCatalogService:
    """Translate device catalog events into app-server project/thread queries."""

    def __init__(
        self,
        request: Callable[[str, dict[str, Any]], Any],
        *,
        workspace: Path,
        desktop_state_path: Path | None = None,
    ) -> None:
        self._request = request
        self.workspace = workspace.expanduser()
        self.desktop_state_path = (
            desktop_state_path
            if desktop_state_path is not None
            else DEFAULT_DESKTOP_STATE_PATH
        )
        self._states: dict[str, _CatalogState] = {}
        self._lock = threading.RLock()

    def handle_device_event(
        self, device_id: str, event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        with self._lock:
            return self._handle_device_event(device_id, event)

    def _handle_device_event(
        self, device_id: str, event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if event_type == "codex_catalog_request":
            return self._list_projects(device_id)
        if event_type == "codex_project_select":
            return self._list_threads(device_id, str(event.get("id") or ""))
        if event_type == "codex_thread_select":
            return self._select_thread(device_id, str(event.get("id") or ""))
        if event_type == "codex_thread_new":
            return [{"type": "error", "text": "请先在电脑 Codex 新建任务，再回设备选择"}]
        if event_type in {"codex_catalog_cancel", "codex_selection_confirm"}:
            self._states.pop(device_id, None)
            return []
        return []

    def _desktop_projects(self, state: _CatalogState) -> list[dict[str, Any]]:
        try:
            desktop = json.loads(self.desktop_state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        local_projects = desktop.get("local-projects")
        if not isinstance(local_projects, dict):
            return []
        order = desktop.get("project-order")
        if not isinstance(order, list) or not order:
            order = sorted(
                local_projects,
                key=lambda project_id: int(
                    (local_projects.get(project_id) or {}).get("updatedAt") or 0
                ),
                reverse=True,
            )
        assignments = desktop.get("thread-project-assignments")
        state.thread_assignments = assignments if isinstance(assignments, dict) else {}
        projects: list[dict[str, Any]] = []
        for raw_id in order:
            raw = local_projects.get(str(raw_id))
            if not isinstance(raw, dict):
                continue
            roots = [
                str(path)
                for path in (raw.get("rootPaths") or [])
                if isinstance(path, str) and path
            ]
            if not roots:
                continue
            projects.append(
                {
                    "project_id": None,
                    "desktop_project_id": str(raw_id),
                    "cwd": roots[0],
                    "roots": roots,
                    "name": str(raw.get("name") or Path(roots[0]).name or "未命名项目"),
                }
            )
        return projects

    def _list_projects(self, device_id: str) -> list[dict[str, Any]]:
        state = self._states.setdefault(device_id, _CatalogState())
        projects = self._desktop_projects(state)
        if not projects:
            result = self._request("project/list", {"limit": 100})
            rows = result.get("data") if isinstance(result, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    roots = [
                        str(root.get("path"))
                        for root in row.get("roots") or []
                        if isinstance(root, dict) and root.get("path")
                    ]
                    if not roots:
                        continue
                    projects.append(
                        {
                            "project_id": str(row.get("id") or "") or None,
                            "desktop_project_id": None,
                            "cwd": roots[0],
                            "roots": roots,
                            "name": str(row.get("name") or Path(roots[0]).name),
                        }
                    )
        if not projects:
            projects = [
                {
                    "project_id": None,
                    "desktop_project_id": None,
                    "cwd": str(self.workspace),
                    "roots": [str(self.workspace)],
                    "name": self.workspace.name or "未分组项目",
                }
            ]
        state.project_catalog = projects
        state.project_page = 0
        return self._render_project_page(state)

    def _render_project_page(self, state: _CatalogState) -> list[dict[str, Any]]:
        projects = state.project_catalog
        page_count = max(1, (len(projects) + CATALOG_ITEMS_PER_PAGE - 1) // CATALOG_ITEMS_PER_PAGE)
        start = state.project_page * CATALOG_ITEMS_PER_PAGE
        visible = projects[start : start + CATALOG_ITEMS_PER_PAGE]
        state.projects = {}
        messages: list[dict[str, Any]] = [
            {"type": "codex_catalog_begin", "scope": "projects"}
        ]
        for index, project in enumerate(visible):
            key = f"p{index}"
            state.projects[key] = project
            messages.append(
                self._item(
                    "projects",
                    key,
                    project["name"],
                    f"项目 {start + index + 1}/{len(projects)}",
                )
            )
        if page_count > 1:
            next_page = (state.project_page + 1) % page_count
            state.projects["p_more"] = {"page": next_page}
            title = "回到第1页" if next_page == 0 else f"下一页 {next_page + 1}/{page_count}"
            messages.append(
                self._item("projects", "p_more", title, f"共 {len(projects)} 个项目")
            )
        messages.append({"type": "codex_catalog_end", "scope": "projects"})
        return messages

    def _list_threads(self, device_id: str, project_key: str) -> list[dict[str, Any]]:
        state = self._states.setdefault(device_id, _CatalogState())
        project = state.projects.get(project_key)
        if project is None:
            return [{"type": "error", "text": "选择的 Codex 项目已经失效"}]
        if "page" in project:
            state.project_page = int(project["page"])
            return self._render_project_page(state)
        state.selected_project = project
        threads = self._project_threads(state, project)
        state.threads = {}
        messages: list[dict[str, Any]] = [
            {"type": "codex_catalog_begin", "scope": "threads"}
        ]
        for index, thread in enumerate(threads[:CATALOG_ITEMS_PER_PAGE + 1]):
            key = f"t{index}"
            title = str(thread.get("name") or thread.get("preview") or "未命名任务")
            state.threads[key] = thread
            status = thread.get("status")
            status_type = status.get("type") if isinstance(status, dict) else status
            detail = "进行中" if status_type in {"active", "running"} else "最近任务"
            messages.append(self._item("threads", key, title, detail))
        messages.append({"type": "codex_catalog_end", "scope": "threads"})
        return messages

    @staticmethod
    def _path_is_within(path: str, roots: list[str]) -> bool:
        try:
            candidate = Path(path).expanduser().resolve()
            return any(
                candidate.is_relative_to(Path(root).expanduser().resolve())
                for root in roots
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _project_threads(
        self, state: _CatalogState, project: dict[str, Any]
    ) -> list[dict[str, Any]]:
        project_id = str(project.get("desktop_project_id") or "")
        roots = [str(root) for root in project.get("roots") or []]
        rows = self._request(
            "thread/list",
            {
                "limit": 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            },
        )
        threads = rows.get("data") if isinstance(rows, dict) else []
        if not isinstance(threads, list):
            return []
        matches: list[dict[str, Any]] = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            thread_id = str(thread.get("id") or "")
            assignment = state.thread_assignments.get(thread_id)
            assigned_project = (
                str(assignment.get("projectId") or "")
                if isinstance(assignment, dict)
                else ""
            )
            if assigned_project:
                if assigned_project != project_id:
                    continue
            elif roots and not self._path_is_within(
                str(thread.get("cwd") or ""), roots
            ):
                continue
            matches.append(thread)
        return matches

    def _select_thread(self, device_id: str, thread_key: str) -> list[dict[str, Any]]:
        state = self._states.setdefault(device_id, _CatalogState())
        thread = state.threads.get(thread_key)
        if thread is None:
            return [{"type": "error", "text": "选择的 Codex 任务已经失效"}]
        title = str(thread.get("name") or thread.get("preview") or "未命名任务")
        project_name = str((state.selected_project or {}).get("name") or "未分组项目")
        last_message = _last_assistant_text(thread)
        payload = {
            "type": "codex_selected",
            "title": _safe_text(title, CATALOG_TITLE_CHARS),
            "project": _safe_text(project_name, CATALOG_DETAIL_CHARS),
        }
        if last_message:
            payload["last_message"] = last_message
        return [payload]

    @staticmethod
    def _item(scope: str, key: str, title: Any, detail: Any) -> dict[str, Any]:
        return {
            "type": "codex_catalog_item",
            "scope": scope,
            "id": key,
            "title": _safe_text(title, CATALOG_TITLE_CHARS),
            "detail": _safe_text(detail, CATALOG_DETAIL_CHARS),
        }
