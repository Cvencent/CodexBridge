"""Persistent pairing state for the phone companion API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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
_REPLACE_ATTEMPTS = 8
# How long a phone may keep using a token it already holds after the same
# installation pairs again. Re-pairing mints a new secret, and a phone whose
# pair response was lost mid-write would otherwise be locked out for good:
# every request answers 401, and it has no way to know it should re-pair.
TOKEN_GRACE_SECONDS = 60 * 60.0
# A restart can drop several pair responses in a row, so the allowance keeps
# the last few secrets rather than only the previous one.
TOKEN_GRACE_HISTORY = 4
PHONE_CAPABILITIES = {
    'status',
    'tasks',
    'control',
    'approval',
    'queue',
    'attachment',
}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prior_tokens_from(value: object) -> tuple:
    """Read the grace ledger, ignoring anything malformed."""
    if not isinstance(value, list):
        return ()
    items = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        digest, expires = entry
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        if not isinstance(expires, (int, float)):
            continue
        items.append((digest, float(expires)))
    return tuple(items[-TOKEN_GRACE_HISTORY:])


def _accept_prior(record, digest: str, now: float) -> bool:
    """True when ``digest`` is an allowed token the phone may still hold."""
    for allowed, expires in record.prior_tokens:
        if expires > now and hmac.compare_digest(allowed, digest):
            return True
    return False


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_token() -> str:
    raw = base64.urlsafe_b64encode(secrets.token_bytes(PHONE_TOKEN_BYTES))
    return PHONE_TOKEN_PREFIX + raw.decode("ascii").rstrip("=")


def _new_instance_id() -> str:
    """Return a stable, IP-independent identity for this Bridge install."""

    return secrets.token_hex(16)


class CompanionStoreError(RuntimeError):
    """Raised when the pairing file cannot be read or safely replaced."""


def _replace_with_retry(source: str, destination: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


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
    # Tokens this phone may still present, oldest first, each with the moment
    # its allowance expires. The whole ledger is cleared as soon as the newest
    # token is used, which is the proof the phone actually received it.
    prior_tokens: tuple = ()

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
        registry: object | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.registry = registry
        self.port = DEFAULT_PORT if port == LEGACY_PORT else port
        self.discovery_port = discovery_port
        self.instance_id = _new_instance_id()
        self.enabled = True
        self.show_intermediate_events = False
        self.phones: dict[str, PhoneRecord] = {}
        self._code = ""
        self._code_expires_at = 0.0
        self._code_attempts = 0
        self._lock = threading.RLock()
        self._loaded_from_disk = self.path is None or not self.path.exists()
        # File stamp of the last load/save, so an unchanged store is skipped
        # instead of re-read and re-projected on every request.
        self._reload_marker: tuple[int, int] | None = None
        self.load_warning = ""

    @property
    def device_id(self) -> str:
        """Stable public device code derived from the instance identity."""

        return "bridge-" + self.instance_id[:12]

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            with self._lock:
                recovered = self._recover_from_registry()
                self._loaded_from_disk = True
            # Persist the newly generated instance identity immediately; it is
            # what phones use to keep a pairing when the LAN address changes.
            self.save()
            return
        except UnicodeDecodeError as error:
            return self._recover_corrupt_store(
                "companion store is not UTF-8: %s" % error
            )
        except OSError as error:
            raise CompanionStoreError(
                "cannot read companion store %s: %s" % (self.path, error)
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            return self._recover_corrupt_store(
                "companion store is not valid JSON: %s" % error
            )
        if not isinstance(payload, dict):
            return self._recover_corrupt_store(
                "companion store root must be an object"
            )
        with self._lock:
            migrated = self._apply_payload(payload)
            recovered = self._recover_from_registry()
            self._sync_all_to_registry()
            self._loaded_from_disk = True
        if migrated or recovered:
            self.save()
        else:
            self._remember_marker()

    def _recover_corrupt_store(self, warning: str) -> None:
        backup = self._quarantine_broken_file()
        self.load_warning = warning
        if backup is not None:
            self.load_warning += "; backup=%s" % backup
        with self._lock:
            self._recover_from_registry()
            self._loaded_from_disk = True
        self.save()

    def _remember_marker(self) -> None:
        """Record the current file stamp so the next reload can skip parsing."""
        if self.path is None:
            self._reload_marker = None
            return
        try:
            stat = self.path.stat()
        except OSError:
            self._reload_marker = None
            return
        self._reload_marker = (int(stat.st_mtime_ns), int(stat.st_size))

    def _quarantine_broken_file(self) -> Path | None:
        if self.path is None or not self.path.exists():
            return None
        backup = self.path.with_suffix(self.path.suffix + ".corrupt.bak")
        index = 1
        while backup.exists():
            backup = self.path.with_suffix(self.path.suffix + ".corrupt.%d.bak" % index)
            index += 1
        try:
            self.path.replace(backup)
        except OSError:
            return None
        self._loaded_from_disk = True
        return backup

    def _apply_payload(self, payload: dict) -> bool:
        migrated = False
        enabled = payload.get("enabled")
        if isinstance(enabled, bool):
            self.enabled = enabled
        show_intermediate_events = payload.get("show_intermediate_events")
        if isinstance(show_intermediate_events, bool):
            self.show_intermediate_events = show_intermediate_events
        instance_id = payload.get("bridge_instance_id") or payload.get("instance_id")
        if (
            isinstance(instance_id, str)
            and len(instance_id) == 32
            and all(ch in "0123456789abcdef" for ch in instance_id.lower())
        ):
            self.instance_id = instance_id.lower()
        else:
            migrated = True
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

        The refresh is skipped when the file stamp is unchanged. The phone API
        answers several authenticated requests per second, and re-reading the
        store plus re-projecting it into the device registry -- a full
        ``devices.json`` rewrite with ``fsync`` -- on every one of them pinned
        a whole CPU core on a single idle phone.
        """
        if self.path is None:
            return
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._loaded_from_disk = True
            self._reload_marker = None
            return
        except OSError as error:
            raise CompanionStoreError(
                "cannot reload companion store %s: %s" % (self.path, error)
            ) from error
        marker = (int(stat.st_mtime_ns), int(stat.st_size))
        if marker == self._reload_marker:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._loaded_from_disk = True
            self._reload_marker = None
            return
        except (OSError, UnicodeDecodeError) as error:
            raise CompanionStoreError(
                "cannot reload companion store %s: %s" % (self.path, error)
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            self._quarantine_broken_file()
            raise CompanionStoreError(
                "companion store is not valid JSON: %s" % error
            ) from error
        if not isinstance(payload, dict):
            self._quarantine_broken_file()
            raise CompanionStoreError("companion store root must be an object")
        # Treat the merge as reload-only. If an older-format file temporarily
        # has no phone while this instance still holds a newer in-memory
        # record, retain the in-memory copy instead of letting the next save
        # erase it. Newer records on disk still win by last_seen below.
        before_ids = set(self.phones)
        before = dict(self.phones)
        migrated = self._apply_payload(payload)
        self._recover_from_registry()
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
        learned = set(self.phones) != before_ids
        self._loaded_from_disk = True
        self._reload_marker = marker
        # Project into the device registry only when this reload actually
        # learned something. The writer already synced its own claim, so an
        # unchanged file has nothing new to publish -- and re-publishing it on
        # every authenticated request is what pinned a CPU core.
        if migrated or recovered or learned:
            self._sync_all_to_registry()
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
        prior_tokens = _prior_tokens_from(value.get('prior_tokens'))
        return PhoneRecord(
            phone_id=phone_id,
            name=name if isinstance(name, str) else "",
            token_hash=token_hash,
            created_at=float(created) if isinstance(created, (int, float)) else 0.0,
            last_seen=float(last_seen) if isinstance(last_seen, (int, float)) else 0.0,
            enabled=set(enabled) & PHONE_CAPABILITIES,
            client_id=client_id.strip()[:128] if isinstance(client_id, str) else "",
            prior_tokens=prior_tokens,
        )

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            if not self._loaded_from_disk:
                raise CompanionStoreError(
                    "refusing to overwrite a companion store that was not loaded"
                )
            payload = {
                "version": 1,
                "enabled": self.enabled,
                "show_intermediate_events": self.show_intermediate_events,
                "port": self.port,
                "discovery_port": self.discovery_port,
                "bridge_instance_id": self.instance_id,
                "phones": [
                    {
                        "phone_id": record.phone_id,
                        "name": record.name,
                        "token_hash": record.token_hash,
                        "created_at": record.created_at,
                        "last_seen": record.last_seen,
                        "enabled": sorted(record.capabilities()),
                        "client_id": record.client_id,
                        "prior_tokens": [list(item) for item in record.prior_tokens],
                    }
                    for record in self.phones.values()
                ],
                "pairing": {
                    "code": self._code if self._code_expires_at > time.time() else "",
                    "expires_at": self._code_expires_at if self._code_expires_at > time.time() else 0,
                },
            }
        temporary_name = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary_name = str(temporary)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary_name, self.path)
            temporary_name = ""
            self._remember_marker()
        except OSError as error:
            raise CompanionStoreError(
                "cannot save companion store %s: %s" % (self.path, error)
            ) from error
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except OSError:
                    pass

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
            self._sync_phone_to_registry(record)
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
            phones = self._registry_phone_views()
            if phones is None:
                phones = [record.public() for record in self.phones.values()]
            return {
                "enabled": self.enabled,
                "show_intermediate_events": self.show_intermediate_events,
                "port": self.port,
                "discovery_port": self.discovery_port,
                "bridge_instance_id": self.instance_id,
                "device_id": self.device_id,
                "phones": phones,
                "pairing_expires_at": self._code_expires_at if self.enabled else 0,
            }

    def _registry_phone_views(self) -> list[dict] | None:
        if self.registry is None:
            return None
        try:
            rows = self.registry.public_all()
        except (AttributeError, TypeError, ValueError):
            return None
        phones = []
        for row in rows:
            if row.get("kind") != "phone":
                continue
            phones.append(
                {
                    "phone_id": row.get("device_id"),
                    "name": row.get("name") or "",
                    "last_seen": row.get("last_seen") or 0.0,
                    "enabled": row.get("enabled") or [],
                    "kind": "phone",
                    "transport": "companion_http",
                    "registration_state": row.get("registration_state"),
                    "has_token": bool(row.get("has_token")),
                }
            )
        return phones

    def _sync_phone_to_registry(self, record: PhoneRecord) -> None:
        if self.registry is None:
            return
        self.registry.upsert_phone(
            record.phone_id,
            name=record.name,
            token_hash=record.token_hash,
            supported=sorted(record.capabilities()),
            enabled=sorted(record.capabilities()),
            last_seen=record.last_seen,
            client_id=record.client_id,
        )
        self.registry.save()

    def _sync_all_to_registry(self) -> None:
        if self.registry is None:
            return
        for record in self.phones.values():
            self._sync_phone_to_registry(record)

    def _recover_from_registry(self) -> bool:
        if self.registry is None:
            return False
        try:
            rows = self.registry.all()
        except (AttributeError, TypeError, ValueError):
            return False
        recovered = False
        for row in rows:
            if str(getattr(row, "kind", "")) != "phone":
                continue
            phone_id = str(getattr(row, "device_id", "") or "")
            token_hash = str(getattr(row, "token_hash", "") or "")
            if not phone_id or len(token_hash) != 64 or phone_id in self.phones:
                continue
            last_seen = getattr(row, "last_seen", 0) or 0.0
            self.phones[phone_id] = PhoneRecord(
                phone_id=phone_id,
                name=str(getattr(row, "name", "") or ""),
                token_hash=token_hash,
                created_at=float(last_seen),
                last_seen=float(last_seen),
                enabled=set(getattr(row, "enabled", set()) or set()) & PHONE_CAPABILITIES,
                client_id=str(getattr(row, "client_id", "") or ""),
            )
            recovered = True
        return recovered

    def _remove_from_registry(self, phone_id: str) -> None:
        if self.registry is None:
            return
        if self.registry.remove(phone_id):
            self.registry.save()

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
                        self._remove_from_registry(duplicate.phone_id)
                    existing.client_id = stable_id

            if existing is None and len(self.phones) >= MAX_PHONES:
                # Prefer evicting an unclaimed legacy record over one that can
                # still be recognized by its installation identity.
                oldest = min(
                    self.phones.values(),
                    key=lambda item: (bool(item.client_id), item.created_at),
                )
                self.phones.pop(oldest.phone_id, None)
                self._remove_from_registry(oldest.phone_id)

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
            # The phone may still hold the token from its previous pair. If
            # this response never arrived -- a dropped socket during a bridge
            # restart is the common case -- the phone would keep presenting a
            # secret this store no longer knows and every request would answer
            # 401 with no way back. Honour the secrets it may be holding until
            # one of them proves it reached the phone.
            if record.token_hash:
                ledger = list(record.prior_tokens)
                ledger.append((record.token_hash, now + TOKEN_GRACE_SECONDS))
                ledger = [item for item in ledger if item[1] > now]
                record.prior_tokens = tuple(ledger[-TOKEN_GRACE_HISTORY:])
            record.token_hash = _hash_token(token)
            record.last_seen = now
            self.phones[phone_id] = record
            self._sync_phone_to_registry(record)
            self._code = ""
            self._code_expires_at = 0.0
            self._code_attempts = 0
            self.save()
        return {"phone_id": phone_id, "token": token, "name": record.name}

    def authorize(self, token: str) -> PhoneRecord | None:
        if not isinstance(token, str) or not token:
            return None
        digest = _hash_token(token)
        now = time.time()
        with self._lock:
            self._reload_locked()
            for record in self.phones.values():
                if hmac.compare_digest(record.token_hash, digest):
                    # The newest secret works, so the phone has it: every
                    # allowance for an older one is now pointless.
                    if record.prior_tokens:
                        record.prior_tokens = ()
                        self.save()
                    return record
                if _accept_prior(record, digest, now):
                    return record
            self._expire_grace(now)
        return None

    def _expire_grace(self, now: float) -> None:
        """Drop allowances that ran out while nobody presented them."""
        stale = []
        for record in self.phones.values():
            if not record.prior_tokens:
                continue
            kept = tuple(item for item in record.prior_tokens if item[1] > now)
            if len(kept) != len(record.prior_tokens):
                record.prior_tokens = kept
                stale.append(record)
        if stale:
            self.save()

    def touch(self, phone_id: str) -> None:
        with self._lock:
            self._reload_locked()
            record = self.phones.get(phone_id)
            if record is None:
                return
            if time.time() - record.last_seen < 60:
                return
            record.last_seen = time.time()
            self._sync_phone_to_registry(record)
            self.save()

    def get_phone(self, phone_id: str) -> PhoneRecord | None:
        """Return one live phone record, refreshing from disk first."""
        if not isinstance(phone_id, str) or not phone_id:
            return None
        with self._lock:
            self._reload_locked()
            return self.phones.get(phone_id)

    def remove_phone(self, phone_id: str) -> bool:
        """Remove one paired phone and its registry projection."""
        with self._lock:
            self._reload_locked()
            removed = self.phones.pop(phone_id, None) is not None
            if removed:
                self._remove_from_registry(phone_id)
                self.save()
            return removed
