import json
from typing import Any

MAX_LINE_BYTES = 768
TEXT_CHUNK_BYTES = 480
MAX_TITLE_BYTES = 160
TRUNCATION_SUFFIX = "..."


def encode(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise TypeError("message must be a dict")
    payload = json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(payload) > MAX_LINE_BYTES:
        raise ValueError("encoded message exceeds the line limit")
    return payload


def decode(line: bytes) -> dict[str, Any] | None:
    if not isinstance(line, bytes) or len(line) > MAX_LINE_BYTES:
        return None
    line = line.strip()
    if not line.startswith(b"{"):
        return None
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def split_text(text: str, limit: int = TEXT_CHUNK_BYTES) -> list[str]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if limit <= 0:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if size > limit:
            raise ValueError("one character exceeds the chunk limit")
        if current and current_bytes + size > limit:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current or not text:
        chunks.append("".join(current))
    return chunks


def _truncate_utf8(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    suffix_bytes = len(TRUNCATION_SUFFIX.encode("utf-8"))
    if limit < suffix_bytes:
        raise ValueError("limit is too small for the truncation suffix")
    budget = limit - suffix_bytes
    kept: list[str] = []
    size = 0
    for character in text:
        character_size = len(character.encode("utf-8"))
        if size + character_size > budget:
            break
        kept.append(character)
        size += character_size
    return "".join(kept) + TRUNCATION_SUFFIX


def wire_messages(message: dict[str, Any]) -> list[bytes]:
    if message.get("type") != "message":
        return [encode(message)]

    prepared = dict(message)
    title = prepared.get("title")
    if isinstance(title, str):
        prepared["title"] = _truncate_utf8(title, MAX_TITLE_BYTES)

    body = prepared.get("body", "")
    if not isinstance(body, str):
        raise TypeError("message body must be a string")

    limit = TEXT_CHUNK_BYTES
    while True:
        chunks = split_text(body, limit)
        total = len(chunks)
        try:
            return [
                encode(
                    {
                        **prepared,
                        "body": chunk,
                        "seq": seq,
                        "total": total,
                    }
                )
                for seq, chunk in enumerate(chunks)
            ]
        except ValueError:
            if limit == 1:
                raise
            limit = max(1, limit - 64)
