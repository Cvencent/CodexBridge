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
from codexbridge.companion.events import EventStream
from codexbridge.protocol.commands import CommandDispatcher

from codexbridge.codex.app_server import (
    AppServerClient,
    AppServerError,
    _resolve_codex_command,
    account_snapshot,
    thread_snapshot,
)
from codexbridge.codex.desktop_ipc import DesktopIpcError

SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
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
_BRIDGE_PENDING_TITLE = "新任务"

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


def _is_no_active_turn_error(error: BaseException) -> bool:
    text = str(error or "").strip().lower()
    if "no active turn" in text or "no active writer" in text:
        return True
    return "not active" in text and ("turn" in text or "writer" in text or "steer" in text)


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
    if kind == "task_complete" and payload.get("error"):
        kind = "task_failed"
    return stamp, kind


_ROLLOUT_STEM_RE = re.compile(
    r"^rollout-(?:\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-)?(.+)$"
)


def _rollout_thread_id(filename: str) -> str:
    """Extract the thread id from a ``rollout-*.jsonl`` filename.

    Live rollouts are named ``rollout-<timestamp>-<thread-id>.jsonl`` while
    test and migrated rollouts may be ``rollout-<thread-id>.jsonl``.
    """
    name = str(filename or "")
    if not name.endswith(".jsonl"):
        return ""
    match = _ROLLOUT_STEM_RE.match(name[:-len(".jsonl")])
    return match.group(1) if match else ""


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


def _generated_thread_title(text: str) -> str:
    value = " ".join(str(text or "").replace("", "").split())
    if not value:
        return ""
    for marker in ("。", "！", "？", ".", "!", "?", "\n"):
        if marker in value:
            value = value.split(marker, 1)[0]
            break
    value = value.strip(" ：:，，；;") or "Codex 任务"
    if len(value) > 60:
        value = value[:59].rstrip() + "…"
    return value


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
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cwd = Path(cwd)
        self.codex_command = codex_command
        self._client_factory = client_factory
        self._creation_client_factory = self._make_client if client_factory is None else None
        self._desktop = None
        self._desktop_followed: dict[str, str] = {}
        self._desktop_required: set[str] = set()
        self._desktop_subscription_generation = 0
        self._desktop_subscriptions: dict[str, str] = {}
        self._desktop_discovery_due: dict[str, float] = {}
        self._desktop_turn_status: dict[tuple[str, str], str] = {}
        self._desktop_turn_started: dict[tuple[str, str], int] = {}
        self._desktop_status_order: dict[tuple[str, str], int] = {}
        self._desktop_status_seq = 0
        self._desktop_completed_turns: set[tuple[str, str, str]] = set()
        self._desktop_pump: threading.Thread | None = None
        self._desktop_reconnect_lock = threading.Lock()
        self.desktop_state_path = (
            Path(desktop_state_path)
            if desktop_state_path is not None
            else Path.home() / ".codex" / ".codex-global-state.json"
        )
        self._client: Any | None = None
        self._client_lock = threading.RLock()
        self._generation = 0
        self._stop = threading.Event()
        self._events = EventStream(subscriber_capacity=MAX_EVENT_QUEUE)
        self.commands = CommandDispatcher(self)
        self._approvals: dict[int, dict[str, Any]] = {}
        self._desktop_approval_routes: dict[int, tuple[str, str, Any, str]] = {}
        self._next_desktop_approval = 1_000_000_000
        self._approval_lock = threading.RLock()
        self._account_cache: dict[str, Any] = {}
        self._account_cache_epoch = 0.0
        self._log: list[str] = []
        self._resumed_threads: set[str] = set()
        self._resumed_lock = threading.Lock()
        self._sessions_root = (
            Path(sessions_root)
            if sessions_root is not None
            else Path.home() / ".codex" / "sessions"
        )
        self._log_cache: dict[str, tuple[tuple[int, int] | None, tuple[float, str, float] | None]] = {}
        self._log_cache_lock = threading.Lock()
        self._rollout_index: dict[str, Path] = {}
        self._rollout_index_epoch = 0.0
        self._detail_status_cache: dict[str, tuple[str, int, str]] = {}
        self._observed_threads: dict[str, dict[str, Any]] | None = None
        self._poll_thread: threading.Thread | None = None
        self._untitled_threads: set[str] = set()

    def start(self) -> None:
        self._stop.clear()
        self._ensure_client()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._watch_threads, name="companion-thread-watch", daemon=True,
            )
            self._poll_thread.start()

    def _watch_threads(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.poll_thread_changes()
            except (AppServerError, OSError, ValueError) as error:
                self._note(f"任务同步暂时不可用：{error}")

    def poll_thread_changes(self) -> None:
        current = {
            row["thread_id"]: row for row in self.list_threads(limit=80)["threads"]
        }
        if self._client_factory is None:
            for thread_id in sorted(current, key=lambda key: self._desktop_discovery_due.get(key, 0)):
                if self._stop.is_set():
                    break
                if self._desktop_discovery_due.get(thread_id, 0) > time.monotonic():
                    continue
                self._desktop_discovery_due[thread_id] = time.monotonic() + 30
                try:
                    self._desktop_owner(thread_id)
                except (RuntimeError, OSError):
                    break
                # Bound discovery work so list/completion reconciliation stays live.
                break
        previous = self._observed_threads
        self._observed_threads = current
        if previous is None:
            return
        for thread_id, row in current.items():
            before = previous.get(thread_id)
            if before == row:
                continue
            self.publish({"type": "threads_changed", "thread_id": thread_id})
            if (row["status"] == "completed" and before is not None and
                    (before["status"] != "completed" or
                     before.get("completed_at") != row.get("completed_at"))):
                turn_id = str(row.get("turn_id") or "")
                if not turn_id:
                    detail = self.read_thread(thread_id, message_limit=1)
                    turn_id = str(detail["task"].get("turn_id") or "")
                if turn_id:
                    self.publish({
                        "type": "turn_completed", "thread_id": thread_id,
                        "turn_id": turn_id, "status": "completed",
                    })

    def stop(self) -> None:
        self._stop.set()
        if self._desktop is not None:
            self._desktop.close()
        if self._desktop_pump is not None:
            self._desktop_pump.join(timeout=1.0)
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
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
        return AppServerClient(command, self.cwd, prefer_shared=True)

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

    def connection_status(self) -> dict[str, Any]:
        client = self._client
        getter = getattr(client, "connection_status", None) if client is not None else None
        status = getter() if callable(getter) else {
            "desktop_realtime_shared": False,
            "desktop_sync_mode": "polling",
            "transport": "unstarted",
            "reason": "client_status_unavailable",
        }
        if self._desktop is not None:
            desktop = self._desktop.connection_status()
            status['desktop_ipc_connected'] = bool(desktop.get('desktop_ipc_connected'))
            status['desktop_owned_threads'] = len(self._desktop_followed)
            if status['desktop_ipc_connected'] and self._desktop_followed:
                status['desktop_sync_mode'] = 'owner_routed'
                status['reason'] = 'per_thread_desktop_owner'
        return status

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

    def _thread_log(
        self,
        thread_id: str,
        rollout_path: Path | str | None = None,
    ) -> tuple[float, str, float] | None:
        """Return (newest_timestamp, last_turn_event) for a rollout log.

        Result is ``(newest_timestamp, last_turn_event, event_timestamp)``. The
        event is the last ``task_started`` / ``task_complete`` /
        ``turn_aborted`` line seen in the tail, or ``""`` when the tail is too
        short to contain one.
        """
        key = str(thread_id or "")
        if not key:
            return None
        path = self._find_rollout_log(key, rollout_path)
        try:
            stat = path.stat() if path is not None else None
            signature = (stat.st_mtime_ns, stat.st_size) if stat is not None else None
        except OSError:
            signature = None
        with self._log_cache_lock:
            cached = self._log_cache.get(key)
        if cached is not None and signature is not None and cached[0] == signature:
            return cached[1]
        found = self._read_log_tail(path) if path is not None else None
        with self._log_cache_lock:
            if len(self._log_cache) > 512:
                self._log_cache.clear()
            self._log_cache[key] = (signature, found)
        return found

    def _find_rollout_log(
        self,
        thread_id: str,
        hint: Path | str | None = None,
    ) -> Path | None:
        """Locate a thread's rollout file.

        The app-server ``thread/list`` rows carry the exact rollout ``path``,
        so prefer that hint. Fall back to a full sessions index rather than
        only today and yesterday, which previously left older threads
        resolving to a stale list status.
        """
        if hint:
            try:
                candidate = Path(str(hint))
                if candidate.is_file():
                    return candidate
            except OSError:
                pass
        return self._rollout_index_get().get(str(thread_id))

    def _rollout_index_get(self, *, refresh: bool = False) -> dict[str, Path]:
        now = time.monotonic()
        with self._log_cache_lock:
            fresh = bool(self._rollout_index) and now - self._rollout_index_epoch < 30.0
            if fresh and not refresh:
                return dict(self._rollout_index)
        found: dict[str, Path] = {}
        roots = [self._sessions_root, self._sessions_root.parent / "archived_sessions"]
        for root in roots:
            if not root.is_dir():
                continue
            try:
                candidates = list(root.rglob("rollout-*.jsonl"))
            except OSError:
                continue
            for path in candidates:
                thread_id = _rollout_thread_id(path.name)
                if not thread_id:
                    continue
                previous = found.get(thread_id)
                try:
                    if previous is None or path.stat().st_mtime > previous.stat().st_mtime:
                        found[thread_id] = path
                except OSError:
                    continue
        with self._log_cache_lock:
            self._rollout_index = found
            self._rollout_index_epoch = time.monotonic()
        return dict(found)

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
        last_record_type = ""
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
                last_record_type = ""
            else:
                stamp = _line_stamp(line)
                # Keep enough information to distinguish a usage-only append
                # from a real turn event when the turn marker fell outside the
                # bounded tail.
                last_record_type = (
                    "token_usage_record"
                    if '"type": "token_usage_record"' in line
                    else ""
                )
            if stamp is None:
                continue
            with_timestamps += 1
            if stamp > newest:
                newest = stamp
        if with_timestamps == 0:
            return None
        if not kind and last_record_type == "token_usage_record":
            kind = "usage_only"
            kind_stamp = newest
        return newest, kind, kind_stamp

    def _log_status(self, thread_id: str, rollout_path: Path | str | None = None) -> str:
        activity = self._thread_log(thread_id, rollout_path)
        if activity is None:
            return ""
        newest, kind, kind_stamp = activity
        if newest <= 0 or not kind:
            return ""
        now = time.time()
        # Terminal rollout markers are durable evidence, not a short-lived
        # activity hint. Keeping them after the active window is what makes
        # list and detail agree when thread/list omits turn history.
        if kind == "task_complete":
            return "completed"
        if kind == "task_failed":
            return "failed"
        if kind == "turn_aborted":
            return "idle"
        if kind == "task_started" and now - newest <= LOG_ACTIVE_SECONDS:
            return "working"
        if kind == "task_started":
            # A stale start with no terminal marker means the turn was
            # abandoned (usually an app-server restart). Callers may confirm
            # the terminal status from turn history.
            return "stale_active"
        return ""

    def _desktop_status_entry(
        self, thread_id: str, turn_id: str = ""
    ) -> tuple[str, int]:
        """Return the latest desktop turn status and its start time."""
        if turn_id:
            key = (thread_id, turn_id)
            status = self._desktop_turn_status.get(key)
            if status:
                return status, self._desktop_turn_started.get(key, 0)
        matches = [
            key for key in self._desktop_turn_status
            if key[0] == thread_id
        ]
        if not matches:
            return "", 0
        latest = max(matches, key=lambda key: self._desktop_status_order.get(key, 0))
        return (
            self._desktop_turn_status.get(latest, ""),
            self._desktop_turn_started.get(latest, 0),
        )

    def _desktop_status(self, thread_id: str, turn_id: str = "") -> str:
        return self._desktop_status_entry(thread_id, turn_id)[0]

    def _resolve_status(
        self,
        thread: dict[str, Any],
        summary: dict[str, Any],
        *,
        allow_log: bool = True,
        allow_detail: bool = True,
    ) -> str:
        """Resolve list and detail status from the same ordered evidence.

        Desktop IPC events are authoritative for desktop-owned turns. Rollout
        markers are the next strongest evidence because they are written at
        the turn boundary. The list row is the fallback.
        """
        thread_id = str(summary.get("thread_id") or thread.get("id") or "")
        turn_id = str(summary.get("turn_id") or "")
        rollout_path = summary.get("_rollout_path") or thread.get("path")
        desktop, desktop_started_at = self._desktop_status_entry(thread_id, turn_id)
        if desktop:
            normalized = str(desktop).replace("_", "").lower()
            if normalized in {
                "inprogress", "running", "active",
                "waitinguser", "waiting", "approval",
            }:
                if allow_log:
                    inferred = self._log_status(thread_id, rollout_path)
                    activity = self._thread_log(thread_id, rollout_path)
                    if inferred in {"completed", "failed", "idle"} and activity is not None:
                        marker_at = int(activity[2] or 0)
                        started_at = max(
                            int(summary.get("started_at") or 0),
                            int(desktop_started_at or 0),
                        )
                        if not started_at or marker_at >= started_at:
                            return inferred
                if normalized in {"waitinguser", "waiting", "approval"}:
                    return "waiting_user"
                return "working"
            if normalized == "completed":
                return "completed"
            if normalized in {"failed", "interrupted", "cancelled", "canceled"}:
                return "failed"
        if not turn_id and allow_detail:
            remembered = self._detail_status(
                thread_id,
                rollout_path,
                turn_id=turn_id,
                started_at=int(summary.get("started_at") or 0),
            )
            if remembered:
                return remembered
        if allow_log:
            inferred = self._log_status(thread_id, rollout_path)
            if inferred:
                activity = self._thread_log(thread_id, rollout_path)
                started_at = int(summary.get("started_at") or 0)
                # A terminal marker older than the latest known turn is stale.
                # Include ``failed``: a previous failed turn must not override
                # a newer turn that is currently running.
                if activity is not None and inferred in {"completed", "idle", "failed"}:
                    if started_at and int(activity[2]) < started_at:
                        settled = self._confirm_terminal_status(thread_id) if allow_detail else ""
                        if settled and int(activity[2]) >= started_at:
                            return settled
                        log_started = self._log_status(thread_id, rollout_path)
                        if log_started == "stale_active":
                            return "working"
                        return str(summary.get("status") or "idle")
                # A stale ``task_started`` with no terminal marker means the
                # turn was abandoned. Confirm with turn history so list and
                # detail agree, then cache the terminal status.
                if inferred == "stale_active":
                    settled = self._confirm_terminal_status(thread_id)
                    if settled:
                        return settled
                    return "working"
                return inferred
            # No turn marker survived in the rollout tail. Confirm with turn
            # history whenever a rollout exists so the list cannot disagree
            # with the detail view. Usage-only and response-only appends can
            # be recent even after the turn has already ended.
            if allow_detail and not inferred and not turn_id:
                activity = self._thread_log(thread_id, rollout_path)
                # Without a rollout there is nothing stronger than the list
                # row to appeal to.
                if activity is not None:
                    settled = self._detail_status(
                        thread_id,
                        rollout_path,
                        turn_id=turn_id,
                        started_at=int(summary.get("started_at") or 0),
                    )
                    if not settled:
                        settled = self._confirm_terminal_status(thread_id)
                    if settled:
                        return settled
        return str(summary.get("status") or "idle")

    def _detail_status(
        self,
        thread_id: str,
        rollout_path: Any = None,
        turn_id: str = "",
        started_at: int = 0,
    ) -> str:
        """Return a cached settled status while it still describes the latest turn.

        Rollout files receive unrelated usage records between turns, so the
        cache is keyed by turn identity rather than by the file signature.
        """
        if not thread_id:
            return ""
        with self._log_cache_lock:
            entry = self._detail_status_cache.get(thread_id)
        if entry is None:
            return ""
        cached_turn_id, cached_started_at, status = entry
        if turn_id and cached_turn_id and turn_id != cached_turn_id:
            return ""
        if started_at and cached_started_at and started_at > cached_started_at:
            return ""
        activity = self._thread_log(thread_id, rollout_path)
        if activity is not None:
            _, kind, kind_stamp = activity
            marker_at = int(kind_stamp or 0)
            if (
                kind in {"task_started", "task_complete", "task_failed", "turn_aborted"}
                and marker_at > cached_started_at
            ):
                return ""
        return status

    def _rollout_signature(
        self, thread_id: str, rollout_path: Any = None
    ) -> tuple[int, int] | None:
        path = self._find_rollout_log(thread_id, rollout_path) if thread_id else None
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _remember_detail_status(
        self,
        thread_id: str,
        status: str,
        rollout_path: Any = None,
        turn_id: str = "",
        started_at: int = 0,
    ) -> None:
        """Cache a settled detail status for the latest known turn."""
        if not thread_id or status not in {"completed", "failed"}:
            return
        with self._log_cache_lock:
            previous = self._detail_status_cache.get(thread_id)
            if previous is not None:
                previous_turn_id, previous_started_at, _ = previous
                if turn_id and previous_turn_id and turn_id != previous_turn_id:
                    if started_at and previous_started_at and started_at < previous_started_at:
                        return
                elif started_at and previous_started_at and started_at < previous_started_at:
                    return
            if len(self._detail_status_cache) > 512:
                self._detail_status_cache.clear()
            self._detail_status_cache[thread_id] = (turn_id, started_at, status)

    def _confirm_terminal_status(self, thread_id: str) -> str:
        """Read turn history to settle a thread whose rollout tail is dangling."""
        if not thread_id:
            return ""
        result: Any = None
        for attempt in range(3):
            try:
                result = self._read("thread/read", {"threadId": thread_id, "includeTurns": True})
                break
            except (AppServerError, OSError, ValueError):
                if attempt < 2:
                    time.sleep(0.05)
        if result is None:
            return ""
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return ""
        turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
        if not turns:
            return ""
        turns.sort(key=lambda turn: _epoch(
            turn.get("startedAt") or turn.get("createdAt") or turn.get("updatedAt")
        ))
        latest = turns[-1]
        status = str(latest.get("status") or "").replace("_", "").lower()
        settled = ""
        if status == "completed":
            settled = "completed"
        elif status in {"failed", "interrupted", "cancelled", "canceled"}:
            settled = "failed"
        if not settled:
            return ""
        self._remember_detail_status(
            thread_id,
            settled,
            turn_id=str(latest.get("id") or ""),
            started_at=_epoch(
                latest.get("startedAt")
                or latest.get("createdAt")
                or latest.get("updatedAt")
            ),
        )
        return settled

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
    def subscribe(self, after: str = '') -> queue.Queue:
        return self._events.subscribe(after)

    def unsubscribe(self, target: queue.Queue) -> None:
        self._events.unsubscribe(target)

    def publish(self, payload: dict[str, Any]) -> None:
        self._events.publish(payload)

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
                if getattr(client, "shared", False):
                    continue
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
        for key in ('permissions', 'requestedPermissions', 'scope', 'requestedSchema'):
            value = params.get(key)
            if isinstance(value, (dict, list, str)):
                detail[key] = value
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
            payload = self._approvals.get(request_id)
        if payload and payload.get('kind') == 'mcp':
            return self.respond_approval(request_id, {'action': decision, 'content': None})
        if payload and payload.get('kind') in {'permissions', 'input'}:
            if decision in {'accept', 'acceptForSession'}:
                raise ValueError('structured_response_required')
            return self.respond_approval(request_id, {
                'permissions': {}, 'scope': 'turn'} if payload['kind'] == 'permissions'
                else {'answers': {}})
        return self.respond_approval(request_id, {'decision': decision})

    def respond_approval(self, request_id: int, response: dict[str, Any]) -> bool:
        if not isinstance(response, dict):
            raise ValueError('response')
        with self._approval_lock:
            payload = self._approvals.get(request_id)
        if payload is None:
            return False
        kind = payload.get('kind')
        if kind == 'input':
            answers = response.get('answers')
            if (set(response) != {'answers'} or not isinstance(answers, dict) or
                    any(not isinstance(key, str) or not isinstance(value, dict) or
                        set(value) != {'answers'} or not isinstance(value['answers'], list) or
                        any(not isinstance(item, str) for item in value['answers'])
                        for key, value in answers.items())):
                raise ValueError('answers')
        elif kind == 'mcp':
            if (set(response) - {'action', 'content', '_meta'} or
                    response.get('action') not in {'accept', 'decline', 'cancel'}):
                raise ValueError('action')
        elif kind == 'permissions':
            if (set(response) - {'permissions', 'scope', 'strictAutoReview'} or
                    not isinstance(response.get('permissions'), dict) or
                    response.get('scope', 'turn') not in {'turn', 'session'}):
                raise ValueError('permissions')
        elif (set(response) != {'decision'} or
              response.get('decision') not in {'accept', 'acceptForSession', 'decline', 'cancel'}):
            raise ValueError('decision')
        route = self._desktop_approval_routes.get(request_id)
        if route is not None:
            self._desktop.respond_approval(*route, response)
        else:
            client = self._client
            if client is None:
                return False
            client.respond(request_id, response)
        with self._approval_lock:
            self._approvals.pop(request_id, None)
            self._desktop_approval_routes.pop(request_id, None)
        self._note(f"Approval resolved: {payload.get('kind')}")
        self.publish(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "thread_id": payload.get("thread_id") or "",
                "decision": str(response.get('decision') or response.get('action') or 'submitted'),
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
                "title": str(params.get("threadName") or params.get("name") or ""),
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
            "connection": self.connection_status(),
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
            summary = self._summary(row)
            # Retain the exact rollout path from the row as a private hint for
            # status resolution; it is stripped before the list is returned.
            summary["_rollout_path"] = str(row.get("path") or "")
            threads.append(summary)
        for summary in threads:
            status = self._resolve_status(
                {"id": summary.get("thread_id")}, summary, allow_log=True
            )
            if status:
                summary["status"] = status
            activity = self._thread_log(
                str(summary.get("thread_id") or ""),
                summary.get("_rollout_path"),
            )
            if activity is not None and status == "completed":
                summary["completed_at"] = int(activity[2])
        for summary in threads:
            summary.pop("_rollout_path", None)
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
            "turn_id": str(latest_turn.get("id") or ""),
            "started_at": _epoch(latest_turn.get("startedAt")),
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
        summary = self._summary(thread)
        rollout_path = str(thread.get("path") or "")
        summary["_rollout_path"] = rollout_path
        summary["status"] = self._resolve_status(thread, summary, allow_log=True)
        summary.pop("_rollout_path", None)
        self._remember_detail_status(
            str(summary.get("thread_id") or ""),
            str(summary.get("status") or ""),
            rollout_path,
            turn_id=str(summary.get("turn_id") or ""),
            started_at=int(summary.get("started_at") or 0),
        )
        task = thread_snapshot(thread)
        task["status"] = summary["status"]
        return {
            "thread": summary,
            "task": task,
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
    def _invalidate_desktop_queries(self) -> None:
        if self._desktop is None:
            return
        invalidate = getattr(self._desktop, 'invalidate_query_cache', None)
        if not callable(invalidate):
            return
        for query_key in (['tasks'], ['list-pinned-threads'], ['archived-threads', 'local']):
            try:
                invalidate(query_key)
            except (DesktopIpcError, OSError, ValueError) as error:
                self._note('desktop_cache_invalidation_failed')
                break

    def rename_thread(self, thread_id: str, title: str) -> dict[str, Any]:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError('thread_id')
        title = ' '.join(str(title or "").split())
        if not title:
            raise ValueError('title')
        if len(title) > 200:
            title = title[:199].rstrip() + "…"
        params = {'threadId': thread_id, 'name': title}
        try:
            self._ensure_client().request('thread/name/set', params)
        except AppServerError as error:
            if 'not found' not in str(error).lower():
                raise
            self._ensure_thread(thread_id)
            self._ensure_client().request('thread/name/set', params)
        self._untitled_threads.discard(thread_id)
        self.publish({'type': 'thread_name', 'thread_id': thread_id, 'title': title})
        self.publish({'type': 'threads_changed', 'thread_id': thread_id})
        self._invalidate_desktop_queries()
        return {'thread_id': thread_id, 'title': title, 'renamed': True}

    def _assign_generated_title(self, thread_id: str, text: str) -> None:
        if thread_id not in self._untitled_threads:
            return
        title = _generated_thread_title(text)
        if not title:
            return
        try:
            self.rename_thread(thread_id, title)
        except (AppServerError, DesktopIpcError, OSError, ValueError) as error:
            self._note('generated_title_not_persisted')

    def start_thread(self, cwd: str = "", model: str = "", ephemeral: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = str(cwd)
        if model:
            params["model"] = str(model)
        if ephemeral:
            params["ephemeral"] = True
        else:
            # Interactive phone-created tasks must classify as user tasks so
            # Codex Desktop can hydrate and open them normally.
            params["threadSource"] = "user"
        if self._creation_client_factory is not None and not ephemeral:
            creator = self._creation_client_factory()
            try:
                creator.start()
                result = creator.request('thread/start', params)
                thread = result.get("thread") if isinstance(result, dict) else None
                thread_id = str((thread or {}).get("id") or "")
                if thread_id:
                    # An empty thread/start row has no rollout file, so the
                    # desktop deep link fails with "no rollout found". Naming
                    # the thread commits its session file before handing the
                    # active writer over to the desktop.
                    creator.request('thread/name/set',
                                    {'threadId': thread_id, 'name': '新任务'})
            finally:
                # The desktop cannot hydrate a task while another writer owns it.
                creator.close()
        else:
            result = self._ensure_client().request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            raise AppServerError("App Server 未返回新任务")
        thread_id = str(thread.get('id') or '')
        if not thread_id:
            raise AppServerError('App Server 未返回任务 ID')
        self._untitled_threads.add(thread_id)
        if self._desktop is not None or self._client_factory is None:
            self._desktop_required.add(thread_id)
            owner = None
            try:
                self._desktop.open_thread(thread_id)
            except (OSError, ValueError, RuntimeError) as error:
                self._note(f'Desktop task registration unavailable: {error}')
                raise AppServerError('desktop_handoff_unavailable') from error
            # Opening a persisted task makes the desktop hydrate and own it.
            # Do not claim success or accept a first reply before ownership is
            # actually visible, otherwise the new empty task is not usable.
            for _ in range(40):
                try:
                    owner = self._desktop_owner(thread_id)
                except (RuntimeError, OSError):
                    owner = None
                if owner:
                    break
                if self._stop.wait(0.25):
                    break
            if not owner:
                raise DesktopIpcError('desktop_handoff_pending')
        self.publish({'type': 'threads_changed', 'thread_id': thread_id})
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
        if thread_id in self._desktop_required and thread_id not in self._desktop_followed:
            raise DesktopIpcError('desktop_handoff_pending')
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
        owner = self._desktop_owner(thread_id)
        if owner:
            request = {key: value for key, value in params.items() if key != 'threadId'}

            def deliver(owner_id: str) -> dict[str, Any]:
                # Desktop follower turns inherit thread settings, so an explicit
                # model/effort from the phone has to be written to the thread
                # first; otherwise the selection is silently ignored.
                if model or effort:
                    self._desktop.update_thread_settings(
                        owner_id, thread_id, model=model, effort=effort
                    )
                return self._desktop.start_turn(owner_id, thread_id, request)

            try:
                result = deliver(owner)
            except (DesktopIpcError, OSError):
                # Reconnect once; a stale IPC frame must not lose the user's
                # message or fall back to a second app-server writer.
                self._desktop_subscriptions.clear()
                self._desktop_subscription_generation = 0
                try:
                    self._desktop.close()
                except (OSError, RuntimeError):
                    pass
                owner = self._desktop_owner(thread_id)
                if not owner:
                    raise DesktopIpcError('Desktop owner temporarily unavailable; write not sent')
                result = deliver(owner)
            turn = result.get('turn') if isinstance(result, dict) else None
            turn_id = str((turn or {}).get('id') or '')
            self._assign_generated_title(thread_id, content)
            self.publish({'type': 'turn_started', 'thread_id': thread_id,
                          'turn_id': turn_id, 'status': 'working',
                          'user_message': _clip(content, MAX_MESSAGE_CHARS)})
            return {'thread_id': thread_id, 'turn_id': turn_id, 'status': 'working',
                    'execution_transport': 'desktop_owner'}
        self._ensure_thread(thread_id)
        client = self._ensure_client()
        try:
            result = client.request("turn/start", params)
        except AppServerError as error:
            if not _is_active_writer_error(error):
                raise
            result = client.request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "input": params["input"],
                },
            )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = str((turn or {}).get("id") or "")
        self._assign_generated_title(thread_id, content)
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

    def _desktop_owner(self, thread_id: str) -> str | None:
        from codexbridge.codex.desktop_ipc import DesktopIpcClient, DesktopIpcError
        if self._stop.is_set():
            raise DesktopIpcError('Companion stopped')
        # Custom app-server factories are isolated from the user's live desktop.
        if self._desktop is None and self._client_factory is not None:
            return None
        for attempt in range(2):
            try:
                if self._desktop is None:
                    self._desktop = DesktopIpcClient()
                self._desktop.start()
                if isinstance(self._desktop, DesktopIpcClient):
                    if self._desktop_subscription_generation != self._desktop.generation:
                        self._desktop_subscriptions.clear()
                        self._desktop_subscription_generation = self._desktop.generation
                owner = self._desktop.find_thread_owner(thread_id)
                if owner is None and (thread_id in self._desktop_followed or thread_id in self._desktop_required):
                    raise DesktopIpcError('Desktop owner temporarily unavailable; write not sent')
                if owner and isinstance(self._desktop, DesktopIpcClient):
                    if self._desktop_subscriptions.get(thread_id) != owner:
                        self._desktop.follow_thread(owner, thread_id)
                        self._desktop_followed[thread_id] = owner
                        self._desktop_subscriptions[thread_id] = owner
                    if self._desktop_pump is None or not self._desktop_pump.is_alive():
                        self._desktop_pump = threading.Thread(target=self._pump_desktop,
                            name='companion-desktop-events', daemon=True)
                        self._desktop_pump.start()
                return owner
            except (DesktopIpcError, OSError) as error:
                if attempt == 0:
                    self._desktop_subscriptions.clear()
                    self._desktop_subscription_generation = 0
                    try:
                        self._desktop.close()
                    except (OSError, RuntimeError):
                        pass
                    continue
                if thread_id in self._desktop_followed or thread_id in self._desktop_required:
                    raise DesktopIpcError('Desktop owner temporarily unavailable; write not sent') from error
                # Discovery is read-only; only here is independent execution safe.
                self._note(f'Desktop owner discovery unavailable: {error}')
                return None
        return None

    def _pump_desktop(self) -> None:
        while not self._stop.is_set():
            try:
                self.handle_desktop_event(self._desktop.events.get(timeout=0.5))
            except queue.Empty:
                status_getter = getattr(self._desktop, 'connection_status', None)
                if callable(status_getter):
                    try:
                        connected = bool(status_getter().get('desktop_ipc_connected'))
                    except (OSError, RuntimeError, ValueError):
                        connected = False
                    if not connected:
                        self._reconnect_desktop()
                continue
            except (RuntimeError, OSError, ValueError) as error:
                self._desktop_subscriptions.clear()
                self.publish({'type': 'resync_required'})
                self._note(f'Desktop event stream reconnect required: {error}')
                self._reconnect_desktop()

    def _reconnect_desktop(self) -> None:
        """Reconnect IPC and explicitly resubscribe every known desktop thread."""
        if self._stop.is_set() or self._desktop is None:
            return
        with self._desktop_reconnect_lock:
            if self._stop.is_set() or self._desktop is None:
                return
            try:
                self._desktop.start()
                followed = dict(self._desktop_followed)
                self._desktop_subscriptions.clear()
                for thread_id, previous_owner in followed.items():
                    try:
                        owner = self._desktop.find_thread_owner(thread_id)
                    except (RuntimeError, OSError, ValueError):
                        owner = None
                    if owner:
                        self._desktop.follow_thread(owner, thread_id)
                        self._desktop_followed[thread_id] = owner
                        self._desktop_subscriptions[thread_id] = owner
                    elif previous_owner:
                        self._desktop_followed[thread_id] = previous_owner
                self.publish({'type': 'resync_required',
                              'reason': 'desktop_ipc_reconnected'})
                self._note('Codex Desktop IPC reconnected and subscriptions restored')
            except (RuntimeError, OSError, ValueError) as error:
                self._note(f'Codex Desktop IPC reconnect failed: {error}')

    def handle_desktop_event(self, message: dict[str, Any]) -> None:
        if message.get('type') == 'resync_required':
            self.publish({'type': 'resync_required'})
            return
        if (message.get('method') != 'thread-stream-state-changed' or
                message.get('version') != 11):
            return
        params = message.get('params') or {}
        thread_id = params.get('conversationId')
        if params.get('hostId') != 'local' or not isinstance(thread_id, str):
            return
        change = params.get('change') or {}
        if change.get('type') == 'snapshot':
            state = change.get('conversationState') or {}
            history = (state.get('turnHistory') or {}).get('history') or {}
            entities = history.get('entitiesByKey') or {}
            for turn in entities.values() if isinstance(entities, dict) else []:
                if not isinstance(turn, dict) or not turn.get('turnId'):
                    continue
                turn_id = str(turn['turnId'])
                status = str(turn.get('status') or '')
                key = (thread_id, turn_id)
                self._desktop_status_seq += 1
                self._desktop_status_order[key] = self._desktop_status_seq
                self._desktop_turn_status[key] = status
                started_at = _epoch(
                    turn.get('turnStartedAtMs')
                    or turn.get('startedAt')
                    or turn.get('startedAtMs')
                )
                if started_at:
                    self._desktop_turn_started[key] = started_at
                terminal = status if status in {'completed', 'failed', 'interrupted'} else ''
                completed_key = (thread_id, turn_id, terminal)
                if terminal and completed_key not in self._desktop_completed_turns:
                    self._desktop_completed_turns.add(completed_key)
                    self.publish({'type': 'turn_completed', 'thread_id': thread_id,
                        'turn_id': turn_id,
                        'status': 'completed' if terminal == 'completed' else 'failed'})
            owner = message.get('sourceClientId')
            requests = state.get('requests', [])
            if isinstance(owner, str) and isinstance(requests, list):
                seen = set()
                for request in requests:
                    if not isinstance(request, dict):
                        continue
                    kind = APPROVAL_KINDS.get(request.get('method'))
                    original_id = request.get('id')
                    if kind is None or not isinstance(original_id, (int, str)):
                        continue
                    route = (owner, thread_id, original_id, kind)
                    with self._approval_lock:
                        local_id = next((key for key, value in self._desktop_approval_routes.items()
                                         if value == route), None)
                        if local_id is None:
                            self._next_desktop_approval += 1
                            local_id = self._next_desktop_approval
                        seen.add(local_id)
                        detail = request.get('params') or {}
                        payload = {'request_id': local_id, 'thread_id': thread_id, 'kind': kind,
                            'method': request.get('method'), 'turn_id': detail.get('turnId', ''),
                            'command': detail.get('command', ''), 'cwd': detail.get('cwd', ''),
                            'reason': detail.get('reason', ''), 'raw': self._approval_detail(detail),
                            'created_at': time.time(), 'expires_at': time.time() + 600}
                        self._desktop_approval_routes[local_id] = route
                        self._approvals[local_id] = payload
                    self.publish({'type': 'approval_request', **payload})
                with self._approval_lock:
                    for key, route in list(self._desktop_approval_routes.items()):
                        if route[1] == thread_id and key not in seen:
                            self._desktop_approval_routes.pop(key, None)
                            self._approvals.pop(key, None)
                            self.publish({'type': 'approval_resolved', 'thread_id': thread_id,
                                          'request_id': key})
        elif change.get('type') == 'patches':
            owner = message.get('sourceClientId')
            if isinstance(owner, str):
                self._desktop.follow_thread(owner, thread_id)
        # Desktop patches describe private UI state, not app-server messages.
        # Invalidate canonical reads instead of misinterpreting that schema.
        self.publish({'type': 'threads_changed', 'thread_id': thread_id})

    def steer_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        content = (text or "").strip()
        if not content:
            raise ValueError("text")
        owner = self._desktop_owner(thread_id)
        if owner:
            result = self._desktop.steer_turn(owner, thread_id, [
                {'type': 'text', 'text': content[:MAX_MESSAGE_CHARS]}])
            turn = result.get('turn') if isinstance(result, dict) else None
            return {'thread_id': thread_id, 'turn_id': str((turn or {}).get('id') or ''),
                    'status': 'working', 'execution_transport': 'desktop_owner'}
        self._ensure_thread(thread_id)
        client = self._ensure_client()
        steer_params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}],
        }
        try:
            result = client.request("turn/steer", steer_params)
        except AppServerError as error:
            if not _is_no_active_turn_error(error):
                raise
            result = client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": steer_params["input"],
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
        owner = self._desktop_owner(thread_id)
        if owner:
            self._desktop.interrupt_turn(owner, thread_id, turn_id)
            self.publish({'type': 'turn_interrupted', 'thread_id': thread_id,
                          'turn_id': turn_id})
            return {'thread_id': thread_id, 'turn_id': turn_id,
                    'execution_transport': 'desktop_owner'}
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