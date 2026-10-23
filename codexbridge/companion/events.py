"""Transport-independent event journal with atomic replay subscriptions."""

from collections import deque
from copy import deepcopy
import queue
import threading
import time
import uuid


class EventStream:
    def __init__(self, capacity=1000, subscriber_capacity=300):
        if capacity < 1 or subscriber_capacity < 1:
            raise ValueError('capacity')
        self._history = deque(maxlen=capacity)
        self._subscriber_capacity = subscriber_capacity
        self._subscribers = set()
        self._lock = threading.Lock()
        self._epoch = uuid.uuid4().hex
        self._sequence = 0

    def _resync(self):
        return {'type': 'resync_required', 'reason': 'event_history_unavailable',
                'event_id': f'{self._epoch}:{self._sequence}'}

    def subscribe(self, after=''):
        with self._lock:
            target = queue.Queue(maxsize=max(self._subscriber_capacity, len(self._history) + 1))
            if after:
                records = list(self._history)
                index = next((i for i, event in enumerate(records)
                              if event['event_id'] == after), None)
                if index is None:
                    target.put_nowait(self._resync())
                else:
                    for event in records[index + 1:]:
                        target.put_nowait(deepcopy(event))
            self._subscribers.add(target)
            return target

    def unsubscribe(self, target):
        with self._lock:
            self._subscribers.discard(target)

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload):
        with self._lock:
            self._sequence += 1
            event = deepcopy(payload)
            event['event_id'] = f'{self._epoch}:{self._sequence}'
            event['server_time'] = time.time()
            self._history.append(event)
            for target in self._subscribers:
                try:
                    target.put_nowait(deepcopy(event))
                except queue.Full:
                    while True:
                        try:
                            target.get_nowait()
                        except queue.Empty:
                            break
                    target.put_nowait(self._resync())
            return deepcopy(event)
