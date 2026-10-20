import os
import unittest
import json
import socket
import struct
import threading
from unittest.mock import patch
from codexbridge.codex.desktop_ipc import DesktopIpcClient, DesktopIpcError

class DesktopIpcTests(unittest.TestCase):
    def test_close_wakes_pending_request_immediately(self):
        import queue
        client = DesktopIpcClient()
        target = queue.Queue(maxsize=1)
        client._pending['r'] = target
        client.close()
        self.assertFalse(target.empty())
        self.assertIsInstance(target.get_nowait(), DesktopIpcError)

    def test_old_reader_cannot_read_replacement_connection(self):
        client = DesktopIpcClient()
        old, old_peer = socket.socketpair()
        replacement, replacement_peer = socket.socketpair()
        self.addCleanup(old.close)
        self.addCleanup(old_peer.close)
        self.addCleanup(replacement.close)
        self.addCleanup(replacement_peer.close)
        client._stream = replacement
        with self.assertRaises(DesktopIpcError):
            client._exact(4, old)

    def test_owner_routed_approval_uses_kind_specific_method(self):
        client = DesktopIpcClient()
        with patch.object(client, '_request', return_value={'resultType': 'success'}) as request:
            client.respond_approval('owner', 'thread', 'request', 'input', {'answers': {}})
        self.assertEqual(request.call_args.args[0], 'thread-follower-submit-user-input')
        self.assertEqual(request.call_args.args[1]['response'], {'answers': {}})

    def test_open_thread_validates_id_before_launching_uri(self):
        client = DesktopIpcClient()
        with patch('os.startfile', create=True) as launch:
            client.open_thread('01234567-89ab-cdef-0123-456789abcdef')
            launch.assert_called_once_with('codex://threads/01234567-89ab-cdef-0123-456789abcdef')
            with self.assertRaises(ValueError):
                client.open_thread('bad/path?prompt=execute')

    def test_follow_sends_targeted_broadcast(self):
        client = DesktopIpcClient()
        client._client_id = 'bridge'
        with patch.object(client, '_send') as send:
            client.follow_thread('owner', 'thread')
        self.assertEqual(send.call_args.args[0], {
            'type': 'broadcast', 'sourceClientId': 'bridge',
            'method': 'thread-stream-following-changed', 'version': 1,
            'params': {'conversationId': 'thread', 'hostId': 'local', 'following': True},
            'targetClientIds': ['owner']})

    def test_owner_routed_steer_and_interrupt(self):
        client = DesktopIpcClient()
        response = {'resultType': 'success', 'result': {'result': {'turn': {'id': 'turn-1'}}}}
        with patch.object(client, '_request', return_value=response) as request:
            self.assertEqual(client.steer_turn('owner', 'thread', [{'type': 'text', 'text': 'hi'}]),
                             {'turn': {'id': 'turn-1'}})
            self.assertEqual(request.call_args.args[0], 'thread-follower-steer-turn')
            self.assertEqual(request.call_args.kwargs['target_client_id'], 'owner')
            self.assertEqual(request.call_args.args[1]['restoreMessage']['context']['prompt'], 'hi')
            self.assertEqual(request.call_args.args[1]['input'][0]['text_elements'], [])
            client.interrupt_turn('owner', 'thread', 'turn-1')
            self.assertEqual(request.call_args.args[0], 'thread-follower-interrupt-turn')
            self.assertEqual(request.call_args.args[2], 4)
            self.assertEqual(request.call_args.args[1]['expectedTurnId'], 'turn-1')

    def test_owner_routed_start_uses_versioned_desktop_request(self):
        client = DesktopIpcClient()
        client._client_id = 'bridge-id'
        response = {'resultType': 'success', 'result': {'result': {'turn': {'id': 'turn-1'}}}}
        with patch.object(client, '_request', return_value=response) as request:
            result = client.start_turn('owner-id', 'thread-1', {'input': [{'type': 'text', 'text': 'hi'}]})
        self.assertEqual(result, {'turn': {'id': 'turn-1'}})
        self.assertEqual(request.call_args.args[0], 'thread-follower-start-turn')
        self.assertEqual(request.call_args.args[2], 2)
        self.assertEqual(request.call_args.kwargs['target_client_id'], 'owner-id')
        self.assertEqual(request.call_args.args[1]['turnStart']['request']['input'][0]['text'], 'hi')
        self.assertEqual(request.call_args.args[1]['turnStart']['request']['input'][0]['text_elements'], [])
        self.assertEqual(request.call_args.args[1]['turnStart']['request']['threadId'], 'thread-1')

    def test_owner_routed_settings_update_uses_versioned_desktop_request(self):
        client = DesktopIpcClient()
        response = {
            'type': 'response', 'resultType': 'success',
            'method': 'thread-follower-update-thread-settings',
            'handledByClientId': 'owner-id', 'result': {'applied': True},
        }
        with patch.object(client, '_request', return_value=response) as request:
            applied = client.update_thread_settings(
                'owner-id', 'thread-1', model='Auto/Kimi-K3', effort='high')
        self.assertTrue(applied)
        self.assertEqual(request.call_args.args[0], 'thread-follower-update-thread-settings')
        self.assertEqual(request.call_args.args[2], 2)
        self.assertEqual(request.call_args.kwargs['target_client_id'], 'owner-id')
        self.assertEqual(request.call_args.args[1], {
            'conversationId': 'thread-1',
            'threadSettings': {'model': 'Auto/Kimi-K3', 'effort': 'high'},
        })

    def test_owner_routed_sandbox_policy_uses_the_desktop_string_shape(self):
        client = DesktopIpcClient()
        response = {
            'resultType': 'success', 'result': {'applied': True},
        }
        with patch.object(client, '_request', return_value=response) as request:
            client.update_thread_settings(
                'owner-id',
                'thread-1',
                sandbox_policy={'type': 'dangerFullAccess'},
            )

        self.assertEqual(
            request.call_args.args[1]['threadSettings']['sandboxPolicy'],
            'danger-full-access',
        )

    def test_settings_update_tolerates_nested_result(self):
        client = DesktopIpcClient()
        response = {'resultType': 'success', 'result': {'result': {'applied': True}}}
        with patch.object(client, '_request', return_value=response):
            self.assertTrue(client.update_thread_settings(
                'owner-id', 'thread-1', model='Auto/Kimi-K3'))

    def test_settings_update_without_selection_touches_no_ipc(self):
        client = DesktopIpcClient()
        with patch.object(client, '_request') as request:
            self.assertTrue(client.update_thread_settings('owner-id', 'thread-1'))
        request.assert_not_called()

    def test_rejected_settings_update_raises(self):
        client = DesktopIpcClient()
        with patch.object(client, '_request', return_value={'resultType': 'error', 'error': 'nope'}):
            with self.assertRaises(DesktopIpcError):
                client.update_thread_settings('owner-id', 'thread-1', model='Auto/Kimi-K3')

    def test_query_invalidation_uses_exact_desktop_broadcast(self):
        client = DesktopIpcClient()
        client._client_id = "bridge-id"
        with patch.object(client, "_send") as send:
            client.invalidate_query_cache(["tasks"])
        self.assertEqual(send.call_args.args[0], {
            "type": "broadcast", "sourceClientId": "bridge-id",
            "method": "query-cache-invalidate", "version": 0,
            "params": {"queryKey": ["tasks"], "reset": False},
        })

    @unittest.skipUnless(os.name == 'nt', 'Windows named pipes only')
    def test_windows_pipe_busy_retries_until_available(self):
        from codexbridge.codex.desktop_ipc import _open_windows_pipe_handle
        busy = OSError(231, 'pipe busy')
        busy.winerror = 231
        with patch('codexbridge.codex.desktop_ipc._create_windows_pipe_handle',
                   side_effect=[busy, 42]) as create, \
                patch('codexbridge.codex.desktop_ipc._wait_windows_pipe',
                      return_value=True) as wait:
            handle = _open_windows_pipe_handle(r'\\.\pipe\codex-ipc', 0.5)
        self.assertEqual(handle, 42)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(wait.call_count, 1)

    def test_initial_state_does_not_claim_shared(self):
        self.assertFalse(DesktopIpcClient().connection_status()['desktop_realtime_shared'])

    def test_handshake_and_owner_discovery(self):
        connection, server = socket.socketpair()
        observed = []

        def receive():
            def exact(size):
                data = b''
                while len(data) < size:
                    chunk = server.recv(size - len(data))
                    if not chunk:
                        raise EOFError
                    data += chunk
                return data
            return json.loads(exact(struct.unpack('<I', exact(4))[0]))

        def send(message):
            body = json.dumps(message).encode()
            frame = struct.pack('<I', len(body)) + body
            for byte in frame:
                server.sendall(bytes([byte]))

        def router():
            with server:
                init = receive()
                observed.append(init)
                send({'type': 'response', 'requestId': init['requestId'],
                      'resultType': 'success', 'result': {'clientId': 'bridge-id'}})
                request = receive()
                observed.append(request)
                send({'type': 'client-discovery-request', 'requestId': 'discover'})
                observed.append(receive())
                send({'type': 'response', 'requestId': request['requestId'],
                      'resultType': 'success', 'handledByClientId': 'desktop-owner'})
                server.recv(1)

        worker = threading.Thread(target=router)
        worker.start()
        client = DesktopIpcClient(timeout=1)
        try:
            with patch.object(client, '_connect_endpoint', return_value=connection):
                client.start()
            self.assertEqual(client.find_thread_owner('thread-1'), 'desktop-owner')
            self.assertEqual(observed[0]['params'], {'clientType': 'codexbridge'})
            self.assertEqual(observed[1]['version'], 1)
            self.assertEqual(observed[1]['params'],
                             {'hostId': 'local', 'conversationId': 'thread-1'})
            self.assertEqual(observed[2]['response'], {'canHandle': False})
        finally:
            client.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_timeout_closes_connection(self):
        connection, server = socket.socketpair()
        client = DesktopIpcClient(timeout=0.05)
        try:
            with patch.object(client, '_connect_endpoint', return_value=connection):
                with self.assertRaisesRegex(DesktopIpcError, 'timed out'):
                    client.start()
            self.assertFalse(client.connection_status()['desktop_ipc_connected'])
        finally:
            client.close()
            server.close()

    def test_invalid_frame_fails_pending_request(self):
        connection, server = socket.socketpair()

        def router():
            with server:
                server.recv(4096)
                server.sendall(struct.pack('<I', 0))

        worker = threading.Thread(target=router)
        worker.start()
        client = DesktopIpcClient(timeout=1)
        try:
            with patch.object(client, '_connect_endpoint', return_value=connection):
                with self.assertRaisesRegex(DesktopIpcError, 'frame'):
                    client.start()
            self.assertFalse(client.connection_status()['desktop_ipc_connected'])
        finally:
            client.close()
            worker.join(2)

    def test_oversized_frame_is_skipped_instead_of_killing_the_pipe(self):
        connection, server = socket.socketpair()
        client = DesktopIpcClient(timeout=1)
        client.MAX_FRAME_BYTES = 4096
        client.MAX_SKIPPED_FRAME_BYTES = 200000
        client.DISCARD_CHUNK_BYTES = 1024

        def read_exact(stream, size):
            data = b''
            while len(data) < size:
                chunk = stream.recv(size - len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return data

        def read_message(stream):
            return json.loads(read_exact(stream, struct.unpack('<I', read_exact(stream, 4))[0]))

        def send(stream, message):
            body = json.dumps(message).encode()
            stream.sendall(struct.pack('<I', len(body)) + body)

        release = threading.Event()

        def router():
            with server:
                init = read_message(server)
                send(server, {'type': 'response', 'requestId': init['requestId'],
                              'resultType': 'success', 'result': {'clientId': 'bridge-id'}})
                request = read_message(server)
                oversized = json.dumps({
                    'type': 'broadcast', 'method': 'thread-stream-state-changed',
                    'params': {'payload': 'x' * 6000},
                }).encode()
                server.sendall(struct.pack('<I', len(oversized)) + oversized)
                send(server, {'type': 'response', 'requestId': request['requestId'],
                              'resultType': 'success', 'handledByClientId': 'desktop-owner'})
                release.wait(5.0)

        worker = threading.Thread(target=router)
        worker.start()
        try:
            with patch.object(client, '_connect_endpoint', return_value=connection):
                client.start()
            self.assertEqual(client.find_thread_owner('thread-1'), 'desktop-owner')
            self.assertEqual(client.events.get_nowait(), {'type': 'resync_required'})
            self.assertTrue(client.connection_status()['desktop_ipc_connected'])
        finally:
            release.set()
            client.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_frame_beyond_the_skip_limit_is_still_rejected(self):
        connection, server = socket.socketpair()
        client = DesktopIpcClient(timeout=1)
        client.MAX_FRAME_BYTES = 4096
        client.MAX_SKIPPED_FRAME_BYTES = 8192

        def router():
            with server:
                server.recv(4096)
                server.sendall(struct.pack('<I', 9000))

        worker = threading.Thread(target=router)
        worker.start()
        try:
            with patch.object(client, '_connect_endpoint', return_value=connection):
                with self.assertRaisesRegex(DesktopIpcError, 'frame'):
                    client.start()
        finally:
            client.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())
