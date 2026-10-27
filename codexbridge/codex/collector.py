from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from codexbridge.messages_store import MessageRecord
from codexbridge.codex.app_server import AppServerError, CodexAppServerSource
from codexbridge.codex.quota import MAX_THREAD_BYTES, quota_from_snapshot


STATUS_FIELDS = (
    "status",
    "thread_id",
    "turn_id",
    "title",
    "current_action",
    "duration_seconds",
    "started_at_epoch",
    "last_updated_epoch",
    "completed_at_epoch",
    "token_count",
    "token_count_available",
    "plan_completed",
    "plan_total",
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _task_data(task: Any, *, include_result: bool = False) -> dict[str, Any]:
    source = _dict(task)
    result = {key: source[key] for key in STATUS_FIELDS if key in source}
    thread_id = str(source.get("thread_id") or "")
    if thread_id:
        result["message_id"] = f"task:{thread_id}"
    if include_result:
        result["last_user_message"] = str(source.get("last_user_message") or "")
        result["last_assistant_message"] = str(source.get("last_assistant_message") or "")
        messages = source.get("codex_messages")
        result["conversation_text"] = "\n\n".join(
            str(item) for item in messages if isinstance(item, str)
        ) if isinstance(messages, list) else ""
    return result


def records_from_snapshot(
    snapshot: dict[str, Any],
    now: float | None = None,
) -> list[MessageRecord]:
    del now  # Snapshot timestamps are authoritative and keep message IDs stable.
    if not isinstance(snapshot, dict):
        return []

    generated_at = float(snapshot.get("generated_at_epoch") or 0)
    account = _dict(snapshot.get("account"))
    primary = _task_data(snapshot.get("task"))
    tasks = snapshot.get("tasks")
    visible_tasks = [
        _task_data(task)
        for task in tasks
        if isinstance(task, dict) and str(task.get("thread_id") or "")
    ] if isinstance(tasks, list) else []
    primary_thread = str(primary.get("thread_id") or "")
    primary_status = str(primary.get("status") or "idle")
    records: list[MessageRecord] = []

    if primary_thread:
        records.append(
            MessageRecord(
                f"status:{primary_thread}:{primary_status}",
                primary_thread,
                "status",
                primary_status,
                str(primary.get("title") or "Codex 状态"),
                str(primary.get("current_action") or ""),
                generated_at,
                data={
                    "generated_at_epoch": generated_at,
                    "account": dict(account),
                    "task": primary,
                    # The desktop only answers for one thread at a time. This is
                    # that thread's line, and the one a client that has not
                    # chosen a conversation is shown instead.
                    "aggregate": True,
                },
            )
        )
        records.append(
            MessageRecord(
                f"task:{primary_thread}",
                primary_thread,
                "task",
                primary_status,
                str(primary.get("title") or "Codex 任务"),
                "\n".join(str(task.get("title") or "") for task in visible_tasks),
                generated_at,
                data={
                    "generated_at_epoch": generated_at,
                    "tasks": visible_tasks,
                    "aggregate": True,
                },
            )
        )

    # Every other running conversation gets the same two records, so a client
    # watching one of them can follow its own progress instead of whichever
    # thread the desktop happened to pick as primary.
    LIVE_TASK_STATUSES = {"working", "waiting_user", "queued", "starting"}
    for task in visible_tasks:
        task_thread = str(task.get("thread_id") or "")
        task_status = str(task.get("status") or "idle")
        if not task_thread or task_thread == primary_thread:
            continue
        if task_status not in LIVE_TASK_STATUSES:
            # A finished conversation is already reported through its result
            # record; only live ones need a line a client can follow.
            continue
        records.append(
            MessageRecord(
                f"status:{task_thread}:{task_status}",
                task_thread,
                "status",
                task_status,
                str(task.get("title") or "Codex 状态"),
                str(task.get("current_action") or ""),
                generated_at,
                data={
                    "generated_at_epoch": generated_at,
                    "task": task,
                    "aggregate": False,
                },
            )
        )
        records.append(
            MessageRecord(
                f"task:{task_thread}",
                task_thread,
                "task",
                task_status,
                str(task.get("title") or "Codex 任务"),
                str(task.get("title") or ""),
                generated_at,
                data={
                    "generated_at_epoch": generated_at,
                    "tasks": [task],
                    "aggregate": False,
                },
            )
        )

    result = _task_data(snapshot.get("result"), include_result=True)
    result_thread = str(result.get("thread_id") or "")
    result_status = str(result.get("status") or "")
    completed_at = float(result.get("completed_at_epoch") or 0)
    if result_thread and result_status in {
        "completed",
        "failed",
        "interrupted",
        "cancelled",
        "canceled",
    }:
        result_status = {
            "cancelled": "interrupted",
            "canceled": "interrupted",
        }.get(result_status, result_status)
        message_id = f"result:{result_thread}:{int(completed_at)}"
        result["message_id"] = message_id
        records.append(
            MessageRecord(
                message_id,
                result_thread,
                "result",
                result_status,
                str(result.get("title") or "Codex 结果"),
                str(result.get("last_assistant_message") or result.get("conversation_text") or ""),
                completed_at,
                data={
                    "generated_at_epoch": generated_at,
                    "task": result,
                },
            )
        )

    quota = quota_from_snapshot(snapshot)
    if quota:
        quota_thread = str(quota.get("thread_id") or primary_thread or "account")
        if len(quota_thread.encode("utf-8")) > MAX_THREAD_BYTES:
            quota_thread = quota_thread.encode("utf-8")[:MAX_THREAD_BYTES].decode(
                "utf-8", "ignore"
            )
        records.append(
            MessageRecord(
                f"codex_status:{quota_thread}",
                quota_thread,
                "codex_status",
                str(quota.get("task_status") or primary_status),
                "Codex 配额与状态",
                "",
                generated_at,
                data={"quota": quota, "generated_at_epoch": generated_at},
            )
        )
    return records


def run_codex_collector(
    runtime_folder: Path,
    cwd: Path,
    interval: float,
    stop: threading.Event,
    publish: Callable[[list[MessageRecord]], None],
    codex_command: str | None = None,
    report: Callable[[str, str], None] | None = None,
    restart: threading.Event | None = None,
    enabled: Callable[[], bool] | None = None,
    source_factory: Callable[[], Any] | None = None,
    on_source: Callable[[Any | None], None] | None = None,
    interval_for: Callable[[], float] | None = None,
    demand: Callable[[], bool] | None = None,
) -> None:
    """Poll the Codex snapshot until ``stop`` is set.

    ``interval_for`` returns the cadence for the next cycle. It is used to slow
    the loop down while nobody is watching (no device online, no phone stream
    open): an unattended bridge still collects, just far less often, so data is
    never lost -- only the beat changes. ``demand`` is consulted during that
    slow sleep so the first client to appear triggers an immediate refresh
    instead of waiting out the whole idle interval.
    """

    def current_interval() -> float:
        if interval_for is not None:
            try:
                value = float(interval_for())
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return float(interval)

    def demand_present() -> bool:
        if demand is None:
            return False
        try:
            return bool(demand())
        except (OSError, RuntimeError, ValueError):
            return False

    backoff = 2.0
    while not stop.is_set():
        if enabled is not None and not enabled():
            # The caller asked for a hard stop (shutdown/restart), not a
            # throttle: wait without collecting until it is switched back on.
            if report is not None:
                report("disabled", "Codex 采集已暂停")
            while not stop.is_set() and not (restart is not None and restart.is_set()):
                stop.wait(0.25)
            if restart is not None:
                restart.clear()
            backoff = 2.0
            continue

        source: Any = None
        try:
            source = (
                source_factory()
                if source_factory is not None
                else CodexAppServerSource(runtime_folder, cwd, codex_command)
            )
            source.start()
            if on_source is not None:
                on_source(source)
            if report is not None:
                report("online", "Codex App Server 已连接")
            backoff = 2.0
            while (
                not stop.is_set()
                and not (restart is not None and restart.is_set())
                and (enabled is None or enabled())
            ):
                records = records_from_snapshot(source.snapshot())
                if records:
                    publish(records)
                if report is not None:
                    report("online", "数据采集正常")
                cadence = max(1.0, current_interval())
                # "Slow" is only meaningful relative to the normal cadence: when
                # the two are equal we are in the original always-on mode and
                # must not break out of the sleep early.
                slow_mode = cadence > max(1.0, float(interval))
                deadline = time.monotonic() + cadence
                while (
                    not stop.is_set()
                    and not (restart is not None and restart.is_set())
                    and (enabled is None or enabled())
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    stop.wait(min(0.25, remaining))
                    if slow_mode and demand_present():
                        # A device or phone just showed up. Refresh now so its
                        # first request is fresh instead of up to one slow
                        # interval old.
                        break
        except Exception as error:  # noqa: BLE001
            # Anything unexpected (a sqlite3 error from publish(), a projection
            # failure, a malformed payload) used to escape this loop and kill
            # the collector thread outright: the badge then froze at its last
            # value while the app kept running, with no trace anywhere. Report
            # it and keep the loop alive with backoff instead.
            if report is not None:
                report("error", f"{type(error).__name__}: {error}")
            deadline = time.monotonic() + backoff
            while (
                not stop.is_set()
                and not (restart is not None and restart.is_set())
                and (enabled is None or enabled())
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                stop.wait(min(0.25, remaining))
            backoff = min(30.0, backoff * 2)
        finally:
            if on_source is not None:
                on_source(None)
            if source is not None and hasattr(source, "close"):
                source.close()
        if restart is not None and restart.is_set():
            restart.clear()
            backoff = 2.0
            if report is not None:
                report("starting", "正在重新连接 Codex App Server")
