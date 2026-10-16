from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codexbridge.protocol.capabilities import (
    MINIMAL,
    UNLIMITED,
    default_for,
    normalize,
    parse_max_text_chars,
)

TOKEN_BYTES = 24


@dataclass
class DeviceRecord:
    device_id: str
    model: str
    name: str | None = None
    token: str | None = None
    supported: set[str] = field(default_factory=set)
    enabled: set[str] = field(default_factory=set)
    last_seen: float | None = None
    # None means the device accepts unlimited text; set by the device's own
    # handshake or by the local per-device override.
    max_text_chars: int | None = UNLIMITED
    # True once the local user has toggled capabilities by hand. Until then
    # the enabled set is derived from the model defaults, so a firmware that
    # grows new capabilities starts using them without a manual re-enable.
    customized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "name": self.name,
            "token": self.token,
            "supported": sorted(self.supported),
            "enabled": sorted(self.enabled),
            "last_seen": self.last_seen,
            "max_text_chars": self.max_text_chars,
            "customized": self.customized,
        }

    @classmethod
    def from_dict(cls, device_id: str, value: dict[str, Any]) -> DeviceRecord:
        model = str(value.get("model") or "unknown")
        token_value = value.get("token")
        token = str(token_value) if isinstance(token_value, str) and token_value else None
        last_seen_value = value.get("last_seen")
        last_seen = (
            float(last_seen_value)
            if isinstance(last_seen_value, (int, float))
            else None
        )
        supported = normalize(value.get("supported") or [])
        max_text_chars = parse_max_text_chars(value.get("max_text_chars"))
        return cls(
            device_id=str(value.get("device_id") or device_id),
            model=model,
            name=str(value["name"]) if value.get("name") else None,
            token=token,
            supported=supported,
            enabled=(
                normalize(value["enabled"])
                if "enabled" in value
                else default_for(model) & (supported | set(MINIMAL))
            ),
            last_seen=last_seen,
            max_text_chars=max_text_chars,
            customized=bool(value.get("customized", False)),
        )


_REPLACE_ATTEMPTS = 8


def _replace_with_retry(source: str, destination: Path) -> None:
    """Atomically replace a file, tolerating transient Windows denials.

    Windows can answer os.replace with WinError 5 while an indexer or virus
    scanner briefly holds either path, so retry instead of failing the save.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


class DeviceRegistry:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._devices: dict[str, DeviceRecord] = {}
        self._lock = threading.RLock()

    def load(self) -> None:
        with self._lock:
            if self.path is None or not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return

            raw_devices = raw.get("devices", raw) if isinstance(raw, dict) else raw
            if isinstance(raw_devices, list):
                raw_devices = {
                    str(item.get("device_id")): item
                    for item in raw_devices
                    if isinstance(item, dict) and item.get("device_id")
                }
            if not isinstance(raw_devices, dict):
                return

            loaded: dict[str, DeviceRecord] = {}
            for device_id, value in raw_devices.items():
                if isinstance(value, dict):
                    loaded[str(device_id)] = DeviceRecord.from_dict(str(device_id), value)
            self._devices = loaded

    def save(self) -> None:
        with self._lock:
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "devices": {
                    device_id: record.to_dict()
                    for device_id, record in self._devices.items()
                },
            }
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
                    temporary.flush()
                    os.fsync(temporary.fileno())
                _replace_with_retry(temporary_name, self.path)
            finally:
                if temporary_name and os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def get(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            return self._devices.get(device_id)

    def upsert(
        self,
        device_id: str,
        *,
        model: str,
        name: str | None = None,
        token: str | None = None,
        supported: Iterable[str] | None = None,
    ) -> DeviceRecord:
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                declared = normalize(supported or [])
                record = DeviceRecord(
                    device_id=device_id,
                    model=model,
                    name=name,
                    token=token,
                    supported=declared,
                    enabled=(
                        default_for(model) & (declared | set(MINIMAL))
                        if supported is not None
                        else default_for(model)
                    ),
                )
                self._devices[device_id] = record
                return record

            previous_model = record.model
            previous_enabled = set(record.enabled)
            record.model = model
            if name is not None:
                record.name = name
            if token is not None:
                record.token = token
            if supported is not None:
                record.supported = normalize(supported)
                if not record.customized:
                    # Re-derive from the (possibly upgraded) declaration. A
                    # device that starts advertising `audio` should not need a
                    # host-side checkbox change to begin using it.
                    record.enabled = default_for(record.model) & (
                        record.supported | set(MINIMAL)
                    )
            if (
                previous_model == "unknown"
                and model != "unknown"
                and previous_enabled == default_for(previous_model)
            ):
                record.enabled = default_for(model) & (
                    record.supported | set(MINIMAL)
                )
            return record

    def remove(self, device_id: str) -> bool:
        with self._lock:
            return self._devices.pop(device_id, None) is not None

    def set_max_text_chars(self, device_id: str, max_text_chars: int | None) -> int | None:
        """Persist the per-device body budget.

        The device declares its own budget at handshake; this is the local
        override. ``None`` means unlimited, and the value is normalised the same
        way the handshake value is so both paths agree.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise KeyError(device_id)
            record.max_text_chars = parse_max_text_chars(max_text_chars)
            self.save()
            return record.max_text_chars

    def set_enabled(self, device_id: str, enabled: Iterable[str]) -> None:
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise KeyError(device_id)
            record.enabled = normalize(enabled)
            record.customized = True

    def mint_token(self, device_id: str) -> str:
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                record = self.upsert(device_id, model="unknown")
            if record.token is None:
                record.token = secrets.token_urlsafe(TOKEN_BYTES)
                self.save()
            return record.token

    def migrate_legacy(self, legacy_path: Path) -> bool:
        with self._lock:
            try:
                raw = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(raw, dict):
                return False
            host = raw.get("host")
            token = raw.get("token")
            if not isinstance(host, str) or not host:
                return False
            if not isinstance(token, str) or len(token) < 16:
                return False
            self.upsert(
                f"legacy:{host}",
                model="FoloOS-AI-Passport",
                name="FoloOS AI Passport",
                token=token,
                supported=["status", "text", "read_ack"],
            )
            self.save()
            return True
