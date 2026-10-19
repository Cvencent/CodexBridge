"""Shared, allowlisted commands for HTTP and constrained device transports."""

from collections import OrderedDict
from copy import deepcopy
import json
import threading


class CommandError(ValueError):
    pass


# method: (required capability, required fields, optional fields)
COMMANDS = {
    'status.read': ('status', (), ()),
    'threads.list': ('tasks', (), ('limit', 'search', 'cursor')),
    'thread.read': ('tasks', ('thread_id',), ('message_limit',)),
    'thread.start': ('control', (), ('cwd', 'model')),
    'thread.rename': ('control', ('thread_id', 'title'), ()),
    'turn.start': ('control', ('thread_id', 'text'), ('model', 'effort')),
    'turn.steer': ('control', ('thread_id', 'text'), ()),
    'turn.interrupt': ('control', ('thread_id',), ('turn_id',)),
    'models.list': ('tasks', (), ()),
    'projects.list': ('tasks', (), ()),
    'approvals.list': ('approval', (), ()),
    'approval.resolve': ('approval', ('approval_id', 'decision'), ()),
    'approval.respond': ('approval', ('approval_id', 'response'), ()),
    'queue.list': ('queue', ('thread_id',), ()),
    'queue.enqueue': ('queue', ('thread_id', 'text'), ('model', 'effort')),
    'queue.edit': ('queue', ('queue_id', 'text', 'expected_revision'), ('model', 'effort')),
    'queue.delete': ('queue', ('queue_id', 'expected_revision'), ()),
    'queue.steer': ('queue', ('queue_id', 'expected_revision'), ()),
    # CBDP read-state uplink. Keeping this in the shared dispatcher means the
    # device path uses the same validation, idempotency and read-state fan-out
    # as the phone companion path instead of inventing a transport-specific
    # implementation.
    'message.read.update': ('read_unread', ('message_id', 'read_at_ms'), ('thread_id',)),
}


class CommandDispatcher:
    def __init__(self, service, cache_size=1000):
        self.service = service
        self._cache = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.RLock()

    @staticmethod
    def catalog():
        return {'protocol': 'codexbridge', 'command_schema': 1,
                'methods': [{'method': method, 'capability': value[0],
                             'required': list(value[1]), 'optional': list(value[2])}
                            for method, value in COMMANDS.items()]}

    def execute(self, device_id, capabilities, request):
        if not isinstance(request, dict) or not isinstance(device_id, str) or not device_id:
            raise CommandError('invalid_request')
        method = request.get('method')
        if not isinstance(method, str) or method not in COMMANDS:
            raise CommandError('unsupported_method')
        capability, required, optional = COMMANDS[method]
        if capability not in capabilities:
            raise CommandError('capability_not_enabled')
        request_id = request.get('request_id')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise CommandError('invalid_request_id')
        params = request.get('params', {})
        if not isinstance(params, dict) or set(params) - set(required + optional):
            raise CommandError('invalid_params')
        for field in required:
            if field not in params:
                raise CommandError('invalid_params')
        for field, value in params.items():
            if field == 'response':
                if not isinstance(value, dict) or len(json.dumps(value)) > 20000:
                    raise CommandError('invalid_params')
            elif field in {'limit', 'message_limit', 'approval_id', 'expected_revision', 'read_at_ms'}:
                if field == 'read_at_ms':
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise CommandError('invalid_params')
                elif field == 'message_limit':
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise CommandError('invalid_params')
                elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise CommandError('invalid_params')
            elif not isinstance(value, str) or len(value) > 20000:
                raise CommandError('invalid_params')
            if field in required and isinstance(value, str) and not value.strip():
                raise CommandError('invalid_params')
        fingerprint = json.dumps([method, params], sort_keys=True, ensure_ascii=True)
        # Protocol 17.4 keys idempotency on (device_id, idem). request_id is
        # only unique per connection, so keying on it made a device collide
        # with its own 24h history after every restart.
        idem = request.get('idem')
        if isinstance(idem, (bytes, bytearray)) and len(idem) == 16:
            key = (device_id, bytes(idem).hex())
        else:
            key = (device_id, request_id)
        # Serialize dispatch and cache admission, including concurrent retries.
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise CommandError('request_id_conflict')
                if isinstance(cached[1], Exception):
                    raise CommandError('outcome_unknown') from cached[1]
                return self._visible(deepcopy(cached[1]), capabilities)
            try:
                result = self._dispatch(device_id, method, params, request_id)
            except Exception as error:
                # A failed transport may have dispatched the write already.
                self._remember(key, fingerprint, error)
                raise
            response = {'type': 'command_result', 'request_id': request_id,
                        'method': method, 'ok': True, 'result': result}
            self._remember(key, fingerprint, deepcopy(response))
            return self._visible(response, capabilities)

    @staticmethod
    def _visible(response, capabilities):
        if 'approval' not in capabilities and isinstance(response.get('result'), dict):
            if 'pending_approvals' in response['result']:
                response['result']['pending_approvals'] = []
        return response

    def _remember(self, key, fingerprint, response):
        self._cache[key] = (fingerprint, response)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _dispatch(self, device_id, method, params, request_id):
        service = self.service
        if method == 'status.read':
            return service.snapshot()
        if method == 'threads.list':
            return service.list_threads(**params)
        if method == 'thread.read':
            return service.read_thread(**params)
        if method == 'thread.start':
            return service.start_thread(**params)
        if method == 'thread.rename':
            return service.rename_thread(params['thread_id'], params['title'])
        if method == 'turn.start':
            return service.start_turn(**params, client_message_id=request_id)
        if method == 'turn.steer':
            return service.steer_turn(**params)
        if method == 'turn.interrupt':
            return service.interrupt(**params)
        if method == 'models.list':
            return service.list_models()
        if method == 'projects.list':
            return service.list_projects()
        if method == 'approvals.list':
            return {'pending_approvals': service.pending_approvals()}
        if method == 'approval.respond':
            return {'resolved': service.respond_approval(params['approval_id'], params['response'])}
        if method == 'queue.list':
            return service.queue_list(**params)
        if method == 'queue.enqueue':
            return service.queue_enqueue(**params)
        if method == 'queue.edit':
            return service.queue_edit(**params)
        if method == 'queue.delete':
            return service.queue_delete(**params)
        if method == 'queue.steer':
            return service.queue_steer(**params)
        if method == 'message.read.update':
            return service.mark_message_read(**params, source=device_id)
        return {'resolved': service.resolve_approval(params['approval_id'], params['decision'])}
