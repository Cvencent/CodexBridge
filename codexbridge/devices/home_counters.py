"""Lightweight UDP push of the home-screen Codex counters.

The AI Passport home screen only needs three numbers (running, completed
unread, failed unread) and cannot afford to keep a CBDP TCP session open at the
same time as a network-radio stream. This module unicasts the aggregate
counters over the existing discovery UDP port, so a device that is sitting on
its home screen never has to open TCP just to keep those numbers fresh.

It is an additive channel: the CBDP TCP event ``system.codex_state(0x0704)``
stays exactly as it is, and a device may receive both.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from codexbridge.cbdp.constants import (
    FLAG_EVENT,
    QOS_STATE,
    TAG_COMPLETED_UNREAD,
    TAG_FAILED_UNREAD,
    TAG_RUNNING_COUNT,
    TYPE_HOME_COUNTERS,
)
from codexbridge.cbdp.frame import Frame, encode_frame
from codexbridge.cbdp.refs import SYSTEM_THREAD_SUMMARIES_TOPIC
from codexbridge.cbdp.tlv import u16
from codexbridge.devices.discovery import DISCOVERY_PORT

# A home screen refreshes on a one-second beat; two seconds bounds a lost
# datagram without making the LAN chatty. A count change is pushed immediately.
DEFAULT_INTERVAL_SECONDS = 2.0


def _counter(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def build_home_counters_frame(counts: dict) -> bytes:
    """Encode one ``0x1E`` frame carrying the three aggregate counters."""

    payload = {
        TAG_RUNNING_COUNT: u16(_counter(counts.get("running"))),
        TAG_COMPLETED_UNREAD: u16(_counter(counts.get("completed_unread"))),
        TAG_FAILED_UNREAD: u16(_counter(counts.get("failed_unread"))),
    }
    # Counters travel in header TLVs, matching the discovery frames on this
    # channel and keeping the datagram well under one MTU.
    return encode_frame(
        Frame(
            message_type=TYPE_HOME_COUNTERS,
            flags=FLAG_EVENT,
            topic_id=SYSTEM_THREAD_SUMMARIES_TOPIC,
            qos=QOS_STATE,
            header=payload,
        )
    )


class HomeCounterSender:
    """Unicast the aggregate counters to devices seen on the discovery port.

    Targets are registered from presence announcements. Only devices that have
    actually announced themselves receive datagrams, so a device that is not on
    its home screen and not listening is never woken by this channel.
    """

    def __init__(
        self,
        counts: Callable[[], dict],
        *,
        port: int = DISCOVERY_PORT,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sendto: Callable[[bytes, tuple], object] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._counts = counts
        self._port = int(port)
        self._interval = float(interval_seconds)
        self._clock = clock
        self._sendto = sendto
        self._lock = threading.RLock()
        self._targets: dict[str, tuple[str, int]] = {}
        self._last_sent_at: dict[str, float] = {}
        self._last_counts: dict[str, tuple[int, int, int]] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _key(counts: dict) -> tuple[int, int, int]:
        return (
            _counter(counts.get("running")),
            _counter(counts.get("completed_unread")),
            _counter(counts.get("failed_unread")),
        )

    def register(self, device_id: str, host: str) -> None:
        """Remember one announcing device as a unicast target."""

        if not device_id or not host:
            return
        with self._lock:
            self._targets[str(device_id)] = (str(host), self._port)
        self._wake.set()

    def unregister(self, device_id: str) -> None:
        with self._lock:
            self._targets.pop(str(device_id), None)
            self._last_sent_at.pop(str(device_id), None)
            self._last_counts.pop(str(device_id), None)

    def notify(self) -> None:
        """Ask the loop to push immediately (used when counters change)."""

        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="home-counter-sender",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            woke = self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                # A wake means "counters changed" or "a new target appeared",
                # so send even if the numeric value looks unchanged.
                self.push(force=woke)
            except OSError:
                continue

    def push(self, *, force: bool = True) -> int:
        """Send the current counters to every registered target.

        With ``force=False`` a target whose counters have not changed is only
        re-sent once its refresh interval has elapsed, so a lost periodic
        datagram still recovers and a change goes out immediately.
        """

        counts = self._counts()
        signature = self._key(counts)
        frame = build_home_counters_frame(counts)
        now = self._clock()
        sent = 0
        with self._lock:
            targets = list(self._targets.items())
        for device_id, endpoint in targets:
            previous = self._last_counts.get(device_id)
            last_sent = self._last_sent_at.get(device_id, 0.0)
            due = now - last_sent >= self._interval
            if not force and previous == signature and not due:
                continue
            try:
                self._send(frame, endpoint)
            except OSError:
                continue
            self._last_counts[device_id] = signature
            self._last_sent_at[device_id] = now
            sent += 1
        return sent

    def _send(self, frame: bytes, endpoint: tuple[str, int]) -> None:
        if self._sendto is not None:
            self._sendto(frame, endpoint)
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(frame, endpoint)
