"""CBDP v1 account quota and usage payload construction.

The numbers themselves come from the Codex app server snapshot produced by
``codexbridge.codex.app_server.account_snapshot``; this module only translates
the measured values onto the stable wire fields defined in
``docs/CBDP-V1-PROTOCOL.md`` section 8.11.6 and 16.2.

Design rules:

* Only measured values are emitted. A missing window or a non-ChatGPT account
  leaves the field absent so a device renders ``--`` instead of a fake zero.
* Remaining percent is sent, never ``usedPercent``. The upstream API reports
  usage; deriving the complement once here removes that footgun from every
  firmware.
* Reset times become ``reset_at_ms`` on the wire, since the v1 spec declares
  that tag as an ``i64`` epoch-millisecond value.
* Percentages are clamped to 0..100 and totals to a non-negative integer.
"""

from __future__ import annotations

from typing import Any

from codexbridge.cbdp.constants import (
    TAG_PLAN_TYPE,
    TAG_RATE_LIMITS,
    TAG_RATE_LIMIT_WINDOW,
    TAG_REMAINING_PERCENT,
    TAG_RESET_AT_MS,
    TAG_SEVEN_DAY_AS_OF,
    TAG_SEVEN_DAY_TOKENS,
    TAG_SEVEN_DAY_WINDOW_DAYS,
)
from codexbridge.cbdp.tlv import encode_array, encode_tlvs, u8, u64, utf8

# The 7-day total is defined by the protocol as a fixed window. Devices that
# receive the value should not have to guess what it covers.
SEVEN_DAY_WINDOW_DAYS = 7

# The plan string is a short label ("plus", "pro"), bounded like the
# legacy projection bounds it so one event cannot grow without limit.
MAX_PLAN_BYTES = 31

# Wire field name -> (remaining percent key, reset-at key). The names match the
# published ``rate_limit_window`` values in the v1 spec.
_WINDOWS = (
    ("five_hour", "five_hour_remaining_percent", "five_hour_reset_at"),
    ("weekly", "weekly_remaining_percent", "weekly_reset_at"),
)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


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


def _percent(value: Any) -> int | None:
    number = _int(value)
    if number is None:
        return None
    return max(0, min(100, number))


def _epoch_ms(value: Any) -> int | None:
    """Convert the snapshot's epoch-second reset time to epoch milliseconds."""

    number = _int(value)
    if number is None or number <= 0:
        return None
    return number * 1000


def account_rate_limits_payload(quota: dict[str, Any]) -> dict[int, bytes]:
    """Build the ``rate_limits.changed`` payload for one quota snapshot."""

    if not isinstance(quota, dict):
        return {}
    windows: list[bytes] = []
    for name, percent_key, reset_key in _WINDOWS:
        fields: list[tuple[int, bytes]] = [(TAG_RATE_LIMIT_WINDOW, utf8(name))]
        percent = _percent(quota.get(percent_key))
        if percent is not None:
            fields.append((TAG_REMAINING_PERCENT, u8(percent)))
        reset_ms = _epoch_ms(quota.get(reset_key))
        if reset_ms is not None:
            fields.append((TAG_RESET_AT_MS, u64(reset_ms)))
        if len(fields) == 1:
            # The upstream did not measure this window at all. Emitting a bare
            # window name would tell a device the window exists with unknown
            # quota, which is worse than omitting it.
            continue
        windows.append(encode_tlvs(fields))
    if not windows:
        return {}
    return {TAG_RATE_LIMITS: encode_array(TAG_RATE_LIMIT_WINDOW, windows)}


def account_plan_payload(quota: dict[str, Any]) -> dict[int, bytes]:
    """Build the ``account.changed`` payload for the account plan type.

    The plan name is already collected for the legacy snapshot and is the only
    account-level field the v1 spec defines for ``account.read``. It is emitted
    only when the upstream actually reported a plan, never inferred from the
    remaining percentages.
    """

    if not isinstance(quota, dict):
        return {}
    plan = quota.get("plan_type")
    if not isinstance(plan, str) or not plan:
        return {}
    return {TAG_PLAN_TYPE: utf8(_clip(plan, MAX_PLAN_BYTES))}


def account_token_usage_payload(quota: dict[str, Any]) -> dict[int, bytes]:
    """Build the ``token_usage.changed`` payload for the 7-day token total."""

    if not isinstance(quota, dict):
        return {}
    total = _int(quota.get("seven_day_tokens"))
    if total is None or total < 0:
        return {}
    payload: dict[int, bytes] = {
        TAG_SEVEN_DAY_TOKENS: u64(total),
        TAG_SEVEN_DAY_WINDOW_DAYS: u8(SEVEN_DAY_WINDOW_DAYS),
    }
    as_of = quota.get("seven_day_as_of")
    if isinstance(as_of, str) and as_of:
        # Already a short ``MM-DD`` string from the app server snapshot; clip
        # defensively so a future caller cannot bloat one event payload.
        payload[TAG_SEVEN_DAY_AS_OF] = utf8(as_of[:32])
    return payload
