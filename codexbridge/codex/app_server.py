#!/usr/bin/env python3
"""Read Codex account and thread state through the official app-server protocol."""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from codexbridge import VERSION
from codexbridge.codex.rollout_activity import (
    RolloutActivity,
    status_from_activity,
)
from codexbridge.console import emit
from codexbridge.messages_store import IN_FLIGHT_STATUSES

DETACHED_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
# A turn that stopped updating this long ago is over, not still running.
RESUMED_ACTIVE_WINDOW_SECONDS = 120
# A failed/interrupted turn with no end timestamp is only treated as still
# running while its last activity is inside this window. Longer silence means
# the turn really stopped; settle it at the last observed activity.
STALE_TURN_WINDOW_SECONDS = 300
TRANSIENT_REFRESH_FAILURE_LIMIT = 6


class AppServerError(RuntimeError):
    """Raised when the Codex app-server cannot satisfy a request."""


class AppServerProtocolError(AppServerError):
    """The app-server answered, so the pipe is healthy but the method failed.

    A JSON-RPC error payload -- unknown method, bad params, a rejected
    approval -- comes back from a live server. Restarting it fixes nothing
    and costs a fresh process spawn, which is the most expensive thing a
    device poll can do. Transport failures keep raising the plain
    ``AppServerError`` so callers still reconnect on a dead pipe.
    """


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
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return max(0, int(parsed.timestamp()))
        except ValueError:
            return 0
    return 0


def _text(value: Any) -> str:
    """Extract user-visible text without exposing tool arguments or output."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value))).strip()
    if not isinstance(value, dict):
        return ""
    item_type = str(value.get("type", ""))
    if item_type in {"image", "localImage", "audio", "localAudio"}:
        return ""
    for key in ("text", "value"):
        if isinstance(value.get(key), str):
            return value[key].strip()
    return _text(value.get("content"))


def _summary_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_summary_text(item) for item in value))).strip()
    if isinstance(value, dict):
        for key in ("text", "summaryText", "summary_text"):
            if isinstance(value.get(key), str):
                return value[key].strip()
        return _summary_text(value.get("summary"))
    return ""


def _user_message_text(value: Any) -> str:
    """Remove desktop attachment metadata while retaining the authored request."""
    text = _text(value)
    for marker in ("\n## My request:\n", "\n# My request:\n"):
        if marker in text:
            return text.rsplit(marker, 1)[1].strip()
    return text


def _safe_line(value: str, maximum: int) -> str:
    cleaned = " ".join(value.replace("\x00", "").split())
    for marker in ("**", "__", "`", "###", "##"):
        cleaned = cleaned.replace(marker, "")
    return cleaned.encode("utf-8")[:maximum].decode("utf-8", errors="ignore").strip()


def _safe_message(value: str, maximum: int | None = None) -> str:
    """Keep visible paragraph breaks, optionally applying a byte cap."""
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    if maximum is None:
        return cleaned
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= maximum:
        return cleaned
    marker = b"..."
    if maximum <= len(marker):
        return marker[:maximum].decode("ascii")
    visible = encoded[: maximum - len(marker)].decode("utf-8", errors="ignore").rstrip()
    return visible + marker.decode("ascii")


def _codex_install_candidates() -> list[Path]:
    """Return known per-user Codex CLI locations when PATH is not inherited."""
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if bin_root.is_dir():
            versioned = sorted(
                (path / "codex.exe" for path in bin_root.iterdir() if path.is_dir()),
                key=lambda path: path.parent.stat().st_mtime,
                reverse=True,
            )
            candidates.extend(versioned)
        candidates.append(bin_root / "codex.exe")
    app_data = os.environ.get("APPDATA")
    if app_data:
        candidates.extend(
            [
                Path(app_data) / "npm" / "codex.cmd",
                Path(app_data) / "npm" / "codex.exe",
            ]
        )
    return candidates


def _resolve_codex_command(explicit: str | None, runtime_folder: Path) -> list[str]:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    environment = os.environ.get("STATE_DISPLAY_CODEX_COMMAND")
    if environment:
        candidates.append(environment)

    for value in candidates:
        candidate = Path(value).expanduser()
        executable = str(candidate) if candidate.is_file() else shutil.which(value)
        if executable:
            if Path(executable).suffix.lower() == ".js":
                node = shutil.which("node")
                if not node:
                    raise AppServerError("找到了 Codex CLI，但没有找到 Node.js")
                return [node, executable]
            return [executable]

    executable = shutil.which("codex")
    if executable:
        return [executable]
    for candidate in _codex_install_candidates():
        if candidate.is_file():
            return [str(candidate)]
    raise AppServerError("未找到 Codex CLI，请确认 Codex 已安装并位于 PATH 中")


class AppServerClient:
    READ_CACHE_SECONDS = 10.0
    # A full ``thread/read`` with ``includeTurns=true`` returns the entire
    # stored thread in one JSON line; on a long desktop thread that measured
    # 84.7 MB and json.loads() held the GIL for ~2 s, stalling every other
    # Python thread in this process and freezing the system pointer. The
    # collector now reads bounded pages instead, so this bound only has to
    # catch pathological responses while still admitting a worst-case
    # ``thread/items/list`` page of large items.
    MAX_STDOUT_LINE_CHARS = 8_000_000
    _STDOUT_DISCARD_CHARS = 1 << 20
    READ_CACHE_METHODS = frozenset({
        "account/read",
        "account/rateLimits/read",
        "account/usage/read",
        "model/list",
        "project/list",
        "thread/list",
        "thread/read",
        "thread/turns/list",
    })

    def __init__(self, command: list[str], cwd: Path, timeout: float = 15.0,
                 prefer_shared: bool = False) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.prefer_shared = prefer_shared
        self.shared = False
        self.transport = "unstarted"
        self.transport_reason = "not_started"
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._read_cache_lock = threading.Lock()
        self._read_cache: dict[str, tuple[float, Any]] = {}
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        # Server-initiated requests (approval prompts and tool questions).
        # Only clients that explicitly consume this queue need them; the rest
        # keep the previous behaviour of ignoring them.
        self.server_requests: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        self._closed = threading.Event()
        self.oversized_stdout_lines = 0

    @staticmethod
    def _desktop_socket() -> Path | None:
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        path = root / "app-server-control" / "app-server-control.sock"
        if not path.exists() or not hasattr(socket, "AF_UNIX"):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(path))
            return path
        except OSError:
            return None

    def start(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.close()
        endpoint = self._desktop_socket() if self.prefer_shared else None
        self.shared = endpoint is not None
        self.transport = "proxy" if self.shared else "stdio"
        self.transport_reason = ("shared_endpoint" if self.shared else ("shared_endpoint_unavailable" if self.prefer_shared else "not_requested"))
        args = ([*self.command, "app-server", "proxy", "--sock", str(endpoint)]
                if endpoint is not None else
                [*self.command, "app-server", "--listen", "stdio://"])
        if self.prefer_shared and not self.shared:
            emit("Codex 桌面共享连接不可用，使用独立 app-server；桌面实时同步暂不可用。")
        try:
            self.process = subprocess.Popen(
                args,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                    if os.name == "nt"
                    else 0
                ),
            )
        except OSError as error:
            raise AppServerError(f"无法启动 Codex app-server：{error}") from error
        threading.Thread(target=self._read_stdout, name="codex-app-server", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="codex-app-server-errors", daemon=True).start()
        self.request("initialize", {
            "clientInfo": {
                # The desktop hydrates persisted tasks by client identity.
                # Keep the public title as CodexBridge while using the
                # interactive desktop protocol identity for rollout metadata.
                "name": "Codex Desktop",
                "title": "CodexBridge",
                "version": VERSION,
            },
            # thread/settings/update is gated by the app-server's experimental
            # capability. The phone's approval-mode control cannot work
            # without this opt-in, even though the method itself is present.
            "capabilities": {"experimentalApi": True},
        })
        self.notify("initialized", {})

    def connection_status(self) -> dict[str, Any]:
        return {
            "desktop_realtime_shared": bool(self.shared),
            "desktop_sync_mode": "shared" if self.shared else "polling",
            "transport": self.transport,
            "reason": self.transport_reason,
        }

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stdout = self.process.stdout
        while True:
            line = stdout.readline(self.MAX_STDOUT_LINE_CHARS + 1)
            if not line:
                break
            if not line.endswith("\n") and len(line) > self.MAX_STDOUT_LINE_CHARS:
                # Drain the remainder of the oversized line so the next
                # readline() resumes on a real message boundary, but never
                # hand the payload to json.loads or keep it alive.
                self.oversized_stdout_lines += 1
                while line and not line.endswith("\n"):
                    line = stdout.readline(self._STDOUT_DISCARD_CHARS)
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            method = message.get("method")
            if isinstance(response_id, int) and isinstance(method, str):
                try:
                    self.server_requests.put_nowait(message)
                except queue.Full:
                    pass
            elif isinstance(response_id, int):
                with self._pending_lock:
                    target = self._pending.get(response_id)
                if target is not None:
                    target.put(message)
            elif isinstance(method, str):
                self.notifications.put(message)
        self._closed.set()

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            cleaned = line.strip()
            if cleaned:
                emit(f"[App Server] {cleaned}")

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise AppServerError("Codex app-server 已退出")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    @staticmethod
    def _read_cache_key(method: str, params: dict[str, Any] | None) -> str:
        return method + "|" + json.dumps(
            params or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _cached_read(self, key: str) -> tuple[bool, Any]:
        now = time.monotonic()
        with self._read_cache_lock:
            entry = self._read_cache.get(key)
            if entry is None or now >= entry[0]:
                if entry is not None:
                    self._read_cache.pop(key, None)
                return False, None
            return True, entry[1]

    def _clear_read_cache(self) -> None:
        with self._read_cache_lock:
            self._read_cache.clear()

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        cacheable = method in self.READ_CACHE_METHODS
        cache_key = self._read_cache_key(method, params) if cacheable else ""
        if cacheable:
            hit, value = self._cached_read(cache_key)
            if hit:
                return value
        with self._request_lock:
            if cacheable:
                hit, value = self._cached_read(cache_key)
                if hit:
                    return value
            request_id = self._next_id
            self._next_id += 1
            target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._pending[request_id] = target
            try:
                message: dict[str, Any] = {"method": method, "id": request_id}
                if params is not None:
                    message["params"] = params
                self._send(message)
                try:
                    response = target.get(timeout=self.timeout)
                except queue.Empty as error:
                    raise AppServerError(f"App Server 请求超时：{method}") from error
                if "error" in response:
                    detail = response.get("error")
                    if isinstance(detail, dict):
                        detail = detail.get("message", detail)
                    raise AppServerProtocolError(
                        f"App Server 请求失败 {method}：{detail}")
                result = response.get("result")
                if cacheable:
                    with self._read_cache_lock:
                        self._read_cache[cache_key] = (
                            time.monotonic() + self.READ_CACHE_SECONDS,
                            result,
                        )
                elif method not in {"initialize"}:
                    self._clear_read_cache()
                return result
            finally:
                with self._pending_lock:
                    self._pending.pop(request_id, None)

    def respond(self, request_id: int, result: Any) -> None:
        """Answer a server-initiated request such as an approval prompt."""
        self._send({"id": request_id, "result": result})
        self._clear_read_cache()

    def close(self) -> None:
        self._clear_read_cache()
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def _rate_windows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    buckets: list[dict[str, Any]] = []
    # The top-level bucket is the active Codex limit. Responses can also
    # contain a base_model_inference weekly placeholder at 0% used; keeping
    # Codex first prevents that equal-duration window from appearing as a
    # transient 100% remaining value.
    single = result.get("rateLimits")
    if isinstance(single, dict):
        buckets.append(single)
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex = by_id.get("codex")
        if isinstance(codex, dict):
            buckets.append(codex)
        buckets.extend(
            value for key, value in by_id.items()
            if key != "codex" and isinstance(value, dict)
        )
    windows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for bucket in buckets:
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            duration = int(_as_number(window.get("windowDurationMins")) or 0)
            reset = int(_as_number(window.get("resetsAt")) or 0)
            marker = (duration, reset)
            if marker not in seen:
                seen.add(marker)
                windows.append(window)
    return windows


def account_snapshot(rate_result: Any, usage_result: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    windows = _rate_windows(rate_result)
    five_hour_candidates = [
        window for window in windows
        if int(_as_number(window.get("windowDurationMins")) or 0) == 5 * 60
    ]
    five_hour = five_hour_candidates[0] if five_hour_candidates else None
    weekly_candidates = [
        window for window in windows
        if (_as_number(window.get("windowDurationMins")) or 0) >= 6 * 24 * 60
    ]
    weekly = max(weekly_candidates, key=lambda item: _as_number(item.get("windowDurationMins")) or 0) \
        if weekly_candidates else None
    account: dict[str, Any] = {
        "five_hour_available": False,
        "five_hour_remaining_percent": 0,
        "five_hour_reset_date": "",
        "five_hour_reset_at_epoch": 0,
        "weekly_available": False,
        "weekly_remaining_percent": 0,
        "weekly_used_percent": 0,
        "reset_date": "",
        "weekly_reset_at_epoch": 0,
        "weekly_tokens_available": False,
        "weekly_tokens": 0,
        "weekly_tokens_as_of": "",
        "plan_type": "",
        "rate_limit_reached": "",
    }
    if five_hour is not None:
        used = _as_number(five_hour.get("usedPercent"))
        reset = _epoch(five_hour.get("resetsAt"))
        if used is not None:
            used = max(0, min(100, used))
            account["five_hour_available"] = True
            account["five_hour_remaining_percent"] = round(100 - used)
        if reset:
            account["five_hour_reset_at_epoch"] = reset
            account["five_hour_reset_date"] = datetime.fromtimestamp(reset).astimezone().strftime("%m-%d %H:%M")
    if weekly is not None:
        used = _as_number(weekly.get("usedPercent"))
        reset = _epoch(weekly.get("resetsAt"))
        if used is not None:
            used = max(0, min(100, used))
            account["weekly_available"] = True
            account["weekly_used_percent"] = round(used)
            account["weekly_remaining_percent"] = round(100 - used)
        if reset:
            account["weekly_reset_at_epoch"] = reset
            account["reset_date"] = datetime.fromtimestamp(reset).astimezone().strftime("%m-%d %H:%M")

    if isinstance(rate_result, dict):
        rate = rate_result.get("rateLimits")
        if isinstance(rate, dict):
            account["plan_type"] = _safe_line(str(rate.get("planType") or ""), 31)
            account["rate_limit_reached"] = _safe_line(str(rate.get("rateLimitReachedType") or ""), 31)

    buckets = usage_result.get("dailyUsageBuckets") if isinstance(usage_result, dict) else None
    if isinstance(buckets, list):
        daily_tokens: dict[date, int] = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            try:
                bucket_date = date.fromisoformat(str(bucket.get("startDate") or "")[:10])
            except ValueError:
                continue
            tokens = _as_number(bucket.get("tokens"))
            if tokens is not None and tokens >= 0:
                daily_tokens[bucket_date] = daily_tokens.get(bucket_date, 0) + int(tokens)
        if daily_tokens:
            latest_date = max(daily_tokens)
            period_start = latest_date - timedelta(days=6)
            account["weekly_tokens_available"] = True
            account["weekly_tokens"] = sum(
                tokens for bucket_date, tokens in daily_tokens.items()
                if period_start <= bucket_date <= latest_date
            )
            account["weekly_tokens_as_of"] = latest_date.strftime("%m-%d")
    return account


def _turn_usage(turn: dict[str, Any]) -> int:
    usage = turn.get("usage") or turn.get("tokenUsage")
    if not isinstance(usage, dict):
        return 0
    for key in ("totalTokens", "total_tokens"):
        value = _as_number(usage.get(key))
        if value is not None and value >= 0:
            return int(value)
    total = 0
    for key in ("inputTokens", "input_tokens", "outputTokens", "output_tokens"):
        value = _as_number(usage.get(key))
        if value is not None and value >= 0:
            total += int(value)
    return total


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def resolve_terminal_state(
    status: str,
    *,
    completed_at: int = 0,
    fallback_timestamps: "tuple[int, ...] | list[int]" = (),
    now_epoch: int | None = None,
) -> tuple[str, int]:
    """Normalize a turn verdict into a status that always carries a real end time.

    Two rules matter for every consumer (thread list, thread detail, status
    events and read state):

    1. ``completed`` / ``failed`` are terminal, so they must carry a positive
       ``completed_at``. When the upstream turn omits ``completedAt`` we fall
       back to the last known activity, then to the local observation time.
    2. A ``failed`` / ``interrupted`` verdict without any end timestamp is a
       transient upstream artefact (provider reconnect, rate-limit retry,
       stream reset), not a finished task. Report it as ``working`` so the
       phone keeps showing progress instead of a phantom failure.
    """

    value = str(status or "").replace("_", "").lower()
    if value not in {"completed", "failed", "interrupted", "cancelled", "canceled"}:
        return value, max(0, int(completed_at or 0))
    stamp = max(0, int(completed_at or 0))
    if value != "completed":
        if stamp <= 0:
            # No end time. A turn that is still moving is a reconnect
            # artefact; one that has been quiet for a long time is a turn that
            # ended without a timestamp, so settle it at its last activity.
            fresh = 0
            for candidate in fallback_timestamps:
                try:
                    number = max(0, int(candidate or 0))
                except (TypeError, ValueError):
                    number = 0
                if number > fresh:
                    fresh = number
            observed = int(now_epoch if now_epoch is not None else time.time())
            if fresh <= 0:
                # No evidence at all that the turn ended. Keep it running
                # rather than inventing a terminal failure.
                return "working", 0
            if observed - fresh < STALE_TURN_WINDOW_SECONDS:
                return "working", 0
            return "failed", fresh
        return "failed", stamp
    if stamp <= 0:
        for candidate in fallback_timestamps:
            try:
                number = max(0, int(candidate or 0))
            except (TypeError, ValueError):
                number = 0
            if number > stamp:
                stamp = number
    if stamp <= 0:
        stamp = int(now_epoch if now_epoch is not None else time.time())
    return "completed", stamp


def _turn_error(turn: dict[str, Any]) -> str:
    """Return a readable message for a failed turn, if the server reported one."""
    error = turn.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("error") or ""
    elif isinstance(error, str):
        message = error
    else:
        message = ""
    return _safe_line(_flatten_provider_error(str(message)), 400)


def _flatten_provider_error(text: str) -> str:
    """Collapse nested provider error JSON into one readable sentence.

    A custom model provider can wrap its failure several layers deep, for
    example ``{"error": {"message": "<json string>"}}``. Unwrap those layers so
    the phone shows the upstream reason instead of raw escaped JSON.
    """
    raw = text.strip()
    if not raw:
        return ""
    payload = _try_json(raw)
    for _ in range(3):
        if not isinstance(payload, dict):
            break
        inner = payload.get("error") if "error" in payload else payload.get("message")
        if isinstance(inner, str):
            parsed = _try_json(inner)
            if parsed is None:
                raw = inner.strip() or raw
                payload = None
                break
            payload = parsed
            continue
        if isinstance(inner, dict):
            payload = inner
            continue
        break
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg")
        if isinstance(message, str) and message.strip():
            raw = message.strip()
    # The provider may still append its own JSON body after a plain sentence.
    head, _, tail = raw.partition("{")
    if tail:
        embedded = _try_json("{" + tail)
        if isinstance(embedded, dict):
            detail = embedded.get("msg") or embedded.get("message")
            if isinstance(detail, str) and detail.strip():
                return (head + detail.strip()).strip()
    return raw


def _plan_counts(turn: dict[str, Any], items: list[dict[str, Any]]) -> tuple[int, int]:
    candidates: list[Any] = [turn.get("plan")]
    candidates.extend(item.get("plan") for item in items if item.get("type") == "plan")
    for candidate in reversed(candidates):
        if not isinstance(candidate, list):
            continue
        entries = [entry for entry in candidate if isinstance(entry, dict)]
        if entries:
            completed = sum(1 for entry in entries if str(entry.get("status", "")).lower() == "completed")
            return completed, len(entries)
    return 0, 0


def thread_snapshot(thread: Any, now_epoch: int | None = None) -> dict[str, Any]:
    now_epoch = now_epoch or int(time.time())
    if not isinstance(thread, dict):
        return {
            "status": "idle", "thread_id": "", "turn_id": "", "title": "暂无任务",
            "last_user_message": "", "last_assistant_message": "", "reasoning_summary": "",
            "conversation_mode": "empty", "codex_messages": [],
            "current_action": "等待新任务", "duration_seconds": 0,
            "user_message_count": 0, "message_count": 0,
            "token_count": 0, "token_count_available": False,
            "plan_completed": 0, "plan_total": 0,
            "started_at_epoch": 0, "last_updated_epoch": 0,
            "error_message": "",
        }

    turns = [turn for turn in thread.get("turns", []) if isinstance(turn, dict)]
    turns.sort(key=lambda turn: _epoch(
        turn.get("startedAt") or turn.get("createdAt") or
        turn.get("updatedAt") or turn.get("completedAt")
    ))
    latest_turn = turns[-1] if turns else {}
    latest_items = [item for item in latest_turn.get("items", []) if isinstance(item, dict)]
    items = latest_items
    latest_agent_messages: list[tuple[str, str]] = []
    user_message_count = 0
    token_count = 0
    for turn in turns:
        token_count += _turn_usage(turn)
        for item in turn.get("items", []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                user_message_count += 1
    # A desktop continuation can create a new active turn without repeating the
    # userMessage item.  Keep current status/progress tied to the latest turn,
    # while resolving the latest visible user request across the whole thread.
    user_message = ""
    for turn in reversed(turns):
        turn_items = [item for item in turn.get("items", []) if isinstance(item, dict)]
        for item in reversed(turn_items):
            if item.get("type") != "userMessage":
                continue
            value = _user_message_text(item.get("content"))
            if value:
                user_message = value
                break
        if user_message:
            break

    assistant_message = ""
    for item in items:
        item_type = item.get("type")
        if item_type == "agentMessage":
            value = _text(item.get("text") or item.get("content"))
            if value:
                phase = str(item.get("phase") or "").replace("_", "").lower()
                latest_agent_messages.append((phase, value))

    runtime = thread.get("status") if isinstance(thread.get("status"), dict) else {}
    active_flags = {str(flag).lower() for flag in runtime.get("activeFlags", []) if isinstance(flag, str)}
    turn_status = str(latest_turn.get("status", "")).replace("_", "").lower()
    last_final_index = -1
    last_visible_activity_index = -1
    for index, item in enumerate(latest_items):
        item_type = str(item.get("type") or "")
        if item_type == "userMessage":
            last_visible_activity_index = index
        elif item_type == "agentMessage":
            phase = str(item.get("phase") or "").replace("_", "").lower()
            if phase in {"final", "finalanswer"}:
                last_final_index = index
            elif phase == "commentary":
                last_visible_activity_index = index
        elif str(item.get("status") or "").replace("_", "").lower() in {
                "inprogress", "running", "pending"}:
            last_visible_activity_index = index
    # Since Codex CLI 0.147, reading a desktop-owned thread from a second
    # app-server can repair ``updatedAt`` to the current time.  ``recencyAt``
    # remains the reliable activity timestamp for that detached view.
    recency = _epoch(thread.get("recencyAt"))
    updated = recency or _epoch(thread.get("updatedAt") or latest_turn.get("updatedAt") or
                                latest_turn.get("completedAt") or latest_turn.get("startedAt"))
    recently_updating = updated > 0 and 0 <= now_epoch - updated < RESUMED_ACTIVE_WINDOW_SECONDS
    detached_recent = recency > 0 and 0 <= now_epoch - recency < DETACHED_ACTIVE_WINDOW_SECONDS
    completed_at = _epoch(latest_turn.get("completedAt"))
    resumed_visible_activity = (
        completed_at > 0 and
        recency > completed_at + 5 and
        last_visible_activity_index >= 0 and
        last_visible_activity_index > last_final_index
    )
    detached_active = (
        turn_status == "interrupted" and
        (not latest_items or resumed_visible_activity) and
        detached_recent and
        str(runtime.get("type", "")).lower() == "notloaded" and
        str(thread.get("source", "")).lower() in {"vscode", "appserver"}
    )
    fallback_stamps = (
        _epoch(latest_turn.get("updatedAt")),
        updated,
        recency,
        _epoch(latest_turn.get("startedAt")),
    )
    if any("waiting" in flag or "approval" in flag for flag in active_flags):
        status = "waiting_user"
    elif turn_status in {"inprogress", "running", "active"}:
        status = "working"
    elif turn_status in {"queued", "starting"}:
        status = turn_status
    elif turn_status == "interrupted" and (
            (latest_turn.get("completedAt") is None and recently_updating) or detached_active):
        # A second read-only app-server sees a desktop-owned in-flight turn as
        # interrupted. Newer versions may also expose an empty placeholder
        # turn with completedAt populated while recencyAt continues to move.
        status = "working"
    elif turn_status in {"completed", "failed", "interrupted", "cancelled", "canceled"}:
        status, completed_at = resolve_terminal_state(
            turn_status,
            completed_at=completed_at,
            fallback_timestamps=fallback_stamps,
            now_epoch=now_epoch,
        )
    elif str(runtime.get("type", "")).lower() == "active":
        status = "working"
    else:
        # No turn reported a terminal verdict. Do not invent "completed":
        # a thread we merely cannot observe is idle, not finished.
        status = "idle"

    progress_messages = [text for phase, text in latest_agent_messages if phase == "commentary"]
    final_messages = [text for phase, text in latest_agent_messages if phase in {"final", "finalanswer"}]
    if status in {"working", "waiting_user"}:
        # Match the Codex app: while a turn is active, expose only the visible
        # commentary stream. A final-answer item is not mixed into progress.
        display_messages = progress_messages
        conversation_mode = "progress"
    elif final_messages:
        # A completed turn displays one final answer and hides all commentary.
        display_messages = [final_messages[-1]]
        conversation_mode = "final"
    elif latest_agent_messages:
        # Older app-server payloads may omit phase. Use only the last visible
        # agent message as the terminal fallback; never replay the whole turn.
        display_messages = [latest_agent_messages[-1][1]]
        conversation_mode = "final"
    else:
        display_messages = []
        conversation_mode = "empty"

    safe_messages = [safe for value in display_messages if (safe := _safe_message(value))]
    if safe_messages:
        assistant_message = safe_messages[-1]

    current_action = (
        progress_messages[-1]
        if status in {"working", "waiting_user"} and progress_messages else ""
    )
    if status == "waiting_user":
        current_action = "等待用户确认"
    elif not current_action:
        for item in reversed(latest_items):
            item_type = item.get("type")
            item_status = str(item.get("status", "")).replace("_", "").lower()
            if item_status not in {"inprogress", "running", "pending"}:
                continue
            current_action = {
                "commandExecution": "正在运行命令",
                "fileChange": "正在修改代码",
                "mcpToolCall": "正在调用工具",
                "dynamicToolCall": "正在调用工具",
                "webSearch": "正在搜索资料",
                "collabToolCall": "正在协同处理任务",
            }.get(str(item_type), "正在处理任务")
            break
    if not current_action:
        current_action = (
            "正在处理任务" if status == "working" else
            "任务已完成" if status == "completed" else
            "等待新任务"
        )

    started = _epoch(latest_turn.get("startedAt") or latest_turn.get("createdAt"))
    # Keep the normalized end time: a terminal verdict without completedAt was
    # repaired above from the last activity or local observation time.
    completed = 0 if status in {"working", "waiting_user", "idle"} else int(completed_at or 0)
    duration = max(0, (completed or now_epoch) - started) if started else 0
    plan_completed, plan_total = _plan_counts(latest_turn, latest_items)
    return {
        "status": status,
        "thread_id": _safe_line(str(thread.get("id") or ""), 63),
        "turn_id": _safe_line(str(latest_turn.get("id") or ""), 63),
        "title": _safe_line(str(thread.get("name") or thread.get("preview") or "暂无任务"), 95),
        "last_user_message": _safe_message(user_message),
        "last_assistant_message": _safe_line(assistant_message, 255),
        "reasoning_summary": "",
        "conversation_mode": conversation_mode,
        "codex_messages": safe_messages,
        "current_action": _safe_line(current_action, 127),
        "duration_seconds": duration,
        "user_message_count": user_message_count,
        # Retain the schema-v1 field for older firmware and web clients.
        "message_count": user_message_count,
        "token_count": token_count,
        "token_count_available": token_count > 0,
        "plan_completed": plan_completed,
        "plan_total": plan_total,
        "started_at_epoch": started,
        "last_updated_epoch": updated,
        "completed_at_epoch": completed,
        "error_message": _turn_error(latest_turn),
    }


class CodexAppServerSource:
    """Maintain a display-safe snapshot using only documented app-server calls."""

    # One app-server list page contains at most 20 threads. Inspect the whole
    # page so every in-flight thread contributes to the running count.
    MAX_DISPLAY_TASKS = 20
    # ``thread/read`` with ``includeTurns=true`` returns the whole stored thread
    # in one JSON line (measured 84.7 MB / ~2 s of GIL-holding json.loads() on a
    # 70-turn desktop thread). The collector instead assembles a bounded view
    # from the paged read methods below.
    TURNS_PAGE_LIMIT = 100
    ITEM_PAGE_LIMIT = 200
    MAX_ITEM_PAGES = 2
    SOURCE_KINDS = [
        "cli", "vscode", "exec", "appServer", "unknown",
    ]
    INTERNAL_THREAD_PREFIXES = (
        "The following is the Codex agent history",
    )

    def __init__(
        self,
        runtime_folder: Path,
        cwd: Path,
        codex_command: str | None = None,
        activity: RolloutActivity | None = None,
        client: AppServerClient | None = None,
        owns_client: bool = True,
    ) -> None:
        self.runtime_folder = runtime_folder
        self.cwd = cwd
        # Same rollout-log liveness the phone API uses. The desktop owns its
        # threads, so "status.type" stays "notLoaded" and "recencyAt" is frozen
        # at the moment a turn started. Without the log, a long-running task
        # looks finished to a device no matter how much work it is doing.
        self.activity = activity or RolloutActivity()
        client_command = getattr(client, "command", []) if client is not None else None
        self.command = (
            list(client_command)
            if isinstance(client_command, (list, tuple))
            else (_resolve_codex_command(codex_command, runtime_folder) if client is None else [])
        )
        self.client = client or AppServerClient(self.command, cwd)
        self._owns_client = bool(owns_client)
        self.account: dict[str, Any] = account_snapshot({}, {})
        self.task: dict[str, Any] = thread_snapshot(None)
        self.tasks: list[dict[str, Any]] = [self.task]
        self.result: dict[str, Any] = thread_snapshot(None)
        self.active_thread_ids: set[str] = set()
        self.last_account_refresh = 0.0
        self._account_refresh_failures = 0
        self._thread_refresh_failures = 0
        self._has_account_snapshot = False
        self._chatgpt_account = False
        self._has_thread_snapshot = False
        self._unreadable_thread_ids: set[str] = set()
        self._thread_detail_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}

    def start(self) -> None:
        self.client.start()
        # Task and thread state work under any account type (including an
        # API-key login). Only quota and usage need a ChatGPT session, so a
        # non-ChatGPT account degrades to task-only instead of aborting the
        # whole collector.
        self._chatgpt_account = self._probe_chatgpt_account()
        if self._chatgpt_account:
            self.refresh_account(force=True)
            self._has_account_snapshot = True
        else:
            self.account = account_snapshot({}, {})

    def _probe_chatgpt_account(self) -> bool:
        account = self.client.request("account/read", {"refreshToken": False})
        account_info = account.get("account") if isinstance(account, dict) else None
        return isinstance(account_info, dict) and account_info.get("type") == "chatgpt"

    def refresh_account(self, force: bool = False) -> None:
        # Quota reads require a ChatGPT session; skip silently otherwise.
        if not self._chatgpt_account:
            return
        if not force and time.monotonic() - self.last_account_refresh < 60:
            return
        rate = self.client.request("account/rateLimits/read")
        usage = self.client.request("account/usage/read")
        self.account = account_snapshot(rate, usage)
        self.last_account_refresh = time.monotonic()

    @staticmethod
    def _sorted_turns(result: Any) -> list[dict[str, Any]]:
        """Normalize a turns listing into oldest-first order.

        ``thread/turns/list`` answers newest-first, while every consumer in
        this module expects the stored order.
        """
        data = result.get("data") if isinstance(result, dict) else None
        turns = [turn for turn in data if isinstance(turn, dict)] if isinstance(data, list) else []
        turns.sort(key=lambda turn: (
            int(_epoch(turn.get("startedAt") or turn.get("createdAt") or
                       turn.get("updatedAt") or turn.get("completedAt"))),
            str(turn.get("id") or ""),
        ))
        return turns

    @staticmethod
    def _unwrap_items(page: Any) -> list[dict[str, Any]]:
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(data, list):
            return []
        items: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if item is None and "type" in entry:
                item = entry
            if isinstance(item, dict):
                items.append(item)
        return items

    def _turn_item_tail(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        """Read the newest items of one turn without materializing the turn.

        ``thread/items/list`` paginates a single turn; requesting
        ``sortDirection=desc`` starts at the newest item, which is the part the
        phone needs for progress and the final answer.
        """
        if not turn_id:
            return []
        items: list[dict[str, Any]] = []
        cursor: Any = None
        for _ in range(self.MAX_ITEM_PAGES):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "turnId": turn_id,
                "limit": self.ITEM_PAGE_LIMIT,
                "itemsView": "full",
                "sortDirection": "desc",
            }
            if cursor:
                params["cursor"] = cursor
            page = self.client.request("thread/items/list", params)
            entries = self._unwrap_items(page)
            if not entries:
                break
            items.extend(entries)
            cursor = page.get("nextCursor") if isinstance(page, dict) else None
            if not cursor or len(entries) < self.ITEM_PAGE_LIMIT:
                break
        # Pages arrive newest-first, so one reverse restores chronological order.
        items.reverse()
        return items

    def _turn_item_head(self, thread_id: str, turn_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Read the first items of a turn (user request and any plan)."""
        if not turn_id:
            return []
        page = self.client.request("thread/items/list", {
            "threadId": thread_id,
            "turnId": turn_id,
            "limit": limit,
            "itemsView": "full",
            "sortDirection": "asc",
        })
        return self._unwrap_items(page)

    def _read_thread_detail(self, thread_id: str) -> dict[str, Any]:
        """Assemble a display thread from bounded app-server reads.

        The previous implementation used ``thread/read`` with
        ``includeTurns=true``. That call serializes every stored item of a
        thread into one JSON line, and on a long desktop thread the resulting
        multi-megabyte ``json.loads`` held the GIL long enough to freeze the
        whole bridge (observed as system-wide pointer lag). This method never
        asks the app-server for a full thread.
        """
        metadata = self.client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = metadata.get("thread") if isinstance(metadata, dict) else None
        if not isinstance(thread, dict):
            return metadata if isinstance(metadata, dict) else {}
        thread = dict(thread)
        turns = self._sorted_turns(self.client.request("thread/turns/list", {
            "threadId": thread_id,
            "limit": self.TURNS_PAGE_LIMIT,
            "itemsView": "summary",
        }))
        thread["turns"] = turns
        if not turns:
            return {"thread": thread}
        latest = turns[-1]
        turn_id = str(latest.get("id") or "")
        turn_items = self._turn_item_tail(thread_id, turn_id)
        # A plan lives at the start of its turn and a long turn can push it out
        # of the tail window, so pull the head too and merge by id.
        head = self._turn_item_head(thread_id, turn_id)
        known = {str(item.get("id")) for item in turn_items if item.get("id")}
        prefix = [item for item in head
                  if not item.get("id") or str(item.get("id")) not in known]
        latest["items"] = prefix + turn_items
        return {"thread": thread}

    def refresh_thread(self) -> None:
        result = self.client.request("thread/list", {
            "limit": 20,
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": self.SOURCE_KINDS,
            "archived": False,
        })
        threads = result.get("data") if isinstance(result, dict) else None
        if not isinstance(threads, list) or not threads:
            self.task = thread_snapshot(None)
            self.tasks = [self.task]
            self.active_thread_ids.clear()
            return

        display_threads = [item for item in threads if self._is_display_thread(item)]
        # Liveness first: a rollout whose log is still growing is running even
        # when the listing buries it far down the recency order.
        liveness: dict[str, str] = {}
        for listed in display_threads:
            if not isinstance(listed, dict):
                continue
            listed_id = str(listed.get("id") or "")
            if not listed_id:
                continue
            state = status_from_activity(self.activity.activity(listed))
            if state:
                liveness[listed_id] = state
        unreadable_ids = getattr(self, "_unreadable_thread_ids", set())
        display_thread_ids = {
            str(item.get("id") or "") for item in display_threads if isinstance(item, dict)
        }
        unreadable_ids.intersection_update(display_thread_ids)
        detail_cache = getattr(self, "_thread_detail_cache", None)
        if detail_cache is None:
            detail_cache = {}
            self._thread_detail_cache = detail_cache
        for cached_id in set(detail_cache) - display_thread_ids:
            detail_cache.pop(cached_id, None)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for selected in display_threads:
            thread_id = str(selected.get("id") or "")
            if not thread_id or thread_id in seen or thread_id in unreadable_ids:
                continue
            if (self._is_active_hint(selected) or thread_id in self.active_thread_ids or
                    liveness.get(thread_id) == "working" or
                    len(candidates) < self.MAX_DISPLAY_TASKS):
                candidates.append(selected)
                seen.add(thread_id)

        active: list[dict[str, Any]] = []
        terminal: list[dict[str, Any]] = []
        fallback: dict[str, Any] | None = None
        unreadable = 0
        for selected in candidates:
            thread_id = str(selected.get("id") or "")
            cache_key = self._thread_detail_cache_key(selected)
            cached = detail_cache.get(thread_id)
            refresh_detail = (
                cached is None
                or cached[0] != cache_key
                or self._is_active_hint(selected)
                or thread_id in self.active_thread_ids
                or liveness.get(thread_id) == "working"
            )
            if not refresh_detail:
                detail = cached[1]
            else:
                try:
                    detail = self._read_thread_detail(thread_id)
                    detail_cache[thread_id] = (cache_key, detail)
                except (AppServerError, OSError, ValueError) as error:
                    # One stored thread may contain an item type unknown to this
                    # App Server build. Keep the other readable tasks flowing.
                    unreadable += 1
                    if thread_id not in unreadable_ids:
                        emit(f"Codex 任务读取失败，已跳过该任务：{error}")
                    unreadable_ids.add(thread_id)
                    continue
            thread = detail.get("thread") if isinstance(detail, dict) else None
            if not self._is_display_thread(thread):
                continue
            snapshot = thread_snapshot(thread)
            listed_id = str(selected.get("id") or thread.get("id") or "")
            if liveness.get(listed_id, "") == "working" and snapshot["status"] not in {
                    *IN_FLIGHT_STATUSES, "completed"}:
                # The desktop is still appending turn records to this rollout.
                # A second app-server cannot see that and reports the live turn
                # as interrupted, so the log outranks the detached reading.
                snapshot["status"] = "working"
                snapshot["conversation_mode"] = "progress"
                idle_action = "等待新任务"
                if snapshot.get("current_action") in ("", idle_action):
                    snapshot["current_action"] = "正在处理任务"
            if fallback is None:
                fallback = snapshot
            if snapshot["status"] in IN_FLIGHT_STATUSES:
                active.append(snapshot)
            elif snapshot["status"] in {"completed", "failed"}:
                terminal.append(snapshot)

        if terminal:
            terminal.sort(key=lambda item: (
                int(item.get("completed_at_epoch") or 0),
                int(item.get("last_updated_epoch") or 0),
                str(item.get("thread_id") or ""),
            ))
            self.result = terminal[-1]

        if active:
            active.sort(key=lambda item: (
                int(item.get("started_at_epoch") or 0),
                str(item.get("thread_id") or ""),
            ))
            active = active[:self.MAX_DISPLAY_TASKS]
            self.tasks = active
            self.task = active[0]
            self.active_thread_ids = {str(item["thread_id"]) for item in active}
            return

        self._unreadable_thread_ids = unreadable_ids
        if fallback is not None:
            self.active_thread_ids.clear()
            self.task = fallback
            self.tasks = [self.task]
        elif unreadable:
            # Do not erase the last valid task view when every selected
            # historical thread is temporarily unreadable.
            emit("Codex 没有可读取的任务，保留上一次成功的任务快照")

    @staticmethod
    def _thread_detail_cache_key(item: dict[str, Any]) -> tuple:
        runtime = item.get("status") if isinstance(item.get("status"), dict) else {}
        turns = [turn for turn in item.get("turns", []) if isinstance(turn, dict)]
        latest = turns[-1] if turns else {}
        return (
            int(_epoch(item.get("updatedAt") or item.get("recencyAt"))),
            int(_epoch(item.get("recencyAt"))),
            str(runtime.get("type") or ""),
            tuple(str(flag) for flag in runtime.get("activeFlags", []) if isinstance(flag, str)),
            str(latest.get("id") or ""),
            str(latest.get("status") or ""),
            int(_epoch(latest.get("completedAt"))),
        )

    @staticmethod
    def _is_active_hint(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        runtime = item.get("status") if isinstance(item.get("status"), dict) else {}
        if str(runtime.get("type", "")).lower() == "active":
            return True
        active_flags = {str(flag).lower() for flag in runtime.get("activeFlags", []) if isinstance(flag, str)}
        if any("waiting" in flag or "approval" in flag for flag in active_flags):
            return True
        recency = _epoch(item.get("recencyAt"))
        if (str(runtime.get("type", "")).lower() == "notloaded" and recency > 0 and
                0 <= int(time.time()) - recency < RESUMED_ACTIVE_WINDOW_SECONDS):
            return True
        turns = [turn for turn in item.get("turns", []) if isinstance(turn, dict)]
        if not turns:
            return False
        latest = turns[-1]
        status = str(latest.get("status", "")).replace("_", "").lower()
        if status in {"inprogress", "running", "active"}:
            return True
        if status == "interrupted" and latest.get("completedAt") is None:
            updated = _epoch(item.get("updatedAt") or latest.get("updatedAt") or latest.get("startedAt"))
            return updated > 0 and int(time.time()) - updated < RESUMED_ACTIVE_WINDOW_SECONDS
        return False

    @classmethod
    def _is_display_thread(cls, item: Any) -> bool:
        if not isinstance(item, dict) or not item.get("id"):
            return False
        candidates: list[str] = []
        for key in ("name", "preview"):
            value = item.get(key)
            if isinstance(value, str):
                candidates.append(value)
        for turn in item.get("turns", []):
            if not isinstance(turn, dict):
                continue
            for entry in turn.get("items", []):
                if isinstance(entry, dict) and entry.get("type") == "userMessage":
                    value = _text(entry.get("content"))
                    if value:
                        candidates.append(value)
        return not any(
            text.strip().startswith(prefix)
            for text in candidates
            for prefix in cls.INTERNAL_THREAD_PREFIXES
        )

    def snapshot(self) -> dict[str, Any]:
        for label, refresh, failure_name, cache_name in (
            ("账户额度", self.refresh_account, "_account_refresh_failures", "_has_account_snapshot"),
            ("任务状态", self.refresh_thread, "_thread_refresh_failures", "_has_thread_snapshot"),
        ):
            try:
                refresh()
                setattr(self, failure_name, 0)
                setattr(self, cache_name, True)
            except (AppServerError, OSError, ValueError) as error:
                failures = int(getattr(self, failure_name, 0)) + 1
                setattr(self, failure_name, failures)
                if (not bool(getattr(self, cache_name, False)) or
                        failures >= TRANSIENT_REFRESH_FAILURE_LIMIT):
                    raise
                emit(f"Codex {label}暂时无法刷新，继续使用上次成功数据：{error}")
        return {
            "generated_at_epoch": int(time.time()),
            "account": self.account,
            "task": self.task,
            "tasks": self.tasks,
            "result": self.result,
        }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

