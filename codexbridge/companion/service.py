"""Thread, turn and approval access for the phone companion API."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from codexbridge.read_state import ReadStateStore
from codexbridge.codex.app_server import (
    AppServerClient,
    AppServerError,
    _resolve_codex_command,
    _turn_error,
    account_snapshot,
    thread_snapshot,
)

SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
# Name given to a continuation fork so the desktop can tell it apart from the
# original task the phone opened.
FORK_TITLE_PREFIX = "手机继续 · "
INTERNAL_THREAD_PREFIXES = ("The following is the Codex agent history",)
MAX_MESSAGE_CHARS = 20000
MAX_MESSAGE_ITEMS = 400
MAX_TURNS_IN_DETAIL = 60
MAX_EVENT_QUEUE = 300
ACCOUNT_CACHE_SECONDS = 45.0
# Desktop turns are not visible to this app-server process, so the phone list
# infers "working" from the rollout log the desktop itself is appending to.
LOG_TAIL_BYTES = 131072
LOG_ACTIVE_SECONDS = 150.0
LOG_SETTLED_SECONDS = 1800.0
LOG_CACHE_SECONDS = 8.0
LOG_SCAN_LIMIT = 24

APPROVAL_KINDS: dict[str, str] = {
    "item/commandExecution/requestApproval": "command",
    "execCommandApproval": "command",
    "item/fileChange/requestApproval": "fileChange",
    "applyPatchApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
    "item/tool/requestUserInput": "input",
    "mcpServer/elicitation/request": "mcp",
}

# Statuses that mean a turn still owns the thread.
ACTIVE_STATUSES = {"working", "waiting_user"}


def _is_active_writer_error(error: BaseException) -> bool:
    text = str(error or "").strip().lower()
    return "active writer" in text and (
        "already" in text or "existing" in text or "has an active writer" in text
    )


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


_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def _parse_stamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _line_stamp(line: str) -> float | None:
    """Read the record ``timestamp`` without parsing the whole payload."""
    match = _TIMESTAMP_RE.search(line)
    if match is None:
        return None
    return _parse_stamp(match.group(1))


def _log_activity(line: str) -> tuple[float, str] | None:
    if '"event_msg"' not in line:
        return None
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    kind = str(payload.get("type") or "")
    if kind not in {"task_started", "task_complete", "turn_aborted"}:
        return None
    stamp = _parse_stamp(str(record.get("timestamp") or ""))
    if stamp is None:
        return None
    return stamp, kind


def _workspace_name(cwd: str) -> str:
    text = str(cwd or "").rstrip("\\/")
    if not text:
        return ""
    name = os.path.basename(text)
    return name or text


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
        sessions_root: Path | None = None,
        read_state: ReadStateStore | None = None,
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
        # Threads whose writer lock is held by another Codex client (desktop
        # app-server). The app-server refuses to resume these, so a turn/start
        # on them fails with a bare "thread not found"; remember the real cause
        # so the phone gets an actionable message instead.
        self._locked_threads: set[str] = set()
        # When a thread is locked by another writer we continue it in a fork
        # that shares its full history, and remember the mapping so a repeat
        # reply reuses the same fork instead of branching again.
        self._forked_threads: dict[str, str] = {}
        # Reverse map (hidden fork id -> phone-visible thread id) so reads and
        # events stay attached to the thread the phone actually opened.
        self._fork_origin: dict[str, str] = {}
        # The fork inherits the original title, which we then overwrite with a
        # "手机继续 · …" label. Remember the original so the phone keeps showing
        # the task name the user opened.
        self._fork_titles: dict[str, str] = {}
        self._resumed_lock = threading.Lock()
        self._load_fork_map()
        self._sessions_root = (
            Path(sessions_root)
            if sessions_root is not None
            else Path.home() / ".codex" / "sessions"
        )
        self._log_cache: dict[str, tuple[float, tuple[float, str, float] | None]] = {}
        self._log_cache_lock = threading.Lock()
        self._read_state = read_state or ReadStateStore(self.data_dir / "read-state.json")

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
            self._locked_threads.clear()
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
            self._locked_threads.clear()
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
                if _is_active_writer_error(exc):
                    with self._resumed_lock:
                        self._locked_threads.add(thread_id)
                    self._thread_note(thread_id)
                    return
                if "not found" in message.lower():
                    raise
                self._reset_client()
                client = self._ensure_client()
                if attempt == 1:
                    raise
        if error is not None:
            raise error

    def _active_turn_id(self, thread_id: str) -> str:
        """Return the id of the turn currently running on a thread, if any."""
        try:
            result = self._read("thread/read", {"threadId": thread_id, "includeTurns": True})
        except AppServerError:
            return ""
        thread = result.get("thread") if isinstance(result, dict) else None
        turns = (thread or {}).get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list):
            return ""
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            status = str(turn.get("status") or "").replace("_", "").lower()
            if status in {"inprogress", "running", "active"}:
                return str(turn.get("id") or "")
        return ""

    def _writable_thread(self, thread_id: str) -> str:
        """Return a thread id this client may write to.

        app-server allows only one writer per stored thread. When the desktop
        or another Codex process holds that thread, ``thread/resume`` and
        ``turn/start`` fail. Forking reuses the full history under a new id we
        own, so the phone can keep replying without stealing the desktop's
        session. Free threads are resumed in place and used directly.
        """
        if not thread_id:
            return thread_id
        with self._resumed_lock:
            existing = self._forked_threads.get(thread_id)
        if existing:
            # A reset app-server forgets loaded threads, so re-hydrate the fork
            # (or the next turn/start fails with "thread not found" again).
            try:
                self._ensure_thread(existing)
            except AppServerError:
                # The fork is gone; drop the stale mapping and branch again.
                with self._resumed_lock:
                    self._forked_threads.pop(thread_id, None)
                    self._fork_origin.pop(existing, None)
                    self._fork_titles.pop(thread_id, None)
                self._save_fork_map()
            else:
                # Forks created before this version were left unnamed, so give
                # them their label the first time they are reused.
                self._ensure_fork_label(thread_id, existing)
                return existing
        self._ensure_thread(thread_id)
        with self._resumed_lock:
            locked = thread_id in self._locked_threads
        if not locked:
            return thread_id
        client = self._ensure_client()
        try:
            result = client.request("thread/fork", {"threadId": thread_id})
        except AppServerError as error:
            raise AppServerError(
                "该任务正在电脑端 Codex 中使用，无法同时从手机继续；"
                "请先在电脑端关闭该会话后重试。"
            ) from error
        thread = result.get("thread") if isinstance(result, dict) else None
        forked_id = str((thread or {}).get("id") or "")
        if not forked_id:
            raise AppServerError("电脑端无法为该任务创建可继续的会话")
        self._thread_note(forked_id)
        with self._resumed_lock:
            self._forked_threads[thread_id] = forked_id
            self._fork_origin[forked_id] = thread_id
        self._save_fork_map()
        label, origin_title = self._label_fork(forked_id, thread)
        if origin_title:
            with self._resumed_lock:
                self._fork_titles[thread_id] = origin_title
            self._save_fork_map()
        self.publish(
            {
                "type": "thread_forked",
                "thread_id": thread_id,
                "continued_as": forked_id,
                "title": label,
            }
        )
        return forked_id

    def _ensure_fork_label(self, thread_id: str, forked_id: str) -> None:
        """Label a fork that predates fork labelling. Failure is cosmetic."""
        if self._original_title(thread_id):
            return
        try:
            result = self._read("thread/read", {"threadId": forked_id, "includeTurns": False})
        except AppServerError:
            return
        thread = result.get("thread") if isinstance(result, dict) else None
        _label, origin_title = self._label_fork(forked_id, thread)
        if not origin_title:
            return
        with self._resumed_lock:
            self._fork_titles[thread_id] = origin_title
        self._save_fork_map()

    def _label_fork(self, forked_id: str, forked: Any) -> tuple[str, str]:
        """Name a continuation fork so it is identifiable in the desktop app.

        The desktop owns the original thread's writer lock, so the phone reply
        lands in a fork that would otherwise look like an unnamed duplicate of
        the same task. Give it a distinct name; failure here is cosmetic.

        ``forked`` is the thread object ``thread/fork`` returned. It still
        carries the name inherited from the original thread, which we return as
        ``origin_title`` so the phone list keeps showing the task the user
        opened.
        """
        base = ""
        if isinstance(forked, dict):
            base = str(forked.get("name") or forked.get("preview") or "").strip()
        # Re-labelling an already labelled fork must stay idempotent.
        if base.startswith(FORK_TITLE_PREFIX):
            base = base[len(FORK_TITLE_PREFIX):].strip()
        label = _clip(f"{FORK_TITLE_PREFIX}{base}".strip() if base else f"{FORK_TITLE_PREFIX}CodexBridge", 120)
        try:
            self._ensure_client().request("thread/name/set", {"threadId": forked_id, "name": label})
        except AppServerError:
            return "", base
        return label, base

    def _fork_map_path(self) -> Path:
        return self.data_dir / "fork-map.json"

    def _load_fork_map(self) -> None:
        try:
            payload = json.loads(self._fork_map_path().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        forks = payload.get("forks")
        if not isinstance(forks, dict):
            return
        for origin, fork in forks.items():
            if not isinstance(origin, str) or not isinstance(fork, str) or not origin or not fork:
                continue
            self._forked_threads[origin] = fork
            self._fork_origin[fork] = origin
        titles = payload.get("titles")
        if isinstance(titles, dict):
            for origin, title in titles.items():
                if isinstance(origin, str) and isinstance(title, str) and origin and title:
                    self._fork_titles[origin] = title

    def _save_fork_map(self) -> None:
        with self._resumed_lock:
            payload = {
                "version": 1,
                "forks": dict(self._forked_threads),
                "titles": dict(self._fork_titles),
            }
        try:
            self._fork_map_path().parent.mkdir(parents=True, exist_ok=True)
            temporary = self._fork_map_path().with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._fork_map_path())
        except OSError:
            return

    def _fork_for(self, thread_id: str) -> str:
        with self._resumed_lock:
            return self._forked_threads.get(str(thread_id or ""), "")

    def _original_title(self, thread_id: str) -> str:
        with self._resumed_lock:
            return self._fork_titles.get(str(thread_id or ""), "")

    def _is_fork_id(self, thread_id: str) -> bool:
        with self._resumed_lock:
            return str(thread_id or "") in self._fork_origin

    def visible_thread_id(self, thread_id: str) -> str:
        """Map a hidden fork id back to the thread the phone opened."""
        key = str(thread_id or "")
        if not key:
            return ""
        with self._resumed_lock:
            return self._fork_origin.get(key, key)

    def _thread_log(self, thread_id: str) -> tuple[float, str, float] | None:
        """Return (newest_timestamp, last_turn_event) for a rollout log.

        Result is ``(newest_timestamp, last_turn_event, event_timestamp)``. The
        event is the last ``task_started`` / ``task_complete`` /
        ``turn_aborted`` line seen in the tail, or ``""`` when the tail is too
        short to contain one.
        """
        key = str(thread_id or "")
        if not key:
            return None
        # A continuation fork is authoritative for the thread it branched from,
        # so read the fork's rollout when the phone asks about the origin.
        key = self._fork_for(key) or key
        now = time.monotonic()
        with self._log_cache_lock:
            cached = self._log_cache.get(key)
        if cached is not None and now - cached[0] <= LOG_CACHE_SECONDS:
            return cached[1]
        path = self._find_rollout_log(key)
        found = self._read_log_tail(path) if path is not None else None
        with self._log_cache_lock:
            if len(self._log_cache) > 512:
                self._log_cache.clear()
            self._log_cache[key] = (now, found)
        return found

    def _find_rollout_log(self, thread_id: str) -> Path | None:
        for offset in (0, 1):
            day = datetime.now() - timedelta(days=offset)
            folder = self._sessions_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            try:
                entries = list(folder.glob(f"*{thread_id}.jsonl"))
            except OSError:
                continue
            if entries:
                return max(entries, key=lambda item: item.stat().st_mtime)
        return None

    @staticmethod
    def _read_log_tail(path: Path) -> tuple[float, str, float] | None:
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > LOG_TAIL_BYTES:
                    handle.seek(size - LOG_TAIL_BYTES)
                blob = handle.read()
        except OSError:
            return None
        newest = 0.0
        kind = ""
        kind_stamp = 0.0
        with_timestamps = 0
        for raw in blob.splitlines():
            line = raw.decode("utf-8", "replace")
            if '"timestamp"' not in line:
                continue
            activity = _log_activity(line)
            if activity is not None:
                stamp, activity_kind = activity
                kind = activity_kind
                kind_stamp = stamp
            else:
                stamp = _line_stamp(line)
            if stamp is None:
                continue
            with_timestamps += 1
            if stamp > newest:
                newest = stamp
        if with_timestamps == 0:
            return None
        return newest, kind, kind_stamp

    def _log_status(self, thread_id: str) -> str:
        activity = self._thread_log(thread_id)
        if activity is None:
            return ""
        newest, kind, kind_stamp = activity
        if newest <= 0:
            return ""
        now = time.time()
        if now - kind_stamp <= LOG_SETTLED_SECONDS:
            if kind == "task_complete":
                return "completed"
            if kind == "turn_aborted":
                return "idle"
        if now - newest <= LOG_ACTIVE_SECONDS:
            # The desktop is still appending to this rollout without having
            # closed the turn, so a turn is in flight.
            return "working"
        return ""

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
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
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
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "turn_id": str(params.get("turnId") or ""),
                "item_id": str(params.get("itemId") or ""),
                "delta": str(params.get("delta") or params.get("text") or ""),
            }
        if method == "item/commandExecution/outputDelta":
            return {
                "type": "command_output",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
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
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "turn_id": str(params.get("turnId") or ""),
                "item": mapped,
            }
        if method == "turn/started":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            return {
                "type": "turn_started",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "turn_id": str(turn.get("id") or ""),
                "status": "working",
            }
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            status = str(turn.get("status") or "completed")
            thread_id = self.visible_thread_id(str(params.get("threadId") or ""))
            completed_at = _epoch(turn.get("completedAt") or turn.get("completedAtMs"))
            return {
                "type": "turn_completed",
                "thread_id": thread_id,
                "turn_id": str(turn.get("id") or ""),
                "status": "completed" if status == "completed" else "failed",
                "duration_ms": turn.get("durationMs"),
                "completed_at": completed_at,
                "error_message": _turn_error(turn),
                "read": bool(
                    thread_id
                    and completed_at > 0
                    and self._read_state.is_thread_read(thread_id, completed_at)
                ),
            }
        if method == "thread/status/changed":
            status = params.get("status") if isinstance(params.get("status"), dict) else {}
            return {
                "type": "thread_status",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "status": str(status.get("type") or "unknown"),
                "active_flags": [str(flag) for flag in status.get("activeFlags") or []],
            }
        if method == "thread/name/updated":
            raw_id = str(params.get("threadId") or "")
            # Relabelling a hidden continuation fork must not rename the task
            # the phone is showing.
            if self._is_fork_id(raw_id):
                return None
            return {
                "type": "thread_name",
                "thread_id": self.visible_thread_id(raw_id),
                "title": str(params.get("name") or ""),
            }
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
            total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
            return {
                "type": "token_usage",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "turn_id": str(params.get("turnId") or ""),
                "total_tokens": int(_as_number(total.get("totalTokens")) or 0),
                "context_window": int(_as_number(usage.get("modelContextWindow")) or 0),
            }
        if method == "account/rateLimits/updated":
            return {"type": "rate_limits"}
        if method == "error":
            return {
                "type": "error",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
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
        seen: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            if not _is_display_thread(row):
                continue
            # A thread we forked to keep replying is still shown as the thread
            # the phone opened; never list the hidden fork as a second task.
            visible_id = self.visible_thread_id(str(row.get("id") or ""))
            if visible_id in seen:
                continue
            seen.add(visible_id)
            summary = self._summary(row)
            summary["thread_id"] = visible_id
            # The hidden fork was renamed for the desktop's benefit; keep the
            # phone row showing the task the user actually opened.
            original = self._original_title(visible_id)
            if original:
                summary["title"] = original
            threads.append(summary)
        for index, summary in enumerate(threads):
            if index >= LOG_SCAN_LIMIT:
                break
            thread_id = str(summary.get("thread_id") or "")
            if str(summary.get("status") or "") not in ACTIVE_STATUSES:
                # List rows carry no turns, so a finished task only shows up via
                # its rollout log. Use that log's completion timestamp as the
                # read watermark; otherwise completed_at stays 0 and the
                # "already read" check below can never match.
                activity = self._thread_log(thread_id)
                inferred = self._log_status(thread_id)
                if inferred:
                    summary["status"] = inferred
                    if inferred in {"completed", "failed"} and activity is not None:
                        _newest, _kind, kind_stamp = activity
                        if kind_stamp > 0 and int(summary.get("completed_at") or 0) <= 0:
                            summary["completed_at"] = int(kind_stamp)
            completed_at = int(summary.get("completed_at") or 0)
            if (
                summary.get("status") in {"completed", "failed"}
                and completed_at > 0
                and self._read_state.is_thread_read(thread_id, completed_at)
            ):
                summary["status"] = "idle"
        threads.sort(
            key=lambda item: (
                0 if str(item.get("status") or "") in ACTIVE_STATUSES else 1,
                -float(item.get("recency_at") or item.get("updated_at") or 0),
            )
        )
        return {"threads": threads, "next_cursor": next_cursor}

    @staticmethod
    def _summary(row: dict[str, Any]) -> dict[str, Any]:
        turns = [turn for turn in row.get("turns", []) if isinstance(turn, dict)]
        latest_turn = turns[-1] if turns else {}
        usage = row.get("tokenUsage") if isinstance(row.get("tokenUsage"), dict) else {}
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        return {
            "thread_id": str(row.get("id") or ""),
            "title": _thread_title(row),
            "preview": _clip(str(row.get("preview") or ""), 400),
            "status": _status_from_thread({**row, "turns": turns}),
            "model": str(row.get("model") or ""),
            "cwd": str(row.get("cwd") or ""),
            "workspace": _workspace_name(str(row.get("cwd") or "")),
            "source": str(row.get("source") or ""),
            "created_at": _epoch(row.get("createdAt")),
            "updated_at": _epoch(row.get("updatedAt") or row.get("recencyAt")),
            "recency_at": _epoch(row.get("recencyAt")),
            "completed_at": _epoch(latest_turn.get("completedAt")),
            "tokens": int(_as_number(total.get("totalTokens")) or 0),
            "ephemeral": bool(row.get("ephemeral")),
            "error_message": _turn_error(latest_turn),
        }
    def read_thread(
        self,
        thread_id: str,
        message_limit: int = MAX_MESSAGE_ITEMS,
    ) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        # When the origin thread is locked by the desktop we read the fork that
        # carries its history, but keep reporting the origin id to the phone.
        source_id = self._fork_for(thread_id) or thread_id
        result = self._read("thread/read", {"threadId": source_id, "includeTurns": True})
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise ValueError("thread")
        live_status = self._log_status(thread_id)
        if live_status:
            thread = {**thread, "__codexbridge_live_status": live_status}
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
        summary = self._summary(thread)
        summary["thread_id"] = thread_id
        original = self._original_title(thread_id)
        if original:
            summary["title"] = original
        task = thread_snapshot(thread)
        task["thread_id"] = thread_id
        if live_status:
            summary["status"] = live_status
            task["status"] = live_status
        completed_at = int(summary.get("completed_at") or task.get("completed_at_epoch") or 0)
        if (
            summary.get("status") in {"completed", "failed"}
            and completed_at > 0
            and self._read_state.is_thread_read(thread_id, completed_at)
        ):
            summary["status"] = "idle"
            task["status"] = "idle"
        return {
            "thread": summary,
            "task": task,
            "messages": trimmed,
            "continued_as": self._fork_for(thread_id),
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

    def mark_thread_read(self, thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        # Read without an acknowledging reader so the summary still carries the
        # completion state we need to detect an unread result.
        detail = self.read_thread(thread_id, message_limit=1)
        summary = detail["thread"]
        status = str(summary.get("status") or "idle")
        completed_at = int(summary.get("completed_at") or 0)
        # The list derives its completion time from the rollout log, which can
        # be a beat later than the turn's completedAt. Mark the later value so
        # the list's read check matches and the row flips to idle too.
        log_activity = self._thread_log(thread_id)
        if log_activity is not None:
            completed_at = max(completed_at, int(log_activity[2] or 0))
        if status in {"completed", "failed"} and completed_at > 0:
            was_unread = not self._read_state.is_thread_read(thread_id, completed_at)
            self._read_state.mark_thread(thread_id, completed_at)
            if was_unread:
                self.publish(
                    {
                        "type": "thread_status",
                        "thread_id": thread_id,
                        "status": "idle",
                        "active_flags": [],
                    }
                )
            status = "idle"
        return {
            "thread_id": thread_id,
            "read_at": self._read_state.thread_read_at(thread_id),
            "status": status,
        }

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
        target_id = self._writable_thread(thread_id)
        params: dict[str, Any] = {
            "threadId": target_id,
            "input": [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}],
        }
        if model:
            params["model"] = str(model)
        if effort:
            params["effort"] = str(effort)
        if client_message_id:
            params["clientUserMessageId"] = str(client_message_id)[:64]
        client = self._ensure_client()
        result = client.request("turn/start", params)
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
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "working",
            "continued_as": target_id if target_id != thread_id else "",
        }

    def steer_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        content = (text or "").strip()
        if not content:
            raise ValueError("text")
        target_id = self._writable_thread(thread_id)
        client = self._ensure_client()
        steer_params = {
            "threadId": target_id,
            "input": [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}],
        }
        expected_turn_id = self._active_turn_id(target_id)
        if not expected_turn_id:
            raise AppServerError("该任务当前没有进行中的对话，无法追加消息")
        steer_params["expectedTurnId"] = expected_turn_id
        result = client.request("turn/steer", steer_params)
        turn = result.get("turn") if isinstance(result, dict) else None
        return {
            "thread_id": thread_id,
            "turn_id": str((turn or {}).get("id") or expected_turn_id),
            "status": "working",
        }

    def interrupt(self, thread_id: str, turn_id: str = "") -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        target_id = self._writable_thread(thread_id)
        params: dict[str, Any] = {"threadId": target_id}
        if turn_id:
            params["turnId"] = turn_id
        else:
            turn_id = self._active_turn_id(target_id)
            if not turn_id:
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
