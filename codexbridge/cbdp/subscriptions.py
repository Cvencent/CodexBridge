from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubscriptionResult:
    topic_id: int
    scope: int
    qos: int
    cursor: int
    recoverable: bool
    replay: list[object] = field(default_factory=list)


@dataclass
class _Subscription:
    topic_id: int
    scope: int
    qos: int
    cursor: int = 0
    pending: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class _Pending:
    global_seq: int
    qos: int


class SubscriptionLimitError(ValueError):
    pass


class SubscriptionManager:
    def __init__(self, event_spine, default_queue_depth=32, default_max_subscriptions=32):
        self.event_spine = event_spine
        self.default_queue_depth = int(default_queue_depth)
        self.default_max_subscriptions = int(default_max_subscriptions)
        self._subscriptions = {}
        self._queue_depth = {}
        self._max_subscriptions = {}
        self._lock = threading.RLock()

    def set_queue_depth(self, device_id, depth):
        if int(depth) <= 0:
            raise ValueError("depth must be positive")
        with self._lock:
            self._queue_depth[str(device_id)] = int(depth)

    def set_max_subscriptions(self, device_id, maximum):
        if int(maximum) <= 0:
            raise ValueError("maximum must be positive")
        with self._lock:
            self._max_subscriptions[str(device_id)] = int(maximum)

    def subscribe(self, device_id, topic_id, scope, qos, cursor=0):
        device_id = str(device_id)
        topic_id = int(topic_id)
        scope = int(scope)
        qos = int(qos)
        cursor = max(0, int(cursor))
        with self._lock:
            key = (device_id, topic_id, scope)
            existing = self._subscriptions.get(key)
            if existing is None:
                count = sum(1 for item in self._subscriptions if item[0] == device_id)
                if count >= self._max_subscriptions.get(device_id, self.default_max_subscriptions):
                    raise SubscriptionLimitError("BUSY")
                existing = _Subscription(topic_id, scope, qos)
                self._subscriptions[key] = existing
            else:
                existing.qos = qos
            recoverable = self.event_spine.cursor_is_recoverable(cursor)
            if not recoverable:
                existing.cursor = 0
                existing.pending = []
                return SubscriptionResult(topic_id, scope, qos, 0, False, [])
            replay = self.event_spine.events_since(
                cursor,
                topic_id=topic_id,
                limit=max(1, self._queue_depth.get(device_id, self.default_queue_depth)),
            )
            next_cursor = cursor
            for event in replay:
                next_cursor = max(next_cursor, int(event.global_seq))
            existing.cursor = next_cursor
            existing.pending = [
                _Pending(int(event.global_seq), int(event.qos)) for event in replay
            ]
            return SubscriptionResult(topic_id, scope, qos, next_cursor, True, replay)

    def unsubscribe(self, device_id, topic_id, scope):
        with self._lock:
            return self._subscriptions.pop((str(device_id), int(topic_id), int(scope)), None) is not None

    def cursor(self, device_id, topic_id, scope):
        with self._lock:
            item = self._subscriptions.get((str(device_id), int(topic_id), int(scope)))
            return 0 if item is None else int(item.cursor)

    def ack(self, device_id, topic_id, scope, cursor):
        cursor = max(0, int(cursor))
        with self._lock:
            item = self._subscriptions.get((str(device_id), int(topic_id), int(scope)))
            if item is None:
                return False
            item.cursor = max(item.cursor, cursor)
            item.pending = [entry for entry in item.pending if entry.global_seq > item.cursor]
            return True

    def deliver(self, topic_id, scope, global_seq, qos):
        global_seq = int(global_seq)
        with self._lock:
            for key, item in self._subscriptions.items():
                if item.topic_id != int(topic_id) or item.scope != int(scope):
                    continue
                if int(qos) == 1 and item.pending and item.pending[-1].qos == 1:
                    item.pending[-1] = _Pending(global_seq, 1)
                    continue
                item.pending.append(_Pending(global_seq, int(qos)))
                depth = self._queue_depth.get(key[0], self.default_queue_depth)
                if len(item.pending) > depth:
                    item.pending = item.pending[-depth:]

    def pending_cursors(self, device_id, topic_id, scope):
        with self._lock:
            item = self._subscriptions.get((str(device_id), int(topic_id), int(scope)))
            return [] if item is None else [entry.global_seq for entry in item.pending]

    def has_live(self, topic_id):
        with self._lock:
            return any(item.topic_id == int(topic_id) and item.scope == 2 for item in self._subscriptions.values())

    def has_subscription(self, topic_id, scope):
        with self._lock:
            return any(
                item.topic_id == int(topic_id) and item.scope == int(scope)
                for item in self._subscriptions.values()
            )

    def has_subscription_for(self, device_id, topic_id, scope):
        """Whether one specific device subscribed to this topic and scope.

        ``has_subscription`` answers a global question used by tests and
        diagnostics; delivery must be per device, otherwise one subscriber
        makes the gateway emit a frame to every session.
        """

        with self._lock:
            return (
                str(device_id),
                int(topic_id),
                int(scope),
            ) in self._subscriptions

    def drop_device(self, device_id):
        with self._lock:
            for key in [key for key in self._subscriptions if key[0] == str(device_id)]:
                self._subscriptions.pop(key, None)
