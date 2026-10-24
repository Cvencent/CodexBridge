from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codexbridge.win_shared import read_text_shared

CATALOG_ITEMS_PER_PAGE = 7
NEW_THREAD_TITLE = "新建任务"
CATALOG_TITLE_CHARS = 22
CATALOG_DETAIL_CHARS = 28
LAST_MESSAGE_CHARS = 120
MODELS_SCOPE = "models"
MODEL_ITEMS_MAX = 7
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
    # Threads this device started during the current session. They may not be
    # persisted in thread/list yet, so they are merged back on every render.
    created_threads: list[dict[str, Any]] = field(default_factory=list)
    # Model catalog cache for this device. Keys are short ids (`m0`, `m1`...)
    # handed to the device, values are the canonical rows from `model/list`.
    models: list[dict[str, Any]] = field(default_factory=list)
    model_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Real Codex thread id this device last selected or created. A voice
    # instruction has to land on *some* thread, and the device only ever sees
    # short catalog keys, so the host keeps the mapping.
    active_thread_id: str = ""


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
        # Model chosen by a device. This is a device preference, not catalog
        # state, so it survives cancel/confirm resets and bridge re-handshakes.
        self._chosen_models: dict[str, str] = {}
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
            if str(event.get("scope") or "") == MODELS_SCOPE:
                return self._list_models(device_id)
            return self._list_projects(device_id)
        if event_type == "codex_model_select":
            return self._select_model(device_id, event)
        if event_type == "codex_project_select":
            return self._list_threads(device_id, str(event.get("id") or ""))
        if event_type == "codex_thread_select":
            return self._select_thread(device_id, str(event.get("id") or ""))
        if event_type == "codex_thread_new":
            return self._start_thread(device_id, event)
        if event_type in {"codex_catalog_cancel", "codex_selection_confirm"}:
            self._states.pop(device_id, None)
            return []
        return []

    def _start_thread(self, device_id: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Create a real Codex thread so the device can work without a PC."""
        state = self._states.setdefault(device_id, _CatalogState())
        cwd = str((state.selected_project or {}).get("cwd") or self.workspace)
        model = event.get("model") or self._chosen_models.get(device_id)
        params: dict[str, Any] = {"cwd": cwd}
        if isinstance(model, str) and model.strip():
            params["model"] = model.strip()
        try:
            result = self._request("thread/start", params)
        except Exception:
            return [{"type": "error", "text": "新建任务失败，请在电脑端查看 Codex"}]
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not str(thread.get("id") or ""):
            return [{"type": "error", "text": "新建任务失败，请在电脑端查看 Codex"}]
        state.selected_project = state.selected_project or {
            "project_id": None,
            "desktop_project_id": None,
            "cwd": cwd,
            "roots": [cwd],
            "name": Path(cwd).name or NEW_THREAD_TITLE,
        }
        # Remember threads started from this device: a freshly created
        # thread is not always persisted in thread/list yet, so it is merged
        # back on every catalog render.
        state.created_threads = [
            item
            for item in state.created_threads
            if str(item.get("id") or "") != str(thread.get("id") or "")
        ]
        state.created_threads.append(thread)
        # Index the new thread under the next free key so the device can
        # select it immediately, then re-render the catalog around it.
        threads = dict(state.threads or {})
        next_key = f"t{len(threads)}"
        threads[next_key] = thread
        state.threads = threads
        selected = self._select_thread(device_id, next_key)
        # Re-render the thread list so the device shows the new task in place
        # instead of keeping a stale catalog that no longer matches state.
        project = state.selected_project or {}
        listing = self._render_thread_list(state, project)
        return selected + listing

    def active_thread(self, device_id: str) -> str:
        """Thread this device is following, or an empty string.

        The hub uses this to decide which live records may reach the device: a
        status line about someone else's session must not cut into a screen that
        is waiting on its own task.
        """
        with self._lock:
            state = self._states.get(device_id)
            return str(state.active_thread_id or "") if state else ""

    def handle_voice_command(self, device_id: str, text: str) -> list[dict[str, Any]]:
        """Turn a confirmed voice instruction into a real Codex turn.

        The device only speaks short confirmations ("发送指令"), so the host owns
        the routing: reuse the thread this device selected, or start a fresh one
        in its current project when nothing is selected yet.
        """
        content = (text or "").strip()
        if not content:
            return [{"type": "error", "text": "没有听到内容，请再说一次"}]
        state = self._states.setdefault(device_id, _CatalogState())
        thread_id = state.active_thread_id
        model = self._chosen_models.get(device_id) or ""
        if not thread_id:
            cwd = str((state.selected_project or {}).get("cwd") or self.workspace)
            params: dict[str, Any] = {"cwd": cwd}
            if model:
                params["model"] = model
            try:
                created = self._request("thread/start", params)
            except Exception:
                return [{"type": "error", "text": "电脑端暂时无法新建任务，请稍后再说一次"}]
            thread = created.get("thread") if isinstance(created, dict) else None
            thread_id = str((thread or {}).get("id") or "")
            if not thread_id:
                return [{"type": "error", "text": "电脑端暂时无法新建任务，请稍后再说一次"}]
            state.created_threads.append(dict(thread or {}))
        input_parts = [{"type": "text", "text": content[:2000]}]
        turn_params: dict[str, Any] = {"threadId": thread_id, "input": input_parts}
        if model:
            turn_params["model"] = model
        try:
            self._request("turn/start", turn_params)
        except Exception as error:
            if not self._is_active_turn_error(error):
                return [{"type": "error", "text": "电脑端没有接收这条指令，请重试"}]
            # A turn is already running on this thread: steer it instead of
            # losing the words the device just captured.
            try:
                self._request("turn/steer", {"threadId": thread_id, "input": input_parts})
            except Exception:
                return [{"type": "error", "text": "电脑端没有接收这条指令，请重试"}]
        state.active_thread_id = thread_id
        return [{"type": "task_queued", "text": "指令已发送到电脑端"}]

    @staticmethod
    def _is_active_turn_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(word in message for word in ("active", "already", "running", "busy"))

    def _list_models(self, device_id: str) -> list[dict[str, Any]]:
        """Render the Codex model catalog so a device can pick one explicitly."""
        state = self._states.setdefault(device_id, _CatalogState())
        chosen = self._chosen_models.get(device_id) or ""
        rows: list[dict[str, Any]] = []
        try:
            result = self._request("model/list", {"limit": 100, "includeHidden": False})
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, list):
                rows = [
                    row
                    for row in data
                    if isinstance(row, dict) and not row.get("hidden")
                ]
        except Exception:
            rows = []
        state.models = [
            {
                "model": str(row.get("model") or row.get("id") or ""),
                "display_name": str(
                    row.get("displayName") or row.get("model") or row.get("id") or ""
                ),
                "effort": str(row.get("defaultReasoningEffort") or ""),
                "is_default": bool(row.get("isDefault")),
            }
            for row in rows
        ]
        state.model_keys = {}
        messages: list[dict[str, Any]] = [
            {"type": "codex_catalog_begin", "scope": MODELS_SCOPE}
        ]
        for index, model in enumerate(state.models[:MODEL_ITEMS_MAX]):
            key = f"m{index}"
            state.model_keys[key] = model
            marker = ""
            if model["model"] and model["model"] == chosen:
                marker = "\uff08\u5df2\u9009\uff09"
            elif model["is_default"]:
                marker = "\uff08\u9ed8\u8ba4\uff09"
            messages.append(
                self._item(
                    MODELS_SCOPE,
                    key,
                    model["display_name"] + marker,
                    model["effort"],
                )
            )
        messages.append({"type": "codex_catalog_end", "scope": MODELS_SCOPE})
        return messages

    def _select_model(self, device_id: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Map a device model choice onto the canonical Codex model name."""
        state = self._states.setdefault(device_id, _CatalogState())
        row = None
        key = str(event.get("id") or "")
        if key:
            row = state.model_keys.get(key)
        if row is None:
            name = str(event.get("model") or event.get("name") or "").strip()
            if name:
                for candidate in state.models:
                    if name in (candidate["display_name"], candidate["model"]):
                        row = candidate
                        break
        if row is None:
            index = event.get("index")
            if isinstance(index, int) and 0 <= index < len(state.models):
                row = state.models[index]
        if row is None:
            # The catalog cache is gone (for example after a bridge restart):
            # re-send the list so the device can choose again.
            return self._list_models(device_id)
        model = row["model"]
        if model:
            self._chosen_models[device_id] = model
        try:
            model_index = state.models.index(row)
        except ValueError:
            model_index = -1
        return [
            {
                "type": "codex_model_selected",
                "model": _safe_text(model, CATALOG_TITLE_CHARS),
                "model_index": model_index,
            }
        ]

    def _desktop_projects(self, state: _CatalogState) -> list[dict[str, Any]]:
        try:
            desktop = json.loads(read_text_shared(self.desktop_state_path))
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
        return self._render_thread_list(state, project, threads)

    def _render_thread_list(
        self,
        state: _CatalogState,
        project: dict[str, Any],
        threads: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if threads is None:
            threads = self._project_threads(state, project)
        # Merge threads this device created during this session. A freshly
        # started thread may not be persisted in thread/list yet, so a plain
        # re-query would drop it and the device would look like it did nothing.
        known = {
            str(thread.get("id") or ""): thread
            for thread in (state.created_threads or [])
            if isinstance(thread, dict) and str(thread.get("id") or "")
        }
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for thread in [*list(known.values()), *threads]:
            thread_id = str(thread.get("id") or "")
            if not thread_id or thread_id in seen:
                continue
            if not self._thread_matches_project(state, project, thread):
                continue
            seen.add(thread_id)
            merged.append(thread)
        state.threads = {}
        threads = merged
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

    def _thread_matches_project(
        self,
        state: _CatalogState,
        project: dict[str, Any],
        thread: dict[str, Any],
    ) -> bool:
        thread_id = str(thread.get("id") or "")
        assignment = state.thread_assignments.get(thread_id)
        assigned_project = (
            str(assignment.get("projectId") or "")
            if isinstance(assignment, dict)
            else ""
        )
        project_id = str(project.get("desktop_project_id") or "")
        if assigned_project:
            return assigned_project == project_id
        roots = [str(root) for root in project.get("roots") or []]
        if not roots:
            return True
        return self._path_is_within(str(thread.get("cwd") or ""), roots)

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
        state.active_thread_id = str(thread.get("id") or "")
        title = str(thread.get("name") or thread.get("preview") or "未命名任务")
        project_name = str((state.selected_project or {}).get("name") or "未分组项目")
        last_message = _last_assistant_text(thread)
        payload = {
            "type": "codex_selected",
            "title": _safe_text(title, CATALOG_TITLE_CHARS),
            "project": _safe_text(project_name, CATALOG_DETAIL_CHARS),
            # The device repeats this id back on every later request, so the
            # host can answer questions about *this* thread rather than the most
            # recent one any client touched.
            "thread_id": _safe_text(state.active_thread_id, 63),
        }
        if last_message:
            payload["last_message"] = last_message
        chosen = self._chosen_models.get(device_id)
        if chosen:
            payload["model"] = _safe_text(chosen, CATALOG_TITLE_CHARS)
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
