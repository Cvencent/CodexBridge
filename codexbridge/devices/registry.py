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
    kind: str = "hardware"
    transport: str = "device_tcp"
    registration_state: str = "pending"
    registration_method: str = "admin_approval"
    last_address: str | None = None
    client_id: str = ""
    token_hash: str = ""
    # Private registration binding for hardware approval. These fields are
    # persisted with the record but never exposed by the public device view.
    registration_nonce: str | None = None
    registration_address: str | None = None
    registration_discovered_at: float | None = None
    registration_error: str = ""
    # None means the device accepts unlimited text; set by the device's own
    # handshake or by the local per-device override.
    max_text_chars: int | None = UNLIMITED
    # True once the local user has toggled capabilities by hand. Until then
    # the enabled set is derived from the model defaults, so a firmware that
    # grows new capabilities starts using them without a manual re-enable.
    customized: bool = False
    # Last known LAN endpoint, remembered from discovery. The host can dial it
    # directly instead of waiting for a broadcast round to line up with a device
    # that only wakes on its access point's traffic interval.
    host: str | None = None
    port: int | None = None

    @property
    def has_token(self) -> bool:
        return bool(self.token or self.token_hash)

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
            "host": self.host,
            "port": self.port,
            "kind": self.kind,
            "transport": self.transport,
            "registration_state": self.registration_state,
            "registration_method": self.registration_method,
            "last_address": self.last_address,
            "client_id": self.client_id,
            "token_hash": self.token_hash,
            "registration_nonce": self.registration_nonce,
            "registration_address": self.registration_address,
            "registration_discovered_at": self.registration_discovered_at,
            "registration_error": self.registration_error,
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
        host_value = value.get("host")
        host = str(host_value) if isinstance(host_value, str) and host_value else None
        port_value = value.get("port")
        port = (
            int(port_value)
            if isinstance(port_value, int) and not isinstance(port_value, bool)
            and 1 <= port_value <= 65535
            else None
        )
        supported = normalize(value.get("supported") or [])
        max_text_chars = parse_max_text_chars(value.get("max_text_chars"))
        kind = str(value.get("kind") or "hardware")
        if kind not in {"phone", "hardware"}:
            kind = "hardware"
        transport = str(value.get("transport") or (
            "companion_http" if kind == "phone" else "device_tcp"
        ))
        if transport not in {"companion_http", "device_tcp"}:
            transport = "companion_http" if kind == "phone" else "device_tcp"
        registration_state = str(value.get("registration_state") or "")
        if registration_state not in {"pending", "approved", "rejected", "revoked"}:
            registration_state = "approved" if token else "pending"
        registration_method = str(value.get("registration_method") or "")
        if registration_method not in {"pairing_code", "admin_approval"}:
            registration_method = "pairing_code" if kind == "phone" else "admin_approval"
        last_address_value = value.get("last_address")
        last_address = (
            str(last_address_value)
            if isinstance(last_address_value, str) and last_address_value
            else None
        )
        client_id_value = value.get("client_id")
        client_id = (
            str(client_id_value).strip()[:128]
            if isinstance(client_id_value, str)
            else ""
        )
        token_hash_value = value.get("token_hash")
        token_hash = (
            str(token_hash_value)
            if isinstance(token_hash_value, str) and len(token_hash_value) == 64
            else ""
        )
        registration_nonce_value = value.get("registration_nonce")
        registration_nonce = (
            str(registration_nonce_value)
            if isinstance(registration_nonce_value, str)
            and 16 <= len(registration_nonce_value) <= 64
            else None
        )
        registration_address_value = value.get("registration_address")
        registration_address = (
            str(registration_address_value)
            if isinstance(registration_address_value, str)
            and registration_address_value
            else None
        )
        registration_discovered_value = value.get("registration_discovered_at")
        registration_discovered_at = (
            float(registration_discovered_value)
            if isinstance(registration_discovered_value, (int, float))
            and not isinstance(registration_discovered_value, bool)
            else None
        )
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
            host=host,
            port=port,
            kind=kind,
            transport=transport,
            registration_state=registration_state,
            registration_method=registration_method,
            last_address=last_address,
            client_id=client_id,
            token_hash=token_hash,
            registration_nonce=registration_nonce,
            registration_address=registration_address,
            registration_discovered_at=registration_discovered_at,
            registration_error=str(value.get("registration_error") or "")[:64],
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
        migrated = False
        with self._lock:
            if self.path is None or not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return

            version = raw.get("version") if isinstance(raw, dict) else None
            migrated = version != 2
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
        if migrated:
            self.save()

    def save(self) -> None:
        with self._lock:
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 2,
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

    def all(self) -> list[DeviceRecord]:
        with self._lock:
            return list(self._devices.values())

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
        host: str | None = ...,
        port: int | None = ...,
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
                    host=None if host is ... else host,
                    port=None if port is ... else port,
                    enabled=(
                        default_for(model) & (declared | set(MINIMAL))
                        if supported is not None
                        else default_for(model)
                    ),
                    kind="hardware",
                    transport="device_tcp",
                    registration_state=("approved" if token else "pending"),
                    registration_method="admin_approval",
                    last_address=None if host is ... else host,
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
                record.registration_state = "approved"
            if host is not ...:
                record.host = host
                if host:
                    record.last_address = host
            if port is not ...:
                record.port = port
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

    def upsert_phone(
        self,
        device_id: str,
        *,
        name: str | None = None,
        token_hash: str = "",
        supported: Iterable[str] | None = None,
        enabled: Iterable[str] | None = None,
        last_seen: float | None = None,
        client_id: str = "",
        model: str = "Android",
    ) -> DeviceRecord:
        with self._lock:
            record = self._devices.get(device_id)
            declared = normalize(supported or [])
            if record is None:
                record = DeviceRecord(
                    device_id=device_id,
                    model=model,
                    name=name,
                    supported=declared,
                    enabled=(
                        normalize(enabled)
                        if enabled is not None
                        else set(declared)
                    ),
                    last_seen=last_seen,
                    kind="phone",
                    transport="companion_http",
                    registration_state="approved",
                    registration_method="pairing_code",
                    client_id=str(client_id or "")[:128],
                    token_hash=str(token_hash or ""),
                    customized=True,
                )
                self._devices[device_id] = record
                return record

            record.kind = "phone"
            record.transport = "companion_http"
            record.registration_state = "approved"
            record.registration_method = "pairing_code"
            record.model = model
            if name is not None:
                record.name = name
            if token_hash:
                record.token_hash = str(token_hash)
            if supported is not None:
                record.supported = declared
            if enabled is not None:
                record.enabled = normalize(enabled)
            if last_seen is not None:
                record.last_seen = last_seen
            if client_id:
                record.client_id = str(client_id)[:128]
            return record

    def public(
        self,
        device_id: str,
        *,
        online: bool = False,
        queued_count: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise KeyError(device_id)
            return self._public_locked(record, online=online, queued_count=queued_count)

    def public_all(
        self,
        *,
        online_ids: Iterable[str] | None = None,
        queued_counts: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        online_set = set(online_ids or ())
        counts = queued_counts or {}
        with self._lock:
            return [
                self._public_locked(
                    record,
                    online=record.device_id in online_set,
                    queued_count=max(0, int(counts.get(record.device_id, 0) or 0)),
                )
                for record in self._devices.values()
            ]

    @staticmethod
    def _public_locked(
        record: DeviceRecord,
        *,
        online: bool,
        queued_count: int,
    ) -> dict[str, Any]:
        return {
            "device_id": record.device_id,
            "kind": record.kind,
            "transport": record.transport,
            "name": record.name,
            "model": record.model,
            "registration_state": record.registration_state,
            "registration_method": record.registration_method,
            "online": bool(online),
            "last_seen": record.last_seen,
            "last_address": record.last_address,
            "has_token": record.has_token,
            "supported": sorted(record.supported),
            "enabled": sorted(record.enabled),
            "max_text_chars": record.max_text_chars,
            "queued_count": int(queued_count),
        }

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
            # Minting is an explicit approval path. Keep the in-memory state in
            # sync with the credential instead of waiting for a reload.
            record.registration_state = "approved"
            record.registration_method = "admin_approval"
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
