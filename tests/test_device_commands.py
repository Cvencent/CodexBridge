import unittest
from codexbridge.protocol.commands import CommandDispatcher, CommandError


class Service:
    def __init__(self):
        self.calls = []

    def rename_thread(self, thread_id, title):
        self.calls.append(('rename', thread_id, title))
        return {'thread_id': thread_id, 'title': title, 'renamed': True}

    def start_turn(self, thread_id, text, **options):
        self.calls.append((thread_id, text, options))
        return {'thread_id': thread_id, 'turn_id': 'turn-1', 'status': 'working'}


class DeviceCommandTests(unittest.TestCase):
    def test_legacy_cancel_is_executed_with_existing_tasks_capability(self):
        from codexbridge.hub import BridgeHub
        from codexbridge.devices.registry import DeviceRegistry
        from tests.test_hub import FakeSession
        service = Service()
        service.interrupt = lambda **params: service.calls.append(params) or params
        registry = DeviceRegistry(None)
        registry.upsert('esp', model='dotii', supported=['tasks'])
        hub = BridgeHub(registry, commands=CommandDispatcher(service))
        session = FakeSession()
        hub.attach('esp', session)
        hub.handle_device_event('esp', {'type': 'cancel_task', 'thread_id': 't1'})
        # Codex work is deferred off the session thread, so the assertion has to
        # meet it at a defined drain point rather than assume it ran inline.
        self.assertTrue(hub.wait_idle(2.0))
        self.assertEqual(service.calls, [{'thread_id': 't1', 'turn_id': ''}])
        self.assertTrue(session.sent[-1]['ok'])

    def test_status_read_does_not_leak_unnegotiated_approvals(self):
        service = Service()
        service.snapshot = lambda: {'pending_approvals': [{'command': 'secret'}]}
        commands = CommandDispatcher(service)
        request = {'request_id': 's', 'method': 'status.read'}
        self.assertEqual(commands.execute('p', {'status', 'approval'}, request)['result']['pending_approvals'],
                         [{'command': 'secret'}])
        self.assertEqual(commands.execute('p', {'status'}, request)['result']['pending_approvals'], [])

    def test_tcp_request_chunks_execute_once_after_complete(self):
        import json
        from codexbridge.hub import BridgeHub
        from codexbridge.devices.registry import DeviceRegistry
        registry = DeviceRegistry(None)
        record = registry.upsert('esp', model='esp', supported=['control'])
        record.enabled.add('control')
        service = Service()
        hub = BridgeHub(registry, commands=CommandDispatcher(service))
        request = json.dumps({'type': 'command', 'request_id': 'r', 'method': 'turn.start',
            'params': {'thread_id': 't1', 'text': 'hello'}})
        midpoint = len(request) // 2
        # An incomplete command must not be dispatched at all. Chunk 0 only
        # buffers, and buffering never touches Codex, so this stays synchronous.
        hub.handle_device_event('esp', {'type': 'command_request_chunk', 'request_id': 'r',
            'seq': 0, 'total': 2, 'data': request[:midpoint]})
        self.assertEqual(service.calls, [])
        hub.handle_device_event('esp', {'type': 'command_request_chunk', 'request_id': 'r',
            'seq': 1, 'total': 2, 'data': request[midpoint:]})
        # The completed command executes on the deferred worker, so meet it at
        # the drain point instead of assuming it ran inline.
        self.assertTrue(hub.wait_idle(2.0))
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0][1], 'hello')

    def test_structured_approval_command_preserves_response(self):
        service = Service()
        service.respond_approval = lambda approval_id, response: response == {'answers': {}}
        result = CommandDispatcher(service).execute('esp', {'approval'}, {
            'request_id': 'a1', 'method': 'approval.respond',
            'params': {'approval_id': 1, 'response': {'answers': {}}}})
        self.assertTrue(result['result']['resolved'])

    def test_tcp_event_subscription_replays_only_permitted_events(self):
        from codexbridge.hub import BridgeHub
        from codexbridge.devices.registry import DeviceRegistry
        from codexbridge.companion.events import EventStream
        service = Service()
        service.events = EventStream()
        service.subscribe = service.events.subscribe
        service.unsubscribe = service.events.unsubscribe
        registry = DeviceRegistry(None)
        registry.upsert('esp', model='esp', supported=['status'])
        hub = BridgeHub(registry, commands=CommandDispatcher(service))
        class Session:
            def __init__(self): self.frames = []
            def send(self, frame): self.frames.append(frame)
        session = Session()
        hub.attach('esp', session)
        hub.handle_device_event('esp', {'type': 'events_subscribe'})
        service.events.publish({'type': 'turn_completed', 'thread_id': 't1'})
        service.events.publish({'type': 'approval_request', 'command': 'secret'})
        hub.poll_events()
        self.assertTrue(any(frame.get('event', {}).get('type') == 'turn_completed' for frame in session.frames))
        self.assertFalse(any(frame.get('event', {}).get('type') == 'approval_request' for frame in session.frames))
        hub.detach('esp')
        self.assertEqual(len(service.events._subscribers), 0)

    def test_tcp_hub_discovers_same_command_catalog(self):
        from codexbridge.hub import BridgeHub
        from codexbridge.devices.registry import DeviceRegistry
        registry = DeviceRegistry(None)
        registry.upsert('esp', model='esp', supported=['tasks'])
        hub = BridgeHub(registry, commands=CommandDispatcher(Service()))
        class Session:
            def __init__(self): self.frames = []
            def send(self, frame): self.frames.append(frame)
        session = Session()
        hub.attach('esp', session)
        hub.handle_device_event('esp', {'type': 'protocol_request', 'request_id': 'catalog'})
        self.assertTrue(session.frames)

    def setUp(self):
        self.service = Service()
        self.commands = CommandDispatcher(self.service)

    def test_same_device_request_is_not_executed_twice(self):
        request = {'request_id': 'r1', 'method': 'turn.start',
                   'params': {'thread_id': 't1', 'text': 'continue'}}
        first = self.commands.execute('device-a', {'control'}, request)
        second = self.commands.execute('device-a', {'control'}, request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(first['result']['turn_id'], 'turn-1')
        changed = {**request, 'params': {'thread_id': 't1', 'text': 'different'}}
        with self.assertRaisesRegex(CommandError, 'request_id_conflict'):
            self.commands.execute('device-a', {'control'}, changed)

    def test_permission_is_checked_even_for_cached_result(self):
        request = {'request_id': 'r1', 'method': 'turn.start',
                   'params': {'thread_id': 't1', 'text': 'continue'}}
        self.commands.execute('device-a', {'control'}, request)
        with self.assertRaisesRegex(CommandError, 'capability_not_enabled'):
            self.commands.execute('device-a', {'tasks'}, request)

    def test_invalid_parameters_never_execute(self):
        for params in [{'thread_id': 't1', 'text': {}},
                       {'thread_id': '', 'text': 'hi'},
                       {'thread_id': 't1', 'text': 'hi', 'unexpected': True}]:
            with self.assertRaises(CommandError):
                self.commands.execute('a', {'control'},
                                      {'request_id': 'x', 'method': 'turn.start', 'params': params})
        self.assertEqual(self.service.calls, [])

    def test_thread_rename_uses_shared_dispatch(self):
        result = self.commands.execute('device-a', {'control'}, {
            'request_id': 'rename-1', 'method': 'thread.rename',
            'params': {'thread_id': 't1', 'title': '新标题'}})
        self.assertTrue(result['ok'])
        self.assertEqual(self.service.calls, [('rename', 't1', '新标题')])

    def test_unknown_method_is_explicit_error(self):
        with self.assertRaisesRegex(CommandError, 'unsupported_method'):
            self.commands.execute('a', {'control'},
                                  {'request_id': 'x', 'method': 'arbitrary.rpc', 'params': {}})

    def test_escaped_result_and_maximum_request_id_fit_wire_limit(self):
        from codexbridge.protocol.messages import wire_messages, decode
        import json
        response = {'type': 'command_result', 'request_id': '\\' * 128,
                    'result': {'text': '\x00' * 3000}}
        frames = wire_messages(response)
        self.assertTrue(all(len(frame) <= 768 for frame in frames))
        restored = json.loads(''.join(decode(frame)['data'] for frame in frames))
        self.assertEqual(restored, response)

    def test_large_command_result_is_chunked_within_device_limit(self):
        from codexbridge.protocol.messages import wire_messages, decode
        response = {'type': 'command_result', 'request_id': 'r1', 'ok': True,
                    'result': {'text': 'long result ' * 300}}
        frames = wire_messages(response)
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) <= 768 for frame in frames))
        chunks = [decode(frame) for frame in frames]
        import json
        restored = json.loads(''.join(chunk['data'] for chunk in chunks))
        self.assertEqual(restored, response)


class QueueCommandTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.calls = []
        self.service.queue_list = lambda **params: self.calls.append(("list", params)) or {"items": []}
        self.service.queue_enqueue = lambda **params: self.calls.append(("enqueue", params)) or {"items": [{"queue_id": "q1"}]}
        self.service.queue_edit = lambda **params: self.calls.append(("edit", params)) or {"items": []}
        self.service.queue_delete = lambda **params: self.calls.append(("delete", params)) or {"removed": True}
        self.service.queue_steer = lambda **params: self.calls.append(("steer", params)) or {"items": []}
        self.commands = CommandDispatcher(self.service)

    def test_queue_commands_require_queue_capability(self):
        request = {
            "request_id": "q-cap",
            "method": "queue.enqueue",
            "params": {"thread_id": "t1", "text": "hello"},
        }
        with self.assertRaisesRegex(CommandError, "capability_not_enabled"):
            self.commands.execute("phone-a", {"control"}, request)
        self.assertEqual(self.calls, [])

    def test_queue_enqueue_dispatches_with_revision_fields(self):
        result = self.commands.execute("phone-a", {"queue"}, {
            "request_id": "q-enqueue",
            "method": "queue.enqueue",
            "params": {"thread_id": "t1", "text": "hello", "model": "gpt-5", "effort": "high"},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][0], "enqueue")
        self.assertEqual(self.calls[0][1]["text"], "hello")

    def test_queue_edit_requires_positive_expected_revision(self):
        for revision in [0, -1, True, "1"]:
            with self.assertRaisesRegex(CommandError, "invalid_params"):
                self.commands.execute("phone-a", {"queue"}, {
                    "request_id": "q-edit-" + str(revision),
                    "method": "queue.edit",
                    "params": {
                        "queue_id": "q1",
                        "text": "updated",
                        "expected_revision": revision,
                    },
                })
        self.assertEqual(self.calls, [])