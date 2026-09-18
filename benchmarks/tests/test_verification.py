import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _codex_artifacts import create, export, owned, save, seal
from _codex_audit import Audit, ARGUMENT_BYTES, RESPONSE_BYTES, encoded
from _codex_capture import Budget, ProbeError, ResponsesStub
import _codex_handler as handler
import _codex_isolation as isolation
import _codex_verify as verify
from _codex_session import Handlers, inspect_policy
from _codex_smoke import (Attempts, account_check, completed_session, live_configuration,
                          main as smoke_main, ChatGPTProvider, preturn, provider_check,
                          run_confirmed, smoke_pair, stock_provider_evidence, weekly_quota)
from codex_preflight import EFFORT, MODEL, ProbeCancelled, digest
from test_preflight import request


def raw(item):
    return {'method': 'rawResponseItem/completed', 'params': {'item': item}}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'audit.jsonl'
        self.path.touch(mode=0o600)
        self.audit = Audit(self.path, Budget())

    def tearDown(self):
        self.audit.close()
        self.directory.cleanup()

    def wrapper(self, call_id='wrapper'):
        event = raw({'type': 'custom_tool_call', 'call_id': call_id,
                     'namespace': 'functions', 'name': 'exec', 'input': 'await tools.read_file({});'})
        self.audit.receive(event, 'control')
        self.audit.receive(event, 'control')
        self.audit.receive(raw({'type': 'custom_tool_call_output', 'call_id': call_id,
                                'output': 'Script completed; result discarded.'}), 'control')

    def dynamic(self, call_id='nested', success=True):
        arguments = {'path': 'missing.py', 'start': 1, 'end': 2}
        start = {'type': 'dynamicToolCall', 'id': call_id, 'tool': 'read_file',
                 'arguments': arguments, 'status': 'inProgress'}
        request = {'id': 0, 'method': 'item/tool/call', 'params': {
            'callId': call_id, 'threadId': 'thread', 'turnId': 'turn',
            'tool': 'read_file', 'namespace': None, 'arguments': arguments}}
        response = {'id': 0, 'result': {'contentItems': [{'type': 'inputText', 'text': 'failed'}],
                                      'success': success}}
        self.audit.receive({'method': 'item/started', 'params': {'item': start}}, 'control')
        self.audit.receive(request, 'control')
        self.audit.handler(request, response, {'ok': success}, 0.01, 'control')
        end = dict(start, status='completed' if success else 'failed', **response['result'])
        self.audit.receive({'method': 'item/completed', 'params': {'item': end}}, 'control')

    def response_done(self):
        self.audit.receive({'method': 'rawResponse/completed', 'params': {
            'responseId': 'response', 'usage': {'inputTokens': 0}}}, 'control')

    def test_usage_requires_every_observed_response(self):
        self.response_done()
        self.audit.receive({'method': 'rawResponse/completed', 'params': {
            'responseId': 'complete', 'usage': {
                'inputTokens': 2, 'outputTokens': 1, 'totalTokens': 3}}}, 'control')
        with self.assertRaisesRegex(ProbeError, 'incompatible_provider_usage'):
            self.audit.usage('control')

    def test_discarded_failed_nested_calls_are_independent_of_wrapper_output(self):
        self.wrapper()
        self.dynamic(success=False)
        self.response_done()
        result = self.audit.verify('control', ['wrapper'])
        self.assertEqual(result['calls'], 2)
        self.assertEqual(result['controlled_calls'], 1)
        self.assertEqual(result['outer_cell_association'], 'unavailable')
        lines = [json.loads(line) for line in self.path.read_text().splitlines()]
        record = next(line for line in lines if line['kind'] == 'handler.completed')
        self.assertFalse(record['value']['response']['result']['success'])
        self.assertEqual(record['value']['arguments']['path'], 'missing.py')
        self.assertIsNone(record['value']['outer_cell_id'])

    def test_missing_raw_events_do_not_pass(self):
        self.dynamic()
        self.response_done()
        with self.assertRaisesRegex(ProbeError, 'missing_raw_call_or_result'):
            self.audit.verify('control', ['wrapper'])

    def test_each_session_requires_its_own_raw_completion_events(self):
        self.response_done()
        with self.assertRaisesRegex(ProbeError, 'missing_raw_response_events'):
            self.audit.verify('grepglint', [])
        with self.assertRaisesRegex(ProbeError, 'missing_raw_response_events'):
            self.audit.verify('control', [], response_count=2)

    def test_disk_reserve_failure_leaves_existing_log_intact(self):
        self.wrapper()
        before = self.path.read_bytes()
        from collections import namedtuple
        usage = namedtuple('Usage', 'total used free')(2000, 1999, 1)
        with patch('_codex_audit.shutil.disk_usage', return_value=usage):
            with self.assertRaisesRegex(ProbeError, 'disk_reserve'):
                self.audit.record('more', {}, 'control')
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_callback_completion_or_response_does_not_pass(self):
        for field in ('completion', 'handler', 'start'):
            with self.subTest(field=field):
                self.audit.dynamic.clear()
                self.dynamic()
                self.response_done()
                del self.audit.dynamic[('control', 'nested')][field]
                with self.assertRaisesRegex(ProbeError, 'incomplete_dynamic_audit'):
                    self.audit.verify('control', [])

    def test_changed_results_and_duplicate_callbacks_fail(self):
        self.dynamic()
        self.response_done()
        state = self.audit.dynamic[('control', 'nested')]
        with self.assertRaisesRegex(ProbeError, 'duplicate_dynamic_request'):
            self.audit.receive(state['request'], 'control')
        state['completion']['success'] = False
        with self.assertRaisesRegex(ProbeError, 'dynamic_result_mismatch'):
            self.audit.verify('control', [])

    def test_nested_calls_consume_limit_and_arguments_are_bounded(self):
        self.audit.call_limit = 1
        self.dynamic()
        with self.assertRaisesRegex(ProbeError, 'tool_call_limit'):
            self.dynamic('second')
        with self.assertRaisesRegex(ProbeError, 'tool_argument_limit'):
            self.audit.call('control', 'large', 'functions.exec', 'x' * ARGUMENT_BYTES, 'top_level')

    def test_raw_output_limit_keeps_an_incomplete_stream(self):
        with self.assertRaisesRegex(ProbeError, 'tool_response_limit'):
            self.audit.receive(raw({'type': 'function_call_output', 'call_id': 'oversize',
                                    'output': 'x' * RESPONSE_BYTES}), 'control')
        self.assertIn('oversize', self.path.read_text())
        with self.assertRaisesRegex(ProbeError, 'missing_raw_call_or_result'):
            self.audit.verify('control', ['oversize'])


class HandlerTests(unittest.TestCase):
    def test_relative_paths_and_patterns_are_validated(self):
        for value in ('/etc/passwd', '../x', 'x/../y', '.git/config', 'x\\y', '-x', 'x\0y', 'x//y'):
            with self.subTest(value=value), self.assertRaises(handler.HandlerError):
                handler.relative_path(value, glob=True)
        self.assertEqual(handler.relative_path('src/*.py', glob=True), 'src/*.py')

    def test_read_ranges_preserve_exact_bytes_and_reject_links_and_bad_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'data.py').write_bytes('α\nreturn 42\n'.encode())
            (source / 'link').symlink_to('/etc/passwd')
            with patch.object(handler, 'SOURCE', source):
                result = handler.read_range({'path': 'data.py', 'start': 2, 'end': 2})
                self.assertEqual(result, {'path': 'data.py', 'start': 2, 'end': 2,
                    'byte_start': 3, 'byte_end': 13, 'bytes': 10, 'content': 'return 42\n'})
                with self.assertRaises(OSError):
                    handler.read_range({'path': 'link', 'start': 1, 'end': 2})
                for start, end in [(True, 2), (0, 2), (3, 2), (1, 12001)]:
                    with self.assertRaises(handler.HandlerError):
                        handler.read_range({'path': 'data.py', 'start': start, 'end': end})

    def test_search_uses_fixed_argv_and_real_grepglint_errors(self):
        query = '$(touch /tmp/forbidden);`id`'
        with patch.object(handler, 'command', return_value=(0, 'matches', '')) as command:
            result = handler.execute('text_search', {'query': query})
            self.assertEqual(command.call_args.args[0][-3:], ['--', query, '.'])
            self.assertIn('--fixed-strings', command.call_args.args[0])
            self.assertEqual(result['content'], 'matches')
        with patch.object(handler, 'command', return_value=(1, '{"error":"Use rg"}\n', '')) as command:
            result = handler.execute('grepglint_search', {'query': '!!!'})
            self.assertEqual(command.call_args.args[0], ['/opt/grepglint', 'search', '--json', '--', '!!!'])
            self.assertEqual(result['content'], '{"error":"Use rg"}\n')
            self.assertFalse(result['success'])

    def test_invalid_names_and_queries_never_execute_a_process(self):
        with patch.object(handler, 'command') as command:
            for name, arguments in [('exec', {}), ('text_search', {'query': ''}),
                                    ('file_list', {'glob': '/etc/*'}),
                                    ('text_search', {'query': 'ok', 'path': '/etc'})]:
                with self.assertRaises(handler.HandlerError):
                    handler.execute(name, arguments)
            command.assert_not_called()

    def test_subprocess_output_is_bounded_and_timeout_stops_work(self):
        with self.assertRaisesRegex(handler.HandlerError, 'output_limit'):
            handler.command([sys.executable, '-c', 'print("x"*100000)'])
        started = time.monotonic()
        with self.assertRaisesRegex(handler.HandlerError, 'deadline'):
            handler.command([sys.executable, '-c', 'import time; time.sleep(5)'], seconds=0.05)
        self.assertLess(time.monotonic() - started, 2)


class PolicyTests(unittest.TestCase):
    def test_contaminated_unknown_instructions_or_nested_skills_fail(self):
        data = request()
        data['instructions'] = 'Unexpected instruction.'
        with self.assertRaisesRegex(ProbeError, 'unattributed_instruction'):
            inspect_policy(data, 'control')
        data['instructions'] = None
        data['tools'].append({'type': 'function', 'name': 'skills__read'})
        with self.assertRaisesRegex(ProbeError, 'unexpected_or_missing_tool'):
            inspect_policy(data, 'control')

    def test_a_foreign_registration_cannot_impersonate_a_controlled_alias(self):
        data = request()
        data['client_metadata']['x-codex-turn-metadata'] = json.dumps({'tool_namespaces_info': {
            'outside': {'functions': {'read': {'direct': False, 'deferred': False,
                                               'code_mode_name': 'read_file'}}}}})
        with self.assertRaisesRegex(ProbeError, 'unexpected_registry_tool'):
            inspect_policy(data, 'control')


class StartupTests(unittest.TestCase):
    def test_missing_user_manager_returns_unsupported_without_starting_a_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch('_codex_verify.prerequisites'), patch('_codex_verify.Child') as child, \
                    patch('_codex_isolation.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'systemctl')), \
                    redirect_stdout(output):
                code = verify.main(['verify', '--artifacts', directory,
                                    '--codex', sys.executable, '--grepglint', sys.executable])
            child.assert_not_called()
            status = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(status['errors'], ['systemd_user_manager_unavailable'])
            self.assertIn('systemd user manager', status['next_action'])
            self.assertTrue(Path(status['receipt']).is_absolute())
            owned(Path(status['receipt']).parent, sealed=True)

    def test_missing_cgroup_controllers_return_unsupported_before_client_startup(self):
        with patch('pathlib.Path.read_text', side_effect=['0::/user/test\n', FileNotFoundError()]):
            with self.assertRaisesRegex(isolation.UnsupportedHost, 'cgroup_controllers_unavailable'):
                isolation.cgroup_limits()
        with tempfile.TemporaryDirectory() as directory:
            run = create(Path(directory))
            with patch('_codex_verify.cgroup_limits', side_effect=isolation.UnsupportedHost('cgroup_controllers_unavailable')), \
                    patch('_codex_verify.Child') as child:
                receipt = verify.verify_worker(Path(sys.executable), Path(sys.executable), run)
            child.assert_not_called()
            self.assertEqual(receipt['status'], 'unsupported')
            self.assertEqual(receipt['errors'], ['cgroup_controllers_unavailable'])
            self.assertIn('cgroup v2', verify.next_action(receipt))
            owned(run, sealed=True)

    def test_namespace_and_landlock_failures_survive_handler_startup(self):
        output = io.StringIO()
        with patch('_codex_isolation.restrict_handler', side_effect=isolation.UnsupportedHost('landlock_unavailable')), \
                patch.object(sys, 'path', sys.path.copy()), redirect_stdout(output):
            self.assertEqual(handler.main(), 2)
        ready = output.getvalue().encode()
        for failure, code in ((ProbeError('client_exited'), 'handler_namespace_unavailable'),
                              (None, 'landlock_unavailable')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, outside = root / 'source', root / 'outside'
                source.mkdir()
                outside.mkdir()
                for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
                    (outside / name).write_text('sentinel')
                with patch('_codex_session.Child') as child, patch('_codex_session.handler_command', return_value=[]):
                    child.return_value.line.side_effect = failure
                    child.return_value.line.return_value = ready
                    with self.assertRaisesRegex(isolation.UnsupportedHost, code):
                        Handlers(source, Path('/unused'), Budget(2), None, 'control')
                    child.return_value.close.assert_called_once()

    def test_service_startup_failure_is_unsupported(self):
        with tempfile.TemporaryDirectory() as directory:
            run = create(Path(directory))
            with patch('_codex_verify.prerequisites'), patch('_codex_verify.check_user_manager'), \
                    patch('_codex_verify.Child') as child, \
                    patch('_codex_verify.subprocess.run', return_value=subprocess.CompletedProcess([], 3)):
                child.return_value.line.side_effect = ProbeError('client_exited')
                receipt = verify.launch(Path(sys.executable), Path(sys.executable), run)
            self.assertEqual(receipt['status'], 'unsupported')
            self.assertEqual(receipt['errors'], ['verification_service_unavailable'])
            self.assertTrue(receipt['cleanup']['service_stopped'])

    def test_cleanup_failure_preserves_the_startup_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            run = create(Path(directory))
            def completed_worker():
                receipt = verify.receipt_template()
                receipt.update(status='unsupported', worker_started=True, errors=['cgroup_controllers_unavailable'])
                save(run, receipt)
                return b'{"status":"unsupported"}\n'
            with patch('_codex_verify.prerequisites'), patch('_codex_verify.check_user_manager'), \
                    patch('_codex_verify.Child') as child, \
                    patch('_codex_verify.subprocess.run', side_effect=subprocess.TimeoutExpired('systemctl', 1)):
                child.return_value.line.side_effect = completed_worker
                receipt = verify.launch(Path(sys.executable), Path(sys.executable), run)
            self.assertEqual(receipt['status'], 'incomplete')
            self.assertEqual(receipt['errors'], ['cgroup_controllers_unavailable', 'service_cleanup_failed'])


class CallbackTests(unittest.TestCase):
    def test_concurrent_callbacks_execute_serially_and_queue_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, outside = root / 'source', root / 'outside'
            source.mkdir()
            outside.mkdir()
            for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
                (outside / name).write_text('sentinel')
            log = root / 'audit.jsonl'
            log.touch(mode=0o600)
            audit = Audit(log, Budget(5))
            code = ('import json,sys,time\nprint(json.dumps({"ready":True,"checks":{"isolated":True}}),flush=True)\n'
                    'for line in sys.stdin:\n time.sleep(0.02); print(json.dumps({"ok":True,"result":{"content":"ok"}}),flush=True)\n')
            with patch('_codex_session.handler_command', return_value=[sys.executable, '-c', code]):
                worker = Handlers(source, Path('/unused'), Budget(5), audit, 'control')
            class Client:
                proc = None
                def __init__(self):
                    self.results = []
                def send(self, result):
                    self.results.append(result)
            client = Client()
            def callback(index):
                return {'method': 'item/tool/call', 'id': index, 'params': {'callId': str(index),
                    'threadId': 'thread', 'turnId': 'turn', 'tool': 'read_file',
                    'namespace': None, 'arguments': {'path': 'example.py', 'start': 1, 'end': 2}}}
            try:
                for index in range(8):
                    event = callback(index)
                    audit.receive(event, 'control')
                    worker.submit(event)
                with self.assertRaisesRegex(ProbeError, 'queue_limit'):
                    worker.submit(callback(9))
                worker.start(client)
                deadline = time.monotonic() + 3
                while len(client.results) < 8 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(client.results), 8)
                self.assertEqual(worker.queued_max, 8)
                records = [json.loads(line)['kind'] for line in log.read_text().splitlines()
                           if json.loads(line)['kind'].startswith('handler.')]
                self.assertEqual(records, ['handler.started', 'handler.completed'] * 8)
            finally:
                worker.close()
                audit.close()

    def test_scripted_stub_retains_multiple_requests_and_controller_errors(self):
        requests = []
        stub = ResponsesStub(Budget(5), responder=lambda _: [], observe=requests.append)
        stub.start()
        try:
            for number in range(2):
                body = json.dumps({'sequence': number}).encode()
                with socket.create_connection(stub.listener.getsockname(), timeout=2) as client:
                    client.sendall(b'POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: '
                                   + str(len(body)).encode() + b'\r\n\r\n' + body)
                    data = b''
                    while chunk := client.recv(4096):
                        data += chunk
                    self.assertIn(b'response.completed', data)
            self.assertEqual(requests, [{'sequence': 0}, {'sequence': 1}])
        finally:
            stub.close()
        self.assertIsNone(stub.error)


class ArtifactTests(unittest.TestCase):
    def test_retention_preserves_unrelated_files_and_changed_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / 'notes.txt'
            unrelated.write_text('keep')
            runs = []
            for _ in range(4):
                run = create(root)
                seal(run)
                runs.append(run)
            (runs[0] / 'audit.jsonl').write_text('unexpected edit')
            with self.assertRaisesRegex(ProbeError, 'artifact_contents_changed'):
                create(root)
            self.assertEqual(unrelated.read_text(), 'keep')
            self.assertTrue(runs[0].exists())

    def test_completed_unchanged_artifacts_expire_and_partial_artifacts_stay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = create(root)
            seal(run)
            with patch('time.time', return_value=time.time() + 3601):
                next_run = create(root)
            self.assertFalse(run.exists())
            with patch('time.time', return_value=time.time() + 7202):
                with self.assertRaisesRegex(ProbeError, 'partial_artifact_requires_inspection'):
                    create(root)
            self.assertTrue(next_run.exists())

    def test_export_omits_transcript_and_unapproved_nested_fields(self):
        result = export({'schema_version': 2, 'client': {'version': 'pinned', 'secret': 'private'},
            'limits': {'deadline_seconds': 60, 'credential': 'private'},
            'source': 'private', 'transcript': 'private', 'provider_verification': {'token': 'private'},
            'sessions': [{'configuration': 'control', 'audit': {'calls': 1, 'content': 'private'}}]})
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['sessions'][0]['audit']['calls'], 1)

    def test_replaced_receipt_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            run = create(Path(directory))
            (run / 'receipt.json').unlink()
            (run / 'receipt.json').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ProbeError, 'artifact_ownership_changed'):
                save(run, {})
            self.assertTrue((run / 'receipt.json').is_symlink())


def quota(percent=10, reset=2000000000):
    return {'ordinaryUsageAllowed': True, 'rateLimitResetCredits': None,
        'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'secondary': {
            'usedPercent': percent, 'windowDurationMins': 10080, 'resetsAt': reset}}}}


class FakeProvider:
    def __init__(self):
        self.turns = []
        self.config_hash = 'configuration-hash'
        self.evidence_value = {'origin': 'pinned_client_request_and_protocol_observation',
            **{key: True for key in stock_provider_evidence() if key not in ('origin', 'source')}}

    def account(self):
        return {'requiresOpenaiAuth': True, 'account': {
            'type': 'chatgpt', 'email': 'fixture@example.invalid', 'planType': 'pro'}}

    def models(self):
        return [{'model': MODEL, 'supportedReasoningEfforts': [{'reasoningEffort': EFFORT}]}]

    def quota(self):
        return quota()

    def preflight_probe(self, configuration):
        return {'configuration_sha256': self.config_hash, 'isolation': True,
                'reported_model': MODEL, 'reported_effort': EFFORT}

    def evidence(self, proofs=None, probe=None):
        return self.evidence_value

    def session(self, configuration, seconds, calls, reserve):
        reserve()
        self.turns.append((configuration, seconds, calls))
        return {'model': MODEL, 'effort': EFFORT, 'calls': 3, 'elapsed_seconds': 1,
                'audit_status': 'passed', 'usage': {'inputTokens': 10, 'outputTokens': 4, 'totalTokens': 14}}


class SmokeTests(unittest.TestCase):
    def test_authenticated_mount_exposes_only_private_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_dir = root / 'bin'
            config = root / 'config'
            binary_dir.mkdir()
            config.mkdir()
            binary = binary_dir / 'codex'
            binary.write_bytes(b'binary')
            auth = root / 'auth.json'
            auth.write_text('PRIVATE_AUTH_VALUE')
            auth.chmod(0o600)
            command = isolation.authenticated_client_command(binary, config, auth, ['app-server'])
            self.assertIn(str(auth), command)
            self.assertNotIn('PRIVATE_AUTH_VALUE', json.dumps(command))
            self.assertEqual(command.count(str(auth)), 1)
            self.assertIn('/work/codex/auth.json', command)
            self.assertNotIn(str(root / 'unrelated'), command)
            self.assertIn('/etc/resolv.conf', command)
            self.assertIn('/etc/ssl/certs', command)

    def test_chatgpt_adapter_uses_protocol_metadata_and_one_turn_submission(self):
        server_source = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "account/read":
        result = {"requiresOpenaiAuth": True, "account": {"type": "chatgpt", "email": "private@example.invalid", "planType": "pro"}}
    elif method == "model/list":
        result = {"data": [{"model": "gpt-5.6-luna", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}], "nextCursor": None}
    elif method == "account/rateLimits/read":
        result = {"ordinaryUsageAllowed": True, "rateLimitResetCredits": None, "rateLimitsByLimitId": {"codex": {"secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 4102444800}}}}
    elif method == "thread/start":
        result = {"model": "gpt-5.6-luna", "reasoningEffort": "high", "thread": {"id": "thread"}}
    else:
        result = {}
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
    if method == "turn/start":
        print(json.dumps({"method": "rawResponseItem/completed", "params": {"item": {"type": "custom_tool_call", "call_id": "wrapper", "namespace": "functions", "name": "exec", "input": "await tools.read_file({});"}}}), flush=True)
        print(json.dumps({"method": "rawResponseItem/completed", "params": {"item": {"type": "custom_tool_call_output", "call_id": "wrapper", "output": "done"}}}), flush=True)
        arguments = {"path": "example.py", "start": 1, "end": 2}
        item = {"type": "dynamicToolCall", "id": "nested", "tool": "read_file", "arguments": arguments, "status": "inProgress"}
        print(json.dumps({"method": "item/started", "params": {"item": item}}), flush=True)
        print(json.dumps({"id": 90, "method": "item/tool/call", "params": {"callId": "nested", "threadId": "thread", "turnId": "turn", "tool": "read_file", "namespace": None, "arguments": arguments}}), flush=True)
        completed = dict(item, status="completed", success=True, contentItems=[{"type": "inputText", "text": "ok"}])
        print(json.dumps({"method": "item/completed", "params": {"item": completed}}), flush=True)
        print(json.dumps({"method": "rawResponse/completed", "params": {"responseId": "one", "usage": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"turn": {"status": "completed"}}}), flush=True)
'''
        class DummyHandlers:
            def __init__(self, source, grepglint, budget, audit, session):
                self.error = None
                self.audit, self.session = audit, session
                self.child = type('Worker', (), {'proc': type('Process', (), {'pid': os.getpid()})()})()
            def start(self, client):
                pass
            def submit(self, request):
                response = {'id': request['id'], 'result': {'success': True,
                    'contentItems': [{'type': 'inputText', 'text': 'ok'}]}}
                self.audit.handler(request, response, {'ok': True}, 0.01, self.session)
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / 'server.py'
            server.write_text(server_source)
            auth = root / 'auth.json'
            auth.write_text('{}')
            auth.chmod(0o600)
            grepglint = root / 'grepglint'
            grepglint.write_text('#!/bin/sh\nprintf \'{"tools":[]}\'\n')
            grepglint.chmod(0o700)
            audit = root / 'audit.jsonl'
            with patch('_codex_smoke.prerequisites'), \
                 patch('_codex_smoke.authenticated_client_command',
                       return_value=[sys.executable, str(server)]), \
                 patch('_codex_smoke.children_in_current_cgroup', return_value=True), \
                 patch('_codex_smoke.Handlers', DummyHandlers):
                provider = ChatGPTProvider(root / 'codex', grepglint, auth, audit)
                try:
                    account_check(provider.account(), provider.models())
                    self.assertEqual(len(weekly_quota(provider.quota(), 100)['buckets']), 1)
                    reservations = []
                    result = provider.session('control', 120, 20,
                                              lambda: reservations.append('control'))
                    self.assertEqual(reservations, ['control'])
                    self.assertEqual(result['usage']['totalTokens'], 6)
                    self.assertEqual(result['calls'], 2)
                finally:
                    provider.close()

    def test_default_smoke_is_offline(self):
        with patch('_codex_smoke.require_local') as local, patch('builtins.print'):
            self.assertEqual(smoke_main([]), 3)
            local.assert_not_called()

    def test_launch_failure_never_overwrites_an_existing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / 'existing.json'
            receipt.write_text('preserve-me')
            args = ['--provider', 'chatgpt', '--local-receipt', str(root / 'local.json'),
                    '--codex', str(root / 'codex'), '--grepglint', str(root / 'grepglint'),
                    '--receipt', str(receipt)]
            with patch('_codex_smoke.check_user_manager',
                       side_effect=isolation.UnsupportedHost('manager_unavailable')), \
                 patch('builtins.print'):
                self.assertEqual(smoke_main(args), 3)
            self.assertEqual(receipt.read_text(), 'preserve-me')

    def test_exact_authentication_model_and_effort(self):
        provider = FakeProvider()
        account_check(provider.account(), provider.models())
        for account in ({'account': {'type': 'apiKey'}}, {'account': None}, {}):
            with self.assertRaises(ProbeError):
                account_check(account, provider.models())
        with self.assertRaisesRegex(ProbeError, 'exact_model_and_effort'):
            account_check(provider.account(), [{'model': 'gpt-other'}])

    def test_provider_evidence_is_derived_from_local_proof_and_live_probe(self):
        checks = {'normalized_configuration': True, 'prompt_and_catalog': True,
                  'runtime_registrations': True, 'direct_skill_handlers': True,
                  'nested_aliases': True, 'negative_capabilities': True}
        probe = {'configuration_sha256': digest(encoded(live_configuration())),
                 'isolation': True}
        evidence = stock_provider_evidence({'checks': checks}, probe)
        provider_check(evidence)
        checks['nested_aliases'] = False
        with self.assertRaisesRegex(ProbeError, 'provider_effective_request_unverified'):
            provider_check(stock_provider_evidence({'checks': checks}, probe))

    def test_weekly_quota_requires_fresh_explicit_availability(self):
        self.assertEqual(weekly_quota(quota(), 100)['buckets'][0]['used_percent'], 10)
        for observation in ({}, quota(100), quota(reset=50),
                            dict(quota(), ordinaryUsageAllowed=None)):
            with self.assertRaises(ProbeError):
                weekly_quota(observation, 100)

    def test_provider_uncertainty_consumes_no_turn_or_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts.json'
            attempts = Attempts(path)
            provider = FakeProvider()
            provider.evidence_value = {'origin': 'pinned_client_request_and_protocol_observation'}
            try:
                with self.assertRaisesRegex(ProbeError, 'provider_effective_request_unverified'):
                    smoke_pair(provider, attempts, 'receipt-hash')
                self.assertEqual(provider.turns, [])
                self.assertEqual(attempts.entries, [])
            finally:
                attempts.close()
            attempts = Attempts(path)
            try:
                attempts.reserve('control', 'receipt')
                self.assertEqual(attempts.entries[0]['status'], 'reserved')
            finally:
                attempts.close()

    def test_two_fake_sessions_exhaust_the_total_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = Attempts(Path(directory) / 'attempts.json')
            provider = FakeProvider()
            try:
                self.assertEqual(len(smoke_pair(provider, attempts, 'hash')), 2)
                self.assertEqual(provider.turns, [('control', 120, 20), ('grepglint', 120, 20)])
                with self.assertRaisesRegex(ProbeError, 'smoke_attempts_exhausted'):
                    attempts.next_configuration()
            finally:
                attempts.close()

    def test_confirmation_binds_quota_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = Attempts(Path(directory) / 'attempts.json')
            provider = FakeProvider()
            proofs = {'receipt_sha256': 'proof'}
            try:
                prepared = preturn(provider, proofs, attempts, now=100)
                provider.quota = lambda: quota(11)
                with self.assertRaisesRegex(ProbeError, 'stale_or_missing_confirmation'):
                    run_confirmed(provider, attempts, proofs, prepared['confirmation'])
                self.assertEqual(attempts.entries, [])
                provider.quota = lambda: quota(10)
                prepared = preturn(provider, proofs, attempts, now=100)
                attempts.reserve('control', 'proof')
                with self.assertRaisesRegex(ProbeError, 'baseline_not_passed'):
                    run_confirmed(provider, attempts, proofs, prepared['confirmation'])
            finally:
                attempts.close()

    def test_uncertain_submission_consumes_baseline_and_blocks_treatment(self):
        class Uncertain(FakeProvider):
            def session(self, configuration, seconds, calls, reserve):
                reserve()
                raise ProbeError('client_exited')

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts.json'
            attempts = Attempts(path)
            provider = Uncertain()
            proofs = {'receipt_sha256': 'proof'}
            events = []
            try:
                prepared = preturn(provider, proofs, attempts)
                with self.assertRaisesRegex(ProbeError, 'client_exited'):
                    run_confirmed(provider, attempts, proofs, prepared['confirmation'],
                                  on_prepared=lambda value: events.append('prepared'),
                                  on_reserved=lambda value: events.append('reserved'))
                self.assertEqual(events, ['prepared', 'reserved'])
                self.assertEqual(attempts.entries[0]['status'], 'failed')
                with self.assertRaisesRegex(ProbeError, 'baseline_not_passed'):
                    attempts.next_configuration()
            finally:
                attempts.close()

    def test_success_remains_reserved_until_cleanup_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            attempts = Attempts(Path(directory) / 'attempts.json')
            provider = FakeProvider()
            proofs = {'receipt_sha256': 'proof'}
            try:
                prepared = preturn(provider, proofs, attempts)
                session = run_confirmed(provider, attempts, proofs, prepared['confirmation'])
                self.assertEqual(session['status'], 'passed')
                self.assertEqual(attempts.entries[0]['status'], 'reserved')
                with self.assertRaisesRegex(ProbeError, 'baseline_not_passed'):
                    attempts.next_configuration()
                attempts.finish('control', 'passed')
                self.assertEqual(attempts.next_configuration(), 'grepglint')
            finally:
                attempts.close()

    def test_worker_cancellation_after_reservation_retains_partial_evidence(self):
        class CancelProvider(FakeProvider):
            def __init__(self, *args):
                super().__init__()
            def session(self, configuration, seconds, calls, reserve):
                reserve()
                raise ProbeCancelled()
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / 'cancelled.json'
            ledger = root / 'attempts.json'
            proofs = {'receipt_sha256': 'proof', 'checks': {}, 'sessions': []}
            args = ['--provider', 'chatgpt', '--local-receipt', str(root / 'local.json'),
                    '--codex', str(root / 'codex'), '--grepglint', str(root / 'grepglint'),
                    '--receipt', str(receipt), '--confirm', 'confirmed',
                    '--worker', '--invocation', '0' * 32]
            with patch('_codex_smoke.cgroup_limits'), \
                 patch('_codex_smoke.require_local', return_value=proofs), \
                 patch('_codex_smoke.ledger_path', return_value=ledger), \
                 patch('_codex_smoke.ChatGPTProvider', CancelProvider), \
                 patch('_codex_smoke.confirmation_value', return_value='confirmed'), \
                 patch('builtins.print'):
                self.assertEqual(smoke_main(args), 3)
            result = json.loads(receipt.read_text())
            self.assertTrue(result['inference_performed'])
            self.assertEqual(result['attempt']['status'], 'failed')
            self.assertIn('quota_before', result['attempt'])
            self.assertEqual(result['errors'], ['cancelled'])
            attempts = Attempts(ledger)
            try:
                self.assertEqual(attempts.entries[0]['status'], 'failed')
                with self.assertRaisesRegex(ProbeError, 'baseline_not_passed'):
                    attempts.next_configuration()
            finally:
                attempts.close()

    def test_all_weekly_buckets_are_retained(self):
        value = quota()
        value['rateLimitsByLimitId']['other'] = {'primary': {
            'usedPercent': 2, 'windowDurationMins': 10080, 'resetsAt': 2000000100},
            'secondary': {'usedPercent': 3, 'windowDurationMins': 300, 'resetsAt': 2000000100}}
        result = weekly_quota(value, 100)
        self.assertEqual([(item['bucket_id'], item['slot']) for item in result['buckets']],
                         [('codex', 'secondary'), ('other', 'primary')])

    def test_missing_usage_reset_and_tool_overflow_do_not_pass(self):
        result = FakeProvider().session('control', 120, 20, lambda: None)
        before, after = weekly_quota(quota(10), 100), weekly_quota(quota(11), 101)
        completed_session(result, before, after)
        for key, value, message in [('usage', None, 'provider_usage_missing'),
                                    ('calls', 21, 'smoke_session_limit'),
                                    ('elapsed_seconds', 121, 'smoke_session_limit'),
                                    ('model', 'wrong', 'provider_model')]:
            with self.assertRaisesRegex(ProbeError, message):
                completed_session(dict(result, **{key: value}), before, after)
        with self.assertRaisesRegex(ProbeError, 'weekly_quota_reset'):
            completed_session(result, before, weekly_quota(quota(0, reset=2000000100), 102))


if __name__ == '__main__':
    unittest.main()
