"""Phone companion API for CodexBridge.

The companion API exposes Codex threads and remote control to paired phones on
the local network. It is separate from the loopback-only admin API and from the
UDP/TCP device protocol: phones authenticate with a pairing code and a long
lived token, then talk HTTP + SSE.
"""

from codexbridge.companion.api import CompanionApi
from codexbridge.companion.discovery import CompanionDiscovery
from codexbridge.companion.service import CompanionService
from codexbridge.companion.store import CompanionStore

__all__ = ["CompanionApi", "CompanionDiscovery", "CompanionService", "CompanionStore"]