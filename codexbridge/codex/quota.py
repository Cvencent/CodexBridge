from __future__ import annotations

from typing import Any


# The wire protocol carries each message on a single line capped at 768 bytes.
# Quota messages repeat the thread id in both message_id and data, so the
# variable-length strings are capped here to keep the encoded line in budget.
MAX_THREAD_BYTES = 36
MAX_ACTION_BYTES = 40
MAX_PLAN_BYTES = 16


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clip(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    clipped = encoded[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def quota_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a Codex snapshot onto the optional quota message fields.

    Only measured values are copied. Missing or malformed fields stay absent
    so devices render them as "--" without the bridge inventing data.
    """
    if not isinstance(snapshot, dict):
        return {}
    account = _dict(snapshot.get("account"))
    task = _dict(snapshot.get("task"))
    result: dict[str, Any] = {}

    if account.get("five_hour_available") is True:
        value = _number(account.get("five_hour_remaining_percent"))
        if value is not None:
            result["five_hour_remaining_percent"] = value
        value = _number(account.get("five_hour_reset_at_epoch"))
        if value is not None and value > 0:
            result["five_hour_reset_at"] = value
    if account.get("weekly_available") is True:
        value = _number(account.get("weekly_remaining_percent"))
        if value is not None:
            result["weekly_remaining_percent"] = value
        value = _number(account.get("weekly_reset_at_epoch"))
        if value is not None and value > 0:
            result["weekly_reset_at"] = value
    if account.get("weekly_tokens_available") is True:
        # account_snapshot computes this total over the latest seven-day
        # window, so expose the same measured value under both protocol names.
        value = _number(account.get("weekly_tokens"))
        if value is not None and value >= 0:
            result["weekly_tokens"] = value
            result["seven_day_tokens"] = value

    plan_type = account.get("plan_type")
    if isinstance(plan_type, str) and plan_type:
        result["plan_type"] = _clip(plan_type, MAX_PLAN_BYTES)

    if isinstance(task.get("status"), str) and task["status"]:
        result["task_status"] = task["status"]
    for name, limit in (
        ("thread_id", MAX_THREAD_BYTES),
        ("current_action", MAX_ACTION_BYTES),
    ):
        value = task.get(name)
        if isinstance(value, str) and value:
            result[name] = _clip(value, limit)
    token_count = _number(task.get("token_count"))
    if token_count is not None and task.get("token_count_available") is not False:
        result["token_count"] = token_count
    for name in ("plan_completed", "plan_total"):
        value = _number(task.get(name))
        if value is not None:
            result[name] = value

    # Allow an upstream seven-day total to win when it is provided directly.
    direct_seven_day = _number(snapshot.get("seven_day_tokens"))
    if direct_seven_day is not None and direct_seven_day >= 0:
        result["seven_day_tokens"] = direct_seven_day
    return result
