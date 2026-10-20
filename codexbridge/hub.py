from __future__ import annotations

import threading
import queue
import json
import time
import uuid
from typing import Any

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.messages_store import (
    KIND_CAPABILITY,
    MessageRecord,
    MessageStore,
    is_fresh,
    is_in_flight,
)
from codexbridge.cbdp.constants import (
    QOS_CRITICAL,
    QOS_STATE,
    TAG_APPLIED_POLICY,
    TAG_BODY,
    TAG_BODY_REF,
    TAG_CREATED_AT_MS,
    TAG_DEVICE_ID,
    TAG_DEVICE_REF,
    TAG_EFFECTIVE_CAPS,
    TAG_KIND,
    TAG_LAST_SEEN_MS,
    TAG_MAX_TEXT_CHARS,
    TAG_MESSAGE_ID,
    TAG_MESSAGE_REF,
    TAG_POLICY_REASON,
    TAG_POLICY_VERSION,
    TAG_PROFILE,
    TAG_READ_AT_MS,
    TAG_REVISION,
    TAG_SENDER,
    TAG_STATUS,
    TAG_SUMMARY,
    TAG_THREAD_ID,
    TAG_THREAD_REF,
    TAG_TITLE,
    TAG_TURN_ID,
    TAG_TURN_REF,
    TAG_UPDATED_AT_MS,
    TAG_RUNNING_COUNT,
    TAG_COMPLETED_UNREAD,
    TAG_FAILED_UNREAD,
)

from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.events import event_scope
from codexbridge.cbdp.projection import (
    EVENT_MESSAGE_APPENDED,
    EVENT_MESSAGE_UPDATED,
)
from codexbridge.cbdp.refs import (
    SYSTEM_BRIDGE_TOPIC,
    SYSTEM_DEVICES_TOPIC,
    SYSTEM_RESYNC_TOPIC,
    SYSTEM_THREAD_SUMMARIES_TOPIC,
)
from codexbridge.cbdp.tlv import encode_tlvs, i64, u8, u16, u32, u64, utf8
from codexbridge.protocol.capabilities import effective
from codexbridge.protocol.messages import clip_text, clip_utf8, decode, wire_messages
from codexbridge.protocol.commands import CommandError
from codexbridge.read_state import ReadStateStore

EVENT_CODEX_STATE = 0x0704


DEVICE_EVENT_CAPABILITY: dict[str, str] = {
    "voice_command": "voice",
    "approval_decision": "approval",
    "cancel_task": "tasks",
    "record_start": "audio",
    "audio_chunk": "audio",
    "record_stop": "audio",
    "record_cancel": "audio",
    "codex_catalog_request": "tasks",
    "codex_project_select": "tasks",
    "codex_thread_select": "tasks",
    "codex_thread_new": "tasks",
    "codex_model_select": "tasks",
    # Both of these belong to the same catalog conversation as the events above,
    # and the cancel in particular can wait on the same Codex call. Running them
    # inline lets them overtake a queued select, so the device would see its own
    # replies out of order.
    "codex_catalog_cancel": "tasks",
    "codex_selection_confirm": "tasks",
    # Generic state pull. Devices that render a task summary (for example a
    # home screen pet) ask for a fresh snapshot instead of having the host
    # push one on every collector cycle; the reply is bounded and cheap.
    "codex_state_request": "status",
}

# These events reach into the Codex app server, where one call can wait for its
# whole 15 s timeout, and several can arrive back to back. The device session
# thread that calls ``handle_device_event`` is also the thread reading that
# device's audio chunks and answering its heartbeats, so running this work there
# stalls the radio: the device sees "未连接", recordings arrive truncated or not
# at all, and the link only recovers minutes later. Blocking work therefore runs
# on a per-device worker thread, which also keeps one slow device from delaying
# another. Ordering within a device is preserved (FIFO per device).
BLOCKING_DEVICE_EVENTS = frozenset(
    {
        "command",
        "approval_decision",
        "cancel_task",
        "voice_command",
        "cbdp_deferred",
        "_replay_state",
    }
    | {name for name, cap in DEVICE_EVENT_CAPABILITY.items() if cap == "tasks"}
)
# A device that keeps talking while Codex is stuck must not grow its queue
# without bound. The refusal is explicit so the device can say something useful
# instead of waiting in silence. Chunk reassembly deliberately stays inline: an
# in-flight multi-part command is cheap until its last part arrives, and counting
# every part against this limit would reject legitimate requests.
WORKER_QUEUE_LIMIT = 24

# A device renders whatever text the host sends; it must never need a table of
# its own for host error codes. Codes stay machine-readable for logic, the
# human sentence travels with them, so one protocol works for any client.
ERROR_TEXT: dict[str, str] = {
    "busy_retry": "\u7535\u8111\u7aef\u8fd8\u5728\u5904\u7406\u4e0a\u4e00\u6761\u8bf7\u6c42\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5",
    "capability_not_enabled": "\u8fd9\u4e2a\u529f\u80fd\u6ca1\u6709\u5728\u7535\u8111\u7aef\u5f00\u542f",
    "codex_catalog_unavailable": "\u7535\u8111\u7aef\u6ca1\u6709\u53ef\u7528\u7684 Codex\uff0c\u8bf7\u786e\u8ba4 Codex \u5df2\u6253\u5f00",
    "codex_catalog_failed": "\u7535\u8111\u7aef\u8bfb\u53d6 Codex\u5217\u8868\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5",
    "codex_unavailable": "\u7535\u8111\u7aef\u7684 Codex \u6682\u65f6\u4e0d\u53ef\u7528",
    "invalid_request_id": "\u8bf7\u6c42\u7f16\u53f7\u65e0\u6548\uff0c\u8bf7\u91cd\u8bd5",
    "invalid_command_chunk": "\u6307\u4ee4\u6570\u636e\u635f\u574f\uff0c\u8bf7\u91cd\u8bd5",
    "invalid_request": "\u8bf7\u6c42\u683c\u5f0f\u4e0d\u6b63\u786e",
    "invalid_event_cursor": "\u8ba2\u9605\u4f4d\u7f6e\u65e0\u6548\uff0c\u8bf7\u91cd\u65b0\u8fde\u63a5",
}

# Byte ceilings for one flat `codex_status_snapshot` line. Together they leave
# comfortable room inside MAX_LINE_BYTES for the field names themselves.
SNAPSHOT_ID_BYTES = 64
SNAPSHOT_STATUS_BYTES = 24
SNAPSHOT_ACTION_BYTES = 240

# Device events whose answer comes from the Codex catalog service. Their
# "no catalog service" error is cheap enough to return synchronously.
CATALOG_BACKED_EVENTS = frozenset(
    {
        "codex_catalog_request",
        "codex_project_select",
        "codex_thread_select",
        "codex_thread_new",
        "codex_model_select",
        "codex_catalog_cancel",
        "codex_selection_confirm",
    }
)


class BridgeHub:
    def __init__(
        self,
        registry: DeviceRegistry,
        store: MessageStore | None = None,
        speech=None,
        codex_catalog=None,
        commands=None,
        read_state: ReadStateStore | None = None,
        event_spine=None,
        projection=None,
        subscriptions=None,
        references=None,
    ):
        self.registry = registry
        self.store = store or MessageStore()
        self.read_state = read_state or ReadStateStore(None)
        self.read_state.add_listener(self.handle_cross_client_read)
        self.companion_events: Any = None
        # Which Codex thread each device is looking at, learned from the catalog
        # selection it confirmed (and echoed back on later requests). Live records
        # for a thread nobody selected must not drive a screen that is waiting on
        # its own task.
        self._device_thread: dict[str, str] = {}
        self.speech = speech
        self.codex_catalog = codex_catalog
        self.commands = commands
        self.event_spine = event_spine
        self.projection = projection
        self.subscriptions = subscriptions
        self.references = references
        self._sessions: dict[str, Any] = {}
        self._event_subscriptions: dict[str, Any] = {}
        self._incoming_commands: dict[str, dict] = {}
        self._lock = threading.RLock()
        # Last aggregate counts published as system.codex_state, so a steady
        # state does not append a duplicate event on every publish tick.
        self._last_codex_state: tuple[int, int, int] | None = None
        # Per-device deferred work: queue, worker handle, and depth counters.
        self._work: dict[str, "queue.Queue[tuple[str, Any]]"] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._depth: dict[str, int] = {}
        self._cbdp_message_events: dict[str, tuple[str, int]] = {}
        self._cbdp_gateways: dict[str, Any] = {}

    def _cbdp_ref(self, kind: str, external_id: str) -> int:
        if self.references is None or not external_id:
            return 0
        return int(self.references.ref_for(kind, external_id))

    def _cbdp_topic(self, thread_id: str = "") -> int:
        if self.references is None or not thread_id:
            return SYSTEM_BRIDGE_TOPIC
        return int(self.references.topic_for(thread_id))

    def _append_event(self, topic_id: int, event_type: int, qos: int, payload: dict):
        if self.event_spine is None:
            return None
        event = self.event_spine.append(topic_id, event_type, qos, payload)
        if self.projection is not None:
            self.projection.apply(event)
        for gateway in list(self._cbdp_gateways.values()):
            deliver = getattr(gateway, "deliver_event", None)
            if deliver is not None:
                deliver(event)
        return event

    def register_cbdp_gateway(self, device_id: str, gateway) -> None:
        with self._lock:
            self._cbdp_gateways[str(device_id)] = gateway

    def unregister_cbdp_gateway(self, device_id: str) -> None:
        with self._lock:
            self._cbdp_gateways.pop(str(device_id), None)

    def _append_message_event(self, record: MessageRecord, appended: bool) -> None:
        data = record.data if isinstance(record.data, dict) else {}
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        turn_id = str(task.get("turn_id") or data.get("turn_id") or "")
        sender = {
            "user_text": 0,
            "assistant_text": 1,
            "commentary": 1,
            "tool": 2,
            "file_change": 2,
            "approval": 3,
            "system": 3,
            "status": 3,
            "task": 3,
            "result": 1,
        }.get(str(record.kind or ""), 3)
        created_at_ms = int(float(record.created_at or 0) * 1000)
        payload = {
            TAG_THREAD_REF: u32(self._cbdp_ref("thread", record.thread_id)),
            TAG_TURN_REF: u32(self._cbdp_ref("turn", turn_id)),
            TAG_MESSAGE_REF: u32(self._cbdp_ref("message", record.message_id)),
            TAG_KIND: u8(
                {
                    "user_text": 0,
                    "assistant_text": 1,
                    "commentary": 2,
                    "tool": 3,
                    "file_change": 4,
                    "error": 5,
                    "approval": 6,
                    "system": 7,
                    "status": 7,
                    "task": 2,
                    "result": 1,
                }.get(str(record.kind or ""), 7)
            ),
            TAG_STATUS: u16(
                {
                    "pending": 0,
                    "streaming": 1,
                    "completed": 2,
                    "failed": 3,
                    "interrupted": 4,
                    "working": 1,
                    "waiting_user": 0,
                    "queued": 0,
                    "starting": 0,
                    "done": 2,
                }.get(str(record.status or ""), 0)
            ),
            TAG_SENDER: u8(sender),
            TAG_SUMMARY: utf8(str(record.title or record.body or "")),
            TAG_BODY_REF: utf8(f"content:{record.message_id}"),
            TAG_BODY: utf8(str(record.body or "")),
            TAG_CREATED_AT_MS: i64(created_at_ms),
            TAG_UPDATED_AT_MS: i64(created_at_ms),
        }
        if record.title:
            payload[TAG_TITLE] = utf8(str(record.title))
        if record.thread_id:
            payload[TAG_THREAD_ID] = utf8(str(record.thread_id))
        if turn_id:
            payload[TAG_TURN_ID] = utf8(turn_id)
        if record.message_id:
            payload[TAG_MESSAGE_ID] = utf8(str(record.message_id))
        return self._append_event(
            self._cbdp_topic(record.thread_id),
            EVENT_MESSAGE_APPENDED if appended else EVENT_MESSAGE_UPDATED,
            QOS_CRITICAL if appended else QOS_STATE,
            payload,
        )

    def set_codex_catalog(self, codex_catalog: Any) -> None:
        with self._lock:
            self.codex_catalog = codex_catalog

    def _record_is_unread(self, record: MessageRecord) -> bool:
        if self.read_state.is_message_read(record.message_id):
            return False
        if (
            record.thread_id
            and record.created_at > 0
            and self.read_state.is_thread_read(
                record.thread_id, int(record.created_at)
            )
        ):
            return False
        return True

    def counts(self) -> dict[str, int]:
        """Aggregate running tasks and globally unread terminal results.

        ``completed_unread`` and ``failed_unread`` are the canonical fields for
        new clients.  ``done`` and ``failed`` remain as compatibility aliases
        with the same unread-only meaning. Only records the collector is still
        refreshing count as running, so a stopped collector cannot leave the
        badge lit forever.
        """
        running = 0
        completed_unread = 0
        failed_unread = 0
        now = time.time()
        with self._lock:
            records = self.store.records()
        for record in records:
            if record.kind == "task":
                if (
                    record.status in {"working", "waiting_user", "queued"}
                    and is_in_flight(record)
                    and is_fresh(record, now)
                ):
                    running += 1
                continue
            if record.kind != "result" or not self._record_is_unread(record):
                continue
            if record.status == "failed":
                failed_unread += 1
            elif record.status == "completed":
                completed_unread += 1
        return {
            "running": running,
            "completed_unread": completed_unread,
            "failed_unread": failed_unread,
            "done": completed_unread,
            "failed": failed_unread,
        }

    def note_device_thread(self, device_id: str, thread_id: str) -> None:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return
        self._device_thread[device_id] = thread_id

    def followed_thread(self, device_id: str) -> str:
        return self._device_thread.get(device_id, "")

    def active_state(self, device_id: str) -> dict:
        """Active thread/turn snapshot for active_state.read."""

        with self._lock:
            thread_id = self._device_thread.get(device_id, "")
        turn_id = ""
        if thread_id:
            try:
                snapshot = self._status_snapshot(device_id) or {}
                if str(snapshot.get("thread_id") or "") == thread_id:
                    turn_id = str(snapshot.get("turn_id") or "")
            except Exception:
                turn_id = ""
        return {"thread_id": thread_id, "turn_id": turn_id}

    def set_active_state(self, device_id: str, thread_id: str, turn_id: str = "") -> dict:
        """Record a device-reported active thread for active_state.set."""

        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError("thread_id")
        self.note_device_thread(device_id, thread_id)
        return {"thread_id": thread_id, "turn_id": str(turn_id or "")}

    def _message_visible(self, device_id: str, payload: dict) -> bool:
        """Can this record change what the device is showing right now?

        Aggregate counters are always shared -- the home screen legitimately
        reports how much work the computer is doing. A single thread's progress
        text is not: without this rule any other Codex session on the desktop
        could cut into a device that is waiting on its own task.
        """
        kind = str(payload.get("kind") or "")
        if kind not in {"status", "task", "result"}:
            return True
        followed = self.followed_thread(device_id)
        if followed:
            return str(payload.get("thread_id") or "") == followed
        # A client that has never picked a conversation keeps the aggregate
        # feed: older firmware, the phone, and any future reader all learn
        # about work this way until they say otherwise. Records the collector
        # marks as one thread's private progress stay out of that feed.
        data = payload.get("data")
        if isinstance(data, dict) and data.get("aggregate") is False:
            return False
        return True

    def _send_codex_state(self, device_id: str, event: dict) -> None:
        asked = event.get("thread_id")
        if isinstance(asked, str) and asked.strip():
            self.note_device_thread(device_id, asked)
        payload = {"type": "codex_state", **self.counts()}
        request_id = event.get("request_id")
        if "request_id" in event:
            payload["request_id"] = request_id
        self._send(device_id, payload)
        # CBDP sessions drop the legacy dict, so answer the explicit pull with
        # a summary event as well.
        with self._lock:
            session = self._sessions.get(device_id)
        if session is not None:
            self._send_codex_state_event(session)
        snapshot = self._status_snapshot(device_id)
        if snapshot is not None and "request_id" in event:
            snapshot["request_id"] = request_id
        if snapshot is not None:
            self._send(device_id, snapshot)

    @staticmethod
    def _codex_state_event_payload(counts: dict[str, int]) -> dict[int, bytes]:
        return {
            TAG_RUNNING_COUNT: u16(max(0, int(counts.get("running") or 0))),
            TAG_COMPLETED_UNREAD: u16(
                max(0, int(counts.get("completed_unread") or 0))
            ),
            TAG_FAILED_UNREAD: u16(max(0, int(counts.get("failed_unread") or 0))),
        }

    def _codex_state_tuple(self) -> tuple[int, int, int]:
        counts = self.counts()
        return (
            int(counts.get("running") or 0),
            int(counts.get("completed_unread") or 0),
            int(counts.get("failed_unread") or 0),
        )

    @staticmethod
    def _event_counts(event) -> tuple[int, int, int] | None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return None
        try:
            return (
                int.from_bytes(payload.get(TAG_RUNNING_COUNT, b"\x00\x00"), "big"),
                int.from_bytes(payload.get(TAG_COMPLETED_UNREAD, b"\x00\x00"), "big"),
                int.from_bytes(payload.get(TAG_FAILED_UNREAD, b"\x00\x00"), "big"),
            )
        except (TypeError, ValueError):
            return None

    def _send_codex_state_event(self, session: Any) -> None:
        """Push the aggregate counts to one attached session.

        CBDP sessions receive a real delta; legacy sessions keep using the
        ``codex_state`` dict sent by ``_send_codex_state``.
        """

        if self.event_spine is None:
            return
        gateway = getattr(session, "gateway", None)
        send_event = getattr(session, "send_event", None)
        if gateway is None or not callable(send_event):
            return
        expected = self._codex_state_tuple()
        event = None
        try:
            event = self.event_spine.latest_event(
                lambda item: (
                    item.event_type == EVENT_CODEX_STATE
                    and self._event_counts(item) == expected
                )
            )
        except Exception:
            event = None
        if event is None:
            try:
                event = self.event_spine.append(
                    SYSTEM_THREAD_SUMMARIES_TOPIC,
                    EVENT_CODEX_STATE,
                    QOS_STATE,
                    self._codex_state_event_payload(self.counts()),
                )
            except Exception:
                return
        try:
            send_event(event)
        except (OSError, RuntimeError):
            return

    def _publish_codex_state(self) -> None:
        """Append one aggregate-count event for CBDP summary subscribers.

        The legacy ``codex_state`` dict has no CBDP translation, so CBDP
        sessions silently dropped it and a device home screen never learned
        the running count. This publishes the same numbers as a committed
        summary-scope event instead.
        """

        if self.event_spine is None:
            return
        expected = self._codex_state_tuple()
        if expected == self._last_codex_state:
            return
        try:
            self._append_event(
                SYSTEM_THREAD_SUMMARIES_TOPIC,
                EVENT_CODEX_STATE,
                QOS_STATE,
                self._codex_state_event_payload(self.counts()),
            )
            self._last_codex_state = expected
        except Exception:
            return

    def publish(self, record: MessageRecord) -> None:
        with self._lock:
            previous = self.store.get(record.message_id)
            unchanged = (
                previous is not None
                and previous.fingerprint == record.fingerprint
            )
            self.store.add(record)
            if unchanged:
                # Same words on the same screen: the periodic snapshot must not
                # turn into periodic radio traffic and device wake-ups.
                return
            event = None
            if self.event_spine is not None:
                event = self._append_message_event(
                    record, appended=previous is None
                )
            capability = KIND_CAPABILITY.get(record.kind)
            if capability is None:
                return
            payload = {"type": "message", **self._message_payload(record)}
            self._broadcast_messages(payload, capability, event=event)
            if record.kind in {"task", "result"}:
                # The badge counts are aggregate state, not a chat message.
                # Push them as soon as a task enters or leaves the terminal
                # set so every connected device updates at the same time.
                self._broadcast_codex_state()

    def _device_profile(self, device_id: str, session: Any = None) -> int:
        record = self.registry.get(device_id)
        profile = getattr(record, "profile", None) if record is not None else None
        if profile is None:
            profile = getattr(session, "profile", None)
        try:
            return max(0, min(2, int(profile or 0)))
        except (TypeError, ValueError):
            return 0

    def _device_last_seen_ms(self, device_id: str) -> int:
        record = self.registry.get(device_id)
        seen = getattr(record, "last_seen", None) if record is not None else None
        try:
            return int(float(seen or 0) * 1000)
        except (TypeError, ValueError):
            return 0

    def attach(self, device_id: str, session: Any) -> None:
        gateway = getattr(session, "gateway", None)
        if gateway is not None:
            self.register_cbdp_gateway(device_id, gateway)
        profile = self._device_profile(device_id, session)
        with self._lock:
            self._sessions[device_id] = session
        self._append_event(
            SYSTEM_DEVICES_TOPIC,
            0x0601,
            QOS_STATE,
            {
                TAG_DEVICE_ID: utf8(str(device_id)),
                TAG_DEVICE_REF: u32(self._cbdp_ref("device", device_id)),
                TAG_PROFILE: u8(profile),
                TAG_LAST_SEEN_MS: i64(self._device_last_seen_ms(device_id)),
            },
        )

    def detach(self, device_id: str) -> None:
        self.unregister_cbdp_gateway(device_id)
        with self._lock:
            session = self._sessions.pop(device_id, None)
            self._incoming_commands.pop(device_id, None)
            # A device that went away has no one left to answer, so its queued
            # work must not keep wait_idle() (or the depth counter) occupied.
            # The worker drains its queue and exits on its own; sends to a
            # detached session are already no-ops.
            self._depth.pop(device_id, None)
            self._unsubscribe_events(device_id)
        profile = self._device_profile(device_id, session)
        self._append_event(
            SYSTEM_DEVICES_TOPIC,
            0x0602,
            QOS_STATE,
            {
                TAG_DEVICE_ID: utf8(str(device_id)),
                TAG_DEVICE_REF: u32(self._cbdp_ref("device", device_id)),
                TAG_PROFILE: u8(profile),
                TAG_LAST_SEEN_MS: i64(self._device_last_seen_ms(device_id)),
            },
        )

    def _unsubscribe_events(self, device_id: str) -> None:
        target = self._event_subscriptions.pop(device_id, None)
        if target is not None and self.commands is not None:
            self.commands.service.unsubscribe(target)

    def poll_events(self) -> None:
        with self._lock:
            subscriptions = list(self._event_subscriptions.items())
        for device_id, target in subscriptions:
            for _ in range(50):
                try:
                    event = target.get_nowait()
                except queue.Empty:
                    break
                kind = event.get('type', '')
                capability = ('queue' if kind.startswith('queue_') else
                              'approval' if kind.startswith('approval_') else
                              'status' if kind.startswith('turn_') or kind == 'resync_required'
                              else 'tasks')
                if capability in self.capabilities_for(device_id):
                    self._send(device_id, {'type': 'device_event', 'event': event})

    def handle_device_event(self, device_id: str, event: dict) -> None:
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type in BLOCKING_DEVICE_EVENTS:
            # Cheap validation stays inline so a device that is not allowed to
            # ask still gets its error immediately, exactly as before. Only the
            # part that can wait on Codex is deferred.
            if self.registry.get(device_id) is None:
                return
            capability = DEVICE_EVENT_CAPABILITY.get(event_type)
            if capability is not None and capability not in self.capabilities_for(
                device_id
            ):
                self._send_error(device_id, "capability_not_enabled")
                return
            # Availability of the Codex service is a cheap attribute check, and a
            # device must learn "not available now" immediately rather than after
            # a queue delay -- that is the difference between an instant message
            # and a device that looks hung.
            if event_type in CATALOG_BACKED_EVENTS or event_type == "voice_command":
                with self._lock:
                    service = self.codex_catalog
                if service is None:
                    self._send_error(device_id, "codex_catalog_unavailable")
                    return
            if event_type == "voice_command" and not str(
                event.get("text") or ""
            ).strip():
                self._send(device_id, {"type": "error", "text": "没有听到内容，请再说一次"})
                return
            self._enqueue(device_id, event)
            return
        self._dispatch_device_event(device_id, event)

    # ------------------------------------------------------------------
    # deferred (Codex-blocking) device work
    # ------------------------------------------------------------------
    def _enqueue(self, device_id: str, event: dict) -> None:
        kind = event.get("type")
        payload = None if kind == "_replay_state" else event
        with self._lock:
            depth = self._depth.get(device_id, 0)
            if depth >= WORKER_QUEUE_LIMIT:
                self._send(
                    device_id,
                    {
                        "type": "error",
                        "code": "busy_retry",
                        "text": ERROR_TEXT["busy_retry"],
                        "request_id": (event or {}).get("request_id"),
                    },
                )
                return
            self._depth[device_id] = depth + 1
            target = self._work.get(device_id)
            if target is None:
                target = queue.Queue()
                self._work[device_id] = target
            # The queue must already hold this event before the lock is
            # released. A worker exits under the same lock only while its
            # queue is empty, so putting here closes the window where it could
            # retire first and leave the event on an orphaned queue.
            target.put((kind, payload))
            if self._workers.get(device_id) is None:
                worker = threading.Thread(
                    target=self._worker_loop,
                    args=(device_id, target),
                    name="device-events-" + device_id[-8:],
                    daemon=True,
                )
                self._workers[device_id] = worker
                worker.start()

    def replay_state_async(self, device_id: str) -> None:
        """Queue the attach-time replay instead of running it on the session
        thread. ``replay_state`` primes the Codex catalog, and that happens the
        moment a device connects -- which is exactly when the device is waiting
        to hear ``bridge_ready`` back."""
        self._enqueue(device_id, {"type": "_replay_state"})

    def defer_blocking_event(self, device_id: str, event: dict) -> None:
        """Run one Codex-blocking callback on the device worker thread.

        CBDP transactions arrive on the socket reader thread. Routing their
        upstream work here keeps the reader draining audio frames and
        heartbeats while Codex is busy.
        """

        self._enqueue(device_id, event)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Drain queued device work. Used by shutdown and by tests."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            with self._lock:
                if not self._depth:
                    return True
            time.sleep(0.01)
        with self._lock:
            return not self._depth

    def _worker_loop(self, device_id: str, work: "queue.Queue[tuple[str, Any]]") -> None:
        while True:
            try:
                kind, payload = work.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if work.empty():
                        self._workers.pop(device_id, None)
                        self._work.pop(device_id, None)
                        return
                continue
            try:
                if kind == "_replay_state":
                    self.replay_state(device_id)
                else:
                    self._dispatch_device_event(device_id, payload)
            except Exception:  # noqa: BLE001 - one bad event must not kill the worker
                pass
            finally:
                with self._lock:
                    left = self._depth.get(device_id, 1) - 1
                    if left > 0:
                        self._depth[device_id] = left
                    else:
                        self._depth.pop(device_id, None)

    def _dispatch_device_event(self, device_id: str, event: dict) -> None:
        """The event pipeline itself. May block on Codex; callers decide where."""
        event_type = event.get("type")
        if event_type == 'command_request_chunk':
            self._handle_command_chunk(device_id, event)
            return
        if event_type in {'events_subscribe', 'events_unsubscribe'}:
            with self._lock:
                self._unsubscribe_events(device_id)
                if event_type == 'events_subscribe' and self.commands is not None:
                    if not self.capabilities_for(device_id) & {'status', 'tasks', 'approval', 'queue'}:
                        self._send_error(device_id, 'capability_not_enabled')
                        return
                    after = event.get('after', '')
                    if not isinstance(after, str) or len(after) > 128:
                        self._send_error(device_id, 'invalid_event_cursor')
                        return
                    self._event_subscriptions[device_id] = self.commands.service.subscribe(after)
            return
        if event_type == 'protocol_request':
            request_id = event.get('request_id')
            if self.registry.get(device_id) is None:
                return
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                self._send_error(device_id, 'invalid_request_id')
                return
            if self.commands is None:
                self._send_error(device_id, 'codex_unavailable')
                return
            self._send(device_id, {'type': 'protocol_catalog', 'request_id': request_id,
                **self.commands.catalog(), 'enabled': sorted(self.capabilities_for(device_id))})
            return
        if event_type == 'command':
            self._handle_command(device_id, event)
            return
        if event_type == 'cbdp_deferred':
            callback = event.get('callback')
            if callable(callback):
                callback()
            return
        if event_type == "read_ack":
            self._handle_read_ack(device_id, event)
            return
        if event_type in {'cancel_task', 'approval_decision'}:
            capability = 'tasks' if event_type == 'cancel_task' else 'approval'
            if capability not in self.capabilities_for(device_id):
                self._send_error(device_id, "capability_not_enabled")
                return
            if self.commands is None:
                self._send_error(device_id, 'codex_unavailable')
                return
            request_id = str(event.get('request_id') or uuid.uuid4())
            try:
                if event_type == 'cancel_task':
                    method = 'turn.interrupt'
                    params = {'thread_id': event.get('thread_id'), 'turn_id': event.get('turn_id', '')}
                    capabilities = self.capabilities_for(device_id) | {'control'}
                else:
                    method = 'approval.resolve'
                    params = {'approval_id': int(event.get('approval_id', event.get('request_id'))),
                              'decision': {'approve': 'accept', 'deny': 'decline'}.get(event.get('decision'), event.get('decision'))}
                    capabilities = self.capabilities_for(device_id)
                result = self.commands.execute(device_id, capabilities,
                    {'request_id': request_id, 'method': method, 'params': params})
            except (ValueError, TypeError) as error:
                result = {'type': 'command_result', 'request_id': request_id,
                          'ok': False, 'error': str(error) if isinstance(error, CommandError) else 'invalid_params'}
            except Exception:
                result = {'type': 'command_result', 'request_id': request_id,
                          'ok': False, 'error': 'codex_unavailable'}
            self._send(device_id, result)
            return

        capability = DEVICE_EVENT_CAPABILITY.get(event_type)
        if capability is None:
            return
        if capability not in self.capabilities_for(device_id):
            self._send_error(device_id, "capability_not_enabled")
            return
        if event_type in CATALOG_BACKED_EVENTS:
            self._handle_codex_catalog(device_id, event)
            return
        if event_type == "codex_state_request":
            self._send_codex_state(device_id, event)
            return
        if event_type == "voice_command":
            self._handle_voice_command(device_id, event)
            return
        if capability == "audio" and self.speech is not None:
            for message in self.speech.handle_event(device_id, event):
                self._send(device_id, message)

    def _handle_voice_command(self, device_id: str, event: dict) -> None:
        """Deliver a confirmed transcript to Codex instead of dropping it.

        A device that shows "正在发送到电脑端" must eventually see a turn, an
        error, or silence only when the host truly cannot reach Codex.
        """
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(device_id, {"type": "error", "text": "没有听到内容，请再说一次"})
            return
        service = self.codex_catalog
        if service is None or not hasattr(service, "handle_voice_command"):
            self._send_error(device_id, "codex_catalog_unavailable")
            return
        try:
            messages = service.handle_voice_command(device_id, text)
        except Exception:
            self._send_error(device_id, "codex_catalog_failed")
            return
        # The voice path may have started a thread of its own; that is the
        # conversation this device is now waiting on.
        if hasattr(service, "active_thread"):
            self.note_device_thread(device_id, service.active_thread(device_id))
        for message in messages or []:
            if isinstance(message, dict):
                self._send(device_id, message)

    def _handle_codex_catalog(self, device_id: str, event: dict) -> None:
        with self._lock:
            service = self.codex_catalog
        if service is None:
            self._send_error(device_id, "codex_catalog_unavailable")
            return
        try:
            messages = service.handle_device_event(device_id, event)
        except Exception:
            self._send_error(device_id, "codex_catalog_failed")
            return
        for message in messages or []:
            if isinstance(message, dict) and message.get("type") == "codex_selected":
                self.note_device_thread(device_id, message.get("thread_id"))
        request_id = event.get("request_id")
        has_request_id = "request_id" in event
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            payload = dict(message)
            if has_request_id:
                payload["request_id"] = request_id
            self._send(device_id, payload)

    def _handle_command(self, device_id: str, event: dict) -> None:
        request_id = event.get('request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            self._send_error(device_id, 'invalid_request_id')
            return
        if self.commands is None:
            self._send_error(device_id, 'codex_unavailable')
            return
        try:
            result = self.commands.execute(device_id, self.capabilities_for(device_id), event)
        except CommandError as error:
            result = {'type': 'command_result', 'request_id': request_id,
                      'ok': False, 'error': str(error)}
        except Exception:
            result = {'type': 'command_result', 'request_id': request_id,
                      'ok': False, 'error': 'codex_unavailable'}
        self._send(device_id, result)

    def _handle_command_chunk(self, device_id: str, event: dict) -> None:
        if self.registry.get(device_id) is None:
            return
        request_id, seq, total, data = (event.get(key) for key in ('request_id', 'seq', 'total', 'data'))
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or
                type(seq) is not int or type(total) is not int or not 1 <= total <= 512 or
                not 0 <= seq < total or not isinstance(data, str) or len(data.encode('utf-8')) > 600):
            self._send_error(device_id, 'invalid_command_chunk')
            return
        with self._lock:
            pending = self._incoming_commands.get(device_id)
            if pending is not None and time.monotonic() - pending['time'] > 30:
                pending = None
                self._incoming_commands.pop(device_id, None)
            if seq == 0:
                pending = {'id': request_id, 'total': total, 'parts': [], 'bytes': 0, 'time': time.monotonic()}
                self._incoming_commands[device_id] = pending
            if (pending is None or pending['id'] != request_id or pending['total'] != total or
                    len(pending['parts']) != seq or pending['bytes'] + len(data.encode('utf-8')) > 65536):
                self._incoming_commands.pop(device_id, None)
                self._send_error(device_id, 'invalid_command_chunk')
                return
            pending['parts'].append(data)
            pending['bytes'] += len(data.encode('utf-8'))
            if seq + 1 != total:
                return
            self._incoming_commands.pop(device_id, None)
        try:
            request = json.loads(''.join(pending['parts']))
            if not isinstance(request, dict) or request.get('request_id') != request_id or request.get('type') != 'command':
                raise ValueError('invalid_request')
        except ValueError:
            self._send_error(device_id, 'invalid_request')
            return
        # Reassembly is cheap and stayed on the session thread; executing the
        # command is a Codex round-trip, so hand that to the worker.
        self._enqueue(device_id, request)

    def poll_speech(self) -> None:
        if self.speech is None:
            return
        for device_id, message in self.speech.poll():
            self._send(device_id, message)

    def capabilities_for(self, device_id: str) -> set[str]:
        record = self.registry.get(device_id)
        if record is None:
            return set()
        return effective(record.supported, record.enabled)

    def has_capability(self, device_id: str, capability: str) -> bool:
        """Capability check that understands legacy aliases.

        ``quota`` is the legacy name for CBDP bit 42 (``account``). Streams
        that were historically gated on the legacy name must not disappear
        just because a CBDP device declares the canonical wire name.
        """

        capabilities = self.capabilities_for(device_id)
        if capability in capabilities:
            return True
        if capability == "quota":
            return "account" in capabilities
        return False

    def note_policy_changed(self, device_id: str) -> None:
        """Publish an explicit device-policy transition after registry updates."""

        if self.event_spine is None:
            return
        record = self.registry.get(device_id)
        capabilities = sorted(effective(
            record.supported if record is not None else [],
            record.enabled if record is not None else [],
        ))
        policy_version = int(getattr(self, "policy_version", 1) or 1)
        self._append_event(
            SYSTEM_DEVICES_TOPIC,
            0x0603,
            QOS_CRITICAL,
            lambda revision: {
                TAG_DEVICE_ID: utf8(str(device_id)),
                TAG_DEVICE_REF: u32(self._cbdp_ref("device", device_id)),
                TAG_EFFECTIVE_CAPS: u64(capability_bitmap(capabilities)),
                TAG_APPLIED_POLICY: encode_tlvs(
                    {
                        TAG_POLICY_VERSION: u32(policy_version),
                        TAG_MAX_TEXT_CHARS: u32(
                            int(getattr(record, "max_text_chars", 0) or 0)
                            if record is not None
                            else 0
                        ),
                    }.items()
                ),
                TAG_POLICY_VERSION: u32(policy_version),
                TAG_POLICY_REASON: utf8("registry_update"),
                TAG_REVISION: u64(revision),
            },
        )
    def state_for(self, device_id: str) -> dict:
        with self._lock:
            capabilities = self.capabilities_for(device_id)
            pending = [
                record
                for record in self.store.pending_for(device_id, capabilities)
                if (
                    # Live state is not a notification anyone can consume: the
                    # phone reading "task is working" must not stop a device
                    # that reconnects later from learning the task is alive.
                    (is_in_flight(record) and is_fresh(record))
                    or not self.read_state.is_message_read(record.message_id)
                )
                and not (
                    not is_in_flight(record)
                    and record.thread_id
                    and record.created_at > 0
                    and self.read_state.is_thread_read(
                        record.thread_id, int(record.created_at)
                    )
                )
            ]
            visible = self.store.pending_for(
                device_id,
                capabilities,
                unread_only=False,
            )
            read_state = (
                [
                    {
                        "message_id": record.message_id,
                        "read_by": sorted(record.read_by),
                    }
                    for record in visible
                    if record.read_by
                ]
                if "read_ack" in capabilities
                else []
            )
            return {
                "messages": [self._message_payload(record) for record in pending],
                "read_state": read_state,
            }

    def push_capabilities(self, device_id: str) -> None:
        capabilities = sorted(self.capabilities_for(device_id))
        self._send(
            device_id,
            {
                "type": "capabilities_update",
                "enabled": capabilities,
            },
        )

    def replay_state(self, device_id: str) -> None:
        with self._lock:
            state = self.state_for(device_id)
        with self._lock:
            session = self._sessions.get(device_id)
        if getattr(session, "is_cbdp", False):
            for message in state["messages"]:
                event = self._cbdp_event_for_message(
                    str(message.get("message_id") or "")
                )
                if event is not None:
                    self._deliver_cbdp_event(session, event)
            for read_state in state["read_state"]:
                event = self._cbdp_event_for_message(
                    str(read_state.get("message_id") or ""), read=True
                )
                if event is not None:
                    self._deliver_cbdp_event(session, event)
        else:
            for message in state["messages"]:
                self._send(device_id, {"type": "message", **message})
            for read_state in state["read_state"]:
                self._send(device_id, {"type": "read_state", **read_state})

        # Devices that render a task summary (for example the home-screen pet)
        # pull codex_state on their own schedule. A short-lived session can
        # reconnect and ask before the host finishes attaching, though, so give
        # every capable device one fresh snapshot at attach time. It is a
        # bounded, capability-gated reply, and it keeps the generic protocol
        # correct for pull-only clients without the host hardcoding them.
        if "status" in self.capabilities_for(device_id):
            self._send_codex_state(device_id, {})
            if session is not None:
                # CBDP sessions cannot translate the legacy dict above; push
                # the aggregate counts as a real summary event so a reconnect
                # immediately refreshes the home screen.
                self._send_codex_state_event(session)

        if "queue" in self.capabilities_for(device_id):
            queue_snapshots = getattr(self.commands.service, "queue_snapshots", None) if self.commands else None
            if callable(queue_snapshots):
                for snapshot in queue_snapshots():
                    self._send(device_id, {"type": "queue_snapshot", **snapshot})

        self._prime_codex_catalog(device_id)

    def _prime_codex_catalog(self, device_id: str) -> None:
        if "tasks" not in self.capabilities_for(device_id):
            return
        with self._lock:
            service = self.codex_catalog
        if service is None:
            return
        try:
            messages = service.handle_device_event(
                device_id,
                {"type": "codex_catalog_request"},
            )
        except Exception:
            return
        for message in messages or []:
            if isinstance(message, dict):
                self._send(device_id, message)

    def _handle_read_ack(self, device_id: str, event: dict) -> None:
        with self._lock:
            if "read_ack" not in self.capabilities_for(device_id):
                self._send_error(device_id, "capability_not_enabled")
                return
            message_id = event.get("message_id")
            if not isinstance(message_id, str):
                return
            record = self.store.mark_read(message_id, device_id)
            if record is None:
                return
            # The first global marker sends the fan-out.  Suppress the thread
            # notification here so one read cannot produce duplicate updates.
            self.read_state.mark_message(record.message_id, source=device_id)
            self._append_read_event(record.message_id, record.thread_id, device_id)
            if record.thread_id and record.created_at > 0:
                self.read_state.mark_thread(
                    record.thread_id,
                    int(record.created_at),
                    source=device_id,
                    notify=False,
                )

    def _broadcast_read_state(
        self, payload: dict, exclude: str = "", event=None
    ) -> None:
        with self._lock:
            targets = [
                session
                for device_id, session in self._sessions.items()
                if device_id != exclude
                and "read_ack" in self.capabilities_for(device_id)
            ]
        for session in targets:
            if getattr(session, "is_cbdp", False):
                if event is not None:
                    self._deliver_cbdp_event(session, event)
                continue
            self._send_to(session, payload)

    def _broadcast_codex_state(self) -> None:
        payload = {"type": "codex_state", **self.counts()}
        with self._lock:
            targets = [
                session
                for device_id, session in self._sessions.items()
                if "status" in self.capabilities_for(device_id)
            ]
        for session in targets:
            self._send_to(session, payload)
        # CBDP sessions cannot translate the legacy dict above, so also publish
        # it through the event spine where subscriptions can deliver it.
        self._publish_codex_state()

    def _retract_companion_badge(self, thread_id: str) -> None:
        companion = self.companion_events
        if not thread_id or companion is None or not hasattr(companion, "publish"):
            return
        try:
            companion.publish(
                {
                    "type": "thread_status",
                    "thread_id": thread_id,
                    "status": "idle",
                    "active_flags": [],
                }
            )
        except Exception:  # noqa: BLE001 - the read already happened
            pass

    def _read_by_for(self, message_id: str, source: str) -> list[str]:
        with self._lock:
            record = self.store.get(message_id)
            if record is not None:
                return sorted(record.read_by)
        return [source] if source else []

    def _thread_message_ids(self, thread_id: str, completed_at: int) -> list[str]:
        if not thread_id:
            return []
        with self._lock:
            return [
                record.message_id
                for record in self.store.records()
                if record.thread_id == thread_id
                and (completed_at <= 0 or int(record.created_at or 0) <= completed_at)
            ]

    def handle_cross_client_read(self, event: dict) -> None:
        """Fan a globally shared read marker out to every connected client."""
        if not isinstance(event, dict):
            return
        message_id = str(event.get("message_id") or "")
        thread_id = str(event.get("thread_id") or "")
        source = str(event.get("source") or "")
        if message_id:
            event = self._append_read_event(message_id, thread_id, source)
            self._broadcast_read_state(
                {
                    "type": "read_state",
                    "message_id": message_id,
                    "read_by": self._read_by_for(message_id, source),
                    "read_at": time.time(),
                },
                exclude=source,
                event=event,
            )
            self._retract_companion_badge(thread_id)
            self._broadcast_codex_state()
            return

        completed_at = int(event.get("completed_at") or 0)
        for covered_id in self._thread_message_ids(thread_id, completed_at):
            with self._lock:
                record = self.store.get(covered_id)
                read_by = (
                    sorted(record.read_by)
                    if record is not None
                    else ([source] if source else [])
                )
            if record is not None:
                self.read_state.mark_message(
                    covered_id,
                    source=source,
                    notify=False,
                )
                event = self._append_read_event(covered_id, thread_id, source)
            else:
                event = None
            self._broadcast_read_state(
                {
                    "type": "read_state",
                    "message_id": covered_id,
                    "read_by": read_by,
                    "read_at": time.time(),
                },
                exclude=source,
                event=event,
            )
        self._retract_companion_badge(thread_id)
        self._broadcast_codex_state()

    def _append_read_event(self, message_id: str, thread_id: str, source: str) -> None:
        if not message_id or self.event_spine is None:
            return
        record = self.store.get(message_id)
        thread_id = str(thread_id or (record.thread_id if record is not None else ""))
        payload = {
            TAG_THREAD_REF: u32(self._cbdp_ref("thread", thread_id)),
            TAG_MESSAGE_REF: u32(self._cbdp_ref("message", message_id)),
            TAG_DEVICE_REF: u32(self._cbdp_ref("device", source)),
            TAG_READ_AT_MS: i64(int(time.time() * 1000)),
        }
        return self._append_event(
            self._cbdp_topic(thread_id),
            0x0303,
            QOS_STATE,
            payload,
        )

    def _broadcast(self, payload: dict, capability: str) -> None:
        with self._lock:
            for device_id, session in self._sessions.items():
                if self.has_capability(device_id, capability):
                    self._send_to(
                        session,
                        self._apply_budget(payload, self.budget_for(device_id)),
                    )

    def _broadcast_messages(
        self, payload: dict, capability: str, event=None
    ) -> None:
        """Fan one record out, but only to devices allowed to see it.

        A CBDP session receives the committed Event Spine event as a DELTA
        carrying event_id/global_seq/topic_id/topic_seq/revision and a real
        transport seq. The legacy compatibility translator must never
        synthesise DELTA frames for a CBDP device.
        """
        with self._lock:
            targets = [
                (device_id, session)
                for device_id, session in list(self._sessions.items())
                if self.has_capability(device_id, capability)
                and self._message_visible(device_id, payload)
            ]
        for device_id, session in targets:
            if getattr(session, "is_cbdp", False):
                # A CBDP device must never receive a synthesised DELTA. If the
                # event spine is unavailable the frame is dropped instead.
                if event is not None:
                    self._deliver_cbdp_event(session, event)
                continue
            self._send_to(
                session,
                self._apply_budget(payload, self.budget_for(device_id)),
            )

    def _cbdp_event_for_message(self, message_id: str, read: bool = False):
        """Find the committed event that already represents this message."""

        if not message_id or self.event_spine is None:
            return None
        message_ref = self._cbdp_ref("message", message_id)
        wanted = {EVENT_MESSAGE_APPENDED, EVENT_MESSAGE_UPDATED}
        event_type = 0x0303 if read else None

        def matches(event) -> bool:
            if event_type is not None:
                if int(event.event_type) != event_type:
                    return False
            elif int(event.event_type) not in wanted:
                return False
            payload = event.payload
            raw_id = payload.get(TAG_MESSAGE_ID)
            if raw_id is not None:
                try:
                    return raw_id.decode("utf-8") == message_id
                except UnicodeDecodeError:
                    return False
            raw_ref = payload.get(TAG_MESSAGE_REF)
            if raw_ref is not None and message_ref:
                return int.from_bytes(raw_ref, "big") == message_ref
            return False

        return self.event_spine.latest_event(matches)

    @staticmethod
    def _deliver_cbdp_event(session, event) -> None:
        """Send one committed event unless the subscription path already did."""

        gateway = getattr(session, "gateway", None)
        if gateway is not None and gateway.has_subscription(
            int(event.topic_id), event_scope(int(event.event_type))
        ):
            # deliver_event() already emitted the subscription copy.
            return
        try:
            session.send_event(event)
        except (OSError, RuntimeError):
            return

    def _send_error(self, device_id: str, code: str) -> None:
        self._send(
            device_id,
            {"type": "error", "code": code, "text": ERROR_TEXT.get(code, code)},
        )

    def _send(self, device_id: str, message: dict) -> None:
        with self._lock:
            session = self._sessions.get(device_id)
        if session is not None:
            self._send_to(session, self._apply_budget(message, self.budget_for(device_id)))

    def _status_snapshot(self, device_id: str) -> dict | None:
        """One bounded line describing what Codex is doing right now.

        A device whose screen stopped making progress pulls this instead of
        waiting for a push that content dedupe will never send. The payload is
        a single flat record -- thread id, status, current action, turn id and
        plan counters -- clipped to the device's declared text budget, so the
        same call works for an ESP32 and for a desktop client.
        """
        with self._lock:
            # Answer about the thread this device is watching, not whichever
            # record the collector touched last.
            live = self.store.live_status(self.followed_thread(device_id))
        if live is None:
            return None
        task = live.data.get("task") if isinstance(live.data, dict) else None
        task = task if isinstance(task, dict) else {}
        # The device owns the character budget; SNAPSHOT_*_BYTES are the room a
        # single flat record has left inside the 768-byte line. Nothing here is
        # specific to a model of hardware.
        budget = self.budget_for(device_id)

        def number(value: Any) -> int:
            try:
                return max(0, min(1_000_000, int(value)))
            except (TypeError, ValueError):
                return 0

        return {
            "type": "codex_status_snapshot",
            "thread_id": clip_utf8(str(live.thread_id or ""),
                                   max_bytes=SNAPSHOT_ID_BYTES),
            "status": clip_utf8(str(live.status or ""),
                                max_bytes=SNAPSHOT_STATUS_BYTES),
            "current_action": clip_utf8(
                str(task.get("current_action") or live.body or ""),
                max_chars=budget,
                max_bytes=SNAPSHOT_ACTION_BYTES,
            ),
            "turn_id": clip_utf8(str(task.get("turn_id") or ""),
                                 max_bytes=SNAPSHOT_ID_BYTES),
            "plan_completed": number(task.get("plan_completed")),
            "plan_total": number(task.get("plan_total")),
            "created_at": int(live.created_at or 0),
        }

    def budget_for(self, device_id: str) -> int | None:
        with self._lock:
            record = self.registry.get(device_id)
        return None if record is None else record.max_text_chars

    @staticmethod
    def _apply_budget(message: dict, max_text_chars: int | None) -> dict:
        if max_text_chars is None or message.get("type") != "message":
            return message
        body = message.get("body")
        if not isinstance(body, str):
            return message
        prepared = dict(message)
        prepared["body"] = clip_text(body, max_text_chars)
        return prepared

    @staticmethod
    def _send_to(session: Any, message: dict) -> None:
        for payload in wire_messages(message):
            chunk = decode(payload)
            if chunk is not None:
                try:
                    session.send(chunk)
                except (OSError, RuntimeError):
                    return

    @staticmethod
    def _message_payload(record: MessageRecord) -> dict:
        payload = {
            "message_id": record.message_id,
            "thread_id": record.thread_id,
            "kind": record.kind,
            "status": record.status,
            "title": record.title,
            "body": record.body,
            "created_at": record.created_at,
        }
        if isinstance(record.data, dict):
            payload["data"] = record.data
        return payload
