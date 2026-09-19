"""Opt-in corpus trials using a real pinned client and local fake Responses only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _paired_store as store

ROOT = Path(__file__).resolve().parents[2]
SOURCES = os.environ.get('GREPGLINT_PAIRED_SNAPSHOTS')


@unittest.skipUnless(os.environ.get('GREPGLINT_CODEX_INTEGRATION') == '1' and SOURCES,
                     'opt-in Linux check requires separately prepared Django source')
class PairedLinux(unittest.TestCase):
    def command(self, root, scenario='success'):
        return [sys.executable, str(ROOT / 'benchmarks/paired.py'), '--task', 'ccx-crossorg-217',
                '--fake', '--fake-scenario', scenario, '--snapshots', SOURCES, '--artifacts', str(root)]

    def run_scenario(self, directory, scenario, expected):
        result = subprocess.run(self.command(Path(directory), scenario), capture_output=True, timeout=55)
        self.assertEqual(result.returncode, expected, result.stdout.decode() + result.stderr.decode())
        status = json.loads(result.stdout)
        run = Path(status['run'])
        store.owned(run, sealed=True)
        self.assertTrue(store.read(run / 'run.json')['cleanup']['temporary_files_removed'])
        return run, [store.read(run / name) for name in ('t0001.json', 't0002.json')]

    def test_native_declaration_retains_catalog_guidance(self):
        from _paired_contract import tools
        result = subprocess.run([str(ROOT / 'target/release/grepglint'), 'tools', '--json'],
                                capture_output=True, check=True)
        catalog = json.loads(result.stdout)
        self.assertEqual(tools('control', catalog), [])
        declared, = tools('grepglint', catalog)
        description = declared['description']
        for phrase in ('related words or identifiers', 'too many matches',
                       'migration dependency graph', 'request middleware exception',
                       'lexical suggestions', 'not exhaustive references or guaranteed answers',
                       'Read the relevant regions', 'all occurrences',
                       'directly read a file', 'may take several seconds',
                       'If indexing fails, use rg and file reads',
                       'repeating the same query will not fix a capacity failure',
                       'Does not edit the repository or use the network'):
            self.assertIn(phrase, description)
        schema = declared['inputSchema']
        self.assertEqual(set(schema['properties']), {'query'})
        self.assertEqual(schema['properties']['query']['description'],
                         catalog['tools'][0]['inputs']['query'])

    def test_selected_capability_proof_uses_real_client_without_account(self):
        self.check_selected_proof('description-only')

    def test_guided_capability_proof_preserves_pair_checks(self):
        self.check_selected_proof('prefer-search-v1')

    def test_skill_capability_proof_checks_discovery_read_and_isolation(self):
        self.check_selected_proof('skill-v1')

    def check_selected_proof(self, guidance):
        from _paired_proof import require_proof
        from _paired_contract import trial_instructions
        from paired import implementation_hash
        with tempfile.TemporaryDirectory() as directory:
            command = self.command(Path(directory))
            command[command.index('--fake')] = '--prove'
            command += ['--guidance', guidance]
            result = subprocess.run(command, capture_output=True, timeout=65)
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
            run = Path(json.loads(result.stdout)['run'])
            proof = require_proof(run, Path(__import__('shutil').which('codex')).resolve(),
                                  ROOT / 'target/release/grepglint', implementation_hash())
            self.assertEqual(set(proof['sessions']), {'t0001', 't0002'})
            from _score_input import Corpus, records
            scored = records([run], Corpus(ROOT / 'benchmarks'))
            self.assertEqual(len(scored), 2)
            self.assertEqual(scored[0]['_effective_instructions'], scored[1]['_effective_instructions'])
            for record in scored:
                self.assertTrue(all(record['session']['negative_checks'].values()))
                self.assertEqual(record['session']['normalized_configuration']['developer_instructions'], trial_instructions(record))
                if guidance == 'skill-v1' and record['configuration'] == 'grepglint':
                    from _paired_skill import SKILL_CATALOG_SHA256, skill_hash
                    self.assertTrue(record['session']['skill_full_read_observed'])
                    self.assertEqual(record['session']['skill']['catalog_sha256'], SKILL_CATALOG_SHA256)
                    self.assertEqual(record['session']['skill']['contents_sha256'], skill_hash())
                else:
                    self.assertNotIn('skill', record['session'])
            for trial in ('t0001', 't0002'):
                raw = (run / (trial + '.jsonl')).read_text()
                self.assertNotIn('account/read', raw)
                self.assertNotIn('account/rateLimits/read', raw)

    def test_default_index_failure_fallback_and_real_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            run, records = self.run_scenario(directory, 'success', 0)
            self.assertEqual(store.validate_run(run)['status'], 'completed')
            treatment = next(r for r in records if r['configuration'] == 'grepglint')
            self.assertTrue(treatment['grepglint']['errors'])
            self.assertEqual(treatment['grepglint']['fallback_calls'], ['list', 'read', 'search'])
            self.assertIn('database or disk is full', (run / treatment['audit']['path']).read_text())
            self.assertEqual(len({r['session']['cache_identity'] for r in records}), 2)
            for record in records:
                self.assertTrue(record['simulation'])
                self.assertEqual(record['source_before']['sha256'], record['source_after']['sha256'])
                self.assertTrue(any(t['name'] == 'exec_command' for t in record['tools']))
                self.assertTrue(all(t['returned_ranges'] == [] for t in record['tools'] if t['name'] == 'exec_command'))
                self.assertEqual(record['answer']['status'], 'valid')
                self.assertTrue(all(record['session']['isolation_checks'].values()))

    def test_optional_nonuse(self):
        with tempfile.TemporaryDirectory() as directory:
            _, records = self.run_scenario(directory, 'non-use', 0)
            for record in records:
                self.assertTrue(record['grepglint']['non_use'])

    def test_guidance_allows_nonuse(self):
        with tempfile.TemporaryDirectory() as directory:
            command = self.command(Path(directory), 'non-use') + ['--guidance', 'prefer-search-v1']
            result = subprocess.run(command, capture_output=True, timeout=65)
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
            run = Path(json.loads(result.stdout)['run'])
            for name in ('t0001.json', 't0002.json'):
                record = store.read(run / name)
                self.assertEqual(record['state'], 'completed')
                self.assertTrue(record['grepglint']['non_use'])

    def test_invalid_answer_and_transport_loss_stop_remaining_trial(self):
        for scenario in ('malformed', 'missing', 'oversized', 'transport-loss'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                run, records = self.run_scenario(directory, scenario, 3)
                self.assertEqual(records[0]['state'], 'failed')
                self.assertEqual(records[1]['state'], 'not-started')
                self.assertEqual(store.validate_run(run)['status'], 'incomplete')
                if scenario != 'transport-loss':
                    self.assertEqual(records[0]['answer']['status'], scenario)

    def test_cancellation_preserves_partial_run(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(self.command(Path(directory)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    runs = list(Path(directory).glob('run-*'))
                    if runs and (runs[0] / 't0001.jsonl').stat().st_size:
                        break
                    if process.poll() is not None:
                        self.fail('runner stopped before cancellation')
                    time.sleep(0.02)
                else:
                    self.fail('runner did not begin a trial')
                process.send_signal(signal.SIGTERM)
                out, err = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 3, out.decode() + err.decode())
                run = Path(json.loads(out)['run'])
                self.assertEqual(store.validate_run(run)['status'], 'incomplete')
                records = [store.read(run / name) for name in ('t0001.json', 't0002.json')]
                self.assertEqual([r['state'] for r in records], ['failed', 'not-started'])
                self.assertTrue(store.read(run / 'run.json')['cleanup']['service_stopped'])
                self.assertTrue(store.read(run / 'run.json')['cleanup']['temporary_files_removed'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()

    def test_abrupt_worker_exit_removes_temporary_files(self):
        from _codex_isolation import service_command
        import secrets
        unit = 'grepglint-paired-' + secrets.token_hex(12)
        temporary = Path('/run/user') / str(os.getuid()) / unit
        program = ("import os; from pathlib import Path; p=Path(os.environ['RUNTIME_DIRECTORY']); "
                   "(p/'probe').write_text('owned probe'); print(p.exists(), flush=True); os.kill(os.getpid(),9)")
        command = service_command(unit, [sys.executable, '-c', program], seconds=15, runtime=unit)
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b'True', result.stderr.decode())
        self.assertFalse(temporary.exists())
