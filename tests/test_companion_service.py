"""Tests for the companion app-server service."""

from __future__ import annotations

import json
import queue
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codexbridge.codex.app_server import AppServerError
from codexbridge.companion.service import CompanionService


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

    def test_remembered_failed_status_wins_over_later_idle_list_row(self):
        from unittest.mock import Mock, patch
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        self.service._detail_status_cache['t1'] = ('', 0, 'failed')
        self.client.responses['thread/list'] = {'data': [thread_row(status={'type': 'notLoaded'})]}
        with patch.object(self.service, '_thread_log', return_value=None), patch.object(
                self.service, '_rollout_signature', return_value=None):
            listed = self.service.list_threads()['threads'][0]
        self.assertEqual(listed['status'], 'failed')

    def test_first_list_retries_terminal_confirmation_before_returning_idle(self):
        from unittest.mock import patch
        failed_detail = thread_detail()
        failed_detail["thread"]["turns"][0]["status"] = "failed"
        calls = {"count": 0}

        def read(params):
            calls["count"] += 1
            if calls["count"] <= 2:
                raise AppServerError("http 503")
            return failed_detail

        self.client.responses["thread/list"] = {"data": [thread_row()]}
        self.client.responses["thread/read"] = read
        with patch.object(self.service, "_thread_log",
                          return_value=(time.time() - 1000, "", 0)), \
                patch.object(self.service, "_rollout_signature", return_value=None):
            listed = self.service.list_threads()["threads"][0]
        detail = self.service.read_thread("t1")
        self.assertEqual(listed["status"], "failed")
        self.assertEqual(detail["thread"]["status"], "failed")
        self.assertEqual(detail["task"]["status"], "failed")

    def test_snapshot_uses_resolved_status_for_list_detail_and_result_buckets(self):
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        self.service._desktop_turn_status[('t1', 'turn-1')] = 'inProgress'
        self.client.responses['thread/list'] = {
            'data': [thread_row(status={'type': 'notLoaded'})],
        }
        self.client.responses['thread/read'] = thread_detail()

        listed = self.service.list_threads()['threads'][0]
        detail = self.service.read_thread('t1')
        snapshot = self.service.snapshot()

        self.assertEqual(listed['status'], 'working')
        self.assertEqual(detail['thread']['status'], 'working')
        self.assertEqual(detail['task']['status'], 'working')
        self.assertEqual(snapshot['task']['status'], 'working')
        self.assertEqual([task['status'] for task in snapshot['tasks']], ['working'])
        self.assertEqual(snapshot['result'], {})

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
        with patch.object(self.service, '_thread_log', return_value=(1791000100, 'task_failed', 1791000100)):
            listed = self.service.list_threads()['threads'][0]
        self.assertEqual(listed['status'], 'working')

    def test_desktop_first_terminal_snapshot_emits_completion(self):
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
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]['thread_id'], 't-first-terminal')
        self.assertEqual(completions[0]['turn_id'], 'turn-first')

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
        self.assertEqual(result["threads"][1]["status"], "idle")

    def test_list_threads_marks_finished_rollout_completed(self) -> None:
        self._write_rollout("t2", "task_complete", seconds_ago=30)
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "completed")

    def test_stale_rollout_start_defers_to_turn_history(self) -> None:
        # A dangling ``task_started`` with no terminal marker means the turn
        # was abandoned; the list must agree with the turn history rather than
        # guessing idle or working.
        self._write_rollout("t2", "task_started", seconds_ago=600)
        self.client.responses["thread/read"] = thread_detail("t2")
        result = self.service.list_threads(limit=10)
        statuses = {item["thread_id"]: item["status"] for item in result["threads"]}
        self.assertEqual(statuses["t2"], "completed")

    def test_completed_rollout_overrides_stale_active_list_row(self) -> None:
        self.client.responses["thread/list"] = {
            "data": [thread_row(status={"type": "active"})],
        }
        self._write_rollout("t1", "task_complete")
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "completed")

    def test_terminal_rollout_overrides_stale_active_desktop_status(self) -> None:
        from unittest.mock import Mock
        desktop = Mock()
        desktop.find_thread_owner.return_value = 'owner'
        self.service._desktop = desktop
        self.service._desktop_turn_status[('t1', 'turn-1')] = 'inProgress'
        self.client.responses['thread/list'] = {
            'data': [thread_row(status={'type': 'active'})],
        }
        self._write_rollout('t1', 'task_complete')
        self.assertEqual(
            self.service.list_threads()['threads'][0]['status'],
            'completed',
        )

    def test_old_completion_does_not_override_newer_running_turn(self) -> None:
        self.client.responses["thread/list"] = {"data": [thread_row(
            status={"type": "active"}, turns=[{
                "id": "new-turn", "status": "inProgress", "startedAt": int(time.time()),
            }],
        )]}
        self._write_rollout("t1", "task_complete", seconds_ago=30)
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "working")

    def test_old_terminal_rollout_matches_detail_without_list_turn_history(self) -> None:
        self.client.responses["thread/list"] = {"data": [thread_row()]}
        self.client.responses["thread/read"] = thread_detail()
        self._write_rollout("t1", "task_complete", seconds_ago=7200)

        listed = self.service.list_threads()["threads"][0]
        detail = self.service.read_thread("t1")

        self.assertEqual(listed["status"], "completed")
        self.assertEqual(detail["thread"]["status"], listed["status"])
        self.assertEqual(detail["task"]["status"], listed["status"])

    def test_cached_terminal_status_survives_unrelated_rollout_append(self) -> None:
        self.client.responses["thread/list"] = {"data": [thread_row()]}
        self.client.responses["thread/read"] = {
            "thread": {
                **thread_row(),
                "turns": [
                    {
                        "id": "turn-failed",
                        "status": "failed",
                        "startedAt": 1791000000,
                        "items": [],
                    }
                ],
            }
        }
        self._write_rollout("t1", "token_usage_record")

        detail = self.service.read_thread("t1")
        self.assertEqual(detail["thread"]["status"], "failed")

        rollout = next(
            (Path(self.temp.name) / "sessions").rglob("rollout-t1.jsonl")
        )
        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "type": "token_usage_record",
                        "payload": {},
                    }
                )
                + "\n"
            )

        listed = self.service.list_threads()["threads"][0]
        self.assertEqual(listed["status"], "failed")
    def test_cached_failed_status_does_not_override_newer_running_turn(self) -> None:
        self.client.responses["thread/list"] = {"data": [thread_row()]}
        self.client.responses["thread/read"] = {
            "thread": {
                **thread_row(),
                "turns": [
                    {
                        "id": "turn-failed",
                        "status": "failed",
                        "startedAt": 1791000000,
                        "items": [],
                    }
                ],
            }
        }
        detail = self.service.read_thread("t1")
        self.assertEqual(detail["thread"]["status"], "failed")

        self.client.responses["thread/list"] = {"data": [thread_row(
            status={"type": "active"},
            turns=[{
                "id": "turn-new",
                "status": "inProgress",
                "startedAt": 1791000200,
            }],
        )]}
        listed = self.service.list_threads()["threads"][0]
        self.assertEqual(listed["status"], "working")
        self.assertEqual(listed["turn_id"], "turn-new")
    def test_terminal_rollout_matches_detail_for_thread_beyond_old_scan_limit(self) -> None:
        rows = [
            thread_row(
                f"t{index}",
                name=f"Task {index}",
                recencyAt=1791000200 - index,
            )
            for index in range(1, 27)
        ]
        self.client.responses["thread/list"] = {"data": rows}
        self.client.responses["thread/read"] = thread_detail("t26")
        self._write_rollout("t26", "task_complete", seconds_ago=7200)

        listed = next(
            item for item in self.service.list_threads(limit=80)["threads"]
            if item["thread_id"] == "t26"
        )
        detail = self.service.read_thread("t26")

        self.assertEqual(listed["status"], "completed")
        self.assertEqual(detail["thread"]["status"], listed["status"])
        self.assertEqual(detail["task"]["status"], listed["status"])

    def test_rollout_changes_invalidate_status_cache_immediately(self) -> None:
        self._write_rollout("t1", "task_started")
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "working")
        self._write_rollout("t1", "task_complete")
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "completed")

    def test_recent_rollout_without_turn_marker_confirms_detail_status(self) -> None:
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{datetime.now().year:04d}"
            / f"{datetime.now().month:02d}"
            / f"{datetime.now().day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "noise"},
        }
        (folder / "rollout-t1.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "completed")

    def test_recent_usage_record_does_not_hide_terminal_turn_status(self) -> None:
        failed_detail = thread_detail()
        failed_detail["thread"]["turns"][0]["status"] = "failed"
        self.client.responses["thread/read"] = failed_detail
        self._write_rollout("t1", "token_usage_record")

        listed = self.service.list_threads()["threads"][0]

        self.assertEqual(listed["status"], "failed")

    def test_list_status_resolves_rollout_from_older_dated_folder(self) -> None:
        # The rollout lives under a previous day's folder, which the old
        # today/yesterday scan could never find.
        day = datetime.now() - timedelta(days=5)
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{day.year:04d}"
            / f"{day.month:02d}"
            / f"{day.day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        }
        (folder / "rollout-t1.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assertEqual(self.service.list_threads()["threads"][0]["status"], "completed")

    def test_failed_task_complete_is_reported_failed_in_list_and_detail(self) -> None:
        folder = (
            Path(self.temp.name)
            / "sessions"
            / f"{datetime.now().year:04d}"
            / f"{datetime.now().month:02d}"
            / f"{datetime.now().day:02d}"
        )
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": "task_complete", "error": {"message": "stream disconnected"}},
        }
        (folder / "rollout-t1.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.client.responses["thread/read"] = thread_detail()

        listed = self.service.list_threads()["threads"][0]
        detail = self.service.read_thread("t1")

        self.assertEqual(listed["status"], "failed")
        self.assertEqual(detail["thread"]["status"], listed["status"])

    def test_list_prefers_row_rollout_path_hint(self) -> None:
        # A row that points at its real rollout file wins over directory scans,
        # which keeps list and detail aligned even for relocated sessions.
        cached = Path(self.temp.name) / "custom" / "rollout-anything-t2.jsonl"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        }) + "\n", encoding="utf-8")
        self.client.responses["thread/list"] = {"data": [thread_row("t2", path=str(cached))]}
        statuses = {
            item["thread_id"]: item["status"]
            for item in self.service.list_threads()["threads"]
        }
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

    def test_read_thread_maps_messages(self) -> None:
        detail = self.service.read_thread("t1")
        kinds = [item["kind"] for item in detail["messages"]]
        self.assertEqual(kinds, ["user", "commentary", "agent", "command"])
        self.assertEqual(detail["messages"][0]["text"], "帮我看一下构建结果")
        self.assertEqual(detail["messages"][3]["detail"]["exit_code"], 0)
        self.assertEqual(detail["messages"][3]["detail"]["output"], "BUILD SUCCESSFUL")
        self.assertEqual(detail["thread"]["completed_at"], 1791000100)

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

    def test_start_turn_uses_steer_when_active_writer_rejects_new_turn(self) -> None:
        calls = {"start": 0, "steer": 0}

        def start(_params):
            calls["start"] += 1
            raise AppServerError("thread already has an active writer")

        def steer(_params):
            calls["steer"] += 1
            return {"turn": {"id": "turn-2", "status": "inProgress"}}

        self.client.responses["turn/start"] = start
        self.client.responses["turn/steer"] = steer
        result = self.service.start_turn("t1", "继续这个任务", model="gpt-5.1-codex")
        self.assertEqual(result["status"], "working")
        self.assertEqual(calls, {"start": 1, "steer": 1})

    def test_steer_turn_falls_back_to_start_when_no_active_writer(self) -> None:
        calls = {"start": 0, "steer": 0}

        def steer(_params):
            calls["steer"] += 1
            raise AppServerError("no active turn to steer")

        def start(_params):
            calls["start"] += 1
            return {"turn": {"id": "turn-3", "status": "inProgress"}}

        self.client.responses["turn/steer"] = steer
        self.client.responses["turn/start"] = start
        result = self.service.steer_turn("t1", "换个方向")
        self.assertEqual(result["status"], "working")
        self.assertEqual(calls, {"start": 1, "steer": 1})
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
        self.client.responses["turn/steer"] = {"turn": {"id": "turn-2", "status": "inProgress"}}
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
