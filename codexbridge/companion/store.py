"""Persistent pairing state for the phone companion API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8568
DEFAULT_DISCOVERY_PORT = 8569
LEGACY_PORT = 8790
LEGACY_COMPANION_PORT = 8791
LEGACY_DISCOVERY_PORT = 8766
PAIRING_CODE_TTL_SECONDS = 10 * 60
PAIRING_MAX_ATTEMPTS = 5
PHONE_TOKEN_BYTES = 32
PHONE_TOKEN_PREFIX = "cbp_"
MAX_PHONES = 16
PHONE_CAPABILITIES = {'status', 'tasks', 'control', 'approval', 'queue'}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_token() -> str:
    raw = base64.urlsafe_b64encode(secrets.token_bytes(PHONE_TOKEN_BYTES))
    return PHONE_TOKEN_PREFIX + raw.decode("ascii").rstrip("=")


@dataclass
class PhoneRecord:
    phone_id: str
    name: str
    token_hash: str
    created_at: float
    last_seen: float = 0.0
    enabled: set[str] = field(default_factory=lambda: set(PHONE_CAPABILITIES))
    # Stable installation identity supplied by the phone. Older records keep
    # this empty and are migrated the first time their phone pairs again.
    client_id: str = ""

    def capabilities(self) -> set[str]:
        return self.enabled & PHONE_CAPABILITIES

    def public(self) -> dict:
        return {
            "phone_id": self.phone_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "enabled": sorted(self.capabilities()),
        }


class CompanionStore:
    """Thread-safe pairing store persisted next to the device registry."""

    def __init__(
        self,
        path: Path | None,
        port: int = DEFAULT_PORT,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.port = DEFAULT_PORT if port == LEGACY_PORT else port
        self.discovery_port = discovery_port
        self.enabled = True
        self.show_intermediate_events = False
        self.phones: dict[str, PhoneRecord] = {}
        self._code = ""
        self._code_expires_at = 0.0
        self._code_attempts = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        with self._lock:
            migrated = self._apply_payload(payload)
        if migrated:
            self.save()

    def _apply_payload(self, payload: dict) -> bool:
        migrated = False
        enabled = payload.get("enabled")
        if isinstance(enabled, bool):
            self.enabled = enabled
        show_intermediate_events = payload.get("show_intermediate_events")
        if isinstance(show_intermediate_events, bool):
            self.show_intermediate_events = show_intermediate_events
        port = payload.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            if port in {LEGACY_PORT, LEGACY_COMPANION_PORT}:
                self.port = DEFAULT_PORT
                migrated = True
            else:
                self.port = port

        discovery_port = payload.get("discovery_port")
        if isinstance(discovery_port, int) and 1 <= discovery_port <= 65535:
            if discovery_port == LEGACY_DISCOVERY_PORT:
                self.discovery_port = DEFAULT_DISCOVERY_PORT
                migrated = True
            else:
                self.discovery_port = discovery_port

        phones = payload.get("phones")
        refreshed: dict[str, PhoneRecord] = {}
        if isinstance(phones, list):
            for item in phones:
                record = self._record_from(item)
                if record is not None:
                    refreshed[record.phone_id] = record
        # Merge, never replace: an older tray instance can otherwise save its
        # empty in-memory list over a phone that was paired by a newer one.
        for phone_id, record in refreshed.items():
            current = self.phones.get(phone_id)
            if current is None or record.last_seen >= current.last_seen:
                self.phones[phone_id] = record

        self._code = ""
        self._code_expires_at = 0.0
        pairing = payload.get("pairing")
        if isinstance(pairing, dict):
            code = pairing.get("code")
            expires = pairing.get("expires_at")
            if (
                isinstance(code, str)
                and len(code) == 6
                and code.isdigit()
                and isinstance(expires, (int, float))
                and float(expires) > time.time()
            ):
                self._code = code
                self._code_expires_at = float(expires)
        return migrated

    def _reload_locked(self) -> None:
        """Refresh disk state before a mutation.

        A stale tray instance must not overwrite a newer phone record with the
        empty phone list it loaded at startup.
        """
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        # Treat the merge as reload-only. If an older-format file temporarily
        # has no phone while this instance still holds a newer in-memory
        # record, retain the in-memory copy instead of letting the next save
        # erase it. Newer records on disk still win by last_seen below.
        before = dict(self.phones)
        migrated = self._apply_payload(payload)
        for phone_id, record in before.items():
            current = self.phones.get(phone_id)
            if current is None or record.last_seen > current.last_seen:
                self.phones[phone_id] = record
        phones = payload.get("phones")
        disk_phone_ids: set[str] = set()
        if isinstance(phones, list):
            disk_phone_ids = {
                record.phone_id
                for record in (self._record_from(item) for item in phones)
                if record is not None
            }
        recovered = any(phone_id not in disk_phone_ids for phone_id in before)
        if migrated or recovered:
            self.save()

    @staticmethod
    def _record_from(value: object) -> PhoneRecord | None:
        if not isinstance(value, dict):
            return None
        phone_id = value.get("phone_id")
        token_hash = value.get("token_hash")
        if not isinstance(phone_id, str) or not phone_id:
            return None
        if not isinstance(token_hash, str) or len(token_hash) != 64:
            return None
        name = value.get("name")
        created = value.get("created_at")
        last_seen = value.get("last_seen")
        enabled = value.get('enabled', list(PHONE_CAPABILITIES))
        if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
            enabled = []
        client_id = value.get('client_id')
        return PhoneRecord(
            phone_id=phone_id,
            name=name if isinstance(name, str) else "",
            token_hash=token_hash,
            created_at=float(created) if isinstance(created, (int, float)) else 0.0,
            last_seen=float(last_seen) if isinstance(last_seen, (int, float)) else 0.0,
            enabled=set(enabled) & PHONE_CAPABILITIES,
            client_id=client_id.strip()[:128] if isinstance(client_id, str) else "",
        )

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = {
                "version": 1,
                "enabled": self.enabled,
                "show_intermediate_events": self.show_intermediate_events,
                "port": self.port,
                "discovery_port": self.discovery_port,
                "phones": [
                    {
                        "phone_id": record.phone_id,
                        "name": record.name,
                        "token_hash": record.token_hash,
                        "created_at": record.created_at,
                        "last_seen": record.last_seen,
                        "enabled": sorted(record.capabilities()),
                        "client_id": record.client_id,
                    }
                    for record in self.phones.values()
                ],
                "pairing": {
                    "code": self._code if self._code_expires_at > time.time() else "",
                    "expires_at": self._code_expires_at if self._code_expires_at > time.time() else 0,
                },
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            return

    # -------------------------------------------------------------- settings
    def negotiate(self, phone_id: str, supported: list[str]) -> list[str]:
        if not isinstance(supported, list) or any(not isinstance(item, str) for item in supported):
            raise ValueError('capabilities')
        with self._lock:
            self._reload_locked()
            record = self.phones.get(phone_id)
            if record is None:
                raise ValueError('phone_id')
            record.enabled = set(supported) & PHONE_CAPABILITIES
            self.save()
            return sorted(record.capabilities())

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._reload_locked()
            self.enabled = bool(enabled)
            self.save()

    def set_show_intermediate_events(self, enabled: bool) -> None:
        with self._lock:
            self._reload_locked()
            self.show_intermediate_events = bool(enabled)
            self.save()

    def set_port(self, port: int) -> None:
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port")
        if port == LEGACY_PORT:
            raise ValueError("port_reserved")
        with self._lock:
            self._reload_locked()
            self.port = port
            self.save()

    def set_discovery_port(self, port: int) -> None:
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port")
        if port == LEGACY_PORT:
            raise ValueError("port_reserved")
        with self._lock:
            self._reload_locked()
            self.discovery_port = port
            self.save()

    def snapshot(self) -> dict:
        with self._lock:
            # The management page can be opened by a process that started
            # before another instance paired a phone. Re-read first so the
            # displayed list matches the durable file instead of stale
            # in-memory state.
            self._reload_locked()
            return {
                "enabled": self.enabled,
                "show_intermediate_events": self.show_intermediate_events,
                "port": self.port,
                "discovery_port": self.discovery_port,
                "phones": [record.public() for record in self.phones.values()],
                "pairing_expires_at": self._code_expires_at if self.enabled else 0,
            }

    # --------------------------------------------------------------- pairing
    def pairing_code(self, regenerate: bool = False) -> dict:
        with self._lock:
            self._reload_locked()
            now = time.time()
            if regenerate or not self._code or self._code_expires_at <= now:
                self._code = _new_code()
                self._code_expires_at = now + PAIRING_CODE_TTL_SECONDS
                self._code_attempts = 0
            code = self._code
            expires_at = self._code_expires_at
            self.save()
        return {"code": code, "expires_at": expires_at}

    def claim(self, code: str, name: str, client_id: str = "") -> dict | None:
        if not isinstance(code, str) or not code:
            return None
        with self._lock:
            self._reload_locked()
            now = time.time()
            if not self._code or self._code_expires_at <= now:
                return None
            if self._code_attempts >= PAIRING_MAX_ATTEMPTS:
                return None
            if not hmac.compare_digest(self._code, code.strip()):
                self._code_attempts += 1
                if self._code_attempts >= PAIRING_MAX_ATTEMPTS:
                    self._code = ""
                    self._code_expires_at = 0.0
                self.save()
                return None
            stable_id = client_id.strip()[:128] if isinstance(client_id, str) else ""
            existing = None
            if stable_id:
                matches = [
                    record for record in self.phones.values()
                    if record.client_id == stable_id
                ]
                if not matches and stable_id in self.phones:
                    # Migration path for a phone that already stored a bridge
                    # assigned phone_id before installation_id existed.
                    matches = [self.phones[stable_id]]
                if matches:
                    matches.sort(
                        key=lambda record: (record.last_seen, record.created_at),
                        reverse=True,
                    )
                    existing = matches[0]
                    for duplicate in matches[1:]:
                        self.phones.pop(duplicate.phone_id, None)
                    existing.client_id = stable_id

            if existing is None and len(self.phones) >= MAX_PHONES:
                # Prefer evicting an unclaimed legacy record over one that can
                # still be recognized by its installation identity.
                oldest = min(
                    self.phones.values(),
                    key=lambda item: (bool(item.client_id), item.created_at),
                )
                self.phones.pop(oldest.phone_id, None)

            if existing is None:
                phone_id = "phone-" + secrets.token_hex(6)
                record = PhoneRecord(
                    phone_id=phone_id,
                    name=(name or "").strip()[:48],
                    token_hash="",
                    created_at=now,
                    client_id=stable_id,
                )
            else:
                phone_id = existing.phone_id
                record = existing
                record.name = (name or "").strip()[:48]

            token = _new_token()
            record.token_hash = _hash_token(token)
            record.last_seen = now
            self.phones[phone_id] = record
            self._code = ""
            self._code_expires_at = 0.0
            self._code_attempts = 0
            self.save()
        return {"phone_id": phone_id, "token": token, "name": record.name}

    def authorize(self, token: str) -> PhoneRecord | None:
        if not isinstance(token, str) or not token:
            return None
        digest = _hash_token(token)
        with self._lock:
            self._reload_locked()
            for record in self.phones.values():
                if hmac.compare_digest(record.token_hash, digest):
                    return record
        return None

    def touch(self, phone_id: str) -> None:
        with self._lock:
            self._reload_locked()
            record = self.phones.get(phone_id)
            if record is None:
                return
            if time.time() - record.last_seen < 60:
                return
            record.last_seen = time.time()
            self.save()

    def remove_phone(self, phone_id: str) -> bool:
        with self._lock:
            self._reload_locked()
            removed = self.phones.pop(phone_id, None) is not None
            if removed:
                self.save()
            return removed
