from collections.abc import Iterable

FEATURES: tuple[str, ...] = (
    "status",
    "text",
    "tasks",
    "results",
    "approval",
    "audio",
    "read_ack",
    "voice",
    "word_bear",
)

MINIMAL: frozenset[str] = frozenset({"status", "text", "read_ack"})
MODEL_DEFAULTS: dict[str, frozenset[str]] = {
    "FoloOS-AI-Passport": MINIMAL,
    "dotii": MINIMAL | frozenset({"tasks", "results"}),
}


def normalize(values: Iterable[str]) -> set[str]:
    known = set(FEATURES)
    return {value for value in values if value in known}


def effective(supported: Iterable[str], enabled: Iterable[str]) -> set[str]:
    return normalize(supported) & normalize(enabled)


def default_for(model: str) -> set[str]:
    return set(MODEL_DEFAULTS.get(model, MINIMAL))
