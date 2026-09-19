"""Native execution audits must retain work without replacing Codex output policy."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paired_contract import LIMITS, NATIVE_FRAME_BYTES, BASE, TOOL_ENVIRONMENT
from _paired_trial import TrialAudit, validate_audit
from _paired_session import configuration, live_configuration, normalize_instructions
from _codex_capture import Budget, ProbeError


class NativeAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 't0001.jsonl'
        self.path.touch(mode=0o600)
        self.audit = TrialAudit(self.path, Budget(60, LIMITS['events_output_bytes']), native=True)
        self.addCleanup(self.audit.close)

    def event(self, method, item):
        self.audit.receive({'method': method, 'params': {'item': item}}, 'control')

    def raw(self, item):
        self.event('rawResponseItem/completed', item)

    def test_native_large_result_and_nested_command_are_replayable(self):
        self.raw({'type': 'function_call', 'call_id': 'cmd', 'name': 'exec_command', 'arguments': '{"cmd":"rg --files"}'})
        for call in ('cmd', 'nested'):
            self.event('item/started', {'type': 'commandExecution', 'id': call, 'status': 'inProgress'})
            self.event('item/completed', {'type': 'commandExecution', 'id': call, 'status': 'completed',
                'exitCode': 0, 'aggregatedOutput': 'x' * 80000, 'durationMs': 125})
        self.raw({'type': 'function_call_output', 'call_id': 'cmd', 'output': 'x' * 80000})
        self.audit.receive({'method': 'rawResponse/completed', 'params': {'responseId': 'one', 'usage': {}}}, 'control')
        checked = self.audit.verify('control', ['cmd'])
        self.assertEqual(checked['calls'], 2)
        self.assertEqual(len(self.audit.tools), 2)
        self.assertEqual(self.audit.tools[1]['returned_ranges'], [])
        self.raw({'type': 'message', 'id': 'final', 'role': 'assistant', 'phase': 'final_answer',
                  'content': [{'type': 'output_text', 'text': '{"explanation":"x","evidence":[]}'}]})
        record = {'state': 'completed', 'configuration': 'control', 'simulation': True,
            'tool_environment': TOOL_ENVIRONMENT, 'tools': self.audit.tools,
            'usage': self.audit.usage_summary(), 'answer': {'raw': next(iter(self.audit.final_messages.values()))},
            'audit': {'sha256': self.audit.sha.hexdigest(), 'records': self.audit.sequence, 'calls': 2}}
        self.assertTrue(validate_audit(self.path, record, Budget(60, LIMITS['events_output_bytes'])))

    def test_missing_native_completion_is_not_success(self):
        self.event('item/started', {'type': 'commandExecution', 'id': 'nested', 'status': 'inProgress'})
        with self.assertRaisesRegex(ProbeError, 'incomplete_native_audit'):
            self.audit.verify('control', [])

    def test_omitted_command_output_has_unknown_byte_count(self):
        self.event('item/started', {'type': 'commandExecution', 'id': 'nested'})
        self.event('item/completed', {'type': 'commandExecution', 'id': 'nested',
            'status': 'completed', 'exitCode': 0, 'aggregatedOutput': None, 'durationMs': 10})
        self.assertIsNone(self.audit.tools[0]['returned_bytes'])
        self.audit.receive({'method': 'rawResponse/completed', 'params': {'responseId': 'one', 'usage': {}}}, 'control')
        self.assertEqual(self.audit.verify('control', [])['calls'], 1)

    def test_call_and_capture_limits_still_stop_work(self):
        self.audit.call_limit = 1
        self.event('item/started', {'type': 'commandExecution', 'id': 'one'})
        with self.assertRaisesRegex(ProbeError, 'tool_call_limit_exceeded'):
            self.event('item/started', {'type': 'commandExecution', 'id': 'two'})
        self.audit.budget.limit = self.audit.budget.used
        with self.assertRaisesRegex(ProbeError, 'capture_limit_exceeded'):
            self.audit.record('test', {'value': 'more'}, 'control')

    def test_native_configuration_keeps_defaults_and_only_transport_changes(self):
        local, live = configuration(), live_configuration()
        self.assertNotIn('model_instructions_file', local)
        self.assertTrue(local['features.shell_tool'])
        self.assertTrue(local['features.unified_exec'])
        self.assertEqual(local['developer_instructions'], BASE)
        self.assertNotIn('tool_output_token_limit', local)
        self.assertNotIn('environments', local)
        fs = local['permissions']['discovery']['filesystem']
        self.assertEqual(fs['<source>'], 'read')
        self.assertEqual(fs['<temporary>/tmp'], 'write')
        self.assertNotIn(':root', fs)
        self.assertNotIn('<temporary>/codex', fs)
        for config in (local, live):
            config.pop('model_provider')
            for key in list(config):
                if key.startswith('model_providers.'):
                    provider = config.pop(key)
                    self.assertEqual(provider['request_max_retries'], 0)
                    self.assertEqual(provider['stream_max_retries'], 0)
        self.assertEqual(local, live)

    def test_normalization_removes_only_temporary_directory_ids(self):
        value = {'input': '/dev/shm/grepglint-native-abcd/codex/tmp/arg0/codex-arg0E22a elsewhere'}
        self.assertEqual(normalize_instructions(value)['input'], '<temporary>/codex/tmp/arg0/<launcher> elsewhere')
        value['input'] = '/run/user/1000/grepglint-paired-0123/grepglint-native-abcd/codex/tmp/arg0/codex-arg0E22a elsewhere'
        self.assertEqual(normalize_instructions(value)['input'], '<temporary>/codex/tmp/arg0/<launcher> elsewhere')

    def test_proof_accepts_nullable_completed_command_output(self):
        from _paired_proof import CorpusProbe
        from _codex_session import NO_ASSISTANCE
        outputs = {'recursive_listing': 'Process exited with code 0',
            'large_output': 'output truncated', 'waited_command': 'native-yield-complete',
            'orchestrator_skills': 'unsupported call', 'executor_skills': 'unsupported call',
            'input_request': NO_ASSISTANCE,
            'isolation': json.dumps({key: True for key in (
                'source', 'oracle', 'credentials', 'history', 'controller', 'auth', 'source_read_only', 'network')})}
        audit = SimpleNamespace(raw_results={('control', key): {'output': value} for key, value in outputs.items()},
            native_items={('control', 'isolation'): {'completion': {'aggregatedOutput': None}},
                ('control', 'nested'): {'completion': {'aggregatedOutput': '42\n'}},
                ('control', 'background_command'): {'completion': {'aggregatedOutput': None}}})
        checks = CorpusProbe('control', audit, Path('.')).check(audit, 'control')
        self.assertTrue(all(checks.values()))
