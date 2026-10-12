from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codexbridge.protocol.capabilities import MINIMAL, default_for, normalize

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "name": self.name,
            "token": self.token,
            "supported": sorted(self.supported),
            "enabled": sorted(self.enabled),
            "last_seen": self.last_seen,
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
        )


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
                os.replace(temporary_name, self.path)
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

    def set_enabled(self, device_id: str, enabled: Iterable[str]) -> None:
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise KeyError(device_id)
            record.enabled = normalize(enabled)

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
