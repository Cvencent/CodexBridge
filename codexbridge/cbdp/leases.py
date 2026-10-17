from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace


class LeaseConflictError(ValueError):
    pass


@dataclass(frozen=True)
class Lease:
    thread_id: str
    owner: str
    generation: int
    expires_at: float


class LeaseManager:
    def __init__(self, clock=None, lease_seconds=300):
        if float(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        self._clock = clock or time.monotonic
        self.lease_seconds = float(lease_seconds)
        self._leases: dict[str, Lease] = {}
        self._generations: dict[str, int] = {}
        self._lock = threading.RLock()

    def _now(self):
        return float(self._clock())

    def _current(self, thread_id):
        lease = self._leases.get(str(thread_id))
        if lease is None:
            return None
        if lease.expires_at <= self._now():
            self._leases.pop(str(thread_id), None)
            return None
        return lease

    def claim(self, thread_id, owner, force=False):
        thread_id = str(thread_id)
        owner = str(owner)
        if not thread_id or not owner:
            raise ValueError("thread_id and owner must not be empty")
        with self._lock:
            current = self._current(thread_id)
            if current is not None and current.owner != owner and not force:
                raise LeaseConflictError("WRITER_CONFLICT")
            generation = self._generations.get(thread_id, 0) + 1
            lease = Lease(
                thread_id=thread_id,
                owner=owner,
                generation=generation,
                expires_at=self._now() + self.lease_seconds,
            )
            self._generations[thread_id] = generation
            self._leases[thread_id] = lease
            return lease

    def refresh(self, thread_id, owner, generation):
        with self._lock:
            lease = self._current(thread_id)
            if (
                lease is None
                or lease.owner != str(owner)
                or lease.generation != int(generation)
            ):
                return None
            refreshed = replace(lease, expires_at=self._now() + self.lease_seconds)
            self._leases[str(thread_id)] = refreshed
            return refreshed

    def release(self, thread_id, owner, generation):
        with self._lock:
            lease = self._current(thread_id)
            if (
                lease is None
                or lease.owner != str(owner)
                or lease.generation != int(generation)
            ):
                return False
            self._leases.pop(str(thread_id), None)
            return True

    def get(self, thread_id):
        with self._lock:
            return self._current(thread_id)

    def has_writer(self, thread_id):
        return self.get(thread_id) is not None