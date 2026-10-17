"""Make the local listener ports belong to exactly one CodexBridge.

On Windows SO_REUSEADDR does not mean "rebind quickly after a restart", it
means "another process may bind this same port at the same time". Two
listeners on one port split incoming connections between them, so a stale old build
left on disk can silently answer part of the admin API and half of the phone traffic.
SO_EXCLUSIVEADDRUSE is the opposite promise: this socket owns the address, every other
bind is refused, and a restart still rebinds at once (measured here: 0 ms after
accepted loopback connections, and no TIME_WAIT wait).
"""

from __future__ import annotations

import socket


def configure_exclusive(sock: socket.socket) -> None:
    """Ask the kernel to refuse any second listener on this address."""
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is None:
        # Non-Windows: the default already refuses a shared bind, and turning reuse
        # off keeps a restarting process from racing its own TIME_WAIT.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    except OSError:
        # An option this host rejects must not stop the bridge from starting;
        # "do not share" is still safer than "anyone may bind".
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)


class ExclusiveBindMixin:
    """Mixin for socketserver classes that must own their port exclusively.

    socketserver applies allow_reuse_address inside server_bind, and on Windows
    SO_REUSEADDR is the literal inverse of SO_EXCLUSIVEADDRUSE, so reuse has to be
    switched off here before the inherited bind runs.
    """

    allow_reuse_address = False

    def server_bind(self):  # type: ignore[override]
        configure_exclusive(self.socket)
        super().server_bind()


def port_is_in_use(host: str, port: int, timeout: float = 0.4) -> bool:
    """Cheap probe so a refused bind can be explained to the user."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect((host, port))
    except OSError:
        probe.close()
        return False
    probe.close()
    return True
