"""Tests for the companion app-server service."""

from __future__ import annotations

import json
import os
import queue
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codexbridge.codex.app_server import AppServerError
from codexbridge.companion.service import LOG_ACTIVE_SECONDS, CompanionService
from codexbridge.read_state import ReadStateStore


class FakeClient:
    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.requests: list[tuple[str, dict]] = []
        self.responses_sent: list[tuple[int, dict]] = []
        self.notifications: queue.Queue = queue.Queue()
        self.server_requests: queue.Queue = queue.Queue()
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def request(self, method: str, params: dict | None = None):
        self.requests.append((method, params or {}))
        handler = self.responses.get(method)
        if handler is None:
            raise AppServerError(f"unsupported: {method}")
        return handler(params or {}) if callable(handler) else handler

    def respond(self, request_id: int, result: dict) -> None:
        self.responses_sent.append((request_id, result))


class SharedClientLifecycleTests(unittest.TestCase):
    def test_stopping_companion_does_not_close_a_shared_client(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp:
            service = CompanionService(
                data_dir=Path(temp),
                cwd=Path(temp),
                client_factory=lambda command, cwd: client,
                close_client_on_stop=False,
            )
            service.start()
            service.stop()
        self.assertTrue(client.started)
        self.assertFalse(client.closed)


def thread_row(thread_id: str = "t1", **overrides):
    row = {
        "id": thread_id,
        "name": "桌面任务",
        "preview": "帮我看一下构建结果",
        "model": "gpt-5.1-codex",
        "cwd": r"D:\work",
        "source": "vscode",
        "createdAt": 1791000000,
        "updatedAt": 1791000200,
        "recencyAt": 1791000200,
        "status": {"type": "notLoaded"},
        "turns": [],
    }
    row.update(overrides)
    return row


def thread_detail(thread_id: str = "t1"):
    return {
        "thread": {
            **thread_row(thread_id),
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 1791000000,
                    "completedAt": 1791000100,
                    "items": [
                        {
                            "id": "item-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "帮我看一下构建结果"}],
                        },
                        {
                            "id": "item-2",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "先检查日志",
                        },
                        {
                            "id": "item-3",
                            "type": "agentMessage",
                            "phase": "final",
                            "text": "构建通过",
                        },
                        {
                            "id": "item-4",
                            "type": "commandExecution",
                            "command": "gradle build",
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "BUILD SUCCESSFUL",
                        },
                    ],
                }
            ],
        }
    }


class CompanionServiceTests(unittest.TestCase):
    def test_owner_discovery_uses_oldest_due_thread(self):
        from unittest.mock import patch
        self.service._client_factory = None
        self.service._desktop_discovery_due = {'t1': 10, 't2': 1}
        with patch.object(self.service, 'list_threads', return_value={'threads': [
                {'thread_id': 't1'}, {'thread_id': 't2'}]}), patch.object(self.service, '_desktop_owner') as discover:
            self.service.poll_thread_changes()
        discover.assert_called_once_with('t2')

    def test_desktop_event_failure_does_not_kill_event_pump(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        self.service._desktop = Mock()
        self.service._desktop.events = queue.Queue()
        self.service._desktop.events.put({'first': True})
        self.service._desktop.events.put({'second': True})
        seen = []
        def consume(event):
            seen.append(event)
            if 'first' in event:
                raise DesktopIpcError('disconnected')
            self.service._stop.set()
        self.service.handle_desktop_event = consume
        self.service._pump_desktop()
        self.assertEqual(len(seen), 2)

    def test_desktop_reconnect_resubscribes_followed_threads_and_requests_resync(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.side_effect = {'t1': 'owner-1', 't2': 'owner-2'}.get
        self.service._desktop = desktop
        self.service._desktop_followed.update({'t1': 'old-1', 't2': 'old-2'})
        events = self.service.subscribe()
        self.service._reconnect_desktop()
        desktop.start.assert_called_once_with()
        self.assertEqual(
            desktop.follow_thread.call_args_list,
            [
                unittest.mock.call('owner-1', 't1'),
                unittest.mock.call('owner-2', 't2'),
            ],
        )
        self.assertEqual(self.service._desktop_followed, {'t1': 'owner-1', 't2': 'owner-2'})
        self.assertEqual(events.get_nowait()['type'], 'resync_required')

    def test_desktop_pump_reconnects_when_connection_drops_without_an_event(self):
        import threading
        from unittest.mock import Mock
        desktop = Mock()
        desktop.events = queue.Queue()
        desktop.connection_status.return_value = {'desktop_ipc_connected': False}
        self.service._desktop = desktop
        reconnected = threading.Event()
        self.service._reconnect_desktop = lambda: reconnected.set()
        worker = threading.Thread(target=self.service._pump_desktop)
        worker.start()
        try:
            self.assertTrue(reconnected.wait(2.0))
        finally:
            self.service._stop.set()
            worker.join(2.0)
        self.assertFalse(worker.is_alive())

    def test_list_and_detail_share_one_status_resolver(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        self.service._desktop_turn_status[('t1', 'turn-1')] = 'completed'
        self.client.responses['thread/list'] = {'data': [thread_row(status={'type': 'notLoaded'})]}
        self.client.responses['thread/read'] = thread_detail()
        listed = self.service.list_threads()['threads'][0]
        detail = self.service.read_thread('t1')
        self.assertEqual(listed['status'], 'completed')
        self.assertEqual(detail['thread']['status'], 'completed')
        self.assertEqual(listed['status'], detail['thread']['status'])




    def test_pending_desktop_handoff_never_falls_back(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        self.service._desktop = Mock()
        self.service._desktop.find_thread_owner.return_value = None
        self.service._desktop_required.add('t1')
        with self.assertRaises(DesktopIpcError):
            self.service.start_turn('t1', 'hello')
        self.assertEqual(self.client.requests, [])

    def test_reconnected_desktop_resubscribes_same_owner(self):
        from unittest.mock import patch
        from codexbridge.codex.desktop_ipc import DesktopIpcClient
        desktop = DesktopIpcClient()
        self.service._desktop = desktop
        self.service._desktop_followed['t1'] = 'owner'
        self.service._desktop_subscription_generation = 1
        desktop.generation = 2
        with patch.object(desktop, 'start'), patch.object(desktop, 'find_thread_owner', return_value='owner'), patch.object(desktop, 'follow_thread') as follow:
            self.service._desktop_owner('t1')
            follow.assert_called_once_with('owner', 't1')

    def test_known_desktop_task_missing_owner_never_runs_independently(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        self.service._desktop = Mock()
        self.service._desktop.find_thread_owner.return_value = None
        self.service._desktop_followed['t1'] = 'old-owner'
        with self.assertRaises(DesktopIpcError):
            self.service.start_turn('t1', 'hello')
        self.assertEqual(self.client.requests, [])

    def test_connection_status_exposes_owner_routing_without_global_shared_claim(self):
        from unittest.mock import Mock
        self.service._desktop = Mock()
        self.service._desktop.connection_status.return_value = {'desktop_ipc_connected': True}
        self.service._desktop_followed['t1'] = 'owner'
        status = self.service.connection_status()
        self.assertTrue(status['desktop_ipc_connected'])
        self.assertEqual(status['desktop_owned_threads'], 1)
        self.assertEqual(status['desktop_sync_mode'], 'owner_routed')

    def test_stale_failed_marker_does_not_override_newer_running_turn(self):
        from unittest.mock import Mock, patch
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        row = thread_row(status={'type': 'notLoaded'}, turns=[{
            'id': 'turn-new', 'status': 'inProgress',
            'startedAt': 1791000200, 'completedAt': 0,
        }])
        self.client.responses['thread/list'] = {'data': [row]}
        self.client.responses['thread/read'] = {'thread': row}
        with patch.object(self.service, '_thread_log', return_value=(1791000100.0, 'task_failed', 1791000100.0, 0.0, 1791000100.0)):
            listed = self.service.list_threads()['threads'][0]
        self.assertEqual(listed['status'], 'working')

    def test_desktop_first_terminal_snapshot_only_establishes_baseline(self):
        events = self.service.subscribe()
        self.service.handle_desktop_event({'type': 'broadcast', 'version': 11,
            'sourceClientId': 'owner', 'method': 'thread-stream-state-changed', 'params': {
                'hostId': 'local', 'conversationId': 't-first-terminal',
                'change': {'type': 'snapshot', 'conversationState': {
                    'turnHistory': {'history': {'entitiesByKey': {'tail:0': {
                        'turnId': 'turn-first', 'status': 'completed'}}}}, 'requests': []}}}})
        self.service.handle_desktop_event({'type': 'broadcast', 'version': 11,
            'sourceClientId': 'owner', 'method': 'thread-stream-state-changed', 'params': {
                'hostId': 'local', 'conversationId': 't-first-terminal',
                'change': {'type': 'snapshot', 'conversationState': {
                    'turnHistory': {'history': {'entitiesByKey': {'tail:0': {
                        'turnId': 'turn-first', 'status': 'completed'}}}}, 'requests': []}}}})
        received = []
        while not events.empty():
            received.append(events.get_nowait())
        completions = [event for event in received if event['type'] == 'turn_completed']
        self.assertEqual(completions, [])

    def test_desktop_snapshot_emits_completed_turn_once(self):
        events = self.service.subscribe()
        message = {'type': 'broadcast', 'version': 11, 'sourceClientId': 'owner',
            'method': 'thread-stream-state-changed', 'params': {'hostId': 'local',
                'conversationId': 't1', 'change': {'type': 'snapshot', 'conversationState': {
                    'turnHistory': {'history': {'entitiesByKey': {'tail:0': {
                        'turnId': 'turn1', 'status': 'completed'}}}}, 'requests': []}}}}
        import copy
        baseline = copy.deepcopy(message)
        baseline['params']['change']['conversationState']['turnHistory']['history']['entitiesByKey']['tail:0']['status'] = 'inProgress'
        self.service.handle_desktop_event(baseline)
        self.service.handle_desktop_event(message)
        self.service.handle_desktop_event(message)
        received = []
        while not events.empty(): received.append(events.get_nowait())
        self.assertEqual(sum(event['type'] == 'turn_completed' for event in received), 1)

    def test_known_desktop_task_does_not_fallback_when_owner_discovery_fails(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        self.service._desktop = Mock()
        self.service._desktop.find_thread_owner.side_effect = DesktopIpcError('disconnected')
        self.service._desktop_followed['t1'] = 'previous-owner'
        with self.assertRaises(DesktopIpcError):
            self.service.start_turn('t1', 'hello')
        self.assertEqual(self.client.requests, [])

    def test_desktop_owned_turn_writes_phone_model_before_start(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.return_value = {'turn': {'id': 'turn-1'}}
        self.service._desktop = desktop
        result = self.service.start_turn(
            't1', '继续修复', model='Auto/Kimi-K3', effort='high')
        desktop.update_thread_settings.assert_called_once_with(
            'owner', 't1', model='Auto/Kimi-K3', effort='high')
        names = [call[0] for call in desktop.mock_calls]
        self.assertLess(names.index('update_thread_settings'), names.index('start_turn'))
        self.assertEqual(result['execution_transport'], 'desktop_owner')

    def test_desktop_owned_turn_without_model_keeps_thread_settings(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.return_value = {'turn': {'id': 'turn-1'}}
        self.service._desktop = desktop
        self.service.start_turn('t1', '继续修复')
        desktop.update_thread_settings.assert_not_called()

    def test_desktop_snapshot_imports_pending_approval_and_routes_response(self):
        from unittest.mock import Mock
        self.service._desktop = Mock()
        self.service.handle_desktop_event({'type': 'broadcast', 'version': 11,
            'sourceClientId': 'owner', 'method': 'thread-stream-state-changed', 'params': {
                'hostId': 'local', 'conversationId': 't1', 'change': {'type': 'snapshot',
                    'conversationState': {'requests': [{'id': 'desktop-request',
                        'method': 'item/tool/requestUserInput', 'params': {'threadId': 't1'}}]}}}})
        pending = self.service.pending_approvals()
        self.assertEqual(len(pending), 1)
        self.assertTrue(self.service.respond_approval(pending[0]['request_id'], {'answers': {}}))
        self.service._desktop.respond_approval.assert_called_once_with(
            'owner', 't1', 'desktop-request', 'input', {'answers': {}})

    def test_structured_input_response_uses_answers_not_decision(self):
        self.service._ensure_client()
        self.service._approvals[99] = {'kind': 'input', 'thread_id': 't1'}
        response = {'answers': {'question': {'answers': ['chosen']}}}
        self.assertTrue(self.service.respond_approval(99, response))
        self.assertEqual(self.client.responses_sent[-1], (99, response))

    def test_invalid_input_response_keeps_request_pending(self):
        self.service._ensure_client()
        self.service._approvals[99] = {'kind': 'input', 'thread_id': 't1'}
        with self.assertRaises(ValueError):
            self.service.respond_approval(99, {'decision': 'accept'})
        self.assertIn(99, self.service._approvals)

    def test_creation_client_releases_writer_before_desktop_open(self):
        from unittest.mock import Mock
        creator = FakeClient()
        creator.responses['thread/start'] = {'thread': thread_row()}
        creator.responses['thread/name/set'] = {}
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.open_thread.side_effect = lambda _: self.assertTrue(creator.closed)
        self.service._desktop = desktop
        self.service._creation_client_factory = lambda: creator
        self.service.start_thread()
        self.assertTrue(creator.closed)
        self.assertFalse(self.client.closed)
        self.assertEqual(
            creator.requests,
            [
                ('thread/start', {'threadSource': 'user'}),
                ('thread/name/set', {'threadId': 't1', 'name': '新任务'}),
            ],
        )

    def test_new_thread_is_opened_for_desktop_ownership(self):
        from unittest.mock import Mock
        desktop = Mock()
        self.service._desktop = desktop
        self.client.responses['thread/start'] = {'thread': thread_row()}
        self.service.start_thread()
        desktop.open_thread.assert_called_once_with('t1')

    def test_new_thread_requires_desktop_ownership_before_success(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        desktop = Mock()
        desktop.find_thread_owner.return_value = None
        self.service._desktop = desktop
        creator = FakeClient()
        creator.responses['thread/start'] = {'thread': thread_row()}
        creator.responses['thread/name/set'] = {}
        self.service._creation_client_factory = lambda: creator
        with self.assertRaises(DesktopIpcError):
            self.service.start_thread()
        self.assertIn('t1', self.service._desktop_required)
        self.assertEqual(
            creator.requests,
            [
                ('thread/start', {'threadSource': 'user'}),
                ('thread/name/set', {'threadId': 't1', 'name': '新任务'}),
            ],
        )
        self.assertEqual(self.client.requests, [])

    def test_new_thread_does_not_use_the_old_placeholder_name(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        creator = FakeClient()
        creator.responses['thread/start'] = {'thread': thread_row()}
        creator.responses['thread/name/set'] = {}
        self.service._creation_client_factory = lambda: creator
        self.service.start_thread()
        self.assertNotIn(('thread/name/set', {'threadId': 't1', 'name': 'CodexBridge'}), creator.requests)
        self.assertIn(('thread/name/set', {'threadId': 't1', 'name': '新任务'}), creator.requests)

    def test_desktop_stream_change_publishes_thread_invalidation(self):
        events = self.service.subscribe()
        self.service.handle_desktop_event({'type': 'broadcast', 'version': 11,
            'method': 'thread-stream-state-changed', 'params': {
                'hostId': 'local', 'conversationId': 't1',
                'change': {'type': 'patches', 'revision': 2, 'patches': []}}})
        self.assertEqual(events.get_nowait()['thread_id'], 't1')

    def test_desktop_steer_and_interrupt_do_not_resume_independent_thread(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.steer_turn.return_value = {'turn': {'id': 'desktop-turn'}}
        self.service._desktop = desktop
        self.assertEqual(self.service.steer_turn('t1', 'continue')['turn_id'], 'desktop-turn')
        self.service.interrupt('t1', 'desktop-turn')
        self.assertEqual(self.client.requests, [])

    def test_desktop_owner_receives_reply_without_independent_resume(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.return_value = {'turn': {'id': 'desktop-turn'}}
        self.service._desktop = desktop
        result = self.service.start_turn('t1', 'hello')
        self.assertEqual(result['turn_id'], 'desktop-turn')
        self.assertEqual(self.client.requests, [])

    def test_uncertain_desktop_reply_never_falls_back(self):
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.side_effect = DesktopIpcError('timed out')
        self.service._desktop = desktop
        with self.assertRaises(DesktopIpcError):
            self.service.start_turn('t1', 'hello')
        self.assertEqual(self.client.requests, [])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.client.responses["thread/list"] = {
            "data": [thread_row(), thread_row("t2", name="第二个任务")],
            "nextCursor": None,
        }
        self.client.responses["thread/read"] = thread_detail()
        self.client.responses["model/list"] = {
            "data": [
                {
                    "id": "gpt-5.1-codex",
                    "model": "gpt-5.1-codex",
                    "displayName": "GPT-5.1 Codex",
                    "description": "默认模型",
                    "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [{"effort": "medium", "description": "平衡"}],
                    "inputModalities": ["text"],
                }
            ]
        }
        self.client.responses["account/rateLimits/read"] = {"rateLimits": {}}
        self.client.responses["account/usage/read"] = {}
        self.client.responses["thread/resume"] = {"thread": thread_row()}
        self.client.responses["turn/start"] = {"turn": {"id": "turn-2", "status": "inProgress"}}
        self.client.responses["turn/interrupt"] = {}
        self.client.responses["thread/name/set"] = {}
        self.service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )

    def test_thread_read_retries_on_stale_list_turns(self) -> None:
        """A live app-server cannot page turns for a thread it just created."""
        from codexbridge.codex.app_server import AppServerProtocolError
        stale = FakeClient()

        def fail(params):
            raise AppServerProtocolError(
                "App Server 请求失败 thread/read：list_turns is not supported yet")

        stale.responses["thread/read"] = fail
        fresh = FakeClient()
        fresh.responses["thread/read"] = thread_detail("t1")
        clients = iter([stale, fresh])
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: next(clients),
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            detail = service.read_thread("t1")
        finally:
            service.stop()
        self.assertEqual(detail["thread"]["thread_id"], "t1")
        self.assertEqual(
            [method for method, _params in stale.requests if method == "thread/read"],
            ["thread/read"],
        )
        self.assertIn("thread/read", [method for method, _params in fresh.requests])

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def _write_rollout(self, thread_id: str, kind: str, seconds_ago: float = 0.0) -> None:
        day = datetime.now()
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        record = {
            "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": kind},
        }
        path = folder / f"rollout-{thread_id}.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_list_threads_reports_workspace_name(self) -> None:
        result = self.service.list_threads(limit=10)
        self.assertEqual(result["threads"][0]["workspace"], "work")
        self.assertEqual(result["threads"][0]["cwd"], r"D:\work")

    def test_list_threads_marks_active_rollout_first(self) -> None:
        # t2 is older by recency, but the desktop is still writing its rollout.
        self._write_rollout("t2", "task_started")
        result = self.service.list_threads(limit=10)
        self.assertEqual(result["threads"][0]["thread_id"], "t2")
        self.assertEqual(result["threads"][0]["status"], "working")
        self.assertEqual(result["threads"][1]["status"], "completed")

    def test_list_falls_back_to_turn_when_log_tail_has_no_lifecycle(self) -> None:
        # A long thread's rollout tail holds no task_started / task_complete
        # record: a 70 MB log spends its last 128 KB on tool output. The row then
        # has nothing to classify from and used to read "idle" while the chat
        # header read the turn and said "failed".
        self.client.responses["thread/list"] = {
            "data": [thread_row("t1", status={"type": "notLoaded"})],
            "nextCursor": None,
        }
        # Only a settings record in the tail, so no lifecycle event is visible.
        # Real tool records, but no task_started / task_complete anywhere in
        # the window, and old enough that no live turn is implied.
        self._append_rollout_record("t1", "token_count", seconds_ago=400)

        result = self.service.list_threads(limit=10)

        self.assertEqual(len(result["threads"]), 1)
        self.assertEqual(result["threads"][0]["status"], "completed")

    def test_list_shows_working_for_a_thread_another_process_owns(self) -> None:
        # thread/list reports a desktop-owned thread as not-loaded to any second
        # reader, so its own status field looks inactive. The rollout log is
        # what proves the turn is running, and the list must trust it -- the row
        # used to stay "idle" while the chat header showed "working".
        self.client.responses["thread/list"] = {
            "data": [thread_row("t1", status={"type": "notLoaded"})],
            "nextCursor": None,
        }
        self._write_rollout("t1", "task_started", seconds_ago=3)

        result = self.service.list_threads(limit=10)

        self.assertEqual(len(result["threads"]), 1)
        self.assertEqual(result["threads"][0]["status"], "working")

    def test_stale_mtime_does_not_hide_a_live_turn(self) -> None:
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{datetime.now().year:04d}"
            / f"{datetime.now().month:02d}"
            / f"{datetime.now().day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "rollout-t2.jsonl"
        stamp = datetime.now(timezone.utc)
        path.write_text(
            json.dumps(
                {
                    "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stale = time.time() - 3600.0
        os.utime(path, (stale, stale))

        # Windows reports a frozen mtime for a rollout that is still being
        # appended to, so a stale mtime must not hide a live turn.
        self.assertEqual(self.service._log_status("t2"), "working")

        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "working")

        # The guard still works: when the records are genuinely old the thread is
        # idle, so a frozen mtime cannot invent a running turn. Rewrite the log
        # with an old stamp and clear the short-lived tail cache before re-reading.
        old = time.time() - LOG_ACTIVE_SECONDS - 60.0
        path.write_text(json.dumps({"timestamp": datetime.fromtimestamp(old, timezone.utc).isoformat().replace("+00:00", "Z"), "type": "event_msg", "payload": {"type": "task_started"}}) + "\n", encoding="utf-8")
        with self.service._log_cache_lock:
            self.service._log_cache.clear()
        self.assertEqual(self.service._log_status("t2"), "")

    def test_rollout_recently_written_still_reports_working(self) -> None:
        # The same log, but actively appended: still a live desktop turn.
        self._write_rollout("t2", "task_started")
        self.assertEqual(self.service._log_status("t2"), "working")

    def _append_rollout_record(self, thread_id: str, payload_type: str, seconds_ago: float = 0.0) -> None:
        day = datetime.now()
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        record = {
            "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": payload_type},
        }
        path = folder / f"rollout-{thread_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_resume_bookkeeping_does_not_force_working(self) -> None:
        # Resuming or reading a long-finished task appends a
        # ``thread_settings_applied`` record with a fresh timestamp, refreshing
        # the file mtime even though no turn is running. That bookkeeping line
        # must never make an idle task flicker back into "working".
        self._write_rollout("t2", "task_complete", seconds_ago=3600)
        self._append_rollout_record("t2", "thread_settings_applied", seconds_ago=1)
        self.assertEqual(self.service._log_status("t2"), "completed")

    def test_real_activity_after_bookkeeping_still_reports_working(self) -> None:
        # A genuine turn writes real activity records, not just the settings
        # bookkeeping line, so a live turn stays "working".
        self._write_rollout("t2", "task_complete", seconds_ago=3600)
        self._append_rollout_record("t2", "thread_settings_applied", seconds_ago=2)
        self._append_rollout_record("t2", "item_completed", seconds_ago=1)
        self.assertEqual(self.service._log_status("t2"), "working")

    def test_list_threads_resume_bookkeeping_stays_idle(self) -> None:
        # The phone list must not flip a finished task to "working" purely
        # because the desktop resumed/read it and touched its rollout file.
        self._write_rollout("t2", "task_complete", seconds_ago=3600)
        self._append_rollout_record("t2", "thread_settings_applied", seconds_ago=1)
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertNotEqual(statuses["t2"], "working")

    def test_list_threads_marks_finished_rollout_completed(self) -> None:
        self._write_rollout("t2", "task_complete", seconds_ago=30)
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "completed")

    def test_read_thread_uses_live_rollout_status_for_desktop_turn(self) -> None:
        detail = thread_detail("t2")
        detail["thread"]["status"] = {"type": "notLoaded"}
        detail["thread"]["turns"][-1]["status"] = "interrupted"
        self.client.responses["thread/read"] = detail
        self._write_rollout("t2", "task_started", seconds_ago=1)

        result = self.service.read_thread("t2")

        self.assertEqual(result["thread"]["status"], "working")
        self.assertEqual(result["task"]["status"], "working")

    def test_list_threads_marks_read_completion_idle(self) -> None:
        self._write_rollout("t2", "task_complete", seconds_ago=30)
        read_state = ReadStateStore(None)
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            read_state=read_state,
        )
        try:
            service.list_threads(limit=10)  # populate the log cache
            read_state.mark_thread("t2", int(self.service._thread_log("t2")[2]))
            result = service.list_threads(limit=10)
        finally:
            service.stop()
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "idle")

    def test_list_threads_ignores_stale_rollout(self) -> None:
        self._write_rollout("t2", "task_started", seconds_ago=600)
        self.client.responses["thread/read"] = thread_detail("t2")
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "completed")

    def test_poll_detects_completion_without_replaying_old_completions(self) -> None:
        target = self.service.subscribe()
        self.service.poll_thread_changes()
        self.assertTrue(target.empty())
        self.client.responses["thread/list"] = {"data": [thread_row(turns=[{
            "id": "new-turn", "status": "completed", "completedAt": 1791000300,
        }])]}
        self.service.poll_thread_changes()
        events = []
        while not target.empty():
            events.append(target.get_nowait())
        completions = [event for event in events if event["type"] == "turn_completed"]
        self.assertEqual([event["turn_id"] for event in completions], ["new-turn"])
        self.service.poll_thread_changes()
        self.assertTrue(target.empty())

    def test_list_threads_maps_rows(self) -> None:
        result = self.service.list_threads(limit=10)
        self.assertEqual(len(result["threads"]), 2)
        first = result["threads"][0]
        self.assertEqual(first["thread_id"], "t1")
        self.assertEqual(first["title"], "桌面任务")
        self.assertEqual(first["model"], "gpt-5.1-codex")
        self.assertIn(first["status"], {"idle", "completed", "working"})
        self.assertEqual(self.client.requests[0][0], "thread/list")

    def test_read_thread_hides_intermediate_events_by_default(self) -> None:
        detail = self.service.read_thread("t1")
        kinds = [item["kind"] for item in detail["messages"]]
        self.assertEqual(kinds, ["user", "agent"])
        self.assertEqual(detail["messages"][0]["text"], "帮我看一下构建结果")
        self.assertEqual(detail["messages"][1]["text"], "构建通过")
        self.assertEqual(detail["message_count"], 2)
        self.assertEqual(detail["thread"]["completed_at"], 1791000100)

    def test_read_thread_includes_intermediate_events_when_enabled(self) -> None:
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            show_intermediate_events=True,
        )
        try:
            detail = service.read_thread("t1")
        finally:
            service.stop()
        kinds = [item["kind"] for item in detail["messages"]]
        self.assertEqual(kinds, ["user", "commentary", "agent", "command"])
        self.assertEqual(detail["messages"][3]["detail"]["exit_code"], 0)
        self.assertEqual(detail["messages"][3]["detail"]["output"], "BUILD SUCCESSFUL")

    def test_detail_reports_one_status_for_list_and_chat(self) -> None:
        # The phone list renders thread.status while the chat header renders
        # task.status. They must never disagree for the same thread.
        self.client.responses["thread/read"] = self._interrupted_desktop_thread()

        detail = self.service.read_thread("t1")

        self.assertEqual(detail["thread"]["status"], detail["task"]["status"])

    def test_interrupted_turn_without_live_activity_is_not_working(self) -> None:
        # A desktop thread that stopped long ago must not be reported as a
        # running turn, otherwise the chat header shows "正在处理任务" forever.
        self.client.responses["thread/read"] = self._interrupted_desktop_thread()

        detail = self.service.read_thread("t1")

        self.assertNotEqual(detail["task"]["status"], "working")
        self.assertNotEqual(detail["thread"]["status"], "working")

    def _interrupted_desktop_thread(self, seconds_ago: float = 833.0) -> dict:
        """A desktop-owned thread whose last turn was interrupted a while ago.

        Mirrors a real thread (01a13af3): the last turn is ``interrupted`` with
        ``completedAt: None`` and a single userMessage item, runtime status is
        ``notLoaded``, source is ``vscode``, and ``recencyAt``/``updatedAt`` are
        ``seconds_ago`` old (well past any plausible activity window).
        CodexBridge must not call this a running turn.
        """
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        epoch = int(stamp.timestamp())
        started = (stamp - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        older = (stamp - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
        return {
            "thread": {
                **thread_row("t1"),
                "status": {"type": "notLoaded"},
                "source": "vscode",
                "recencyAt": epoch,
                "updatedAt": epoch,
                "createdAt": older,
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "startedAt": older,
                        "completedAt": older,
                        "items": [
                            {
                                "id": "item-1",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "第一句"}],
                            }
                        ],
                    },
                    {
                        "id": "turn-2",
                        # Real payloads carry no updatedAt on an interrupted turn.
                        "status": "interrupted",
                        "startedAt": started,
                        "completedAt": None,
                        "items": [
                            {
                                "id": "item-2",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "继续"}],
                            }
                        ],
                    },
                ],
            }
        }

    def test_new_thread_uses_pending_title_and_tracks_untitled(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        creator = FakeClient()
        creator.responses['thread/start'] = {'thread': thread_row(name='')}
        creator.responses['thread/name/set'] = {}
        self.service._creation_client_factory = lambda: creator
        result = self.service.start_thread()
        self.assertIn(('thread/name/set', {'threadId': 't1', 'name': '新任务'}), creator.requests)
        self.assertIn('t1', self.service._untitled_threads)
        self.assertNotIn('新任务', result['thread']['title'])

    def test_first_user_turn_generates_and_persists_title(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.return_value = {'turn': {'id': 'desktop-turn'}}
        self.service._desktop = desktop
        self.service._untitled_threads.add('t1')
        events = self.service.subscribe()
        self.service.start_turn('t1', '修复手机列表状态不同步的问题。还有通知')
        self.assertEqual(self.service._ensure_client().requests[-1], (
            'thread/name/set', {'threadId': 't1', 'name': '修复手机列表状态不同步的问题'}))
        self.assertNotIn('t1', self.service._untitled_threads)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        names = [event for event in emitted if event['type'] == 'thread_name']
        self.assertEqual(names[-1]['title'], '修复手机列表状态不同步的问题')
        self.assertTrue(any(event['type'] == 'threads_changed' for event in emitted))

    def test_generated_title_failure_does_not_break_start_turn(self) -> None:
        from unittest.mock import Mock
        from codexbridge.codex.desktop_ipc import DesktopIpcError
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        desktop.start_turn.return_value = {'turn': {'id': 'desktop-turn'}}
        self.service._desktop = desktop
        self.service._untitled_threads.add('t1')
        self.service.rename_thread = Mock(side_effect=DesktopIpcError('name write unavailable'))
        result = self.service.start_turn('t1', '继续处理')
        self.assertEqual(result['status'], 'working')
        self.assertEqual(result['turn_id'], 'desktop-turn')

    def test_rename_thread_publishes_and_invalidates_desktop(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        self.service._desktop = desktop
        self.client.responses['thread/name/set'] = {}
        events = self.service.subscribe()
        result = self.service.rename_thread('t1', '  新标题  ')
        self.assertEqual(result['title'], '新标题')
        self.assertIn(('thread/name/set', {'threadId': 't1', 'name': '新标题'}), self.client.requests)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        self.assertTrue(any(event['type'] == 'thread_name' and event['title'] == '新标题' for event in emitted))
        desktop.invalidate_query_cache.assert_any_call(['tasks'])
    def test_start_turn_publishes_event(self) -> None:
        target = self.service.subscribe()
        result = self.service.start_turn("t1", "继续修复", model="gpt-5.1-codex")
        self.assertEqual(result["turn_id"], "turn-2")
        event = target.get(timeout=1.0)
        self.assertEqual(event["type"], "turn_started")
        self.assertEqual(event["thread_id"], "t1")
        method, params = self.client.requests[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"][0]["text"], "继续修复")
        self.assertEqual(params["model"], "gpt-5.1-codex")

    def test_start_turn_falls_back_to_fork_when_lock_cannot_be_cleared(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}

        def resume(_params):
            calls["resume"] += 1
            raise AppServerError("thread t1 already has an active writer")

        def fork(params):
            calls["fork"] += 1
            self.assertEqual(params["threadId"], "t1")
            return {"thread": thread_row("t1-fork")}

        def start(params):
            calls["start"] += 1
            self.assertEqual(params["threadId"], "t1-fork")
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        with mock.patch(
            "codexbridge.companion.service._clear_writer_lock", return_value=False
        ):
            result = self.service.start_turn("t1", "继续这个任务", model="gpt-5.1-codex")

        self.assertEqual(result["status"], "working")
        self.assertEqual(result["continued_as"], "t1-fork")
        self.assertEqual(calls, {"resume": 2, "fork": 1, "start": 1})

    def test_active_writer_lock_is_cleared_before_forking(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}
        original_resume = {"thread": thread_row()}

        def resume(_params):
            calls["resume"] += 1
            if calls["resume"] == 1:
                raise AppServerError("thread t1 already has an active writer")
            return original_resume

        def fork(_params):
            calls["fork"] += 1
            return {"thread": thread_row("t1-fork")}

        def start(params):
            calls["start"] += 1
            self.assertEqual(params["threadId"], "t1")
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        with mock.patch(
            "codexbridge.companion.service._clear_writer_lock", return_value=True
        ) as clear:
            result = self.service.start_turn("t1", "继续同一个任务")

        self.assertEqual(result["status"], "working")
        self.assertEqual(result["continued_as"], "")
        self.assertEqual(calls, {"resume": 2, "fork": 0, "start": 1})
        clear.assert_called_once_with("t1")

    def test_stale_writer_lock_file_is_removed_from_disk(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"USERPROFILE": self.temp.name, "HOME": self.temp.name},
        ):
            lock_dir = Path(self.temp.name) / ".codex" / "thread-writer-locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock = lock_dir / "t1.lock"
            lock.write_bytes(b"locked")
            self.client.responses["thread/resume"] = lambda _params: (_ for _ in ()).throw(
                AppServerError("thread t1 already has an active writer")
            )

            with mock.patch(
                "codexbridge.companion.service.writer_lock_path",
                return_value=lock,
            ):
                self.service._ensure_thread("t1")

            self.assertFalse(lock.exists())
            self.assertFalse((lock_dir / "t1.lock.removed").exists())

    def test_turn_completed_event_marks_history_append(self) -> None:
        payload = {
            "threadId": "t1",
            "turn": {"id": "turn-9", "status": "completed", "completedAt": 1791000300},
        }

        event = self.service._event_payload({"method": "turn/completed", "params": payload})

        self.assertTrue(event["append_only"])

    def test_start_turn_forks_while_the_desktop_runs_a_turn(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}

        def resume(_params):
            calls["resume"] += 1
            raise AppServerError("thread t1 already has an active writer")

        def fork(params):
            calls["fork"] += 1
            self.assertEqual(params["threadId"], "t1")
            return {"thread": thread_row("t1-fork")}

        def start(params):
            calls["start"] += 1
            # turn runs on the fork we own, not the locked original
            self.assertEqual(params["threadId"], "t1-fork")
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        self._write_rollout("t1", "task_started", seconds_ago=5)
        result = self.service.start_turn("t1", "继续这个任务", model="gpt-5.1-codex")

        self.assertEqual(result["status"], "working")
        self.assertEqual(result["continued_as"], "t1-fork")
        self.assertEqual(calls, {"resume": 1, "fork": 1, "start": 1})

    def test_start_turn_reuses_fork_on_next_reply(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        forks = {"n": 0}

        def fork(_params):
            forks["n"] += 1
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "第一次")
        result = self.service.start_turn("t1", "第二次")

        self.assertEqual(forks["n"], 1)
        self.assertEqual(result["continued_as"], "t1-fork")

    def test_fork_is_named_so_the_desktop_can_tell_it_apart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            self.assertEqual(params["threadId"], "t1-fork")
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        names: list[dict] = []

        def name_set(params):
            names.append(params)
            return {}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = name_set

        self.service.start_turn("t1", "继续这个任务")

        self.assertEqual(len(names), 1)
        self.assertEqual(names[0]["threadId"], "t1-fork")
        self.assertIn("手机继续", names[0]["name"])
        self.assertIn("桌面任务", names[0]["name"])

    def test_renaming_the_fork_keeps_the_phone_task_title(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            if params["threadId"] == "t1-fork":
                # Once renamed, the stored fork no longer carries the old name.
                return {"thread": thread_row("t1-fork", name="手机继续 · 桌面任务")}
            return thread_detail()

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = {}

        self.service.start_turn("t1", "继续这个任务")

        listing = self.service.list_threads()
        row = next(item for item in listing["threads"] if item["thread_id"] == "t1")
        self.assertEqual(row["title"], "桌面任务")

        detail = self.service.read_thread("t1")
        self.assertEqual(detail["thread"]["title"], "桌面任务")

    def test_fork_titles_survive_restart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork", name="桌面任务")}

        def read(params):
            if params["threadId"] == "t1-fork":
                return {"thread": thread_row("t1-fork", name="手机继续 · 桌面任务")}
            return thread_detail()

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/read"] = read
        self.client.responses["thread/name/set"] = {}

        self.service.start_turn("t1", "继续这个任务")

        restarted = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            detail = restarted.read_thread("t1")
            self.assertEqual(detail["thread"]["title"], "桌面任务")
        finally:
            restarted.stop()

    def test_fork_naming_failure_does_not_break_the_reply(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        def name_set(_params):
            raise AppServerError("thread metadata update failed")

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["thread/name/set"] = name_set

        result = self.service.start_turn("t1", "继续这个任务")

        self.assertEqual(result["continued_as"], "t1-fork")

    def test_fork_publishes_thread_forked_event(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        subscriber = self.service.subscribe()
        try:
            self.service.start_turn("t1", "继续")
            events = []
            while not subscriber.empty():
                events.append(subscriber.get_nowait())
        finally:
            self.service.unsubscribe(subscriber)

        forked = [event for event in events if event.get("type") == "thread_forked"]
        self.assertEqual(len(forked), 1)
        self.assertEqual(forked[0]["thread_id"], "t1")
        self.assertEqual(forked[0]["continued_as"], "t1-fork")

    def test_existing_fork_mapping_is_reclaimed_back_to_the_original(self) -> None:
        # A fork recorded by an earlier build. The phone must go back to the
        # original thread instead of keeping a copy the desktop never opens.
        fork_map = Path(self.temp.name) / "fork-map.json"
        fork_map.write_text(
            json.dumps({"version": 1, "forks": {"t1": "t1-fork"}}),
            encoding="utf-8",
        )
        self.client.responses["thread/resume"] = {"thread": thread_row("t1")}
        self.client.responses["thread/read"] = {"thread": thread_row("t1")}
        names: list[dict] = []
        self.client.responses["thread/name/set"] = lambda params: names.append(params) or {}
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            result = service.start_turn("t1", "\u7ee7\u7eed")
        finally:
            service.stop()

        self.assertEqual(result["continued_as"], "")
        started = [params for method, params in self.client.requests if method == "turn/start"]
        self.assertEqual(started[-1]["threadId"], "t1")
        # The reclaimed fork is only kept for reading, never renamed.
        self.assertEqual(names, [])
        saved = json.loads(fork_map.read_text(encoding="utf-8"))
        self.assertEqual(saved["forks"], {})
        self.assertEqual(saved["tails"], {"t1": "t1-fork"})

    def test_reclaimed_fork_history_stays_visible(self) -> None:
        fork_map = Path(self.temp.name) / "fork-map.json"
        fork_map.write_text(
            json.dumps({"version": 1, "forks": {"t1": "t1-fork"}, "tails": {"t1": "t1-fork"}}),
            encoding="utf-8",
        )
        # The original carries the desktop history; the fork carries an extra
        # turn the phone made while the desktop held the writer lock.
        self.client.responses["thread/resume"] = {"thread": thread_row("t1")}
        self.client.responses["thread/read"] = thread_detail("t1")
        fork_turn = {
            "id": "turn-phone",
            "status": "completed",
            "startedAt": 1791000400,
            "items": [
                {"id": "i1", "type": "userMessage", "content": [{"type": "input_text", "text": "\u624b\u673a\u53d1\u7684"}]}
            ],
        }

        def read(params):
            if params.get("threadId") == "t1-fork":
                return {"thread": thread_row("t1-fork", turns=[fork_turn])}
            return thread_detail("t1")

        self.client.responses["thread/read"] = read
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            detail = service.read_thread("t1")
        finally:
            service.stop()

        texts = [str(message.get("text") or "") for message in detail["messages"]]
        self.assertIn("\u624b\u673a\u53d1\u7684", texts)

    def test_fork_tail_read_failure_does_not_break_the_reply(self) -> None:
        fork_map = Path(self.temp.name) / "fork-map.json"
        fork_map.write_text(
            json.dumps({"version": 1, "tails": {"t1": "t1-fork"}}),
            encoding="utf-8",
        )
        self.client.responses["thread/resume"] = {"thread": thread_row("t1")}

        def read(params):
            if params.get("threadId") == "t1-fork":
                raise AppServerError("thread not found")
            return thread_detail("t1")

        self.client.responses["thread/read"] = read
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            detail = service.read_thread("t1")
        finally:
            service.stop()
        self.assertTrue(detail["messages"])

    def test_read_thread_reports_continuation_fork(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "继续")

        detail = self.service.read_thread("t1")
        self.assertEqual(detail["continued_as"], "t1-fork")

    def test_read_thread_reports_no_continuation_for_free_thread(self) -> None:
        detail = self.service.read_thread("t1")
        self.assertEqual(detail["continued_as"], "")

    def test_failed_turn_without_end_time_is_not_reported_as_failed(self) -> None:
        # A provider reconnect can mark the turn failed before it really ends.
        # Without an end timestamp the task is still in flight.
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "failed"
        turn.pop("completedAt", None)
        turn["startedAt"] = int(time.time()) - 5
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "working")
        self.assertEqual(result["thread"]["completed_at"], 0)
        self.assertEqual(result["task"]["status"], "working")

    def test_failed_turn_with_end_time_stays_terminal(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "failed"
        turn["completedAt"] = 1791000400
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "failed")
        self.assertEqual(result["thread"]["completed_at"], 1791000400)
        self.assertEqual(result["task"]["completed_at_epoch"], 1791000400)

    def test_completed_turn_without_end_time_gets_one(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        turn.pop("completedAt", None)
        turn["updatedAt"] = 1791000500
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "completed")
        self.assertEqual(result["thread"]["completed_at"], 1791000500)
        self.assertEqual(result["task"]["completed_at_epoch"], 1791000500)

    def test_completed_turn_without_timestamps_falls_back_to_now(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        for key in ("completedAt", "updatedAt", "startedAt"):
            turn.pop(key, None)
        detail["thread"].pop("recencyAt", None)
        detail["thread"].pop("updatedAt", None)
        self.client.responses["thread/read"] = detail

        before = int(time.time())
        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "completed")
        self.assertGreaterEqual(result["thread"]["completed_at"], before)

    def test_missing_turn_state_is_idle_not_completed(self) -> None:
        detail = thread_detail("t1")
        detail["thread"]["turns"][0].pop("status", None)
        detail["thread"]["turns"][0].pop("completedAt", None)
        detail["thread"]["status"] = {"type": "notLoaded", "activeFlags": []}
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "idle")
        self.assertEqual(result["thread"]["completed_at"], 0)

    def test_read_persists_when_completed_at_is_missing(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        turn.pop("completedAt", None)
        turn["updatedAt"] = 1791000600
        detail["thread"]["recencyAt"] = 1791000600
        self.client.responses["thread/read"] = detail
        self.client.responses["thread/list"] = {
            "data": [{**thread_row("t1"), "recencyAt": 1791000600}],
            "nextCursor": None,
        }

        self.service.mark_thread_read("t1")
        page = self.service.list_threads()

        row = next(item for item in page["threads"] if item["thread_id"] == "t1")
        self.assertEqual(row["status"], "idle")

    def test_thread_summary_surfaces_provider_failure_reason(self) -> None:
        payload = json.dumps(
            {
                "error": {
                    "message": "[workbuddy] upstream client (http 400): "
                    + json.dumps({"code": 11129, "msg": "invalid function call parameters"})
                }
            }
        )
        detail = thread_detail("t1")
        detail["thread"]["turns"][0]["status"] = "failed"
        detail["thread"]["turns"][0]["error"] = {"message": payload}
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(
            result["thread"]["error_message"],
            "[workbuddy] upstream client (http 400): invalid function call parameters",
        )

    def test_completed_turn_without_timestamp_gets_one(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        turn.pop("completedAt", None)
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "completed")
        self.assertGreater(result["thread"]["completed_at"], 0)
        self.assertEqual(result["task"]["status"], "completed")
        self.assertGreater(result["task"]["completed_at_epoch"], 0)

    def test_failed_turn_without_timestamp_stays_working(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "failed"
        turn.pop("completedAt", None)
        turn.pop("startedAt", None)
        detail["thread"].pop("updatedAt", None)
        detail["thread"].pop("recencyAt", None)
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        # No end time means the turn is still in flight, not a phantom failure.
        self.assertEqual(result["thread"]["status"], "working")
        self.assertEqual(result["thread"]["completed_at"], 0)

    def test_failed_turn_with_timestamp_is_terminal(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "failed"
        turn["completedAt"] = 1791000200
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "failed")
        self.assertEqual(result["thread"]["completed_at"], 1791000200)

    def test_missing_turn_status_is_not_inferred_as_completed(self) -> None:
        detail = thread_detail("t1")
        detail["thread"]["turns"][0].pop("status", None)
        self.client.responses["thread/read"] = detail

        result = self.service.read_thread("t1")

        self.assertEqual(result["thread"]["status"], "idle")

    def test_terminal_timestamp_never_moves_backwards(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        turn["completedAt"] = 1791000500
        self.client.responses["thread/read"] = detail
        self.service.read_thread("t1", message_limit=1)

        # A later read reports an older completion time; the monotonic guard
        # must keep the newest one.
        turn["completedAt"] = 1791000100
        self.service._read_cache.clear()
        again = self.service.read_thread("t1", message_limit=1)

        self.assertEqual(again["thread"]["completed_at"], 1791000500)

    def test_mark_thread_read_persists_without_terminal_timestamp(self) -> None:
        detail = thread_detail("t1")
        turn = detail["thread"]["turns"][0]
        turn["status"] = "completed"
        turn.pop("completedAt", None)
        self.client.responses["thread/read"] = detail

        result = self.service.mark_thread_read("t1")

        self.assertEqual(result["status"], "idle")
        self.assertGreater(result["read_at"], 0)
        self.assertTrue(self.service._read_state.thread_read_at("t1") > 0)

    def test_turn_completed_event_carries_provider_error(self) -> None:
        payload = {
            "threadId": "t1",
            "turn": {
                "id": "turn-9",
                "status": "failed",
                "completedAt": 1791000300,
                "error": {"message": json.dumps({"error": {"message": "provider exploded"}})},
            },
        }

        event = self.service._event_payload({"method": "turn/completed", "params": payload})

        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_message"], "provider exploded")

    def test_turn_completed_event_without_end_time_stays_running(self) -> None:
        payload = {
            "threadId": "t1",
            "turn": {
                "id": "turn-9",
                "status": "failed",
                "error": {"message": "connection reset"},
            },
        }

        event = self.service._event_payload({"method": "turn/completed", "params": payload})

        self.assertEqual(event["status"], "working")
        self.assertEqual(event["completed_at"], 0)
        self.assertFalse(event["read"])

    def test_fork_rename_does_not_retitle_the_phone_thread(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        self.service.start_turn("t1", "继续")

        event = self.service._event_payload(
            {"method": "thread/name/updated", "params": {"threadId": "t1-fork", "name": "手机继续 · 桌面任务"}}
        )
        self.assertIsNone(event)

    def test_start_turn_reports_locked_thread_when_fork_fails(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            raise AppServerError("fork unsupported")

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork

        with self.assertRaises(AppServerError) as ctx:
            self.service.start_turn("t1", "继续")

        self.assertIn("电脑端", str(ctx.exception))

    def test_forked_reply_is_transparent_in_list_and_read(self) -> None:
        # Lock t1, continue it, then make sure the phone still sees one task
        # named t1 whose history comes from the fork.
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        detail = thread_detail("t1-fork")
        detail["thread"]["turns"][0]["status"] = "completed"
        self.client.responses["thread/read"] = detail
        self.client.responses["thread/list"] = {
            "data": [thread_row("t1-fork"), thread_row("t2", name="第二个任务")],
            "nextCursor": None,
        }

        self.service.start_turn("t1", "继续这个任务")

        listing = self.service.list_threads(limit=10)
        visible = [item["thread_id"] for item in listing["threads"]]
        self.assertIn("t1", visible)
        self.assertNotIn("t1-fork", visible)

        read = self.service.read_thread("t1")
        self.assertEqual(read["thread"]["thread_id"], "t1")
        self.assertEqual(read["task"]["thread_id"], "t1")

    def test_fork_completion_flips_to_idle_after_read(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        detail = thread_detail("t1-fork")
        self.client.responses["thread/read"] = detail
        self.client.responses["thread/list"] = {"data": [thread_row("t1-fork")], "nextCursor": None}

        self.service.start_turn("t1", "继续")
        self._write_rollout("t1-fork", "task_complete", seconds_ago=20)

        before = self.service.list_threads(limit=10)["threads"][0]
        self.assertEqual(before["status"], "completed")

        self.service.mark_thread_read("t1")

        after = self.service.list_threads(limit=10)["threads"][0]
        self.assertEqual(after["thread_id"], "t1")
        self.assertEqual(after["status"], "idle")

    def test_fork_events_report_origin_thread_id(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.service.start_turn("t1", "继续")

        payload = self.service._event_payload(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "t1-fork", "turnId": "turn-2", "itemId": "i1", "delta": "好"},
            }
        )
        self.assertEqual(payload["thread_id"], "t1")

    def test_fork_mapping_survives_restart(self) -> None:
        def resume(_params):
            raise AppServerError("thread t1 already has an active writer")

        def fork(_params):
            return {"thread": thread_row("t1-fork")}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.service.start_turn("t1", "继续")
        self.service.stop()

        restored = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            forks = {"n": 0}

            def fork_again(_params):
                forks["n"] += 1
                return {"thread": thread_row("t1-fork2")}

            self.client.responses["thread/fork"] = fork_again
            result = restored.start_turn("t1", "第二次")
        finally:
            restored.stop()
        # The persisted mapping is reused, so no second fork is created.
        self.assertEqual(forks["n"], 0)
        self.assertEqual(result["continued_as"], "t1-fork")

    def test_start_turn_tolerates_active_writer_during_resume(self) -> None:
        calls = {"resume": 0, "fork": 0, "start": 0}

        def resume(_params):
            calls["resume"] += 1
            raise AppServerError("thread already has an active writer")

        def fork(_params):
            calls["fork"] += 1
            return {"thread": thread_row("t1-fork")}

        def start(_params):
            calls["start"] += 1
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["thread/resume"] = resume
        self.client.responses["thread/fork"] = fork
        self.client.responses["turn/start"] = start

        self._write_rollout("t1", "task_started", seconds_ago=5)
        result = self.service.start_turn("t1", "继续这个旧任务")

        self.assertEqual(result["status"], "working")
        self.assertEqual(calls, {"resume": 1, "fork": 1, "start": 1})

    def test_turn_completed_event_uses_desktop_completion_timestamp(self) -> None:
        event = self.service._event_payload(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t1",
                    "turn": {
                        "id": "turn-2",
                        "status": "completed",
                        "completedAt": 1791000100,
                        "durationMs": 1200,
                    },
                },
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["completed_at"], 1791000100)

    def test_steer_turn_requires_an_active_turn(self) -> None:
        # A resident thread with no in-progress turn cannot be steered; the
        # app-server requires expectedTurnId, so surface a clear error instead
        # of sending an invalid steer.
        calls = {"steer": 0}

        def steer(_params):
            calls["steer"] += 1
            return {"turn": {"id": "turn-3", "status": "inProgress"}}

        self.client.responses["turn/steer"] = steer

        with self.assertRaises(AppServerError):
            self.service.steer_turn("t1", "换个方向")

        self.assertEqual(calls["steer"], 0)

    def test_steer_turn_sends_expected_turn_id(self) -> None:
        detail = thread_detail()
        detail["thread"]["turns"].append(
            {
                "id": "turn-9",
                "status": "inProgress",
                "startedAt": 1791000200,
                "items": [],
            }
        )
        self.client.responses["thread/read"] = detail
        seen = {}

        def steer(params):
            seen.update(params)
            return {"turn": {"id": "turn-9", "status": "inProgress"}}

        self.client.responses["turn/steer"] = steer
        self.service.steer_turn("t1", "换个方向")

        self.assertEqual(seen["expectedTurnId"], "turn-9")
        self.assertEqual(seen["threadId"], "t1")
    def test_start_turn_resumes_disk_thread_first(self) -> None:
        self.service.start_turn("t1", "继续修复", model="gpt-5.1-codex")
        methods = [method for method, _ in self.client.requests]
        self.assertIn("thread/resume", methods)
        self.assertLess(methods.index("thread/resume"), methods.index("turn/start"))
        resume_params = next(
            params for method, params in self.client.requests if method == "thread/resume"
        )
        self.assertEqual(resume_params["threadId"], "t1")
        self.assertTrue(resume_params["excludeTurns"])

    def test_start_turn_skips_repeat_resume_for_same_thread(self) -> None:
        self.service.start_turn("t1", "第一次")
        self.service.start_turn("t1", "第二次")
        resumes = [method for method, _ in self.client.requests if method == "thread/resume"]
        self.assertEqual(len(resumes), 1)
        turns = [method for method, _ in self.client.requests if method == "turn/start"]
        self.assertEqual(len(turns), 2)

    def test_steer_turn_resumes_thread(self) -> None:
        detail = thread_detail()
        detail["thread"]["turns"].append(
            {"id": "turn-9", "status": "inProgress", "startedAt": 1791000200, "items": []}
        )
        self.client.responses["thread/read"] = detail
        self.client.responses["turn/steer"] = {"turn": {"id": "turn-9", "status": "inProgress"}}
        self.service.steer_turn("t1", "改一下方向")
        methods = [method for method, _ in self.client.requests]
        self.assertLess(methods.index("thread/resume"), methods.index("turn/steer"))

    def test_resume_cleared_after_client_reset(self) -> None:
        clients = []

        def factory(command, cwd):
            client = FakeClient()
            clients.append(client)
            client.responses["thread/resume"] = {"thread": thread_row()}
            client.responses["turn/start"] = {"turn": {"id": "turn-2", "status": "inProgress"}}
            return client

        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=factory,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
        )
        try:
            service.start_turn("t1", "第一次")
            service._reset_client()
            service.start_turn("t1", "第二次")
        finally:
            service.stop()
        self.assertGreaterEqual(len(clients), 2)
        self.assertEqual(
            [method for method, _ in clients[0].requests if method == "thread/resume"],
            ["thread/resume"],
        )
        self.assertEqual(
            [method for method, _ in clients[1].requests if method == "thread/resume"],
            ["thread/resume"],
        )

    def test_completed_thread_shows_idle_once_read(self) -> None:
        read_state = ReadStateStore(None)
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            read_state=read_state,
        )
        try:
            self.assertEqual(service.read_thread("t1")["thread"]["status"], "completed")
            read_state.mark_thread("t1", 1791000100)
            self.assertEqual(service.read_thread("t1")["thread"]["status"], "idle")
        finally:
            service.stop()

    def test_mark_thread_read_publishes_idle_for_other_clients(self) -> None:
        target = self.service.subscribe()

        result = self.service.mark_thread_read("t1")

        self.assertEqual(result["status"], "idle")
        event = target.get(timeout=1.0)
        self.assertEqual(event["type"], "thread_status")
        self.assertEqual(event["thread_id"], "t1")
        self.assertEqual(event["status"], "idle")

    def test_event_payload_hides_intermediate_events_by_default(self) -> None:
        command_output = self.service._event_payload({
            "method": "item/commandExecution/outputDelta",
            "params": {
                "threadId": "t1",
                "turnId": "turn-1",
                "itemId": "command-1",
                "delta": "BUILD SUCCESSFUL",
            },
        })
        command_item = self.service._event_payload({
            "method": "item/completed",
            "params": {
                "threadId": "t1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "command": "gradle build",
                    "status": "completed",
                },
            },
        })

        self.assertIsNone(command_output)
        self.assertIsNone(command_item)

    def test_event_payload_hides_commentary_delta_by_default(self) -> None:
        started = self.service._event_payload({
            "method": "item/started",
            "params": {
                "threadId": "t1",
                "turnId": "turn-1",
                "item": {
                    "id": "commentary-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "先检查日志",
                },
            },
        })
        commentary_delta = self.service._event_payload({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "t1",
                "turnId": "turn-1",
                "itemId": "commentary-1",
                "delta": "先检查日志",
            },
        })
        final_delta = self.service._event_payload({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "t1",
                "turnId": "turn-1",
                "itemId": "final-1",
                "delta": "构建通过",
            },
        })

        self.assertIsNone(started)
        self.assertIsNone(commentary_delta)
        self.assertIsNotNone(final_delta)
        self.assertEqual(final_delta["delta"], "构建通过")

    def test_event_payload_includes_commentary_delta_when_enabled(self) -> None:
        service = CompanionService(
            data_dir=Path(self.temp.name),
            cwd=Path(self.temp.name),
            client_factory=lambda command, cwd: self.client,
            desktop_state_path=Path(self.temp.name) / "missing.json",
            sessions_root=Path(self.temp.name) / "sessions",
            show_intermediate_events=True,
        )
        try:
            started = service._event_payload({
                "method": "item/started",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "commentary-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "先检查日志",
                    },
                },
            })
            delta = service._event_payload({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn-1",
                    "itemId": "commentary-1",
                    "delta": "先检查日志",
                },
            })
        finally:
            service.stop()

        self.assertIsNotNone(started)
        self.assertIsNotNone(delta)
        self.assertEqual(delta["delta"], "先检查日志")

    def test_notification_pump_forwards_agent_delta(self) -> None:
        self.service.start()
        target = self.service.subscribe()
        self.client.notifications.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "t1", "turnId": "turn-2", "itemId": "i1", "delta": "你好"},
            }
        )
        event = target.get(timeout=2.0)
        self.assertEqual(event["type"], "agent_delta")
        self.assertEqual(event["delta"], "你好")

    def test_approval_request_can_be_resolved(self) -> None:
        self.service.start()
        target = self.service.subscribe()
        self.client.server_requests.put(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "t1",
                    "turnId": "turn-2",
                    "itemId": "i9",
                    "command": "rm -rf build",
                    "reason": "需要删除构建目录",
                },
            }
        )
        event = target.get(timeout=2.0)
        self.assertEqual(event["type"], "approval_request")
        self.assertEqual(event["request_id"], 7)
        self.assertEqual(event["command"], "rm -rf build")
        pending = self.service.pending_approvals()
        self.assertEqual(len(pending), 1)
        self.assertTrue(self.service.resolve_approval(7, "accept"))
        self.assertEqual(self.client.responses_sent[-1], (7, {"decision": "accept"}))
        self.assertEqual(self.service.pending_approvals(), [])

    def test_models_and_projects(self) -> None:
        models = self.service.list_models()["models"]
        self.assertEqual(models[0]["display_name"], "GPT-5.1 Codex")
        self.assertTrue(models[0]["is_default"])
        projects = self.service.list_projects()
        self.assertIsInstance(projects["projects"], list)

    def test_snapshot_includes_account_and_tasks(self) -> None:
        snapshot = self.service.snapshot()
        self.assertIn("account", snapshot)
        self.assertIn("tasks", snapshot)
        self.assertIn("pending_approvals", snapshot)
        self.assertIn("connection", snapshot)

    def test_interrupt_uses_latest_turn(self) -> None:
        result = self.service.interrupt("t1")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(self.client.requests[-1][0], "turn/interrupt")


if __name__ == "__main__":
    unittest.main()


class QueueLifecycleTests(unittest.TestCase):
    def test_turn_completion_releases_paused_queue_dispatch(self):
        from codexbridge.companion.service import CompanionService
        service = CompanionService.__new__(CompanionService)
        calls = []
        service.queues = type("Q", (), {
            "on_turn_finished": lambda self, thread_id: calls.append(thread_id),
        })()
        service._queue_thread_active = lambda thread_id: False
        CompanionService._on_turn_completed(service, "thread-1")
        self.assertEqual(calls, ["thread-1"])


if __name__ == "__main__":
    unittest.main()
