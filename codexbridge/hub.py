from __future__ import annotations

import threading
import time
from typing import Any

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.messages_store import KIND_CAPABILITY, MessageRecord, MessageStore
from codexbridge.protocol.capabilities import effective
from codexbridge.protocol.messages import decode, wire_messages

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
}


class BridgeHub:
    def __init__(
        self,
        registry: DeviceRegistry,
        store: MessageStore | None = None,
        speech=None,
    ):
        self.registry = registry
        self.store = store or MessageStore()
        self.speech = speech
        self._sessions: dict[str, Any] = {}
        self._lock = threading.RLock()

    def publish(self, record: MessageRecord) -> None:
        with self._lock:
            self.store.add(record)
            capability = KIND_CAPABILITY.get(record.kind)
            if capability is None:
                return
            payload = {"type": "message", **self._message_payload(record)}
            self._broadcast(payload, capability)

    def attach(self, device_id: str, session: Any) -> None:
        with self._lock:
            self._sessions[device_id] = session

    def detach(self, device_id: str) -> None:
        with self._lock:
            self._sessions.pop(device_id, None)

    def handle_device_event(self, device_id: str, event: dict) -> None:
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "read_ack":
            self._handle_read_ack(device_id, event)
            return
        if event_type == "cancel_task":
            if "tasks" not in self.capabilities_for(device_id):
                self._send_error(device_id, "capability_not_enabled")
            return

        capability = DEVICE_EVENT_CAPABILITY.get(event_type)
        if capability is None:
            return
        if capability not in self.capabilities_for(device_id):
            self._send_error(device_id, "capability_not_enabled")
            return
        if capability == "audio" and self.speech is not None:
            for message in self.speech.handle_event(device_id, event):
                self._send(device_id, message)

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

    def state_for(self, device_id: str) -> dict:
        with self._lock:
            capabilities = self.capabilities_for(device_id)
            pending = self.store.pending_for(device_id, capabilities)
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
        for message in state["messages"]:
            self._send(device_id, {"type": "message", **message})
        for read_state in state["read_state"]:
            self._send(device_id, {"type": "read_state", **read_state})

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
            payload = {
                "type": "read_state",
                "message_id": record.message_id,
                "read_by": sorted(record.read_by),
                "read_at": time.time(),
            }
            for other_id, session in self._sessions.items():
                if (
                    other_id != device_id
                    and "read_ack" in self.capabilities_for(other_id)
                ):
                    self._send_to(session, payload)

    def _broadcast(self, payload: dict, capability: str) -> None:
        with self._lock:
            for device_id, session in self._sessions.items():
                if capability in self.capabilities_for(device_id):
                    self._send_to(session, payload)

    def _send_error(self, device_id: str, code: str) -> None:
        self._send(device_id, {"type": "error", "code": code})

    def _send(self, device_id: str, message: dict) -> None:
        with self._lock:
            session = self._sessions.get(device_id)
        if session is not None:
            self._send_to(session, message)

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
