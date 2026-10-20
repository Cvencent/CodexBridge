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
from codexbridge.companion.attachments import AttachmentStore
from codexbridge.protocol.commands import CommandDispatcher
from codexbridge.companion.queue import QueueService

from codexbridge.read_state import ReadStateStore


from codexbridge.codex.app_server import (
    AppServerClient,
    AppServerError,
    AppServerProtocolError,
    _resolve_codex_command,
    _turn_error,
    account_snapshot,
    resolve_terminal_state,
    thread_snapshot,
)
from codexbridge.cbdp.constants import (
    TAG_APPROVAL_ID,
    TAG_APPROVAL_KIND,
    TAG_APPROVAL_REF,
    TAG_EXPIRES_AT_MS,
    TAG_PROMPT,
    TAG_THREAD_REF,
    TAG_TURN_REF,
)
from codexbridge.cbdp.tlv import encode_tlvs, i64, u8, u32, utf8
from codexbridge.codex.desktop_ipc import DesktopIpcError
from codexbridge.codex.rollout_activity import (
    read_log_tail,
    status_from_activity,
)
from codexbridge.companion.read_cache import (
    CACHEABLE_METHODS,
    READ_TTL_SECONDS,
    ReadCache,
    TERMINAL_READ_TTL_SECONDS,
    thread_tag,
)


def _sandbox_policy_object(value: Any) -> Any:
    """Translate the compact wire name into the app-server enum object."""

    if isinstance(value, dict):
        if str(value.get("type") or ""):
            return dict(value)
        raise ValueError("sandbox_policy")
    names = {
        "read-only": "readOnly",
        "readOnly": "readOnly",
        "workspace-write": "workspaceWrite",
        "workspaceWrite": "workspaceWrite",
        "danger-full-access": "dangerFullAccess",
        "dangerFullAccess": "dangerFullAccess",
    }
    name = names.get(str(value or ""))
    if name is None:
        raise ValueError("sandbox_policy")
    return {"type": name}


def _sandbox_policy_name(value: Any) -> str:
    """Canonical device-facing sandbox policy name, for local readback."""

    if isinstance(value, dict):
        value = value.get("type", "")
    names = {
        "read-only": "read-only",
        "readOnly": "read-only",
        "workspace-write": "workspace-write",
        "workspaceWrite": "workspace-write",
        "danger-full-access": "danger-full-access",
        "dangerFullAccess": "danger-full-access",
    }
    return names.get(str(value or ""), str(value or ""))

SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "unknown"]
# Name given to a continuation fork so the desktop can tell it apart from the
# original task the phone opened.
FORK_TITLE_PREFIX = "手机继续 · "
INTERNAL_THREAD_PREFIXES = ("The following is the Codex agent history",)
MAX_MESSAGE_CHARS = 20000
MAX_MESSAGE_ITEMS = 400
MAX_TURNS_IN_DETAIL = 60
INTERMEDIATE_MESSAGE_KINDS = {
    "commentary",
    "reasoning",
    "command",
    "fileChange",
    "tool",
    "plan",
    "notice",
}
MAX_EVENT_QUEUE = 300
# How often the background watcher reconciles thread state. A live turn needs
# a fast beat so a completion reaches every device promptly; an idle Codex
# only has to notice a desktop-initiated turn eventually, and a fixed 2-second
# beat kept the whole machine warm for nothing.
POLL_ACTIVE_SECONDS = 2.0
POLL_IDLE_SECONDS = 15.0
# Turn states that mean the thread body is finished being written.
TERMINAL_BODY_STATUSES = frozenset({'completed', 'failed', 'interrupted'})
ACCOUNT_CACHE_SECONDS = 45.0
# Desktop turns are not visible to this app-server process, so the phone list
# infers "working" from the rollout log the desktop itself is appending to.
LOG_TAIL_BYTES = 131072
LOG_ACTIVE_SECONDS = 150.0
_BRIDGE_PENDING_TITLE = "新任务"
LOG_SETTLED_SECONDS = 1800.0
LOG_CACHE_SECONDS = 8.0
# How many days of rollout folders to search for a thread's log.
ROLLOUT_SCAN_DAYS = 3
LOG_SCAN_LIMIT = 24
# The listing page every caller shares. The background watcher reconciles 80
# rows while a phone status needs a dozen, and because the read cache keys on
# the request params, each caller paid for its own Codex round trip -- and the
# watcher's forced refresh threw the shared entry away before any device could
# reuse it. One wide page, sliced per caller, costs one fetch.
LIST_PAGE_LIMIT = 80
# How long a turn-read verdict may stand for a row the rollout log cannot
# classify. The log stays the authority while it has anything to say, so this
# only bounds the case where it is silent or unreadable.
ROW_VERDICT_SECONDS = 600.0

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


def _is_stale_thread_read(error: BaseException) -> bool:
    """True when a live app-server cannot page turns for a thread it started.

    A long-lived app-server keeps a thread created through ``thread/start`` in
    memory before it has persisted turns. ``thread/read`` then answers
    ``list_turns is not supported yet`` instead of returning the row. A fresh
    process reads the same rollout straight from disk, so the caller resets the
    client and retries once rather than surfacing a spurious failure to the
    phone.
    """
    text = str(error or "").strip().lower()
    if "not supported yet" not in text:
        return False
    return "list_turns" in text or "thread/items/list" in text or "thread/read" in text


def _is_active_writer_error(error: BaseException) -> bool:
    text = str(error or "").strip().lower()
    return "active writer" in text and (
        "already" in text or "existing" in text or "has an active writer" in text
    )


def writer_lock_path(thread_id: str) -> Path:
    return Path.home() / ".codex" / "thread-writer-locks" / f"{thread_id}.lock"


def _clear_writer_lock(thread_id: str) -> bool:
    """Remove a stale Codex thread writer lock when Windows allows it.

    The desktop app-server can keep its lock file after a turn finishes. The
    lock is advisory and owned by an independent process, but Windows POSIX
    semantics let us delete it by rename; once removed a new app-server can
    resume and write the same stored thread. Failure is safe: callers fall back
    to the previous locked-thread behavior.
    """
    if not thread_id or Path(thread_id).name != thread_id:
        return False
    path = writer_lock_path(thread_id)
    try:
        if not path.is_file():
            return False
        path.replace(path.with_name(path.name + ".removed"))
        path.with_name(path.name + ".removed").unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
    latest = turns[-1] if turns else {}
    turn_status = str(latest.get("status") or "").replace("_", "").lower()
    if turn_status in {"inprogress", "running", "active"}:
        return "working"
    if turn_status in {"completed", "failed", "interrupted", "cancelled", "canceled"}:
        status, _stamp = resolve_terminal_state(
            turn_status,
            completed_at=_epoch(latest.get("completedAt")),
            fallback_timestamps=(
                _epoch(latest.get("updatedAt")),
                _epoch(thread.get("recencyAt")),
                _epoch(thread.get("updatedAt")),
                _epoch(latest.get("startedAt")),
            ),
        )
        return status
    runtime_type = str(runtime.get("type") or "").lower()
    if runtime_type == "active":
        return "working"
    # Missing turn state is not evidence of completion.
    return "idle"


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


# Rollout records that the app-server writes when it merely loads/resumes a
# thread. They carry a fresh timestamp but describe no turn progress, so they
# must not count as "the thread is doing work right now".
_BOOKKEEPING_EVENT_TYPES = {"thread_settings_applied"}


def _is_bookkeeping_line(line: str) -> bool:
    """Return True for a rollout record that only refreshes thread settings.

    Resuming or reading a thread appends a ``thread_settings_applied`` event
    with the current time. It is bookkeeping, not turn activity, and must not
    make an idle thread look like it is running.
    """
    if '"event_msg"' not in line:
        return False
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return str(payload.get("type") or "") in _BOOKKEEPING_EVENT_TYPES


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


def _read_file_tail(path: Path, size: int) -> bytes:
    """Read the last ``LOG_TAIL_BYTES`` of a file another process is writing.

    Codex holds its rollout file open with sharing flags that reject a plain
    ``open(path, "rb")`` on Windows, which made the phone read no activity at
    all for a thread that was being written to right then. Open the file the
    way Windows allows shared reads instead of giving up.
    """
    start = size - LOG_TAIL_BYTES if size > LOG_TAIL_BYTES else 0
    if os.name != "nt":
        with path.open("rb") as handle:
            if start:
                handle.seek(start)
            return handle.read()
    import msvcrt

    try:
        # _O_RDONLY | _O_BINARY with the sharing flags Windows itself uses, so
        # a writer that already holds the file open does not lock us out.
        descriptor = msvcrt.open_osfhandle(
            _win32_open_shared(str(path), start),
            os.O_RDONLY | os.O_BINARY,
        )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            return stream.read()
    except OSError:
        return b""


def _win32_open_shared(path: str, offset: int) -> int:
    """Open a file for shared reading on Windows and return a raw HANDLE."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(
        wintypes.LPCWSTR(path),
        wintypes.DWORD(GENERIC_READ),
        wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(0),
        wintypes.HANDLE(None),
    )
    invalid = wintypes.HANDLE(-1).value
    if not handle or handle == invalid:
        raise OSError(ctypes.get_last_error(), "cannot open rollout for shared read", path)
    if offset and kernel32.SetFilePointer(
        wintypes.HANDLE(handle), wintypes.LONG(offset), None, wintypes.DWORD(0)
    ) == 0xFFFFFFFF:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise OSError(ctypes.get_last_error(), "cannot seek rollout", path)
    return handle


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
        show_intermediate_events: bool | Callable[[], bool] = False,
        close_client_on_stop: bool = True,
        isolated: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cwd = Path(cwd)
        self.codex_command = codex_command
        self._client_factory = client_factory
        self._isolated = bool(isolated)
        self._close_client_on_stop = bool(close_client_on_stop)
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
        # A desktop snapshot can arrive after the bridge restart with a whole
        # history of already-finished turns. Establish that snapshot as a
        # baseline and only publish completions on a later active -> terminal
        # transition for a turn we have actually observed as active.
        self._desktop_active_turns: set[tuple[str, str]] = set()
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
        self.queues = QueueService(
            self.data_dir / "queues.json",
            start_turn=self.start_turn,
            steer_turn=self.steer_turn,
            thread_exists=self._queue_thread_exists,
            is_active=self._queue_thread_active,
            publish=self.publish,
        )
        self._approvals: dict[int, dict[str, Any]] = {}
        self._desktop_approval_routes: dict[int, tuple[str, str, Any, str]] = {}
        self._next_desktop_approval = 1_000_000_000
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
        # Forks whose history must still be read after writing moves back to the
        # original thread. A fork copies the thread it branched from, so its
        # turns overlap the original's except for the replies the phone made
        # while the desktop held the writer lock; those extras are appended on
        # read so reclaiming a thread never hides what the phone already sent.
        self._fork_tails: dict[str, str] = {}
        self._resumed_lock = threading.Lock()
        self._load_fork_map()
        self._thread_settings_path = self.data_dir / "thread-settings.json"
        self._thread_settings: dict[str, dict[str, Any]] = {}
        self._thread_settings_lock = threading.RLock()
        self._load_thread_settings()
        self._sessions_root = (
            Path(sessions_root)
            if sessions_root is not None
            else Path.home() / ".codex" / "sessions"
        )
        self._log_cache: dict[str, tuple[float, tuple[float, str, float, float, float] | None]] = {}
        self._log_cache_lock = threading.Lock()
        self._read_cache = ReadCache()
        self.attachments = AttachmentStore(self.data_dir)
        # Threads whose turn has ended. Their body cannot change again
        # until a new turn starts, so their reads stay valid far longer.
        self._terminal_threads: set[str] = set()
        # One monotonic high-water mark per thread so a stale upstream payload
        # can never move a terminal timestamp backwards.
        self._terminal_stamps: dict[str, int] = {}
        self._terminal_stamp_lock = threading.Lock()
        # Rows whose turn read settled their state while the rollout log had
        # nothing to say. Re-reading a 146 KB thread body for every such row on
        # every poll was the dominant cost of the phone API; the verdict stands
        # until the log shows that thread moving again.
        self._row_verdicts: dict[str, tuple[str, int, float]] = {}
        self._observed_threads: dict[str, dict[str, Any]] | None = None
        self._poll_thread: threading.Thread | None = None
        self._untitled_threads: set[str] = set()
        self._read_state = read_state or ReadStateStore(self.data_dir / "read-state.json")
        self._show_intermediate_events = show_intermediate_events
        self._hidden_intermediate_items: dict[str, str] = {}

    def _include_intermediate_events(self) -> bool:
        setting = self._show_intermediate_events
        return bool(setting() if callable(setting) else setting)

    def _codex_inputs(
        self,
        text: str,
        input_parts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Translate device/HTTP input parts into Codex ``turn/start`` input.

        Codex accepts text and inline image inputs on this transport. File
        attachments are kept in the Bridge content store and referenced by the
        message; only images are handed to the model as inline data URLs.
        """

        parts = [part for part in (input_parts or []) if isinstance(part, dict)]
        if not parts:
            content = (text or "").strip()
            return [{"type": "text", "text": content[:MAX_MESSAGE_CHARS]}] if content else []
        result: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for part in parts:
            kind = str(part.get("type") or part.get("part_kind") or "text").lower()
            if kind == "text":
                value = str(part.get("text") or part.get("body") or "")
                if value:
                    text_parts.append(value)
                continue
            attachment_id = str(
                part.get("attachment_id")
                or part.get("attachment_ref")
                or part.get("content_ref")
                or ""
            )
            record = self.attachments.get_by_ref(attachment_id)
            if record is None:
                raise ValueError("attachment_not_found")
            if record.kind != "image":
                raise ValueError("unsupported_attachment_kind")
            result.append(
                {
                    # Codex app-server serializes UserInput::Image as
                    # {"type":"image","url":...}. "input_image" is an OpenAI
                    # message-content type and is rejected by turn/start.
                    "type": "image",
                    "url": self.attachments.data_url(record.attachment_id),
                }
            )
        merged = "\n".join(text_parts).strip() or (text or "").strip()
        if merged:
            result.insert(0, {"type": "text", "text": merged[:MAX_MESSAGE_CHARS]})
        return result

    def start(self) -> None:
        self._stop.clear()
        self.queues.start()
        self._ensure_client()
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._watch_threads, name="companion-thread-watch", daemon=True,
            )
            self._poll_thread.start()

    def _watch_threads(self) -> None:
        while not self._stop.wait(self._poll_seconds()):
            try:
                self.poll_thread_changes()
            except (AppServerError, OSError, ValueError) as error:
                self._note(f"任务同步暂时不可用：{error}")

    def _poll_seconds(self) -> float:
        # A live turn or a waiting approval needs the fast beat; an idle
        # Codex only has to notice a desktop-initiated turn eventually.
        observed = self._observed_threads
        if observed is None:
            return POLL_ACTIVE_SECONDS
        if any(str(row.get('status') or '') in ACTIVE_STATUSES
               for row in observed.values()):
            return POLL_ACTIVE_SECONDS
        if self.pending_approvals():
            return POLL_ACTIVE_SECONDS
        return POLL_IDLE_SECONDS

    def poll_thread_changes(self) -> None:
        current = {
            row["thread_id"]: row
            for row in self.list_threads(limit=80, refresh=True)["threads"]
        }
        if not self._isolated:
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
        for thread_id, row in current.items():
            if str(row.get('status') or '') in ACTIVE_STATUSES:
                # The desktop can resume a finished thread without telling
                # this process, so a thread that woke up loses its cached
                # body as well as its long TTL.
                self._invalidate_reads(thread_id)
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
        self.queues.stop()
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
            self._locked_threads.clear()
        if client is not None and self._close_client_on_stop:
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
        self._read_cache.clear()
        self._terminal_threads.clear()
        self._row_verdicts.clear()
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
        lock_cleared = False
        for attempt in range(2):
            try:
                client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                with self._resumed_lock:
                    self._locked_threads.discard(thread_id)
                self._thread_note(thread_id)
                return
            except AppServerError as exc:
                error = exc
                message = str(exc)
                if _is_active_writer_error(exc):
                    # The desktop keeps a stale writer lock after it finishes a
                    # turn. Remove it so the phone continues the original
                    # conversation instead of silently forking it.
                    if not lock_cleared and _clear_writer_lock(thread_id):
                        lock_cleared = True
                        continue
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
            # A desktop-owned turn is reported as "interrupted" to any second
            # reader even while it is still running, so treat an unfinished
            # turn that is still being written as active too.
            if status == "interrupted" and turn.get("completedAt") is None:
                return str(turn.get("id") or "")
        return ""

    def _writable_thread(self, thread_id: str) -> str:
        """Return a thread id this client may write to.

        app-server allows only one writer per stored thread, and the desktop
        keeps a writer lock on every thread it has opened -- including threads
        whose turn finished long ago. That lock is a file on disk rather than
        proof that a turn is running, so clearing it lets us resume the
        original thread and append to it directly. The phone and the desktop
        then share one conversation and each sees the other's turns.

        Forking stays only as the fallback for the one case where hijacking is
        unsafe: the desktop is running a turn on that thread right now.
        """
        if not thread_id:
            return thread_id
        with self._resumed_lock:
            existing = self._forked_threads.get(thread_id)
        if existing:
            # A fork recorded by an earlier build left this conversation in a
            # copy the desktop never opens, so the two sides drift apart. Go
            # back to the original thread so both share one history again.
            if self._reclaim_original(thread_id, existing):
                return thread_id
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
        # The desktop holds the writer lock. Take the original thread over
        # rather than copying it, unless a turn is running on it right now.
        if self._log_status(thread_id) != "working":
            if self._take_over(thread_id):
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

    def _take_over(self, thread_id: str) -> bool:
        """Clear the writer lock and resume the original thread in place.

        Returns True when the thread is now ours to write to, so the phone
        continues the real conversation instead of a fork of it.
        """
        if not thread_id:
            return False
        # _ensure_thread may already have cleared the lock, and a lock file that
        # is gone is success rather than failure, so clear it best-effort and
        # let the retried resume decide whether we own the thread.
        _clear_writer_lock(thread_id)
        with self._resumed_lock:
            self._resumed_threads.discard(thread_id)
            self._locked_threads.discard(thread_id)
        try:
            self._ensure_thread(thread_id)
        except AppServerError:
            with self._resumed_lock:
                self._locked_threads.add(thread_id)
            return False
        # _ensure_thread returns quietly when the lock is still held by someone
        # else, so the lock set is what tells us whether the thread is ours.
        with self._resumed_lock:
            return thread_id not in self._locked_threads

    def _reclaim_original(self, thread_id: str, fork_id: str) -> bool:
        """Move a forked conversation back onto the thread it came from.

        Forks recorded by earlier builds left the phone's replies in a copy the
        desktop never opens, so the two sides drift apart. Returns False when
        the original can no longer be resumed and the fork must be kept.
        """
        if not self._take_over(thread_id):
            return False
        with self._resumed_lock:
            self._forked_threads.pop(thread_id, None)
            self._fork_titles.pop(thread_id, None)
            # Keep reading the fork's history, but stop writing to it.
            self._fork_tails[thread_id] = fork_id
        self._save_fork_map()
        return True

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
        tails = payload.get("tails")
        if isinstance(tails, dict):
            for origin, tail in tails.items():
                if isinstance(origin, str) and isinstance(tail, str) and origin and tail:
                    self._fork_tails[origin] = tail

    def _save_fork_map(self) -> None:
        with self._resumed_lock:
            payload = {
                "version": 1,
                "forks": dict(self._forked_threads),
                "titles": dict(self._fork_titles),
                "tails": dict(self._fork_tails),
            }
        try:
            self._fork_map_path().parent.mkdir(parents=True, exist_ok=True)
            temporary = self._fork_map_path().with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._fork_map_path())
        except OSError:
            return

    def _load_thread_settings(self) -> None:
        try:
            payload = json.loads(
                self._thread_settings_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        threads = payload.get("threads")
        if not isinstance(threads, dict):
            return
        with self._thread_settings_lock:
            for thread_id, value in threads.items():
                if not isinstance(thread_id, str) or not isinstance(value, dict):
                    continue
                settings = {
                    key: item
                    for key, item in value.items()
                    if key in {
                        "approval_policy",
                        "approvals_reviewer",
                        "sandbox_policy",
                        "personality",
                        "settings_revision",
                    }
                    and isinstance(item, (str, int))
                }
                if settings:
                    self._thread_settings[thread_id] = settings

    def _save_thread_settings(self) -> None:
        with self._thread_settings_lock:
            payload = {
                "version": 1,
                "threads": {
                    thread_id: dict(settings)
                    for thread_id, settings in self._thread_settings.items()
                },
            }
        try:
            self._thread_settings_path.parent.mkdir(parents=True, exist_ok=True)
            # Use a unique temporary file so concurrent settings updates cannot
            # overwrite each other's write before the atomic replace.
            temporary = self._thread_settings_path.with_suffix(
                f".json.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                temporary.replace(self._thread_settings_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except OSError:
            return

    def _remember_thread_settings(
        self,
        thread_id: str,
        *,
        approval_policy: str = "",
        approvals_reviewer: str = "",
        sandbox_policy: str | dict[str, Any] = "",
        personality: str = "",
    ) -> int:
        """Persist settings the upstream app-server does not return on read."""

        with self._thread_settings_lock:
            current = self._thread_settings.setdefault(thread_id, {})
            if approval_policy:
                current["approval_policy"] = str(approval_policy)
            if approvals_reviewer:
                current["approvals_reviewer"] = str(approvals_reviewer)
            if sandbox_policy:
                current["sandbox_policy"] = _sandbox_policy_name(sandbox_policy)
            if personality:
                current["personality"] = str(personality)
            revision = int(current.get("settings_revision") or 0) + 1
            current["settings_revision"] = revision
        self._save_thread_settings()
        return revision

    def _thread_settings_snapshot(self, thread_id: str) -> dict[str, Any]:
        with self._thread_settings_lock:
            return dict(self._thread_settings.get(thread_id, {}))

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

    def _thread_log(self, thread_id: str) -> tuple[float, str, float, float, float] | None:
        """Return rollout-log activity for a thread.

        Result is ``(newest_timestamp, last_turn_event, event_timestamp,
        log_write_time, real_activity_timestamp)``. The
        event is the last ``task_started`` / ``task_complete`` /
        ``turn_aborted`` line seen in the tail, or ``""`` when the tail is too
        short to contain one. ``real_activity_timestamp`` is the newest stamp
        from a record that reflects actual work (anything except the
        ``thread_settings_applied`` bookkeeping line a resume appends), so
        callers can tell "a turn is running" apart from "the file was merely
        touched by a resume".
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
        for offset in range(ROLLOUT_SCAN_DAYS):
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
    def _read_log_tail(path: Path) -> tuple[float, str, float, float, float] | None:
        # One liveness algorithm, shared with the device collector.
        return read_log_tail(path)

    def _log_status(self, thread_id: str) -> str:
        # One liveness algorithm, shared with the device collector: the rollout
        # log the desktop appends to is the only place a turn that another
        # process is running can still be seen as alive.
        return status_from_activity(self._thread_log(thread_id))

    def _read(self, method: str, params: dict[str, Any],
             refresh: bool = False) -> Any:
        # Reads dominate the cost of this API: one phone status call used to
        # fan out into a thread/read per row, and every device polled it, all
        # queued behind one app-server request lock. Cache briefly; retire an
        # entry the moment a change touches its thread.
        if method in CACHEABLE_METHODS:
            return self._read_cache.call(
                method, params, lambda: self._fetch(method, params),
                ttl=self._read_ttl(method, params),
                refresh=refresh)
        return self._fetch(method, params)

    def _read_ttl(self, method: str, params: dict[str, Any]) -> float:
        if method != 'thread/read':
            return READ_TTL_SECONDS
        key = self.visible_thread_id(thread_tag(params))
        if not key or key not in self._terminal_threads:
            return READ_TTL_SECONDS
        # The desktop can resume a finished thread without telling this
        # process, so a thread whose rollout log is writing again loses the
        # long body TTL at once.
        if self._log_status(key):
            return READ_TTL_SECONDS
        return TERMINAL_READ_TTL_SECONDS

    def _monotonic_terminal_stamp(self, thread_id: str, stamp: int) -> int:
        """Keep a thread's terminal timestamp from ever moving backwards."""

        stamp = max(0, int(stamp or 0))
        with self._terminal_stamp_lock:
            previous = int(self._terminal_stamps.get(thread_id) or 0)
            if stamp < previous:
                stamp = previous
            elif stamp > previous:
                self._terminal_stamps[thread_id] = stamp
        return stamp

    def _note_terminal(self, thread_id: str, status: str, live: bool = False) -> None:
        """Remember that a thread's turn is over so its reads can be cached.

        Only an app-server turn that actually reached a terminal state
        qualifies: the rollout log also reports completion for turns another
        process is running, and a thread can be resumed by the desktop at
        any time, so a log-only verdict must not pin a live body.
        """
        if not thread_id:
            return
        if live:
            self._terminal_threads.discard(thread_id)
            return
        if status in TERMINAL_BODY_STATUSES:
            self._terminal_threads.add(thread_id)
        else:
            self._terminal_threads.discard(thread_id)

    def _fetch(self, method: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._ensure_client().request(method, params)
            except AppServerProtocolError as error:
                # The server is alive and said no. Reconnecting spawns a whole
                # Codex process (~1.5 s here) and the same method fails again
                # on the fresh pipe, so report it once and keep the transport.
                # One exception: a long-lived app-server cannot page the turns
                # of a thread it just created, so thread/read answers
                # "list_turns is not supported yet". A fresh process reads the
                # same rollout from disk, so retry once on a reset instead of
                # failing the phone's read.
                if method == "thread/read" and _is_stale_thread_read(error) and attempt == 0:
                    last_error = error
                    self._reset_client()
                    continue
                raise
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

    # Streaming events repeat for every token of one turn, so they carry
    # no new information about a thread body: refreshing the cache on each
    # one would put us back to one read per row per poll.
    STREAMING_EVENT_TYPES = frozenset({
        'agent_delta',
        'command_output',
        'item_started',
        'item_completed',
    })

    def publish(self, payload: dict[str, Any]) -> None:
        # An unknown event type invalidates: a new kind must never be able
        # to leave a device looking at a stale body.
        if payload.get('type') not in self.STREAMING_EVENT_TYPES:
            self._invalidate_reads(str(payload.get('thread_id') or ''))
        self._events.publish(payload)
        if payload.get('type') in {"approval_request", "approval_resolved", "approval_expired"}:
            publish_cbdp = getattr(self, "_publish_cbdp_approval", None)
            if callable(publish_cbdp):
                publish_cbdp(payload)

    def _publish_cbdp_approval(self, payload: dict[str, Any]) -> None:
        """Mirror an approval change onto the CBDP Event Spine.

        Legacy phones read approval_* from the JSON event stream. CBDP devices
        only see committed Event Spine events, so the same transition must be
        appended there or an ESP panel never learns that approval is pending.
        """

        spine = getattr(self, "_cbdp_event_spine", None)
        if spine is None:
            return
        kind = str(payload.get("type") or "")
        event_type = {
            "approval_request": 0x0401,
            "approval_resolved": 0x0402,
            "approval_expired": 0x0403,
        }.get(kind)
        if event_type is None:
            return
        thread_id = str(payload.get("thread_id") or "")
        approval_id = str(payload.get("request_id") or payload.get("approval_id") or "")
        if not approval_id:
            return
        topic_id = 0
        refs = getattr(self, "_cbdp_references", None)
        if refs is not None:
            try:
                topic_id = int(refs.topic_for(thread_id)) if thread_id else 0
            except Exception:
                topic_id = 0
        fields: dict[int, bytes] = {
            TAG_APPROVAL_ID: utf8(approval_id),
        }
        if refs is not None:
            try:
                fields[TAG_APPROVAL_REF] = u32(
                    int(refs.ref_for("approval", approval_id))
                )
                if thread_id:
                    fields[TAG_THREAD_REF] = u32(
                        int(refs.ref_for("thread", thread_id))
                    )
                turn_id = str(payload.get("turn_id") or "")
                if turn_id:
                    fields[TAG_TURN_REF] = u32(int(refs.ref_for("turn", turn_id)))
            except Exception:
                pass
        if kind == "approval_request":
            fields[TAG_APPROVAL_KIND] = u8(
                {
                    "command": 0,
                    "fileChange": 1,
                    "permissions": 3,
                    "mcp": 4,
                    "input": 5,
                }.get(str(payload.get("kind") or ""), 6)
            )
            prompt = str(payload.get("command") or payload.get("reason") or "")
            if prompt:
                fields[TAG_PROMPT] = utf8(_clip(prompt, 512))
            expires = int(float(payload.get("expires_at") or 0) * 1000)
            if expires:
                fields[TAG_EXPIRES_AT_MS] = i64(expires)
        try:
            # EventSpine expects the TLV map itself and encodes it on append.
            spine.append(topic_id, event_type, 0x0, fields)
        except Exception:
            return

    def _invalidate_reads(self, thread_id: str) -> None:
        """Retire the cached reads a change makes stale.

        A phone reads the hidden fork while the desktop holds the writer
        lock, so a change reported against either id stales both bodies.
        """
        if not thread_id:
            self._read_cache.clear()
            self._terminal_threads.clear()
            self._row_verdicts.clear()
            return
        targets = {thread_id,
                   self._fork_for(thread_id),
                   self.visible_thread_id(thread_id),
                   self._fork_origin.get(thread_id, '')}
        for target in targets:
            if target:
                self._terminal_threads.discard(target)
                self._row_verdicts.pop(target, None)
                self._read_cache.invalidate_thread(target)

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
            item_id = str(params.get("itemId") or "")
            phase = str(params.get("phase") or "").replace("_", "").lower()
            if not self._include_intermediate_events():
                if phase == "commentary" or item_id in self._hidden_intermediate_items:
                    if item_id:
                        self._hidden_intermediate_items[item_id] = str(params.get("turnId") or "")
                    return None
            return {
                "type": "agent_delta",
                "thread_id": self.visible_thread_id(str(params.get("threadId") or "")),
                "turn_id": str(params.get("turnId") or ""),
                "item_id": item_id,
                "delta": str(params.get("delta") or params.get("text") or ""),
            }
        if method == "item/commandExecution/outputDelta":
            if not self._include_intermediate_events():
                return None
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
            item_id = str(mapped.get("id") or "")
            intermediate = mapped.get("kind") in INTERMEDIATE_MESSAGE_KINDS
            if item_id:
                if intermediate and method == "item/started":
                    self._hidden_intermediate_items[item_id] = str(params.get("turnId") or "")
                elif method == "item/completed":
                    self._hidden_intermediate_items.pop(item_id, None)
                else:
                    self._hidden_intermediate_items.pop(item_id, None)
            if not self._include_intermediate_events() and intermediate:
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
            turn_id = str(turn.get("id") or "")
            if turn_id:
                for hidden_item in [
                    item_id for item_id, hidden_turn in self._hidden_intermediate_items.items()
                    if hidden_turn == turn_id
                ]:
                    self._hidden_intermediate_items.pop(hidden_item, None)
            thread_id = self.visible_thread_id(str(params.get("threadId") or ""))
            completed_at = _epoch(turn.get("completedAt") or turn.get("completedAtMs"))
            self._on_turn_completed(thread_id)
            resolved, completed_at = resolve_terminal_state(
                status,
                completed_at=completed_at,
                fallback_timestamps=(
                    _epoch(turn.get("updatedAt") or turn.get("updatedAtMs")),
                    _epoch(turn.get("startedAt") or turn.get("startedAtMs")),
                    _epoch(params.get("recencyAt")),
                ),
            )
            if resolved in {"completed", "failed"}:
                completed_at = self._monotonic_terminal_stamp(thread_id, completed_at)
            return {
                "type": "turn_completed",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": resolved,
                "duration_ms": turn.get("durationMs"),
                "completed_at": completed_at,
                "error_message": _turn_error(turn),
                # App-server notifications only reach the bridge that owns a
                # turn. This marker tells every connected client to append the
                # completed turn from disk so desktop-created work is visible
                # even when this process never called turn/start.
                "append_only": True,
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
    def _on_turn_completed(self, thread_id: str) -> None:
        if thread_id:
            self.queues.on_turn_finished(thread_id)

    def _queue_thread_exists(self, thread_id: str) -> bool:
        return bool(thread_id)

    def _queue_thread_active(self, thread_id: str) -> bool:
        try:
            return bool(self._active_turn_id(thread_id))
        except (AppServerError, OSError, ValueError):
            return False

    def queue_list(self, thread_id: str) -> dict[str, Any]:
        return self.queues.list(thread_id)

    def queue_enqueue(
        self,
        thread_id: str,
        text: str,
        model: str = "",
        effort: str = "",
        input_parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.queues.enqueue(
            thread_id,
            text,
            model=model,
            effort=effort,
            input_parts=input_parts,
        )

    def queue_edit(self, queue_id: str, text: str, model: str = "", effort: str = "", expected_revision: int = 0) -> dict[str, Any]:
        return self.queues.edit(queue_id, text, model=model, effort=effort, expected_revision=expected_revision)

    def queue_delete(self, queue_id: str, expected_revision: int = 0) -> dict[str, Any]:
        return self.queues.delete(queue_id, expected_revision=expected_revision)

    def queue_steer(self, queue_id: str, expected_revision: int = 0) -> dict[str, Any]:
        return self.queues.steer(queue_id, expected_revision=expected_revision)

    def queue_snapshots(self) -> list[dict[str, Any]]:
        return self.queues.snapshots()

    def attachment_list(self, thread_id: str) -> dict[str, Any]:
        return self.attachments.list(thread_id)

    def attachment_add(
        self,
        thread_id: str,
        attachment_id: str,
        alt_text: str = "",
    ) -> dict[str, Any]:
        record = self.attachments.get(attachment_id)
        if record is None:
            raise ValueError("attachment_not_found")
        if alt_text:
            record = self.attachments.set_alt_text(attachment_id, alt_text) or record
        return record.public()

    def attachment_remove(self, attachment_id: str) -> dict[str, Any]:
        return {"removed": self.attachments.remove(attachment_id)}

    def attachment_upload_begin(
        self,
        thread_id: str,
        attachment_kind: str,
        byte_size: int,
        filename: str = "",
        mime_type: str = "",
        content_sha256: str = "",
        alt_text: str = "",
    ) -> dict[str, Any]:
        return self.attachments.begin_upload(
            thread_id=thread_id,
            kind=attachment_kind,
            byte_size=byte_size,
            content_sha256=content_sha256,
            filename=filename,
            mime_type=mime_type,
            alt_text=alt_text,
        )

    def attachment_upload_chunk(
        self,
        upload_id: str,
        upload_offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        return self.attachments.upload_chunk(upload_id, upload_offset, data)

    def attachment_upload_end(
        self,
        upload_id: str,
        byte_size: int | None = None,
    ) -> dict[str, Any]:
        return self.attachments.end_upload(upload_id, byte_size=byte_size)

    def attachment_upload_cancel(self, upload_id: str) -> dict[str, Any]:
        return self.attachments.cancel_upload(upload_id)

    def content_read(
        self,
        attachment_id: str,
        offset: int = 0,
        max_bytes: int = 256 * 1024,
    ) -> dict[str, Any]:
        result = self.attachments.read_bytes(attachment_id, offset, max_bytes)
        import base64

        data = result.pop("data", b"")
        result["content_base64"] = base64.b64encode(data).decode("ascii")
        return result

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
            "show_intermediate_events": self._include_intermediate_events(),
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

    def _read_thread_summary(self, thread_id: str) -> dict[str, Any] | None:
        """Return a thread's summary with turns included, or None if unreadable."""
        try:
            result = self._read("thread/read", {"threadId": thread_id, "includeTurns": True})
        except (AppServerError, OSError):
            return None
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return None
        summary = self._summary(thread)
        # Rows the rollout log cannot classify are decided by this read, so
        # a settled verdict here is what lets the next poll skip the trip.
        self._note_terminal(thread_id, str(summary.get("status") or ""),
                           live=bool(self._log_status(thread_id)))
        return summary

    def _row_verdict(self, thread_id: str) -> tuple[str, int] | None:
        """Return a remembered turn-read verdict for a row, if still credible.

        A settled thread keeps its verdict; a thread that started moving again
        is told so by the rollout log, which wins here and clears the memo the
        moment any change touches the thread. The TTL bounds the one case the
        log cannot cover: a rollout this process cannot read at all.
        """
        entry = self._row_verdicts.get(thread_id)
        if entry is None:
            return None
        status, completed_at, stamp = entry
        if time.monotonic() - stamp > ROW_VERDICT_SECONDS:
            self._row_verdicts.pop(thread_id, None)
            return None
        return status, completed_at

    def list_threads(
        self,
        limit: int = 40,
        search: str = "",
        cursor: str | None = None,
        refresh: bool = False,
        project_id: str = "",
    ) -> dict[str, Any]:
        # A plain listing is answered from one shared page. Fetching per caller
        # meant the phone re-read the whole task list on every poll even though
        # the watcher had refreshed the identical rows seconds earlier, because
        # the read cache keys on the request params.
        if project_id:
            return self._project_threads(
                project_id,
                limit=limit,
                search=search,
                refresh=refresh,
            )
        if not search and not cursor:
            page = self._list_page(refresh=refresh)
            want = max(1, min(int(limit or 40), 80))
            rows = page["threads"]
            return {
                "threads": rows[:want] if len(rows) > want else rows,
                "next_cursor": page.get("next_cursor"),
            }
        return self._list_page(limit=limit, search=search, cursor=cursor,
                               refresh=refresh)

    def _project_threads(
        self,
        project_id: str,
        *,
        limit: int = 40,
        search: str = "",
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return threads whose working directory belongs to one project."""

        wanted = str(project_id or "")
        if not wanted:
            return {"threads": [], "next_cursor": None}
        projects = self.list_projects().get("projects") or []
        project = next(
            (
                row for row in projects
                if isinstance(row, dict)
                and str(row.get("project_id") or "") == wanted
            ),
            None,
        )
        if project is None:
            # A reference may point at the project name when the desktop stored
            # the project without an explicit id.
            project = next(
                (
                    row for row in projects
                    if isinstance(row, dict) and str(row.get("name") or "") == wanted
                ),
                None,
            )
        roots = [
            str(path).rstrip("\\/").lower()
            for path in (project or {}).get("roots") or []
            if str(path or "").strip()
        ]
        want = max(1, min(int(limit or 40), 80))
        rows = self._list_page(refresh=refresh).get("threads") or []
        search_text = str(search or "").strip().lower()
        matched: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cwd = str(row.get("cwd") or "").rstrip("\\/").lower()
            if not cwd or not any(
                cwd == root or cwd.startswith(root + "\\") or cwd.startswith(root + "/")
                for root in roots
            ):
                continue
            if search_text and search_text not in " ".join(
                str(row.get(key) or "").lower()
                for key in ("thread_id", "title", "preview", "workspace")
            ):
                continue
            matched.append(row)
        return {
            "threads": matched[:want],
            "next_cursor": None,
        }

    def _list_page(
        self,
        limit: int = LIST_PAGE_LIMIT,
        search: str = "",
        cursor: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit or LIST_PAGE_LIMIT), 80)),
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": SOURCE_KINDS,
            "archived": False,
        }
        if cursor:
            params["cursor"] = cursor
        if search:
            params["searchTerm"] = search[:120]
        result = self._read("thread/list", params, refresh=refresh)
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
            summary["queued_count"] = len(self.queues.list(visible_id).get("items") or [])
            threads.append(summary)
        for index, summary in enumerate(threads):
            if index >= LOG_SCAN_LIMIT:
                break
            thread_id = str(summary.get("thread_id") or "")
            # The rollout log is authoritative for every row, not just the ones
            # this process cannot see. thread/list reports a desktop-owned thread
            # as unloaded to any second reader, so an "active" row can look idle
            # here while the chat header shows it running. Ask the log first and
            # let it win for running work.
            activity = self._thread_log(thread_id)
            inferred = self._log_status(thread_id)
            if inferred:
                summary["status"] = inferred
            if activity is not None:
                _newest, _kind, kind_stamp, _written, _real = activity
                if (
                    summary.get("status") in {"completed", "failed"}
                    and kind_stamp > 0
                    and int(summary.get("completed_at") or 0) <= 0
                ):
                    # List rows carry no turns, so a finished task only shows up
                    # via its rollout log. Use that log's completion timestamp
                    # as the read watermark; otherwise completed_at stays 0 and
                    # the "already read" check below can never match.
                    summary["completed_at"] = int(kind_stamp)
            # A long thread's rollout tail can hold no lifecycle event at all:
            # a 70 MB log spends its last 128 KB on tool output, so the
            # task_started / task_complete records that say how the turn ended
            # are far outside the window. The row then has nothing but the
            # unloaded marker from thread/list and reads as idle, while the chat
            # header reads the turn and says "failed". Ask the app-server for
            # the turn itself, but only for rows the log could not classify.
            if activity is None or not inferred:
                verdict = self._row_verdict(thread_id)
                if verdict is None:
                    detail = self._read_thread_summary(thread_id)
                    read_status = ""
                    settled_at = 0
                    if detail is not None:
                        candidate = str(detail.get("status") or "")
                        # "idle" only means the app-server holds no live turn,
                        # which is also what it says about a thread it never
                        # loaded, so it is not a verdict worth remembering.
                        if candidate and candidate != "idle":
                            read_status = candidate
                            settled_at = int(detail.get("completed_at") or 0)
                    self._row_verdicts[thread_id] = (
                        read_status, settled_at, time.monotonic())
                    verdict = (read_status, settled_at)
                if verdict is not None:
                    read_status, settled_at = verdict
                    if read_status:
                        summary["status"] = read_status
                    if settled_at > int(summary.get("completed_at") or 0):
                        summary["completed_at"] = settled_at
            if summary.get("status") in {"completed", "failed"}:
                # One terminal verdict for list, detail and events: never hand a
                # terminal status to a client without a usable end timestamp.
                status, settled_at = resolve_terminal_state(
                    str(summary.get("status") or ""),
                    completed_at=int(summary.get("completed_at") or 0),
                    fallback_timestamps=(
                        int(kind_stamp) if activity is not None else 0,
                        int(summary.get("updated_at") or 0),
                        int(summary.get("recency_at") or 0),
                        int(summary.get("started_at") or 0),
                    ),
                )
                summary["status"] = status
                summary["completed_at"] = int(settled_at)
                if status in {"completed", "failed"}:
                    summary["completed_at"] = self._monotonic_terminal_stamp(
                        thread_id, int(settled_at)
                    )
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
        status = _status_from_thread({**row, "turns": turns})
        completed_at = _epoch(latest_turn.get("completedAt"))
        if status in {"completed", "failed"} and completed_at <= 0:
            status, completed_at = resolve_terminal_state(
                status,
                completed_at=completed_at,
                fallback_timestamps=(
                    _epoch(latest_turn.get("updatedAt")),
                    _epoch(row.get("recencyAt")),
                    _epoch(row.get("updatedAt")),
                    _epoch(latest_turn.get("startedAt")),
                ),
            )
        return {
            "thread_id": str(row.get("id") or ""),
            "title": _thread_title(row),
            "preview": _clip(str(row.get("preview") or ""), 400),
            "status": status,
            "model": str(row.get("model") or ""),
            "cwd": str(row.get("cwd") or ""),
            "workspace": _workspace_name(str(row.get("cwd") or "")),
            "source": str(row.get("source") or ""),
            "created_at": _epoch(row.get("createdAt")),
            "updated_at": _epoch(row.get("updatedAt") or row.get("recencyAt")),
            "recency_at": _epoch(row.get("recencyAt")),
            "completed_at": completed_at,
            "turn_id": str(latest_turn.get("id") or ""),
            "started_at": _epoch(latest_turn.get("startedAt")),
            "tokens": int(_as_number(total.get("totalTokens")) or 0),
            "ephemeral": bool(row.get("ephemeral")),
            "error_message": _turn_error(latest_turn),
        }
    @staticmethod
    def _resolve_status(
        thread_id: str,
        summary: dict[str, Any],
        task: dict[str, Any],
        live_status: str,
    ) -> str:
        """Pick the one status both the list row and the chat header will use.

        The two views used to be derived by different code paths, so a thread
        could be listed as idle but open as "working". A live rollout write is
        the strongest signal, then the turn snapshot, then the row summary.
        """
        if live_status:
            return live_status
        task_status = str(task.get("status") or "")
        summary_status = str(summary.get("status") or "")
        if task_status in ACTIVE_STATUSES:
            return task_status
        if summary_status in ACTIVE_STATUSES:
            return summary_status
        if task_status:
            return task_status
        return summary_status or "idle"

    def _read_fork_tail_messages(
        self,
        fork_id: str,
        seen_turn_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Return the fork's own turns, which the original thread lacks.

        A fork copies the history of the thread it branched from, so only the
        turns started after the fork are new. Those are the phone replies made
        while the desktop held the writer lock, and they must stay visible once
        writing moves back to the original thread.
        """
        if not fork_id:
            return []
        try:
            result = self._read("thread/read", {"threadId": fork_id, "includeTurns": True})
        except AppServerError:
            return []
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return []
        turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
        turns.sort(key=lambda turn: _epoch(
            turn.get("startedAt") or turn.get("createdAt") or turn.get("updatedAt")
        ))
        messages: list[dict[str, Any]] = []
        for turn in turns:
            turn_id = str(turn.get("id") or "")
            if not turn_id or turn_id in seen_turn_ids:
                continue
            for item in turn.get("items", []):
                if not isinstance(item, dict):
                    continue
                mapped = self._map_item(item, turn)
                if mapped is None:
                    continue
                mapped["turn_id"] = turn_id
                messages.append(mapped)
        return messages

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
        tail_thread: dict[str, Any] | None = None
        tail_id = ""
        with self._resumed_lock:
            if thread_id not in self._forked_threads:
                tail_id = self._fork_tails.get(thread_id, "")
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
        seen_turn_ids = {str(turn.get("id") or "") for turn in turns}
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
        # A reclaimed thread keeps the replies the phone made while it lived in
        # a fork. Those turns are in the fork's rollout but not in the
        # original's, so read the tail and append only what is missing.
        tail_messages = self._read_fork_tail_messages(tail_id, seen_turn_ids)
        if tail_messages:
            messages.extend(tail_messages)
        if not self._include_intermediate_events():
            messages = [
                message for message in messages
                if message.get("kind") not in INTERMEDIATE_MESSAGE_KINDS
            ]
        total = len(messages)
        # CBDP thread.read uses history_depth=0 for a metadata-only response.
        # Other callers keep the historical "at least one message" behavior by
        # sending a positive limit explicitly.
        limit = max(0, int(message_limit))
        trimmed = messages[-limit:] if limit else []
        summary = self._summary(thread)
        summary["thread_id"] = thread_id
        original = self._original_title(thread_id)
        if original:
            summary["title"] = original
        task = thread_snapshot(thread)
        task["thread_id"] = thread_id
        # The phone list renders thread.status while the chat header renders
        # task.status. Resolve them once so the two can never disagree, which
        # used to show a task as idle in the list but "working" after opening it.
        resolved = self._resolve_status(
            thread_id=thread_id,
            summary=summary,
            task=task,
            live_status=live_status,
        )
        summary["status"] = resolved
        task["status"] = resolved
        if resolved in {"completed", "failed"}:
            stamp = self._monotonic_terminal_stamp(
                thread_id,
                int(summary.get("completed_at") or task.get("completed_at_epoch") or 0),
            )
            summary["completed_at"] = stamp
            task["completed_at_epoch"] = stamp
        # A settled thread with a quiet rollout log cannot change again on
        # its own, so its body is worth caching far longer than a live one.
        self._note_terminal(thread_id, resolved, live=bool(live_status))
        if resolved in {"completed", "failed"}:
            completed_at = int(summary.get("completed_at") or task.get("completed_at_epoch") or 0)
            if completed_at <= 0:
                completed_at = int(summary.get("updated_at") or 0)
            if completed_at > 0 and self._read_state.is_thread_read(thread_id, completed_at):
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
            "queue": self.queues.list(thread_id),
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
        use_desktop_handoff = self._creation_client_factory is not None and not ephemeral
        if use_desktop_handoff:
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
                                    {'threadId': thread_id, 'name': _BRIDGE_PENDING_TITLE})
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
        if not use_desktop_handoff:
            # thread/start already loaded the thread in this process, but it has no
            # rollout file until its first turn runs, so a later resume fails with
            # "no rollout found". Remember it so the first reply skips the resume.
            with self._resumed_lock:
                self._locked_threads.discard(thread_id)
            self._thread_note(thread_id)
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

    def mark_message_read(
        self,
        message_id: str,
        read_at_ms: int,
        thread_id: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """Persist one message read marker and return the shared read state."""

        if not message_id:
            raise ValueError("message_id")
        if not isinstance(read_at_ms, int) or isinstance(read_at_ms, bool) or read_at_ms < 0:
            raise ValueError("read_at_ms")
        source = str(source or "")
        self._read_state.mark_message(message_id, source=source)
        if thread_id and read_at_ms > 0:
            self._read_state.mark_thread(
                thread_id,
                int(read_at_ms // 1000),
                source=source,
                notify=False,
            )
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "read_at": int(self._read_state.thread_read_at(thread_id)) if thread_id else 0,
            "read_states": [
                {
                    "read_at_ms": int(read_at_ms),
                    "source": source,
                }
            ],
        }

    def read_thread_settings(self, thread_id: str) -> dict[str, Any]:
        """Return the settings a phone needs for its approval-mode control."""

        if not thread_id:
            raise ValueError("thread_id")
        try:
            detail = self._read(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        except (AppServerError, OSError, ValueError):
            detail = {}
        thread = detail.get("thread") if isinstance(detail, dict) else None
        thread = thread if isinstance(thread, dict) else {}
        runtime = thread.get("status") if isinstance(thread.get("status"), dict) else {}
        persisted = self._thread_settings_snapshot(thread_id)
        return {
            "thread_id": thread_id,
            "model": str(thread.get("model") or ""),
            "effort": str(
                persisted.get("effort")
                or thread.get("effort")
                or thread.get("reasoningEffort")
                or ""
            ),
            "cwd": str(thread.get("cwd") or ""),
            "approval_policy": str(
                persisted.get("approval_policy")
                or thread.get("approvalPolicy")
                or runtime.get("approvalPolicy")
                or ""
            ),
            "approvals_reviewer": str(
                persisted.get("approvals_reviewer")
                or thread.get("approvalsReviewer")
                or runtime.get("approvalsReviewer")
                or ""
            ),
            "sandbox_policy": str(
                persisted.get("sandbox_policy")
                or thread.get("sandboxPolicy")
                or runtime.get("sandboxPolicy")
                or ""
            ),
            "personality": str(
                persisted.get("personality") or thread.get("personality") or ""
            ),
            "settings_revision": int(
                persisted.get("settings_revision")
                or thread.get("settingsRevision")
                or 0
            ),
        }

    def update_thread_settings(
        self,
        thread_id: str,
        model: str = "",
        effort: str = "",
        approval_policy: str = "",
        approvals_reviewer: str = "",
        sandbox_policy: str | dict[str, Any] = "",
        personality: str = "",
    ) -> dict[str, Any]:
        """Persist thread-level settings, including the approval mode."""

        if not thread_id:
            raise ValueError("thread_id")
        if not any(
            [model, effort, approval_policy, approvals_reviewer, sandbox_policy, personality]
        ):
            raise ValueError("settings")
        owner = self._desktop_owner(thread_id)
        if owner:
            applied = self._desktop.update_thread_settings(
                owner,
                thread_id,
                model=model,
                effort=effort,
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
                sandbox_policy=sandbox_policy,
                personality=personality,
            )
        else:
            self._ensure_client()
            params: dict[str, Any] = {"threadId": thread_id}
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            if approval_policy:
                params["approvalPolicy"] = approval_policy
            if approvals_reviewer:
                params["approvalsReviewer"] = approvals_reviewer
            if sandbox_policy:
                params["sandboxPolicy"] = _sandbox_policy_object(sandbox_policy)
            if personality:
                params["personality"] = personality
            self._client.request("thread/settings/update", params)
            applied = True
        revision = 0
        if applied:
            revision = self._remember_thread_settings(
                thread_id,
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
                sandbox_policy=sandbox_policy,
                personality=personality,
            )
        self.publish({"type": "thread_settings_changed", "thread_id": thread_id})
        return {
            "thread_id": thread_id,
            "applied": bool(applied),
            "settings_revision": revision,
        }

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
        if status not in ACTIVE_STATUSES:
            # A settled thread can still arrive without a terminal timestamp
            # (older upstream payloads, repaired rows). Persist a watermark
            # from the best available evidence so the phone's read state
            # survives the next list refresh.
            watermark = completed_at
            if watermark <= 0:
                watermark = int(summary.get("updated_at") or summary.get("recency_at") or 0)
            if watermark <= 0:
                watermark = int(time.time())
            watermark = self._monotonic_terminal_stamp(thread_id, watermark)
            was_unread = not self._read_state.is_thread_read(thread_id, watermark)
            self._read_state.mark_thread(thread_id, watermark)
            completed_at = watermark
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
        input_parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id")
        content = (text or "").strip()
        inputs = self._codex_inputs(content, input_parts)
        if not inputs:
            raise ValueError("text")
        if thread_id in self._desktop_required and thread_id not in self._desktop_followed:
            raise DesktopIpcError('desktop_handoff_pending')
        target_id = thread_id
        params: dict[str, Any] = {
            "threadId": target_id,
            "input": inputs,
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
        target_id = self._writable_thread(thread_id)
        params["threadId"] = target_id
        client = self._ensure_client()
        result = client.request("turn/start", params)
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
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "working",
            "continued_as": target_id if target_id != thread_id else "",
        }

    def _desktop_owner(self, thread_id: str) -> str | None:
        from codexbridge.codex.desktop_ipc import DesktopIpcClient, DesktopIpcError
        if self._stop.is_set():
            raise DesktopIpcError('Companion stopped')
        # Tests and embedders can explicitly isolate this service from the
        # user's live desktop. Production keeps discovery enabled even though
        # it injects a shared app-server client factory.
        if self._desktop is None and self._isolated:
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
                # A terminal snapshot for a turn we have never seen active is
                # history, not a new completion. It must only seed the
                # baseline so a Bridge restart cannot replay old tasks.
                if terminal:
                    if key in self._desktop_active_turns and completed_key not in self._desktop_completed_turns:
                        self._desktop_completed_turns.add(completed_key)
                        self._on_turn_completed(thread_id)
                        self.publish({'type': 'turn_completed', 'thread_id': thread_id,
                            'turn_id': turn_id,
                            'status': 'completed' if terminal == 'completed' else 'failed'})
                    else:
                        self._desktop_completed_turns.add(completed_key)
                    self._desktop_active_turns.discard(key)
                else:
                    self._desktop_active_turns.add(key)
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
        owner = self._desktop_owner(thread_id)
        if owner:
            self._desktop.interrupt_turn(owner, thread_id, turn_id)
            self.publish({'type': 'turn_interrupted', 'thread_id': thread_id,
                          'turn_id': turn_id})
            return {'thread_id': thread_id, 'turn_id': turn_id,
                    'execution_transport': 'desktop_owner'}
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
