from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable


CAPABILITY_BITS: dict[str, int] = {
    "status": 0,
    "text": 1,
    "tasks": 2,
    "results": 3,
    "approval": 4,
    "audio": 5,
    "voice": 6,
    "read_ack": 7,
    "control": 8,
    "history": 9,
    "telemetry": 10,
    "subscribe_live": 11,
    "subscribe_thread": 12,
    "subscribe_summary": 13,
    "writer_lease": 14,
    "multi_page": 15,
    "compression": 16,
    "tls": 17,
    "aead_reserved": 18,
    "device_config": 19,
    "command_status": 20,
    "queue": 21,
    "queue_manage": 22,
    "guidance": 23,
    "message_edit": 24,
    "message_delete": 25,
    "thread_manage": 26,
    "structured_input": 27,
    "attachment": 28,
    "draft": 29,
    "batch": 30,
    "vendor": 31,
    "thread_search": 32,
    "thread_lifecycle": 33,
    "thread_fork": 34,
    "thread_settings": 35,
    "message_revision": 36,
    "item_stream": 37,
    "plan": 38,
    "reasoning_summary": 39,
    "tool_events": 40,
    "mcp": 41,
    "account": 42,
    "context_usage": 43,
    "context_compact": 44,
    "read_unread": 45,
    "project_manage": 46,
    "notice": 47,
    "server_request": 48,
    "input_capabilities": 49,
    "queue_priority": 50,
    "message_reference": 51,
    "model_switch": 52,
    "interrupt_mode": 53,
    "cross_device_active": 54,
    "resume": 55,
    "realtime_reserved": 56,
    "file_reference": 57,
    "image_input": 58,
    "audio_input": 59,
    "structured_approval": 60,
    "events_since": 61,
    "thread_goal": 62,
    "experimental_ext": 63,
}

UNIMPLEMENTED_CAPABILITIES = frozenset(
    {
        "compression",
        "aead_reserved",
        "batch",
        "vendor",
        "context_compact",
        "interrupt_mode",
        "realtime_reserved",
        "structured_approval",
        "thread_goal",
        "experimental_ext",
    }
)


@dataclass(frozen=True)
class ProfileBudget:
    profile: int
    max_frame_bytes: int
    max_text_chars: int
    max_text_bytes: int
    history_depth: int
    queue_depth: int
    heartbeat_seconds: int
    max_subscriptions: int
    snapshot_page_objects: int
    max_chunk_bytes: int
    coalesce_window_ms: int
    window_frames: int
    window_bytes: int


PROFILE_BUDGETS = {
    0: ProfileBudget(0, 256, 160, 480, 0, 8, 30, 2, 0, 96, 300, 3, 768),
    1: ProfileBudget(1, 1024, 512, 1536, 2, 32, 15, 4, 2, 384, 150, 8, 8192),
    2: ProfileBudget(2, 65536, 100000, 1048576, 100, 256, 10, 32, 50, 16384, 100, 64, 1048576),
}


# Older firmwares call CBDP bit 42 "quota"; the wire name is "account".
CAPABILITY_ALIASES = {"quota": "account"}


def capability_bitmap(names: Iterable[str]) -> int:
    bitmap = 0
    for name in names:
        key = str(name)
        bit = CAPABILITY_BITS.get(CAPABILITY_ALIASES.get(key, key))
        if bit is not None:
            bitmap |= 1 << bit
    return bitmap


def capability_names(bitmap: int) -> set[str]:
    value = int(bitmap) & ((1 << 64) - 1)
    return {name for name, bit in CAPABILITY_BITS.items() if value & (1 << bit)}


def effective_capabilities(
    supported: int,
    enabled: int,
    transport_allowed: int,
) -> set[str]:
    return capability_names(int(supported) & int(enabled) & int(transport_allowed))


def profile_budget(profile: int) -> ProfileBudget:
    return PROFILE_BUDGETS.get(int(profile), PROFILE_BUDGETS[0])
