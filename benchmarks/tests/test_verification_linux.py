"""Opt-in real-client tests. All model responses come from the local stub."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'benchmarks'))
from _codex_artifacts import owned


@unittest.skipUnless(os.environ.get('GREPGLINT_CODEX_INTEGRATION') == '1', 'opt-in Linux pinned-client check')
class LinuxVerificationTests(unittest.TestCase):
    def command(self, root):
        return [sys.executable, str(ROOT / 'benchmarks/codex_preflight.py'), 'verify',
                '--codex', str(Path(shutil.which('codex')).resolve()),
                '--grepglint', str(ROOT / 'target/release/grepglint'), '--artifacts', str(root)]

    def test_real_isolation_tools_and_complete_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(self.command(Path('verification')), cwd=directory,
                                    capture_output=True, timeout=65)
            self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
            status = json.loads(result.stdout)
            self.assertTrue(Path(status['receipt']).is_absolute())
            receipt = json.loads(Path(status['receipt']).read_text())
            self.assertEqual(receipt['status'], 'passed')
            self.assertEqual(receipt['provider_verification']['status'], 'pending')
            self.assertTrue(receipt['cleanup']['service_stopped'])
            self.assertEqual(len(receipt['sessions']), 2)
            self.assertEqual(receipt['audit']['calls'], 57)
            for session in receipt['sessions']:
                self.assertTrue(all(session['negative_checks'].values()))
                self.assertTrue(all(session['isolation_checks'].values()))
                self.assertTrue(session['source_unchanged'])
            owned(Path(status['receipt']).parent, sealed=True)

    def test_sigterm_retains_partial_audit_and_stops_owned_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen(self.command(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                run = None
                while time.monotonic() < deadline:
                    runs = list(root.glob('run-*'))
                    if runs and (runs[0] / 'audit.jsonl').stat().st_size:
                        run = runs[0]
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(run)
                process.send_signal(signal.SIGTERM)
                output, errors = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 3, output.decode() + errors.decode())
                receipt = json.loads((run / 'receipt.json').read_text())
                self.assertEqual(receipt['status'], 'incomplete')
                self.assertIn('cancelled', receipt['errors'])
                self.assertTrue(receipt['cleanup']['service_stopped'])
                self.assertGreater((run / 'audit.jsonl').stat().st_size, 0)
                active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', receipt['runtime_unit']],
                                        capture_output=True, timeout=2)
                self.assertIn(active.returncode, (3, 4))
                owned(run, sealed=True)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
