from collections.abc import Iterable

# Legacy JSON devices and CBDP v1 devices share one device registry, so the
# vocabulary must cover both. The CBDP names are the canonical wire names from
# docs/CBDP-V1-PROTOCOL.md 9.2; legacy-only names stay for older firmware.
FEATURES: tuple[str, ...] = (
    # Legacy JSON + CBDP shared
    "status",
    "text",
    "tasks",
    "results",
    "approval",
    "audio",
    "read_ack",
    "voice",
    "control",
    "queue",
    # Legacy-only names still reported by older firmware
    "word_bear",
    "quota",
    # CBDP v1 capability names (bitmap bits 0-63, implemented subset)
    "telemetry",
    "subscribe_live",
    "subscribe_thread",
    "subscribe_summary",
    "writer_lease",
    "multi_page",
    "tls",
    "device_config",
    "command_status",
    "queue_manage",
    "guidance",
    "message_edit",
    "message_delete",
    "thread_manage",
    "structured_input",
    "attachment",
    "draft",
    "thread_search",
    "thread_lifecycle",
    "thread_fork",
    "thread_settings",
    "message_revision",
    "item_stream",
    "plan",
    "reasoning_summary",
    "tool_events",
    "mcp",
    "account",
    "context_usage",
    "read_unread",
    "project_manage",
    "notice",
    "server_request",
    "input_capabilities",
    "queue_priority",
    "message_reference",
    "model_switch",
    "cross_device_active",
    "resume",
    "file_reference",
    "image_input",
    "audio_input",
    "events_since",
)

MINIMAL: frozenset[str] = frozenset({"status", "text", "read_ack"})
MODEL_DEFAULTS: dict[str, frozenset[str]] = {
    # The AI Passport renders status, task lists, results and voice on a real
    # screen, so every capability it declares is useful by default.
    "FoloOS-AI-Passport": MINIMAL | frozenset(
        {"tasks", "results", "audio", "voice", "approval"}
    ),
    "dotii": MINIMAL | frozenset({"tasks", "results", "quota"}),
}


def normalize(values: Iterable[str]) -> set[str]:
    known = set(FEATURES)
    return {value for value in values if value in known}


def effective(supported: Iterable[str], enabled: Iterable[str]) -> set[str]:
    return normalize(supported) & normalize(enabled)


def default_for(model: str) -> set[str]:
    return set(MODEL_DEFAULTS.get(model, MINIMAL))

# Generic text-budget profile.
# Devices differ wildly in RAM and screen size, so the wire protocol must
# not hardcode a truncation length per device. Instead each device declares
# its own budget and CodexBridge honours it for every body field it sends.
# `None` means unlimited full content.
UNLIMITED: int | None = None

# Fallback budget when neither the device nor the local config declares one.
DEFAULT_MAX_TEXT_CHARS: int | None = UNLIMITED

MIN_MAX_TEXT_CHARS = 16
MAX_MAX_TEXT_CHARS = 100000


def parse_max_text_chars(value: object) -> int | None:
    """Normalize a declared text budget.

    Returns `None` for unlimited or missing declarations so callers can
    distinguish a full-content device from a partial-content device.
    """
    if value is None:
        return UNLIMITED
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed: int | None = value
    elif isinstance(value, float):
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return UNLIMITED
        try:
            parsed = int(text)
        except ValueError:
            return None
    else:
        return None
    if parsed is None or parsed <= 0:
        return UNLIMITED
    if parsed < MIN_MAX_TEXT_CHARS:
        return MIN_MAX_TEXT_CHARS
    if parsed > MAX_MAX_TEXT_CHARS:
        return MAX_MAX_TEXT_CHARS
    return parsed
