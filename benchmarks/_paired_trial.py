"""Corpus trial audit and deterministic, account-free Responses scripts."""
import time

from _codex_audit import Audit
from _codex_capture import ProbeError, json_value
from _codex_session import function
from _paired_contract import ANSWER_BYTES, returned_ranges, LIMITS, NATIVE_FRAME_BYTES, TOOL_ENVIRONMENT


class TrialAudit(Audit):
    def __init__(self, path, budget, *, simulation=True, native=False):
        self.simulation = simulation
        self.native = native
        super().__init__(path, budget, call_limit=LIMITS['tool_calls'] if native else 100)
        if native:
            self.argument_limit = LIMITS['argument_bytes']
            self.response_limit = LIMITS['response_bytes']
            self.frame_limit = NATIVE_FRAME_BYTES
        self.native_items = {}
        self.optional_usage = {}
        self.final_messages = {}
        self.final_status = 'missing'
        self.tools = []

    def receive(self, event, session):
        with self.lock:
            self._receive(event, session)

    def _receive(self, event, session):
        method = event.get('method')
        params = event.get('params', {})
        if method == 'rawResponse/completed':
            # Optional provider counters never weaken required response/call audit.
            response = params.get('responseId')
            usage = params.get('usage')
            if not isinstance(response, str) or not response:
                raise ProbeError('incompatible_raw_response')
            if usage is not None and not isinstance(usage, dict):
                raise ProbeError('invalid_provider_usage')
            usage = usage or {}
            counters = {}
            for key in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens'):
                value = usage.get(key)
                if value is not None and (type(value) is not int or value < 0):
                    raise ProbeError('invalid_provider_usage')
                counters[key] = value
            previous = self.optional_usage.setdefault(response, counters)
            if previous != counters:
                raise ProbeError('conflicting_provider_usage')
            # Keep the actual event in the stream. Base audit accepts an empty
            # dictionary, while its stricter smoke-only usage() stays unchanged.
            if params.get('usage') is None:
                self.record('provider.usage_absent', {'response_id': response}, session)
                event = {**event, 'params': {**params, 'usage': {}}}
        super().receive(event, session)
        if self.native and method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            names = {'commandExecution': 'exec_command', 'fileChange': 'apply_patch',
                     'imageView': 'view_image'}
            name = names.get(item.get('type'))
            if name:
                key = (session, item['id'])
                state = self.native_items.setdefault(key, {})
                field = 'start' if method == 'item/started' else 'completion'
                if field in state:
                    if state[field] != item:
                        raise ProbeError('conflicting_native_event')
                    return
                state[field] = item
                if key not in self.calls:
                    self.call(session, item['id'], 'functions.' + name, {}, 'native')
                if field == 'completion':
                    output = item.get('aggregatedOutput')
                    self.tools.append({'call_id': item['id'], 'name': name,
                        'elapsed_seconds': (item.get('durationMs') or 0) / 1000,
                        'success': item.get('status', 'completed') == 'completed' and item.get('exitCode') in (None, 0),
                        'returned_bytes': len(output.encode()) if output is not None else None, 'returned_ranges': [],
                        'range_status': 'unavailable_native_output',
                        'bytes_method': 'app-server command output; model-visible truncation is retained in raw results',
                        'error': None, 'exit_code': item.get('exitCode'), 'disk_peak': None,
                        'result_record': self.sequence - 1})
        if method == 'rawResponseItem/completed':
            item = params.get('item', {})
            if item.get('type') == 'message' and item.get('role') == 'assistant' and item.get('phase') in (None, 'final_answer'):
                parts = item.get('content', [])
                if not isinstance(parts, list) or any(p.get('type') != 'output_text' or not isinstance(p.get('text'), str) for p in parts):
                    raise ProbeError('invalid_final_message')
                text = ''.join(p['text'] for p in parts)
                if len(text.encode()) > ANSWER_BYTES:
                    self.final_status = 'oversized'
                    raise ProbeError('final_answer_limit_exceeded')
                key = item.get('id')
                if not isinstance(key, str) or not key:
                    raise ProbeError('missing_final_message_identity')
                if key in self.final_messages and self.final_messages[key] != text:
                    raise ProbeError('conflicting_final_answer')
                if self.final_messages and key not in self.final_messages:
                    raise ProbeError('multiple_final_answers')
                self.final_messages[key] = text
                self.final_status = 'captured'

    def handler(self, request, response, result, elapsed, session):
        with self.lock:
            self._handler(request, response, result, elapsed, session)

    def _handler(self, request, response, result, elapsed, session):
        super().handler(request, response, result, elapsed, session)
        params = request['params']
        value = result.get('result', {})
        self.tools.append({'call_id': params['callId'], 'name': params['tool'],
            'elapsed_seconds': elapsed, 'success': response['result']['success'],
            'returned_bytes': value.get('bytes', 0),
            'returned_ranges': returned_ranges(params['tool'], result),
            'error': result.get('error'), 'exit_code': value.get('exit_code'),
            'disk_peak': value.get('disk_peak'),
            'result_record': self.sequence - 1})
        if any(term in result.get('error', '') for term in ('limit_exceeded', 'deadline')):
            raise ProbeError('trial_tool_bound_exhausted')

    def usage_summary(self):
        values = list(self.optional_usage.values())
        fields = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')
        return {'simulation': self.simulation, 'scope': 'model_only', 'response_count': len(values),
                'counters': {key: sum(v[key] for v in values) if values and all(v[key] is not None for v in values) else None
                             for key in fields},
                'complete': {key: bool(values) and all(v[key] is not None for v in values) for key in fields},
                'inclusions': 'cached input is included in input; reasoning is included in output; never added again',
                'quota': None, 'quota_completeness': 'not_applicable_simulation' if self.simulation else 'recorded_separately'}

    def verify(self, session, expected, response_count=None):
        if any(set(state) != {'start', 'completion'} for (name, _), state in self.native_items.items() if name == session):
            raise ProbeError('incomplete_native_audit')
        return super().verify(session, expected, response_count)

    def observations(self):
        grep = [t for t in self.tools if t['name'] == 'grepglint_search']
        first_error = next((i for i, t in enumerate(self.tools) if t['name'] == 'grepglint_search' and not t['success']), None)
        fallback = [t['call_id'] for t in self.tools[(first_error + 1):] if t['name'] in ('text_search', 'read_file', 'file_list', 'exec_command', 'write_stdin')] if first_error is not None else []
        peaks = {'cache_bytes': 0, 'database_bytes': 0, 'journal_bytes': 0}
        for tool in self.tools:
            for key, value in (tool.get('disk_peak') or {}).items():
                peaks[key] = max(peaks.get(key, 0), value)
        return {'used': bool(grep), 'non_use': not grep, 'indexing_seconds': grep[0]['elapsed_seconds'] if grep else None,
                'indexing_method': 'first search wall time includes cold indexing; no prewarming',
                'subsequent_search_seconds': [t['elapsed_seconds'] for t in grep[1:]],
                'errors': [t['call_id'] for t in grep if not t['success']], 'fallback_calls': fallback,
                'disk_peak': peaks, 'disk_method': 'sampled file sizes at pipe drains; lower bounds'}


class FakeCodex:
    """Script generic discovery, never derive queries from tasks or reference data."""
    def __init__(self, configuration, audit, scenario='success'):
        self.configuration, self.audit, self.scenario = configuration, audit, scenario
        self.step = 0
        self.expected = []

    def __call__(self, request):
        if self.scenario == 'transport-loss':
            raise ProbeError('simulated_transport_loss')
        step = self.step
        self.step += 1
        if step == 0 and self.configuration == 'grepglint' and self.scenario != 'non-use':
            items = [function('cold_search', 'functions.grepglint_search', {'query': 'benchmark'})]
        elif step <= 1:
            self.step = 2
            items = [function('list', 'functions.exec_command', {'cmd': 'rg --files'})]
        elif step == 2:
            items = [function('read', 'functions.exec_command', {'cmd': "for p in *README*; do sed -n '1,8p' \"$p\"; done"})]
        elif step == 3:
            items = [function('search', 'functions.exec_command', {'cmd': "rg -n -i copyright -g '*README*' ."})]
        else:
            return self.final()
        self.expected.extend(i['call_id'] for i in items)
        return items

    def final(self):
        import json
        evidence = []
        text = json.dumps({'explanation': 'SIMULATED answer for offline workflow testing. No factual claim about the task was generated.', 'evidence': evidence})
        if self.scenario == 'malformed':
            text = 'SIMULATED malformed final answer'
        if self.scenario == 'missing':
            return [{'type': 'message', 'id': 'no_final', 'role': 'assistant', 'phase': 'commentary',
                     'content': [{'type': 'output_text', 'text': 'SIMULATED missing final answer'}]}]
        if self.scenario == 'oversized':
            text = 'X' * (ANSWER_BYTES + 1)
        return [{'type': 'message', 'id': 'simulated_final', 'role': 'assistant', 'phase': 'final_answer',
                 'content': [{'type': 'output_text', 'text': text}]}]


class ReplayAudit(TrialAudit):
    """Replay the same required call checks without opening a writable artifact."""
    def __init__(self, budget, native=False):
        import hashlib
        import threading
        self.budget = budget
        self.native = native
        self.native_items = {}
        self.call_limit = LIMITS['tool_calls'] if native else 100
        if native:
            self.argument_limit = LIMITS['argument_bytes']
            self.response_limit = LIMITS['response_bytes']
            self.frame_limit = NATIVE_FRAME_BYTES
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.sequence = 0
        self.sha = hashlib.sha256()
        self.calls, self.raw_results, self.dynamic = {}, {}, {}
        self.responses, self.response_usage, self.optional_usage = {}, {}, {}
        self.final_messages = {}
        self.final_status = 'missing'
        self.tools = []

    def record(self, kind, value, session):
        pass


def validate_audit(path, record, budget):
    import hashlib
    replay = ReplayAudit(budget, native=record.get('tool_environment') == TOOL_ENVIRONMENT)
    replay.simulation = record['simulation']
    sha = hashlib.sha256()
    sequence = 0
    partial = False
    with path.open('rb') as stream:
        while line := stream.readline(replay.frame_limit + 1):
            budget.add(len(line))
            if len(line) > replay.frame_limit:
                raise ProbeError('audit_frame_limit_exceeded')
            sha.update(line)
            if not line.endswith(b'\n'):
                partial = True
                break
            event = json_value(line)
            if event.get('sequence') != sequence:
                raise ProbeError('audit_sequence_mismatch')
            sequence += 1
            if record['state'] != 'completed':
                continue
            if event.get('session') != record['configuration']:
                raise ProbeError('audit_session_mismatch')
            replay.sequence = sequence
            if event['kind'] == 'rpc.received':
                replay.receive(event['value'], event['session'])
            elif event['kind'] == 'handler.completed':
                value = event['value']
                state = replay.dynamic.get((event['session'], value['call_id']), {})
                request = state.get('request')
                if not request or value['arguments'] != request['params']['arguments'] or value['name'] != request['params']['tool']:
                    raise ProbeError('audit_handler_reference_mismatch')
                replay.handler(request, value['response'], value['result'], value['elapsed_seconds'], event['session'])
    if record['state'] == 'completed':
        metadata = record['audit']
        if partial or metadata['sha256'] != sha.hexdigest() or metadata['records'] != sequence or metadata['calls'] != len(replay.calls):
            raise ProbeError('audit_identity_or_count_mismatch')
        expected = [call_id for (session, call_id), call in replay.calls.items() if call['kind'] == 'top_level']
        replay.verify(record['configuration'], expected, record['usage'].get('response_count'))
        if record['tools'] != replay.tools or record['usage'] != replay.usage_summary():
            raise ProbeError('audit_tool_or_usage_mismatch')
        if list(replay.final_messages.values()) != [record['answer']['raw']]:
            raise ProbeError('audit_final_answer_mismatch')
    return not partial
