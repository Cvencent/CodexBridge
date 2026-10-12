from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.devices.session import DeviceSession
from codexbridge.hub import BridgeHub


class DeviceManager:
    def __init__(
        self,
        registry: DeviceRegistry,
        hub: BridgeHub,
        *,
        session_factory: Any = DeviceSession,
        heartbeat_seconds: float = 5.0,
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

    def sync(self, results: Iterable[DiscoveryResult]) -> None:
        if self._stopping.is_set():
            return
        for result in results:
            self._sync_one(result)

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

    def _sync_one(self, result: DiscoveryResult) -> None:
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
        )
        if legacy is not None and result.device_id != legacy_id:
            self.registry.remove(legacy_id)
        record.last_seen = time.time()
        self.registry.save()
        if not record.token:
            return

        with self._lock:
            if result.device_id in self._sessions:
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
            timeout_seconds=self.timeout_seconds,
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
        finally:
            self._offline(device_id, session)

    def _handle_device_info(self, device_id: str, info: dict) -> set[str]:
        record = self.registry.get(device_id)
        if record is None:
            return set()
        model = info.get("model")
        name = info.get("name")
        capabilities = info.get("capabilities")
        self.registry.upsert(
            device_id,
            model=model if isinstance(model, str) and model else record.model,
            name=name if isinstance(name, str) and name else record.name,
            supported=capabilities if isinstance(capabilities, list) else None,
        )
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
        self.hub.replay_state(device_id)

    def _offline(self, device_id: str, session: Any) -> None:
        self.hub.detach(device_id)
        with self._lock:
            if self._sessions.get(device_id) is session:
                self._sessions.pop(device_id, None)
                self._threads.pop(device_id, None)
