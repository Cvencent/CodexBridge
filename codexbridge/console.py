"""Console output that survives a host console with a legacy code page."""

from __future__ import annotations

import sys


def emit(message: str, *, flush: bool = False) -> None:
    """Print ``message`` even when the console cannot encode every character.

    Windows consoles start on a legacy code page (cp1252, cp936, ...), and
    ``print`` raises ``UnicodeEncodeError`` for characters outside that page.
    Falling back to a lossy copy keeps the surrounding startup or refresh path
    alive instead of failing on the way to the log line.
    """

    stream = sys.stdout
    try:
        print(message, flush=flush)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        print(message.encode(encoding, "replace").decode(encoding, "replace"), flush=flush)
