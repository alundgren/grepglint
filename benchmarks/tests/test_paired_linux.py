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
        return run, [store.read(run / name) for name in ('t0001.json', 't0002.json')]

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
                self.assertTrue(any(t['returned_ranges'] for t in record['tools']))
                self.assertEqual(record['answer']['status'], 'valid')
                self.assertTrue(all(record['session']['isolation_checks'].values()))

    def test_optional_nonuse_and_adversarial_search_schema(self):
        for scenario in ('non-use', 'adversarial'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _, records = self.run_scenario(directory, scenario, 0)
                for record in records:
                    if scenario == 'non-use':
                        self.assertTrue(record['grepglint']['non_use'])
                    else:
                        denied = [t for t in record['tools'] if t['call_id'].startswith('denied_')]
                        self.assertEqual(len(denied), 5)
                        self.assertTrue(all(not t['success'] for t in denied))

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
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()
