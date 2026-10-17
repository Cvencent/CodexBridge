from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.protocol.capabilities import parse_max_text_chars
from codexbridge.devices.session import DeviceSession
from codexbridge.hub import BridgeHub

# The host heartbeat is a liveness ping, not a status channel. It must arrive
# before the device's session timeout, so a 5 s beat is intentional.
DEFAULT_HEARTBEAT_SECONDS = 5.0

DEFAULT_PORT = 8765

# Remembered-endpoint dials are bounded so a stale IP cannot turn into a retry
# storm: a device is only re-dialled after this many seconds.
REDIAL_COOLDOWN_SECONDS = 10.0

# A remembered endpoint is a guess: the LAN is close, so a device that is not
# there should refuse the socket long before this. Anything slower is a dead
# host and the round should move on.
CONNECT_TIMEOUT_SECONDS = 3.0


class DeviceManager:
    def __init__(
        self,
        registry: DeviceRegistry,
        hub: BridgeHub,
        *,
        session_factory: Any = DeviceSession,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        timeout_seconds: float = 15.0,
    ):
        self.registry = registry
        self.hub = hub
        self.session_factory = session_factory
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._redial_at: dict[str, float] = {}

    def sync(self, results: Iterable[DiscoveryResult]) -> None:
        if self._stopping.is_set():
            return
        for result in results:
            self._sync_one(result)

    def paired_count(self) -> int:
        """Paired devices that do not have a live session right now.

        Per device, not globally: a phone with a healthy session must not make a
        passport that is waiting for its first dial look connected. That mix-up
        is what let one online device push the broadcast cadence to fifteen
        seconds and starve every other device out of the LAN.
        """
        with self._lock:
            sessions = set(self._sessions)
        return sum(
            1
            for record in self.registry.all()
            if record.token
            and record.device_id not in sessions
            and not record.device_id.startswith("legacy:")
        )

    def online_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def redial_paired(self) -> int:
        """Dial remembered endpoints for paired devices with no live session.

        UDP discovery stays the authority on *which* devices exist. This only
        shortens the wait for one that is already known and currently listening:
        a Wi-Fi station in power-save mode can answer a broadcast later than the
        host's listen window, which is what made the first entry into an app read
        "not connected" for close to ten seconds.
        """
        now = time.time()
        with self._lock:
            waiting = [
                record
                for record in self.registry.all()
                if record.token
                and record.host
                and not record.device_id.startswith("legacy:")
                and record.device_id not in self._sessions
                and now - self._redial_at.get(record.device_id, 0.0)
                >= REDIAL_COOLDOWN_SECONDS
            ]
            for record in waiting:
                self._redial_at[record.device_id] = now
        started = 0
        for record in waiting:
            if self._stopping.is_set():
                break
            self._sync_one(
                DiscoveryResult(
                    host=str(record.host),
                    device=record.model,
                    device_id=record.device_id,
                    name=record.name,
                    port=record.port or DEFAULT_PORT,
                    capabilities=sorted(record.supported),
                ),
                dial_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            started += 1
        return started

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            sessions = list(self._sessions.values())
            threads = list(self._threads.values())
        for session in sessions:
            session.stop()
        for thread in threads:
            thread.join(timeout=1.0)

    def remove(self, device_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(device_id, None)
            thread = self._threads.pop(device_id, None)
        self.hub.detach(device_id)
        if session is not None:
            session.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _sync_one(
        self,
        result: DiscoveryResult,
        dial_timeout_seconds: float | None = None,
    ) -> None:
        supported = result.capabilities
        legacy_id = f"legacy:{result.host}"
        legacy = self.registry.get(legacy_id)
        token = None
        if legacy is not None:
            existing = self.registry.get(result.device_id)
            if existing is None or not existing.token:
                token = legacy.token
        record = self.registry.upsert(
            result.device_id,
            model=result.device,
            name=result.name or result.device,
            token=token,
            supported=supported,
            host=result.host,
            port=result.port,
        )
        if legacy is not None and result.device_id != legacy_id:
            self.registry.remove(legacy_id)
        with self._lock:
            alive = result.device_id in self._sessions
        if alive:
            # We are already talking to this device. An announcement or broadcast
            # from it is expected while it is connected, and rewriting the
            # registry file every round buys nothing.
            return
        record.last_seen = time.time()
        self.registry.save()
        if not record.token:
            return

        session = self.session_factory(
            result.host,
            result.port,
            record.token,
            device_id=result.device_id,
            on_device_info=lambda info: self._handle_device_info(
                result.device_id,
                info,
            ),
            on_event=lambda event: self.hub.handle_device_event(result.device_id, event),
            on_online=lambda: self._online(result.device_id, session),
            on_offline=lambda: self._offline(result.device_id, session),
            heartbeat_seconds=self.heartbeat_seconds,
            timeout_seconds=(
                dial_timeout_seconds
                if dial_timeout_seconds is not None
                else self.timeout_seconds
            ),
        )
        thread = threading.Thread(
            target=self._run_session,
            args=(result.device_id, session),
            daemon=True,
        )
        with self._lock:
            if result.device_id in self._sessions:
                session.stop()
                return
            self._sessions[result.device_id] = session
            self._threads[result.device_id] = thread
        thread.start()

    def _run_session(self, device_id: str, session: Any) -> None:
        try:
            session.run_once()
        except OSError as exc:
            # A remembered endpoint can be stale after a DHCP lease moves, so a
            # refused dial is expected occasionally and must not raise a thread
            # traceback into the user's console.
            print(f"CodexBridge: dial {device_id} failed: {exc}")
        finally:
            self._offline(device_id, session)

    def _handle_device_info(self, device_id: str, info: dict) -> set[str]:
        record = self.registry.get(device_id)
        if record is None:
            return set()
        model = info.get("model")
        name = info.get("name")
        capabilities = info.get("capabilities")
        # A device may declare how much body text it can render. Honour the
        # declaration so one protocol serves an ESP32 and a desktop alike.
        declared_budget = parse_max_text_chars(info.get("max_text_chars"))
        self.registry.upsert(
            device_id,
            model=model if isinstance(model, str) and model else record.model,
            name=name if isinstance(name, str) and name else record.name,
            supported=capabilities if isinstance(capabilities, list) else None,
        )
        updated_record = self.registry.get(device_id)
        if updated_record is not None and declared_budget is not None:
            updated_record.max_text_chars = declared_budget
        updated = self.registry.get(device_id)
        updated.last_seen = time.time()
        self.registry.save()
        return self.hub.capabilities_for(device_id)

    def _online(self, device_id: str, session: Any) -> None:
        record = self.registry.get(device_id)
        if record is not None:
            record.last_seen = time.time()
            self.registry.save()
        self.hub.attach(device_id, session)
        # The attach-time replay primes the Codex catalog, which is a blocking
        # RPC. On this thread it would also delay the session's own read loop,
        # so hand it to the hub's per-device worker instead.
        self.hub.replay_state_async(device_id)

    def _offline(self, device_id: str, session: Any) -> None:
        self.hub.detach(device_id)
        with self._lock:
            if self._sessions.get(device_id) is session:
                self._sessions.pop(device_id, None)
                self._threads.pop(device_id, None)
