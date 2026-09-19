"""Offline authorization and real adapter protocol tests. No account access."""
import copy
import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _codex_capture import Budget, ProbeError, Child
from _codex_smoke import ChatGPTProvider, weekly_quota
from _codex_session import Handlers
from _paired_attempts import Attempts
from _paired_live import quota_compatible, observation, Run
from _paired_proof import plan_hash
from _paired_trial import TrialAudit
import _paired_store as store
import _paired_contract as contract
import _paired_live as live
import paired
from test_paired import tiny_plan, fixture
from _paired_source import verify


def quota(used=10, reset=None, now=None):
    now = time.time() if now is None else now
    return weekly_quota({'ordinaryUsageAllowed': True, 'accountId': 'private',
        'rateLimitsByLimitId': {'codex': {'primary': {'windowDurationMins': 10080,
             'usedPercent': used, 'resetsAt': reset or int(now + 100000)}}}}, now)


class Authorization(unittest.TestCase):
    def test_consumption_survives_process_restart_and_artifact_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts'
            plan = tiny_plan()
            binding = {'plan_sha256': plan_hash(plan)}
            ledger = Attempts(path)
            token = ledger.prepare('run-a', binding, plan['trials'])
            with self.assertRaisesRegex(ProbeError, 'another_account'):
                Attempts(path)
            ledger.start('run-a', binding, token)
            ledger.trial('run-a', 't0001', 'reserved')
            ledger.close()
            ledger = Attempts(path)
            try:
                self.assertEqual(ledger.runs['run-a']['states']['t0001'], 'reserved')
                self.assertEqual(ledger.runs['run-a']['states']['t0002'], 'not-started')
                with self.assertRaisesRegex(ProbeError, 'consumed'):
                    ledger.start('run-a', binding, token)
                with self.assertRaisesRegex(ProbeError, 'consumed'):
                    ledger.trial('run-a', 't0001', 'reserved')
                new = ledger.prepare('run-b', binding, plan['trials'])
                self.assertNotEqual(new, token)
                with self.assertRaises(ProbeError):
                    ledger.start('run-b', binding, token)
            finally:
                ledger.close()
            self.assertFalse((Path(directory) / 'codex-smoke-attempts-v1.json').exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_all_binding_changes_and_partial_ledger_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts'
            ledger = Attempts(path)
            binding = {'plan_sha256': 'a', 'account': 'a', 'proof': 'b', 'config': 'c'}
            token = ledger.prepare('run', binding, tiny_plan()['trials'])
            for key in binding:
                changed = {**binding, key: 'changed'}
                with self.assertRaisesRegex(ProbeError, 'changed_plan'):
                    ledger.start('run', changed, token)
            ledger.close()
            with path.open('ab') as stream:
                stream.write(b'{')
            before = path.read_bytes()
            with self.assertRaisesRegex(ProbeError, 'partial_attempt'):
                Attempts(path)
            self.assertEqual(before, path.read_bytes())

    def test_capacity_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Attempts(Path(directory) / 'ledger')
            try:
                with patch('_paired_attempts.MAX_RUNS', 1):
                    ledger.prepare('one', {'plan_sha256': 'a'}, tiny_plan()['trials'])
                    before = ledger.path.read_bytes()
                    with self.assertRaisesRegex(ProbeError, 'capacity'):
                        ledger.prepare('two', {'plan_sha256': 'a'}, tiny_plan()['trials'])
                    self.assertEqual(before, ledger.path.read_bytes())
            finally:
                ledger.close()

    def test_partial_write_blocks_all_further_ledger_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Attempts(Path(directory) / 'ledger')
            try:
                original = os.write
                with patch('_paired_attempts.os.write', side_effect=lambda fd, data: original(fd, data[:10])):
                    with self.assertRaisesRegex(ProbeError, 'partial_attempt'):
                        ledger.prepare('run', {'plan_sha256': 'a'}, tiny_plan()['trials'])
                before = ledger.path.read_bytes()
                with self.assertRaisesRegex(ProbeError, 'failed_attempt_write'):
                    ledger.prepare('other', {'plan_sha256': 'a'}, tiny_plan()['trials'])
                self.assertEqual(before, ledger.path.read_bytes())
            finally:
                ledger.close()

    def test_new_history_invalidates_old_unused_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Attempts(Path(directory) / 'ledger')
            try:
                binding = {'plan_sha256': 'a'}
                token = ledger.prepare('old', binding, tiny_plan()['trials'])
                ledger.prepare('new', binding, tiny_plan()['trials'])
                with self.assertRaisesRegex(ProbeError, 'changed_plan'):
                    ledger.start('old', binding, token)
            finally:
                ledger.close()

    def test_export_quota_allowlist_omits_raw_private_fields(self):
        from _paired_live import public_quota
        value = observation(SimpleNamespace(quota=lambda: {
            'ordinaryUsageAllowed': False, 'accountId': 'PRIVATE', 'token': 'PRIVATE',
            'rateLimitsByLimitId': {'codex': {'primary': {'windowDurationMins':10080,
                'usedPercent':100, 'resetsAt':4102444800}}}}), ['trial_after'])
        exported = public_quota([value])
        self.assertNotIn('PRIVATE', json.dumps(exported))
        self.assertEqual(exported[0]['buckets'][0]['remaining_percent'], 0)
        self.assertFalse(exported[0]['available'])

    def test_digest_binds_selection_order_source_configuration_limits(self):
        plan = tiny_plan()
        plan['prepared_sources'] = {'source': {'sha256': 'a', 'seconds': 1}}
        original = plan_hash(plan)
        same = copy.deepcopy(plan)
        same['prepared_sources']['source']['seconds'] = 2
        self.assertEqual(original, plan_hash(same))
        changes = [lambda p: p['trials'].reverse(),
                   lambda p: p['trials'][0].update(question='changed'),
                   lambda p: p['trials'][0].update(repetition=2),
                   lambda p: p['prepared_sources']['source'].update(sha256='b'),
                   lambda p: p.update(requested_model='other'),
                   lambda p: p['limits'].update(tool_calls=101)]
        for change in changes:
            other = copy.deepcopy(plan)
            change(other)
            self.assertNotEqual(original, plan_hash(other))

    def test_usage_can_increase_without_renewed_approval(self):
        before = quota(now=1000, reset=100000)
        quota_compatible(before, quota(used=11, now=1100, reset=100000))
        for later in (quota(used=9, now=1100, reset=100000), quota(now=1100, reset=100001)):
            with self.assertRaises(ProbeError):
                quota_compatible(before, later)
        a = quota(0, now=1000, reset=605800)
        b = quota(0, now=1010, reset=605810)
        quota_compatible(a, b)
        quota_compatible(a, quota(1, now=1010, reset=605800))
        b['account_identity_sha256'] = 'other'
        with self.assertRaisesRegex(ProbeError, 'account_changed'):
            quota_compatible(a, b)
        provider = SimpleNamespace(quota=lambda: {'ordinaryUsageAllowed': False})
        result = observation(provider, ['trial_after'])
        self.assertIsNone(result['weekly'])
        self.assertFalse(result['raw']['ordinaryUsageAllowed'])
        self.assertIsNotNone(result['error'])


SERVER = '''import json, sys
from pathlib import Path
log = Path(sys.argv[1])
scenario = sys.argv[2]
for line in sys.stdin:
 r = json.loads(line)
 with log.open('a') as out: out.write(json.dumps(r)+'\\n')
 m = r.get('method')
 if 'id' not in r: continue
 if m == 'account/read':
  result = {'requiresOpenaiAuth':True,'account':{'type':'chatgpt','email':'fake@example.invalid','planType':'pro'}}
 elif m == 'model/list':
  result = {'data':[{'model':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'high'}]}],'nextCursor':None}
 elif m == 'account/rateLimits/read':
  result = {'ordinaryUsageAllowed':True,'rateLimitsByLimitId':{'codex':{'primary':{'windowDurationMins':10080,'usedPercent':10,'resetsAt':4102444800}}}}
 elif m == 'thread/start':
  result = {'model':'gpt-5.6-luna','reasoningEffort':'high','thread':{'id':'fresh'}}
 else: result = {}
 if m == 'turn/start' and scenario == 'disconnect': sys.exit(0)
 if m == 'account/rateLimits/read' and scenario == 'missing-quota' and sum(json.loads(x).get('method') == m for x in log.read_text().splitlines()) >= 4: result = {'ordinaryUsageAllowed':False}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
 if m == 'turn/start' and scenario == 'tool-limit':
  arguments = {'glob':'**/*','path':'.'}
  print(json.dumps({'method':'item/started','params':{'item':{'type':'dynamicToolCall','id':'list','tool':'file_list','arguments':arguments,'status':'inProgress'}}}),flush=True)
  print(json.dumps({'id':0,'method':'item/tool/call','params':{'callId':'list','threadId':'fresh','turnId':'turn','tool':'file_list','arguments':arguments}}),flush=True)
  continue
 if m == 'turn/start':
  text = 'malformed' if scenario == 'malformed' else json.dumps({'explanation':'SIMULATED provider answer','evidence':[]})
  print(json.dumps({'method':'rawResponseItem/completed','params':{'item':{'type':'message','id':'final','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':text}]}}}),flush=True)
  for i in range(2): print(json.dumps({'method':'rawResponse/completed','params':{'responseId':'one','usage':None}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'turn':{'status':'completed'}}}),flush=True)
'''


class DummyHandlers:
    def __init__(self, source, grepglint, budget, audit, session, **kwargs):
        self.error = None
        self.checks = {'simulated_isolation': True}
        self.cache_identity = os.urandom(8).hex()
        self.child = None
    def start(self, client):
        pass
    def close(self):
        pass


class OutputLimitHandlers(Handlers):
    """Exercise the real handler thread with a bounded-output failure response."""
    def __init__(self, source, grepglint, budget, audit, session, **kwargs):
        self.budget, self.audit, self.session = budget, audit, session
        self.error = None
        self.queued_max = 0
        self.requests = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.send_lock = threading.Lock()
        self.child = SimpleNamespace(send=lambda value: None, close=lambda: None,
            line=lambda: b'{"ok":false,"error":"handler_output_limit_exceeded"}')
        self.thread = None
        self.checks = {'simulated_isolation': True}
        self.cache_identity = 'simulated-cache'


class Adapter(unittest.TestCase):
    def exercise(self, scenario, all_tasks=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, corpus, identity = fixture(root)
            snapshots = root / 'snapshots'
            snapshots.mkdir()
            source.rename(snapshots / 'test')
            source = snapshots / 'test'
            planned = contract.plan(paired.CORPUS, [], True) if all_tasks else tiny_plan()
            planned.update(schema_version=2, simulation=False, execution='chatgpt',
                           implementation_sha256='a' * 64, grepglint_sha256='b' * 64)
            for trial in planned['trials']:
                trial['source'] = identity
            planned['prepared_sources'] = {'test': verify(source, identity, corpus, Budget())}
            run_path = store.create(root / 'artifacts', planned)
            cg = root / 'cgroup'
            cg.mkdir()
            (cg / 'memory.peak').write_text('12345')
            server = root / 'server.py'
            server.write_text(SERVER)
            log = root / 'protocol.jsonl'
            ledger_path = root / 'attempts.jsonl'
            args = SimpleNamespace(worker=run_path, snapshots=snapshots, codex=root / 'codex',
                grepglint=root / 'grepglint', auth=root / 'auth', proof=root / 'proof',
                live_action='readiness', confirm=None, fake_scenario='success', prove=False)
            args.auth.write_text('{}')
            args.auth.chmod(0o600)
            proofs = {'plan_sha256': plan_hash(planned), 'receipt_sha256': 'c' * 64,
                      'provider_evidence': 'https://github.com/alundgren/grepglint/issues/39#issuecomment-5732530859', 'checks': {key: True for key in (
                'native_tools','native_isolation','prompt_and_catalog')}}
            catalog = {'tools':[{'name':'search','use_when':'search','returns':'results','follow_up':'read','side_effects':'index'}]}
            from _codex_audit import encoded
            from codex_preflight import digest
            proofs['sessions'] = {t['trial_id']: {'catalog_sha256': digest(encoded(contract.tools(t['configuration'], catalog))), 'prompt_sha256': t['prompt_sha256']} for t in planned['trials']}
            def native_client(cmd, cwd, env, budget, **kwargs):
                copied = Path(env['CODEX_HOME']) / 'auth.json'
                self.assertFalse(copied.is_symlink())
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                self.assertEqual(copied.read_bytes(), args.auth.read_bytes())
                copied.write_text('{"temporary_refresh":true}')
                self.assertEqual(args.auth.read_text(), '{}')
                return Child([sys.executable, str(server), str(log), scenario], cwd, env, budget, **kwargs)
            with patch('paired.CORPUS', corpus), patch('paired.implementation_hash', return_value='a' * 64), patch('paired.verify_source', verify), \
                 patch('_paired_live.require_proof', return_value=proofs), \
                 patch.object(Run, 'check_identities'), \
                 patch('_paired_live.Attempts', side_effect=lambda: Attempts(ledger_path)), \
                 patch('_paired_session.prerequisites'), \
                 patch('_codex_smoke.children_in_current_cgroup', return_value=True), \
                 patch('_codex_smoke.Handlers', OutputLimitHandlers if scenario == 'tool-limit' else DummyHandlers), \
                 patch('_paired_session.Child', side_effect=native_client):
                ready = live.worker(args, catalog, cg)
                self.assertEqual(ready['status'], 'ready')
                self.assertNotIn('turn/start', log.read_text())
                args.live_action = 'execute'
                args.confirm = ready['authorization']['confirmation']
                result = live.worker(args, catalog, cg)
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                turns = [r for r in rows if r.get('method') == 'turn/start']
                expected = len(planned['trials']) if scenario == 'success' else 1
                self.assertEqual(len(turns), expected)
                self.assertEqual(turns[0]['params']['input'][0]['text'], planned['trials'][0]['question'])
                for r in rows:
                    if r.get('method') == 'thread/start':
                        self.assertTrue(r['params']['ephemeral'])
                        self.assertEqual(r['params']['model'], 'gpt-5.6-luna')
                self.assertEqual(result['status'], 'completed' if scenario == 'success' else 'incomplete')
                store.seal(run_path)
                self.assertEqual(store.validate_run(run_path)['status'], result['status'])
                first = store.read(run_path / 't0001.json')
                self.assertTrue(first['inference_performed'])
                self.assertFalse(first['simulation'])
                if scenario == 'success':
                    self.assertEqual(first['usage']['response_count'], 1)
                    self.assertIsNone(first['usage']['counters']['inputTokens'])
                    self.assertTrue(first['grepglint']['non_use'])
                    from _score_input import live_identity
                    live_identity(run_path / 't0001.jsonl', first, {'question': planned['trials'][0]['question']}, planned, result)
                    changed = copy.deepcopy(first)
                    changed['authorization_sha256'] = '0' * 64
                    with self.assertRaisesRegex(ProbeError, 'authorization'):
                        live_identity(run_path / 't0001.jsonl', changed, {'question': planned['trials'][0]['question']}, planned, result)
                else:
                    self.assertEqual(store.read(run_path / 't0002.json')['state'], 'not-started')
                if scenario == 'tool-limit':
                    self.assertEqual(first['errors'], ['trial_tool_bound_exhausted'])
                    self.assertEqual(result['errors'], first['errors'])
                    self.assertEqual(first['tools'][0]['error'], 'handler_output_limit_exceeded')
                    after = first['quota_observations'][-1]
                    self.assertIsNone(after['weekly'])
                    self.assertEqual(after['error'], 'client_exited')
                ledger = Attempts(ledger_path)
                try:
                    with self.assertRaisesRegex(ProbeError, 'consumed'):
                        ledger.start(run_path.name, ready['authorization']['binding'], args.confirm)
                    self.assertEqual(ledger.runs[run_path.name]['state'], 'stopped')
                finally:
                    ledger.close()

    def test_80_trials_one_confirmation_real_adapter_with_fake_transport(self):
        self.exercise('success', all_tasks=True)

    def test_uncertain_transport_is_consumed_once_and_stops_run(self):
        self.exercise('disconnect')

    def test_missing_posttrial_quota_is_retained_and_stops_partner(self):
        self.exercise('missing-quota')

    def test_invalid_final_answer_stops_partner(self):
        self.exercise('malformed')

    def test_output_limit_survives_client_exit_and_missing_posttrial_quota(self):
        self.exercise('tool-limit')
