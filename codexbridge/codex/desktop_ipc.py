"""Versioned adapter for the desktop's private cross-client IPC router.

This is not an app-server transport. Connecting does not imply shared turns.
The observed protocol is version-specific and deliberately fails closed.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_SANDBOX_POLICY_NAMES = {
    "read-only": "readOnly",
    "readOnly": "readOnly",
    "workspace-write": "workspaceWrite",
    "workspaceWrite": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
    "dangerFullAccess": "dangerFullAccess",
}


def _sandbox_policy_wire_name(value: Any) -> dict[str, Any]:
    """Desktop IPC expects the internally tagged sandbox enum object."""

    if isinstance(value, dict):
        if str(value.get("type") or ""):
            return dict(value)
        raise DesktopIpcError("sandbox_policy")
    name = _SANDBOX_POLICY_NAMES.get(str(value or ""))
    if name is None:
        raise DesktopIpcError(f"Unsupported sandbox policy: {value}")
    return {"type": name}


def _desktop_text_inputs(request: dict[str, Any]) -> dict[str, Any]:
    inputs = request.get('input')
    if not isinstance(inputs, list):
        return request
    return {**request, 'input': [
        {**item, 'text_elements': (
            item.get('text_elements')
            if isinstance(item.get('text_elements'), list) else []
        )} if isinstance(item, dict) and item.get('type') == 'text' else item
        for item in inputs
    ]}


class DesktopIpcError(RuntimeError):
    pass


if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

    _ERROR_PIPE_BUSY = 231
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)

    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _KERNEL32.WaitNamedPipeW.restype = wintypes.BOOL
    _KERNEL32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _KERNEL32.WriteFile.restype = wintypes.BOOL
    _KERNEL32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _KERNEL32.ReadFile.restype = wintypes.BOOL
    _KERNEL32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _KERNEL32.PeekNamedPipe.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL

    def _create_windows_pipe_handle(path: str) -> int:
        handle = _KERNEL32.CreateFileW(
            path, _GENERIC_READ | _GENERIC_WRITE, 0, None,
            _OPEN_EXISTING, 0, None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _wait_windows_pipe(path: str, timeout_ms: int) -> bool:
        return bool(_KERNEL32.WaitNamedPipeW(path, timeout_ms))

    def _open_windows_pipe_handle(path: str, timeout: float) -> int:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                return _create_windows_pipe_handle(path)
            except OSError as error:
                error_code = getattr(error, 'winerror', None) or error.errno
                if error_code != _ERROR_PIPE_BUSY:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DesktopIpcError('Desktop IPC pipe is busy') from error
                wait_ms = max(1, min(250, int(remaining * 1000)))
                _wait_windows_pipe(path, wait_ms)

    class _WindowsNamedPipe:
        def __init__(self, path: str, timeout: float) -> None:
            self._handle = _open_windows_pipe_handle(path, timeout)
            self._closed = threading.Event()

        def write(self, data: Any) -> int:
            if self._closed.is_set():
                raise DesktopIpcError('IPC disconnected')
            payload = bytes(data)
            written = wintypes.DWORD()
            ok = _KERNEL32.WriteFile(
                self._handle, payload, len(payload), ctypes.byref(written), None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            return written.value

        def read(self, size: int) -> bytes:
            chunks = []
            remaining = max(0, size)
            while remaining:
                if self._closed.is_set():
                    raise DesktopIpcError('IPC disconnected')
                available = wintypes.DWORD()
                if not _KERNEL32.PeekNamedPipe(
                        self._handle, None, 0, None,
                        ctypes.byref(available), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not available.value:
                    time.sleep(0.005)
                    continue
                count = min(remaining, available.value)
                buffer = ctypes.create_string_buffer(count)
                read = wintypes.DWORD()
                if not _KERNEL32.ReadFile(
                        self._handle, buffer, count,
                        ctypes.byref(read), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not read.value:
                    raise DesktopIpcError('IPC disconnected')
                chunks.append(buffer.raw[:read.value])
                remaining -= read.value
            return b''.join(chunks)

        def close(self) -> None:
            if self._closed.is_set():
                return
            self._closed.set()
            handle, self._handle = self._handle, None
            if handle is not None:
                _KERNEL32.CloseHandle(handle)


class DesktopIpcClient:
    # Desktop state frames carry the whole turn history of a thread, so a long
    # conversation can outgrow any fixed budget. Frames up to MAX_FRAME_BYTES
    # are parsed, larger ones are drained and answered with a resync request.
    #
    # Parsing is done by json.loads, the C scanner, which holds the GIL for the
    # whole payload. A multi-megabyte frame therefore freezes every other
    # thread in this process, and because this pipe is shared with the Codex
    # desktop the desktop's own writes back up behind it. The budget is kept
    # small so one frame can never stall the UI for long.
    MAX_FRAME_BYTES = 8 * 1024 * 1024
    # Broadcasts are the bulk of the traffic and most describe state this
    # reader does not consume. Oversized broadcasts are replaced with a resync
    # request instead of being parsed in full.
    MAX_BROADCAST_BYTES = 2 * 1024 * 1024
    BROADCAST_PREFIX_BYTES = 512
    MAX_SKIPPED_FRAME_BYTES = 512 * 1024 * 1024
    DISCARD_CHUNK_BYTES = 1024 * 1024

    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout
        self._stream: Any = None
        self._client_id = ''
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self.generation = 0
        self._reader: threading.Thread | None = None
        self._error = 'not_started'
        self.events: queue.Queue = queue.Queue(maxsize=300)

    def _connect_endpoint(self) -> Any:
        if os.name == 'nt':
            return _WindowsNamedPipe(r'\\.\pipe\codex-ipc', self.timeout)
        root = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.settimeout(self.timeout)
            stream.connect(str(root / 'ipc' / 'ipc.sock'))
            stream.settimeout(None)
            return stream
        except BaseException:
            stream.close()
            raise

    def start(self) -> None:
        with self._start_lock:
            self._start()

    def _start(self) -> None:
        if self._client_id:
            return
        self.close()
        try:
            self._stream = self._connect_endpoint()
            self._reader = threading.Thread(target=self._read, daemon=True,
                                            args=(self._stream,),
                                            name='desktop-ipc-read')
            self._reader.start()
            response = self._request('initialize', {'clientType': 'codexbridge'}, 0)
            client_id = response.get('result', {}).get('clientId')
            if not isinstance(client_id, str) or not client_id:
                raise DesktopIpcError('Invalid IPC initialize response')
            self._client_id = client_id
            self.generation += 1
            self._error = ''
        except (OSError, DesktopIpcError) as error:
            self._error = str(error)
            self.close()
            raise DesktopIpcError(str(error)) from error

    def _send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, separators=(',', ':')).encode('utf-8')
        frame = struct.pack('<I', len(body)) + body
        with self._write_lock:
            stream = self._stream
            if stream is None:
                raise DesktopIpcError('IPC disconnected')
            if isinstance(stream, socket.socket):
                stream.sendall(frame)
            else:
                view = memoryview(frame)
                while view:
                    written = stream.write(view)
                    if not written:
                        raise DesktopIpcError('IPC write failed')
                    view = view[written:]

    def _exact(self, size: int, expected_stream: Any = None) -> bytes:
        data = bytearray()
        while len(data) < size:
            stream = self._stream
            if expected_stream is not None and stream is not expected_stream:
                raise DesktopIpcError('IPC connection replaced')
            if stream is None:
                raise DesktopIpcError('IPC disconnected')
            count = size - len(data)
            if isinstance(stream, socket.socket):
                chunk = stream.recv(count)
            else:
                chunk = stream.read(count)
            if not chunk:
                raise DesktopIpcError('IPC disconnected')
            data.extend(chunk)
        return bytes(data)

    def _discard(self, size: int, expected_stream: Any) -> None:
        remaining = size
        while remaining:
            count = min(remaining, self.DISCARD_CHUNK_BYTES)
            self._exact(count, expected_stream)
            remaining -= count

    @staticmethod
    def _is_unwanted_broadcast(payload: bytes) -> bool:
        """True when this frame is a broadcast this reader never consumes.

        Peeking at the head of the frame avoids a full ``json.loads`` for the
        many broadcasts that describe state only the desktop cares about.
        """

        head = payload[:DesktopIpcClient.BROADCAST_PREFIX_BYTES]
        if b'"broadcast"' not in head:
            return False
        return b'thread-stream-state-changed' not in head

    def _enqueue_event(self, message: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(message)
        except queue.Full:
            # Consumers reconcile authoritative state after overflow.
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
            self.events.put_nowait({'type': 'resync_required'})

    def _read(self, stream: Any) -> None:
        try:
            while True:
                size, = struct.unpack('<I', self._exact(4, stream))
                if size == 0 or size > self.MAX_SKIPPED_FRAME_BYTES:
                    # A truncated or interleaved frame must not leave the
                    # reader spinning on the same four bytes forever. Close
                    # and let the service reconnect and resubscribe.
                    raise DesktopIpcError('Invalid IPC frame length')
                if size > self.MAX_FRAME_BYTES:
                    # Dropping the connection on a real state snapshot would
                    # surface as a failed phone reply or a lost completion, so
                    # drain it and let consumers reconcile from the list.
                    self._discard(size, stream)
                    self._enqueue_event({'type': 'resync_required'})
                    continue
                payload = self._exact(size, stream)
                if self._is_unwanted_broadcast(payload):
                    continue
                if size > self.MAX_BROADCAST_BYTES and b'"broadcast"' in payload[:self.BROADCAST_PREFIX_BYTES]:
                    # Too large to parse without stalling the shared GIL;
                    # consumers reconcile from the authoritative list instead.
                    self._enqueue_event({'type': 'resync_required'})
                    continue
                message = json.loads(payload)
                if not isinstance(message, dict):
                    raise DesktopIpcError('Invalid IPC message')
                kind = message.get('type')
                if kind == 'broadcast' and message.get('method') == 'thread-stream-state-changed':
                    self._enqueue_event(message)
                if kind == 'response':
                    with self._lock:
                        target = self._pending.get(message.get('requestId'))
                    if target is not None:
                        target.put_nowait(message)
                elif kind == 'client-discovery-request':
                    self._send({'type': 'client-discovery-response',
                                'requestId': message.get('requestId'),
                                'response': {'canHandle': False}})
        except (OSError, ValueError, DesktopIpcError, queue.Full) as error:
            if self._stream is not stream:
                return
            self._error = str(error)
            self._client_id = ''
            with self._lock:
                for target in self._pending.values():
                    try:
                        target.put_nowait(DesktopIpcError(str(error)))
                    except queue.Full:
                        pass

    def _request(self, method: str, params: dict[str, Any], version: int,
                 *, target_client_id: str | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        target: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = target
        try:
            message = {'type': 'request', 'requestId': request_id,
                        'sourceClientId': self._client_id or 'initializing-client',
                        'method': method, 'version': version, 'params': params,
                        'timeoutMs': int(self.timeout * 1000)}
            if target_client_id:
                message['targetClientId'] = target_client_id
            self._send(message)
            try:
                response = target.get(timeout=self.timeout)
            except queue.Empty as error:
                raise DesktopIpcError(f'IPC request timed out: {method}') from error
            if isinstance(response, BaseException):
                raise response
            return response
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def follow_thread(self, owner_id: str, thread_id: str, following: bool = True) -> None:
        self._send({'type': 'broadcast', 'sourceClientId': self._client_id,
                    'method': 'thread-stream-following-changed', 'version': 1,
                    'params': {'conversationId': thread_id, 'hostId': 'local',
                               'following': following}, 'targetClientIds': [owner_id]})

    def invalidate_query_cache(
        self,
        query_key: list[Any],
        *,
        reset: bool = False,
    ) -> None:
        """Ask desktop renderers to refetch a React Query key prefix."""
        if not query_key:
            raise ValueError('query_key')
        self._send({
            'type': 'broadcast',
            'sourceClientId': self._client_id,
            'method': 'query-cache-invalidate',
            'version': 0,
            'params': {'queryKey': list(query_key), 'reset': bool(reset)},
        })

    def open_thread(self, thread_id: str) -> None:
        if str(uuid.UUID(thread_id)) != thread_id.lower():
            raise ValueError('invalid_thread_id')
        if os.name != 'nt':
            raise DesktopIpcError('Desktop task opening is not available on this platform')
        os.startfile(f'codex://threads/{thread_id}')

    def start_turn(self, owner_id: str, thread_id: str,
                   request: dict[str, Any]) -> dict[str, Any]:
        request = _desktop_text_inputs(request)
        response = self._request('thread-follower-start-turn', {
            'conversationId': thread_id,
            'turnStart': {'request': {**request, 'threadId': thread_id},
                          'context': {'inheritThreadSettings': True}},
        }, 2, target_client_id=owner_id)
        return self._turn_result(response)

    @staticmethod
    def _turn_result(response: dict[str, Any]) -> dict[str, Any]:
        if response.get('resultType') != 'success':
            raise DesktopIpcError(str(response.get('error') or 'Desktop rejected turn'))
        result = response.get('result', {}).get('result')
        if not isinstance(result, dict):
            raise DesktopIpcError('Invalid desktop turn response')
        return result

    def update_thread_settings(self, owner_id: str, thread_id: str, *,
                               model: str = '', effort: str = '',
                               approval_policy: str = '',
                               approvals_reviewer: str = '',
                               sandbox_policy: str | dict[str, Any] = '',
                               personality: str = '') -> bool:
        """Persist explicit model/effort choices before a desktop-owned turn.

        The desktop inherits thread settings for follower turns, so a model
        that is only present in the turn request is ignored; the phone's
        selection has to be written to the thread first.
        """
        settings: dict[str, Any] = {}
        if model:
            settings['model'] = str(model)
        if effort:
            settings['effort'] = str(effort)
        if approval_policy:
            settings['approvalPolicy'] = str(approval_policy)
        if approvals_reviewer:
            settings['approvalsReviewer'] = str(approvals_reviewer)
        if sandbox_policy:
            settings['sandboxPolicy'] = _sandbox_policy_wire_name(sandbox_policy)
        if personality:
            settings['personality'] = str(personality)
        if not settings:
            return True
        response = self._request('thread-follower-update-thread-settings', {
            'conversationId': thread_id,
            'threadSettings': settings,
        }, 2, target_client_id=owner_id)
        if response.get('resultType') != 'success':
            raise DesktopIpcError(str(response.get('error') or 'Desktop rejected thread settings'))
        # This method answers {"result": {"applied": true}}; unlike follower
        # turns the payload is not nested one level deeper, so unwrap only
        # when a nested result is really present.
        result = response.get('result')
        if isinstance(result, dict) and isinstance(result.get('result'), dict):
            result = result['result']
        if not isinstance(result, dict):
            raise DesktopIpcError('Invalid desktop settings response')
        return bool(result.get('applied', True))

    def steer_turn(self, owner_id: str, thread_id: str,
                   inputs: list[dict[str, Any]]) -> dict[str, Any]:
        inputs = [{**item, 'text_elements': item.get('text_elements', [])}
                  if item.get('type') == 'text' else item for item in inputs]
        text = '\n'.join(item.get('text', '') for item in inputs if item.get('type') == 'text')
        message_id = str(uuid.uuid4())
        restore = {'id': message_id, 'text': text, 'createdAt': int(time.time() * 1000),
                   'context': {'prompt': text, 'addedFiles': [], 'fileAttachments': [],
                               'ideContext': None, 'imageAttachments': []}}
        return self._turn_result(self._request('thread-follower-steer-turn', {
            'conversationId': thread_id, 'input': inputs,
            'restoreMessage': restore, 'clientUserMessageId': message_id, 'attachments': [],
        }, 1, target_client_id=owner_id))

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str = '') -> None:
        params = {'conversationId': thread_id, 'mode': 'user-stop'}
        if turn_id:
            params['expectedTurnId'] = turn_id
        response = self._request('thread-follower-interrupt-turn', params,
                                 4 if turn_id else 3, target_client_id=owner_id)
        if response.get('resultType') != 'success':
            raise DesktopIpcError(str(response.get('error') or 'Desktop rejected interrupt'))

    def respond_approval(self, owner_id: str, thread_id: str, request_id: Any,
                         kind: str, response: dict[str, Any]) -> None:
        methods = {'command': 'command-approval-decision',
                   'fileChange': 'file-approval-decision',
                   'permissions': 'permissions-request-approval-response',
                   'input': 'submit-user-input', 'mcp': 'submit-mcp-server-elicitation-response'}
        if kind not in methods:
            raise ValueError('approval_kind')
        params = {'conversationId': thread_id, 'requestId': request_id}
        if kind in {'command', 'fileChange'}:
            params['decision'] = response['decision']
        else:
            params['response'] = response
        result = self._request('thread-follower-' + methods[kind], params, 1,
                               target_client_id=owner_id)
        if result.get('resultType') != 'success':
            raise DesktopIpcError(str(result.get('error') or 'Desktop rejected approval'))

    def find_thread_owner(self, thread_id: str) -> str | None:
        if not self._client_id:
            raise DesktopIpcError('IPC not initialized')
        response = self._request('thread-owner-discovery',
                                 {'hostId': 'local', 'conversationId': thread_id}, 1)
        if response.get('resultType') == 'error':
            if response.get('error') == 'no-client-found':
                return None
            raise DesktopIpcError(str(response.get('error')))
        owner = response.get('handledByClientId')
        if not isinstance(owner, str) or not owner:
            raise DesktopIpcError('Invalid IPC owner response')
        return owner

    def connection_status(self) -> dict[str, Any]:
        return {'desktop_ipc_connected': bool(self._client_id),
                'desktop_realtime_shared': False,
                'desktop_sync_mode': 'discovery_only',
                'reason': self._error or 'ipc_discovery_available'}

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._client_id = ''
        with self._lock:
            for target in self._pending.values():
                try:
                    target.put_nowait(DesktopIpcError('IPC closed'))
                except queue.Full:
                    pass
        if stream is not None:
            if isinstance(stream, socket.socket):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            stream.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=0.2)
