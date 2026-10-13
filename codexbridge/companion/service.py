"""Thread, turn and approval access for the phone companion API."""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from codexbridge.codex.app_server import (
    AppServerClient,
    AppServerError,
    _resolve_codex_command,
    account_snapshot,
    thread_snapshot,
)

SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
INTERNAL_THREAD_PREFIXES = ("The following is the Codex agent history",)
MAX_MESSAGE_CHARS = 20000
MAX_MESSAGE_ITEMS = 400
MAX_TURNS_IN_DETAIL = 60
MAX_EVENT_QUEUE = 300
ACCOUNT_CACHE_SECONDS = 45.0

APPROVAL_KINDS: dict[str, str] = {
    "item/commandExecution/requestApproval": "command",
    "execCommandApproval": "command",
    "item/fileChange/requestApproval": "fileChange",
    "applyPatchApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "input",
    "mcpServer/elicitation/request": "mcp",
}

ACTIVE_STATUSES = {"working", "waiting_user"}


def _as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _epoch(value: Any) -> int:
    number = _as_number(value)
    if number is not None:
        if number > 10_000_000_000:
            number /= 1000
        return max(0, int(number))
    if isinstance(value, str):
        try:
            return max(0, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()))
        except ValueError:
            return 0
    return 0


def _clip(value: str, maximum: int = MAX_MESSAGE_CHARS) -> str:
    text = value.replace("\x00", "").strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [part for part in (_text_of(item) for item in value) if part]
        return "\n".join(parts)
    if not isinstance(value, dict):
        return ""
    item_type = str(value.get("type") or "")
    if item_type in {"image", "localImage", "audio", "localAudio"}:
        return ""
    for key in ("text", "value", "delta"):
        if isinstance(value.get(key), str):
            return value[key]
    return _text_of(value.get("content"))


def _user_text(value: Any) -> str:
    text = _text_of(value)
    for marker in ("\n## My request:\n", "\n# My request:\n"):
        if marker in text:
            return text.rsplit(marker, 1)[1]
    return text


def _status_from_thread(thread: dict[str, Any]) -> str:
    runtime = thread.get("status") if isinstance(thread.get("status"), dict) else {}
    flags = {str(flag).lower() for flag in runtime.get("activeFlags", []) if isinstance(flag, str)}
    if any("waiting" in flag or "approval" in flag for flag in flags):
        return "waiting_user"
    turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
    turn_status = str(turns[-1].get("status") if turns else "").replace("_", "").lower()
    if turn_status in {"inprogress", "running", "active"}:
        return "working"
    if turn_status == "completed":
        return "completed"
    if turn_status in {"failed", "interrupted", "cancelled", "canceled"}:
        return "failed"
    runtime_type = str(runtime.get("type") or "").lower()
    if runtime_type == "active":
        return "working"
    return "idle" if not turns else "completed"


def _thread_title(thread: dict[str, Any]) -> str:
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), 200)
    return "未命名任务"


def _is_display_thread(thread: dict[str, Any]) -> bool:
    candidates: list[str] = []
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for turn in thread.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []):
            if isinstance(item, dict) and item.get("type") == "userMessage":
                text = _text_of(item.get("content"))
                if text:
                    candidates.append(text)
    return not any(
        text.strip().startswith(prefix)
        for text in candidates
        for prefix in INTERNAL_THREAD_PREFIXES
    )


class CompanionService:
    """Own the app-server connection that serves paired phones."""

    def __init__(
        self,
        data_dir: Path,
        cwd: Path,
        codex_command: str | None = None,
        client_factory: Callable[[list[str], Path], Any] | None = None,
        desktop_state_path: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cwd = Path(cwd)
        self.codex_command = codex_command
        self._client_factory = client_factory
        self.desktop_state_path = (
            Path(desktop_state_path)
            if desktop_state_path is not None
            else Path.home() / ".codex" / ".codex-global-state.json"
        )
        self._client: Any | None = None
        self._client_lock = threading.RLock()
        self._generation = 0
        self._stop = threading.Event()
        self._subscribers: set[queue.Queue] = set()
        self._subscriber_lock = threading.Lock()
        self._approvals: dict[int, dict[str, Any]] = {}
        self._approval_lock = threading.RLock()
        self._account_cache: dict[str, Any] = {}
        self._account_cache_epoch = 0.0
        self._log: list[str] = []
        self._resumed_threads: set[str] = set()
        self._resumed_lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._ensure_client()

    def stop(self) -> None:
        self._stop.set()
        with self._client_lock:
            client = self._client
            self._client = None
            self._generation += 1
        with self._resumed_lock:
            self._resumed_threads.clear()
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(["codex"], self.cwd)
        command = _resolve_codex_command(self.codex_command, self.data_dir)
        return AppServerClient(command, self.cwd)

    def _ensure_client(self) -> Any:
        with self._client_lock:
            if self._client is not None:
                return self._client
            client = self._make_client()
            client.start()
            self._client = client
            self._generation += 1
            generation = self._generation
        threading.Thread(
            target=self._pump_notifications,
            args=(client, generation),
            name="companion-events",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_requests,
            args=(client, generation),
            name="companion-approvals",
            daemon=True,
        ).start()
        return client

    def _reset_client(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
            self._generation += 1
        with self._resumed_lock:
            self._resumed_threads.clear()
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _thread_note(self, thread_id: str) -> None:
        with self._resumed_lock:
            self._resumed_threads.add(thread_id)

    def _ensure_thread(self, thread_id: str) -> None:
        """Load a disk-backed thread into the live app-server before acting on it.

        Threads that predate the current app-server process (or were created
        before a client reset) are readable from the rollout files but unknown
        to ``turn/start``, which fails with "thread not found". Resuming by id
        hydrates the thread without forking or rewriting history.
        """
        if not thread_id:
            return
        with self._resumed_lock:
            known = thread_id in self._resumed_threads
        if known:
            return
        client = self._ensure_client()
        error: AppServerError | None = None
        for attempt in range(2):
            try:
                client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                self._thread_note(thread_id)
                return
            except AppServerError as exc:
                error = exc
                message = str(exc)
                if "not found" in message.lower():
                    raise
                self._reset_client()
                client = self._ensure_client()
                if attempt == 1:
                    raise
        if error is not None:
            raise error

    def _read(self, method: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._ensure_client().request(method, params)
            except AppServerError as error:
                last_error = error
                self._reset_client()
                if attempt == 1:
                    raise
        raise last_error if last_error else AppServerError("Codex App Server 不可用")
    def subscribe(self) -> queue.Queue:
        target: queue.Queue = queue.Queue(maxsize=MAX_EVENT_QUEUE)
        with self._subscriber_lock:
            self._subscribers.add(target)
        return target

    def unsubscribe(self, target: queue.Queue) -> None:
        with self._subscriber_lock:
            self._subscribers.discard(target)

    def publish(self, payload: dict[str, Any]) -> None:
        with self._subscriber_lock:
            subscribers = list(self._subscribers)
        for target in subscribers:
            try:
                target.put_nowait(payload)
            except queue.Full:
                try:
                    target.get_nowait()
                    target.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass

    def event_log(self) -> list[str]:
        return list(self._log)

    def _note(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"{stamp} {text}")
        del self._log[:-80]

    def _pump_notifications(self, client: Any, generation: int) -> None:
        while not self._stop.is_set():
            try:
                message = client.notifications.get(timeout=1.0)
            except queue.Empty:
                if generation != self._generation:
                    return
                continue
            if generation != self._generation:
                return
            try:
                payload = self._event_payload(message)
            except Exception as error:  # noqa: BLE001
                self._note(f"事件处理失败：{error}")
                continue
            if payload is not None:
                self.publish(payload)

    def _pump_requests(self, client: Any, generation: int) -> None:
        requests = getattr(client, "server_requests", None)
        if requests is None:
            return
        while not self._stop.is_set():
            try:
                message = requests.get(timeout=1.0)
            except queue.Empty:
                if generation != self._generation:
                    return
                continue
            if generation != self._generation:
                return
            request_id = message.get("id")
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if not isinstance(request_id, int):
                continue
            kind = APPROVAL_KINDS.get(method)
            if kind is None:
                try:
                    client.respond(request_id, {"decision": "decline"})
                except Exception:  # noqa: BLE001
                    pass
                continue
            payload = {
                "request_id": request_id,
                "kind": kind,
                "method": method,
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(params.get("turnId") or ""),
                "item_id": str(params.get("itemId") or params.get("callId") or ""),
                "command": str(params.get("command") or ""),
                "cwd": str(params.get("cwd") or ""),
                "reason": str(params.get("reason") or ""),
                "created_at": time.time(),
                "expires_at": time.time() + 600,
                "raw": self._approval_detail(params),
            }
            with self._approval_lock:
                self._approvals[request_id] = payload
            self._note(f"等待手机确认：{kind}")
            self.publish({"type": "approval_request", **payload})

    @staticmethod
    def _approval_detail(params: dict[str, Any]) -> dict[str, Any]:
        detail: dict[str, Any] = {}
        changes = params.get("changes")
        if isinstance(changes, list):
            detail["paths"] = [
                str(item.get("path"))
                for item in changes
                if isinstance(item, dict) and item.get("path")
            ][:40]
        patch = params.get("patch") or params.get("diff")
        if isinstance(patch, str) and patch:
            detail["patch"] = _clip(patch, 4000)
        questions = params.get("questions")
        if isinstance(questions, list):
            detail["questions"] = [
                {
                    "id": str(item.get("id") or ""),
                    "question": _clip(_text_of(item.get("question")), 500),
                    "options": [
                        _clip(_text_of(option.get("label")), 200)
                        for option in item.get("options") or []
                        if isinstance(option, dict)
                    ],
                }
                for item in questions[:6]
                if isinstance(item, dict)
            ]
        message = params.get("message")
        if isinstance(message, str) and message:
            detail["message"] = _clip(message, 2000)
        return detail

    def pending_approvals(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._approval_lock:
            expired = [key for key, value in self._approvals.items() if value.get("expires_at", 0) < now]
            for key in expired:
                self._approvals.pop(key, None)
            return [dict(value) for value in self._approvals.values()]

    def resolve_approval(self, request_id: int, decision: str) -> bool:
        allowed = {"accept", "acceptForSession", "decline", "cancel"}
        if decision not in allowed:
            raise ValueError("decision")
        with self._approval_lock:
            payload = self._approvals.pop(request_id, None)
        if payload is None:
            return False
        client = self._client
        if client is None:
            return False
        client.respond(request_id, {"decision": decision})
        self._note(f"手机已处理确认：{payload.get('kind')} -> {decision}")
        self.publish(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "thread_id": payload.get("thread_id") or "",
                "decision": decision,
            }
        )
        return True

    def _event_payload(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "item/agentMessage/delta":
            return {
                "type": "agent_delta",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(params.get("turnId") or ""),
                "item_id": str(params.get("itemId") or ""),
                "delta": str(params.get("delta") or params.get("text") or ""),
            }
        if method == "item/commandExecution/outputDelta":
            return {
                "type": "command_output",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(params.get("turnId") or ""),
                "item_id": str(params.get("itemId") or ""),
                "delta": str(params.get("delta") or params.get("chunk") or ""),
            }
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                return None
            mapped = self._map_item(item, None)
            if mapped is None:
                return None
            return {
                "type": "item_started" if method == "item/started" else "item_completed",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(params.get("turnId") or ""),
                "item": mapped,
            }
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            return {
                "type": "turn_started",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(turn.get("id") or ""),
                "status": "working",
            }
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "completed")
            return {
                "type": "turn_completed",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(turn.get("id") or ""),
                "status": "completed" if status == "completed" else "failed",
                "duration_ms": turn.get("durationMs"),
            }
        if method == "thread/status/changed":
            status = params.get("status") if isinstance(params.get("status"), dict) else {}
            return {
                "type": "thread_status",
                "thread_id": str(params.get("threadId") or ""),
                "status": str(status.get("type") or "unknown"),
                "active_flags": [str(flag) for flag in status.get("activeFlags") or []],
            }
        if method == "thread/name/updated":
            return {
                "type": "thread_name",
                "thread_id": str(params.get("threadId") or ""),
                "title": str(params.get("name") or ""),
            }
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
            total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
            return {
                "type": "token_usage",
                "thread_id": str(params.get("threadId") or ""),
                "turn_id": str(params.get("turnId") or ""),
                "total_tokens": int(_as_number(total.get("totalTokens")) or 0),
                "context_window": int(_as_number(usage.get("modelContextWindow")) or 0),
            }
        if method == "account/rateLimits/updated":
            return {"type": "rate_limits"}
        if method == "error":
            return {
                "type": "error",
                "thread_id": str(params.get("threadId") or ""),
                "message": _clip(str(params.get("message") or params.get("error") or ""), 500),
            }
        return None
    def snapshot(self) -> dict[str, Any]:
        account = self._account()
        listing = self.list_threads(limit=12)
        threads = listing.get("threads") or []
        tasks: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        reads = 0
        for summary in threads:
            if reads >= 4:
                break
            status = str(summary.get("status") or "")
            if status not in ACTIVE_STATUSES and reads >= 2:
                continue
            if status in ACTIVE_STATUSES or reads == 0:
                try:
                    detail = self.read_thread(str(summary.get("thread_id") or ""), message_limit=40)
                except (AppServerError, OSError, ValueError):
                    continue
                reads += 1
                task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
                task["thread_id"] = summary.get("thread_id")
                task["title"] = task.get("title") or summary.get("title")
                task["model"] = summary.get("model")
                task["updated_at"] = summary.get("updated_at")
                if task.get("status") in ACTIVE_STATUSES:
                    tasks.append(task)
                elif not result and task.get("status") in {"completed", "failed"}:
                    result = task
        return {
            "generated_at_epoch": int(time.time()),
            "account": account,
            "task": tasks[0] if tasks else (threads[0] if threads else {}),
            "tasks": tasks,
            "result": result,
            "pending_approvals": self.pending_approvals(),
        }

    def _account(self) -> dict[str, Any]:
        if self._account_cache and time.monotonic() - self._account_cache_epoch < ACCOUNT_CACHE_SECONDS:
            return self._account_cache
        try:
            rate = self._read("account/rateLimits/read", {})
            usage = self._read("account/usage/read", {})
            self._account_cache = account_snapshot(rate, usage)
            self._account_cache_epoch = time.monotonic()
        except (AppServerError, OSError, ValueError):
            if not self._account_cache:
                self._account_cache = account_snapshot({}, {})
        return self._account_cache

    def list_threads(
        self,
        limit: int = 40,
        search: str = "",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or 40), 80)),
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": SOURCE_KINDS,
            "archived": False,
        }
        if cursor:
            params["cursor"] = cursor
        if search:
            params["searchTerm"] = search[:120]
        result = self._read("thread/list", params)
        rows = result.get("data") if isinstance(result, dict) else None
        next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
        threads = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            if not _is_display_thread(row):
                continue
            threads.append(self._summary(row))
        return {"threads": threads, "next_cursor": next_cursor}

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        turns = [turn for turn in row.get("turns", []) if isinstance(turn, dict)]
        usage = row.get("tokenUsage") if isinstance(row.get("tokenUsage"), dict) else {}
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        return {
            "thread_id": str(row.get("id") or ""),
            "title": _thread_title(row),
            "preview": _clip(str(row.get("preview") or ""), 400),
            "status": _status_from_thread({**row, "turns": turns}),
            "model": str(row.get("model") or ""),
            "cwd": str(row.get("cwd") or ""),
            "source": str(row.get("source") or ""),
            "created_at": _epoch(row.get("createdAt")),
            "updated_at": _epoch(row.get("updatedAt") or row.get("recencyAt")),
            "recency_at": _epoch(row.get("recencyAt")),
            "tokens": int(_as_number(total.get("totalTokens")) or 0),
            "ephemeral": bool(row.get("ephemeral")),
        }
    def read_thread(self, thread_id: str, message_limit: int = MAX_MESSAGE_ITEMS) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        result = self._read("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise ValueError("thread")
        turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
        turns.sort(key=lambda turn: _epoch(
            turn.get("startedAt") or turn.get("createdAt") or turn.get("updatedAt")
        ))
        turns = turns[-MAX_TURNS_IN_DETAIL:]
        messages: list[dict[str, Any]] = []
        for turn in turns:
            turn_id = str(turn.get("id") or "")
            for item in turn.get("items", []):
                if not isinstance(item, dict):
                    continue
                mapped = self._map_item(item, turn)
                if mapped is None:
                    continue
                mapped["turn_id"] = turn_id
                messages.append(mapped)
        total = len(messages)
        trimmed = messages[-max(1, int(message_limit)):]
        return {
            "thread": self._summary(thread),
            "task": thread_snapshot(thread),
            "messages": trimmed,
            "message_count": total,
            "truncated": total > len(trimmed),
            "pending_approvals": [
                value for value in self.pending_approvals()
                if value.get("thread_id") == thread_id
            ],
        }

    @staticmethod
    def _map_item(item: dict[str, Any], turn: dict[str, Any] | None) -> dict[str, Any] | None:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        created_at = _epoch(
            item.get("createdAt")
            or (turn or {}).get("startedAt")
            or (turn or {}).get("createdAt")
            or item.get("startedAtMs")
            or item.get("completedAtMs")
        )
        if item_type == "userMessage":
            text = _user_text(item.get("content"))
            if not text:
                return None
            return {"id": item_id, "kind": "user", "text": _clip(text), "created_at": created_at}
        if item_type == "agentMessage":
            text = _clip(str(item.get("text") or _text_of(item.get("content")) or ""))
            if not text:
                return None
            phase = str(item.get("phase") or "").replace("_", "").lower()
            kind = "commentary" if phase == "commentary" else "agent"
            return {"id": item_id, "kind": kind, "text": text, "created_at": created_at}
        if item_type == "reasoning":
            parts: list[str] = []
            summary = item.get("summary")
            if isinstance(summary, list):
                parts.extend(_text_of(entry) for entry in summary)
            if not parts:
                parts.append(_text_of(item.get("content")))
            text = _clip("\n".join(part for part in parts if part))
            if not text:
                return None
            return {"id": item_id, "kind": "reasoning", "text": text, "created_at": created_at}
        if item_type == "commandExecution":
            command = _clip(str(item.get("command") or ""), 2000)
            output = item.get("aggregatedOutput") or item.get("output") or item.get("stdout") or ""
            detail = {
                "status": str(item.get("status") or ""),
                "exit_code": item.get("exitCode"),
                "cwd": str(item.get("cwd") or ""),
                "duration_ms": item.get("durationMs"),
                "output": _clip(str(output), 6000),
            }
            return {
                "id": item_id,
                "kind": "command",
                "text": command or "运行命令",
                "detail": detail,
                "created_at": created_at,
            }
        if item_type == "fileChange":
            paths = [
                str(change.get("path"))
                for change in item.get("changes") or []
                if isinstance(change, dict) and change.get("path")
            ]
            diff = item.get("diff") or item.get("patch") or ""
            detail = {
                "status": str(item.get("status") or ""),
                "paths": paths[:40],
                "diff": _clip(str(diff), 6000),
            }
            return {
                "id": item_id,
                "kind": "fileChange",
                "text": "、".join(paths[:6]) or "文件变更",
                "detail": detail,
                "created_at": created_at,
            }
        if item_type in {"mcpToolCall", "dynamicToolCall", "collabToolCall", "webSearch", "imageGeneration"}:
            label = str(
                item.get("tool")
                or item.get("name")
                or item.get("toolName")
                or item_type
            )
            detail = {
                "status": str(item.get("status") or ""),
                "result": _clip(_text_of(item.get("result") or item.get("output")), 4000),
            }
            return {
                "id": item_id,
                "kind": "tool",
                "text": label,
                "detail": detail,
                "created_at": created_at,
            }
        if item_type == "plan":
            steps = [
                {"step": _clip(_text_of(entry.get("step")), 500), "status": str(entry.get("status") or "")}
                for entry in item.get("plan") or item.get("steps") or []
                if isinstance(entry, dict)
            ]
            return {
                "id": item_id,
                "kind": "plan",
                "text": "\n".join(step["step"] for step in steps if step["step"]),
                "detail": {"steps": steps},
                "created_at": created_at,
            }
        if item_type in {"contextCompaction", "compaction"}:
            return {
                "id": item_id,
                "kind": "notice",
                "text": "上下文已压缩",
                "created_at": created_at,
            }
        return None
    def start_thread(self, cwd: str = "", model: str = "", ephemeral: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = str(cwd)
        if model:
            params["model"] = str(model)
        if ephemeral:
            params["ephemeral"] = True
        result = self._ensure_client().request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise AppServerError("App Server 未返回新任务")
        return {"thread": self._summary(thread)}

    def start_turn(
        self,
        thread_id: str,
        text: str,
        model: str = "",
        effort: str = "",
        client_message_id: str = "",
    ) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        content = (text or "").strip()
        if not content:
            raise ValueError("text")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}],
        }
        if model:
            params["model"] = str(model)
        if effort:
            params["effort"] = str(effort)
        if client_message_id:
            params["clientUserMessageId"] = str(client_message_id)[:64]
        self._ensure_thread(thread_id)
        result = self._ensure_client().request("turn/start", params)
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = str((turn or {}).get("id") or "")
        self.publish(
            {
                "type": "turn_started",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "working",
                "user_message": _clip(content, MAX_MESSAGE_CHARS),
            }
        )
        return {"thread_id": thread_id, "turn_id": turn_id, "status": "working"}

    def steer_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        content = (text or "").strip()
        if not content:
            raise ValueError("text")
        self._ensure_thread(thread_id)
        result = self._ensure_client().request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}],
            },
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        return {
            "thread_id": thread_id,
            "turn_id": str((turn or {}).get("id") or ""),
            "status": "working",
        }

    def interrupt(self, thread_id: str, turn_id: str = "") -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        params: dict[str, Any] = {"threadId": thread_id}
        self._ensure_thread(thread_id)
        if turn_id:
            params["turnId"] = turn_id
        else:
            detail = self.read_thread(thread_id, message_limit=1)
            task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
            turn_id = str(task.get("turn_id") or "")
            if not turn_id:
                raise ValueError("turn_id")
            params["turnId"] = turn_id
        self._ensure_client().request("turn/interrupt", params)
        self.publish({"type": "turn_interrupted", "thread_id": thread_id, "turn_id": turn_id})
        return {"thread_id": thread_id, "turn_id": turn_id}

    def list_models(self) -> dict[str, Any]:
        result = self._read("model/list", {"limit": 100, "includeHidden": False})
        rows = result.get("data") if isinstance(result, dict) else None
        models = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("hidden"):
                continue
            efforts = [
                {
                    "effort": str(item.get("effort") or ""),
                    "description": _clip(_text_of(item.get("description")), 200),
                }
                for item in row.get("supportedReasoningEfforts") or []
                if isinstance(item, dict)
            ]
            models.append(
                {
                    "id": str(row.get("id") or ""),
                    "model": str(row.get("model") or row.get("id") or ""),
                    "display_name": _clip(str(row.get("displayName") or row.get("model") or ""), 120),
                    "description": _clip(_text_of(row.get("description")), 300),
                    "is_default": bool(row.get("isDefault")),
                    "default_effort": str(row.get("defaultReasoningEffort") or ""),
                    "efforts": efforts,
                    "input_modalities": [str(value) for value in row.get("inputModalities") or []],
                }
            )
        return {"models": models}

    def list_projects(self) -> dict[str, Any]:
        projects: list[dict[str, Any]] = []
        try:
            desktop = json.loads(self.desktop_state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            desktop = {}
        local = desktop.get("local-projects")
        if isinstance(local, dict):
            order = desktop.get("project-order")
            if not isinstance(order, list) or not order:
                order = sorted(
                    local,
                    key=lambda key: int((local.get(key) or {}).get("updatedAt") or 0),
                    reverse=True,
                )
            for raw_id in order:
                raw = local.get(str(raw_id))
                if not isinstance(raw, dict):
                    continue
                roots = [
                    str(path)
                    for path in raw.get("rootPaths") or []
                    if isinstance(path, str) and path
                ]
                if not roots:
                    continue
                projects.append(
                    {
                        "project_id": str(raw_id),
                        "name": _clip(str(raw.get("name") or Path(roots[0]).name or "未命名项目"), 120),
                        "cwd": roots[0],
                        "roots": roots[:6],
                        "updated_at": _epoch(raw.get("updatedAt")),
                    }
                )
        if not projects:
            seen: dict[str, dict[str, Any]] = {}
            for summary in self.list_threads(limit=60).get("threads", []):
                cwd = str(summary.get("cwd") or "")
                if not cwd or cwd in seen:
                    continue
                seen[cwd] = {
                    "project_id": "",
                    "name": Path(cwd).name or cwd,
                    "cwd": cwd,
                    "roots": [cwd],
                    "updated_at": int(summary.get("updated_at") or 0),
                }
            projects = sorted(
                seen.values(),
                key=lambda item: item.get("updated_at") or 0,
                reverse=True,
            )
        return {"projects": projects[:40], "default_cwd": str(self.cwd)}