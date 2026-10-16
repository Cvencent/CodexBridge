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


TAIL_MARKER = "..."


def clip_text(text: str, max_text_chars: int | None) -> str:
    """Clip a body to a device-declared character budget.

    ``None`` means the device accepts unlimited content, so the text is
    returned unchanged. Otherwise the text is cut on a character boundary
    and marked with an ellipsis so the device can show truncation.
    """
    if not isinstance(text, str):
        return text
    if max_text_chars is None:
        return text
    if max_text_chars <= 0:
        return ""
    if len(text) <= max_text_chars:
        return text
    keep = max_text_chars - len(TAIL_MARKER)
    if keep <= 0:
        return TAIL_MARKER[:max_text_chars]
    return text[:keep] + TAIL_MARKER


def clip_utf8(text: str, max_chars: int | None = None,
              max_bytes: int | None = None) -> str:
    """Cut text on a character boundary, first by count then by encoded size.

    A device declares how many characters it can render (``max_text_chars``);
    the host additionally has to stay inside one wire line. Applying both, in
    that order, keeps a bounded field generic: no per-device number lives here.
    """
    if not isinstance(text, str):
        return ""
    result = clip_text(text, max_chars) if max_chars else text
    if max_bytes is None:
        return result
    if len(result.encode("utf-8")) <= max_bytes:
        return result
    suffix_bytes = len(TRUNCATION_SUFFIX.encode("utf-8"))
    if max_bytes < suffix_bytes:
        return ""
    budget = max_bytes - suffix_bytes
    kept: list[str] = []
    size = 0
    for character in result:
        character_size = len(character.encode("utf-8"))
        if size + character_size > budget:
            break
        kept.append(character)
        size += character_size
    return "".join(kept) + TRUNCATION_SUFFIX


def wire_messages(message: dict[str, Any]) -> list[bytes]:
    if message.get('type') in {'command_result', 'protocol_catalog', 'device_event'}:
        try:
            return [encode(message)]
        except ValueError:
            serialized = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
            limit = 240
            while True:
                chunks = split_text(serialized, limit)
                try:
                    return [encode({'type': 'command_chunk',
                                    'request_id': message.get('request_id', ''),
                                    'seq': index, 'total': len(chunks), 'data': chunk})
                            for index, chunk in enumerate(chunks)]
                except ValueError:
                    if limit == 1:
                        raise
                    limit = max(1, limit // 2)
    if message.get("type") != "message":
        return [encode(message)]

    prepared = dict(message)
    title = prepared.get("title")
    if isinstance(title, str):
        prepared["title"] = _truncate_utf8(title, MAX_TITLE_BYTES)

    body = prepared.get("body", "")
    if not isinstance(body, str):
        raise TypeError("message body must be a string")

    # The line budget covers the whole encoded record, not just the body.
    # Sequential metadata plus optional `data` can already fill most of the
    # 768-byte line, so derive the chunk size from the real overhead.
    overhead = len(
        json.dumps(
            {key: value for key, value in prepared.items() if key != "body"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    # Reserve room for the seq/total fields themselves.
    available = int(MAX_LINE_BYTES) - 1 - overhead - 32
    limit = min(TEXT_CHUNK_BYTES, available)
    if limit < 1:
        # Metadata alone cannot fit one line; drop optional context rather
        # than losing the message entirely.
        prepared.pop("data", None)
        overhead = len(
            json.dumps(
                {key: value for key, value in prepared.items() if key != "body"},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        available = int(MAX_LINE_BYTES) - 1 - overhead - 32
        limit = min(TEXT_CHUNK_BYTES, available)
    if limit < 1:
        limit = 1
    while True:
        try:
            chunks = split_text(body, limit)
        except ValueError:
            # A single character cannot fit; fall back to one chunk so the
            # device still receives a (possibly clipped) record.
            chunks = [body[:1]]
            break
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
