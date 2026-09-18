"""Streaming exact call records and independent completion checks."""
import hashlib
import json
import os
import shutil
import threading
import time

from _codex_capture import FRAME_LIMIT, ProbeError, json_value
from _codex_isolation import FREE_RESERVE

ARGUMENT_BYTES = 16 * 1024
RESPONSE_BYTES = 64 * 1024
CALL_LIMIT = 100


def encoded(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()


class Audit:
    def __init__(self, path, budget, call_limit=CALL_LIMIT):
        self.output = os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW), 'ab', buffering=0)
        self.path = path
        self.budget = budget
        self.call_limit = call_limit
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.sequence = 0
        self.sha = hashlib.sha256()
        self.calls = {}
        self.raw_results = {}
        self.dynamic = {}
        self.responses = {}

    def record(self, kind, value, session):
        with self.lock:
            data = encoded({'sequence': self.sequence, 'elapsed_seconds': time.monotonic() - self.started,
                            'session': session, 'kind': kind, 'value': value}) + b'\n'
            if len(data) > FRAME_LIMIT:
                raise ProbeError('audit_frame_limit_exceeded')
            if shutil.disk_usage(self.path.parent).free - len(data) < FREE_RESERVE:
                raise ProbeError('corpus_disk_reserve_unavailable')
            self.budget.add(len(data))
            self.output.write(data)
            os.fsync(self.output.fileno())
            self.sha.update(data)
            self.sequence += 1

    def call(self, session, call_id, name, arguments, kind):
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
            raise ProbeError('incompatible_call_fields')
        if len(encoded(arguments)) > ARGUMENT_BYTES:
            raise ProbeError('tool_argument_limit_exceeded')
        key = (session, call_id)
        value = {'name': name, 'arguments': arguments, 'kind': kind}
        previous = self.calls.get(key)
        if previous and (previous['name'], previous['arguments']) != (name, arguments):
            raise ProbeError('conflicting_call_id')
        if previous is None:
            self.calls[key] = value
        if len(self.calls) > self.call_limit:
            raise ProbeError('tool_call_limit_exceeded')

    def receive(self, event, session):
        self.record('rpc.received', event, session)
        method, params = event.get('method'), event.get('params', {})
        with self.lock:
            if method == 'rawResponseItem/completed':
                item = params.get('item')
                if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                    raise ProbeError('incompatible_raw_event')
                kind = item['type']
                if kind in ('function_call', 'custom_tool_call'):
                    arguments = item.get('arguments') if kind == 'function_call' else item.get('input')
                    if not isinstance(arguments, str):
                        raise ProbeError('incompatible_raw_arguments')
                    name = (item.get('namespace') or 'functions') + '.' + item.get('name', '')
                    self.call(session, item.get('call_id'), name, arguments, 'top_level')
                elif kind in ('function_call_output', 'custom_tool_call_output'):
                    if 'call_id' not in item or 'output' not in item:
                        raise ProbeError('incompatible_raw_result')
                    if len(encoded(item['output'])) > RESPONSE_BYTES:
                        raise ProbeError('tool_response_limit_exceeded')
                    key = (session, item['call_id'])
                    if key in self.raw_results and self.raw_results[key] != item:
                        raise ProbeError('conflicting_raw_result')
                    self.raw_results[key] = item
                elif kind not in ('message', 'reasoning', 'compaction', 'additional_tools', 'tool_definitions'):
                    raise ProbeError('unexpected_raw_item_type')
            elif method == 'rawResponse/completed':
                if not isinstance(params.get('responseId'), str) or not isinstance(params.get('usage'), dict):
                    raise ProbeError('incompatible_raw_response')
                self.responses.setdefault(session, set()).add(params['responseId'])
            elif method in ('item/started', 'item/completed'):
                item = params.get('item', {})
                if item.get('type') == 'dynamicToolCall':
                    key = (session, item['id'])
                    state = self.dynamic.setdefault(key, {})
                    field = 'start' if method == 'item/started' else 'completion'
                    if field in state and state[field] != item:
                        raise ProbeError('conflicting_dynamic_event')
                    state[field] = item
            elif method == 'item/tool/call':
                required = {'callId', 'tool', 'arguments', 'threadId', 'turnId'}
                if not required <= params.keys() or 'id' not in event:
                    raise ProbeError('incompatible_dynamic_call')
                key = (session, params['callId'])
                state = self.dynamic.setdefault(key, {})
                if 'request' in state:
                    raise ProbeError('duplicate_dynamic_request')
                state['request'] = event
                name = (params.get('namespace') or 'functions') + '.' + params['tool']
                # A direct raw call has the wire JSON string; callback arguments
                # are decoded. They are checked at completion, never guessed.
                if key not in self.calls:
                    self.call(session, params['callId'], name, params['arguments'], 'controlled')

    def handler(self, request, response, result, elapsed, session):
        params = request['params']
        value = {'call_id': params['callId'], 'request_id': request['id'],
                 'name': params['tool'], 'arguments': params['arguments'],
                 'result': result, 'response': response, 'elapsed_seconds': elapsed,
                 'outer_cell_id': None, 'outer_cell_association': 'unavailable'}
        if len(encoded(response['result'])) > RESPONSE_BYTES:
            raise ProbeError('tool_response_limit_exceeded')
        self.record('handler.completed', value, session)
        with self.lock:
            self.dynamic[(session, params['callId'])]['handler'] = value

    def verify(self, session, expected, response_count=None):
        with self.lock:
            observed_top = {key[1] for key, call in self.calls.items()
                            if key[0] == session and call['kind'] == 'top_level'}
            if observed_top - set(expected):
                raise ProbeError('unexpected_top_level_call')
            for call_id in expected:
                key = (session, call_id)
                if key not in self.calls or key not in self.raw_results:
                    raise ProbeError('missing_raw_call_or_result')
            for key, call in self.calls.items():
                if key[0] == session and call['kind'] == 'top_level' and key not in self.raw_results:
                    raise ProbeError('missing_raw_result')
            for key, state in self.dynamic.items():
                if key[0] != session:
                    continue
                if set(state) != {'start', 'request', 'handler', 'completion'}:
                    raise ProbeError('incomplete_dynamic_audit')
                params = state['request']['params']
                raw_call = self.calls.get(key)
                if raw_call and raw_call['kind'] == 'top_level':
                    if (raw_call['name'] != (params.get('namespace') or 'functions') + '.' + params['tool']
                            or json_value(raw_call['arguments']) != params['arguments']):
                        raise ProbeError('raw_and_dynamic_call_mismatch')
                for event in (state['start'], state['completion']):
                    if event.get('tool') != params['tool'] or event.get('arguments') != params['arguments']:
                        raise ProbeError('dynamic_audit_mismatch')
                result = state['handler']['response']['result']
                completed = state['completion']
                if (completed.get('contentItems') != result['contentItems']
                        or completed.get('success') != result['success']
                        or completed.get('status') not in ('completed', 'failed')):
                    raise ProbeError('dynamic_result_mismatch')
            responses = self.responses.get(session, set())
            if not responses or response_count is not None and len(responses) != response_count:
                raise ProbeError('missing_raw_response_events')
            return {'status': 'passed', 'calls': sum(key[0] == session for key in self.calls),
                    'controlled_calls': sum(key[0] == session for key in self.dynamic),
                    'outer_cell_association': 'unavailable'}

    def close(self):
        self.output.close()
