import json
import os
from pathlib import Path
import socket
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _codex_capture import Budget, Child, FRAME_LIMIT, ProbeError, ResponsesStub, json_value
from codex_preflight import (BASE, EFFORT, MARKERS, MODEL, child_environment, dynamic_tools,
                             exclusive_probe, inspect_request, run, tool_catalog, write_receipt)


def request(tools=None):
    tools = dynamic_tools('control') if tools is None else tools
    registry = {'functions': {'functions': {t['name']: {'name': t['name'], 'direct': True,
                'code_mode_name': None, 'deferred': False} for t in tools}}}
    return {'model': MODEL, 'reasoning': {'effort': EFFORT}, 'tools': tools,
            'instructions': BASE, 'input': [], 'client_metadata': {
                'x-codex-turn-metadata': json.dumps({'tool_namespaces_info': registry})}}


class CatalogTests(unittest.TestCase):
    def test_flat_namespace_and_input_additional_tools(self):
        data = request([])
        data['input'] = [{'type': 'tool_definitions', 'additional_tools': [
            {'type': 'namespace', 'name': 'nested', 'tools': [
                {'type': 'function', 'name': 'read_resource'},
                {'type': 'function', 'function': {'name': 'deferred_read'}}]}]}]
        result = tool_catalog(data)
        self.assertEqual(result['names'], ['nested.deferred_read', 'nested.read_resource'])
        self.assertTrue(any('additional_tools' in c['location'] for c in result['catalogs']))

    def test_empty_catalog_is_different_from_missing(self):
        data = request([])
        self.assertNotIn('missing_tool_catalog', tool_catalog(data)['errors'])
        self.assertEqual(tool_catalog(data)['names'], [])
        del data['tools']
        self.assertIn('missing_tool_catalog', tool_catalog(data)['errors'])
        data['tools'] = None
        self.assertIn('invalid_tool_catalog', tool_catalog(data)['errors'])

    def test_code_mode_uses_declarations_not_examples(self):
        data = request([])
        data['tools'] = [{'type': 'namespace', 'name': 'functions', 'tools': [
            {'name': 'exec', 'type': 'custom', 'description':
             'Example tools.exec_command()\n### `read_file`\n'
             'declare const tools: { read_file(args: {}): Promise<unknown>; };\n'}]}]
        self.assertEqual(tool_catalog(data)['names'], ['functions.exec', 'read_file'])
        data['tools'][0]['tools'][0]['description'] += '\n### `unparsed_tool`\n'
        self.assertIn('unparsed_code_mode_catalog', tool_catalog(data)['errors'])

    def test_registry_retains_tools_missing_from_visible_catalog(self):
        data = request([])
        data['client_metadata']['x-codex-turn-metadata'] = json.dumps({'tool_namespaces_info': {
            'skills': {'functions': {'read': {'name': 'read', 'direct': False,
                'code_mode_name': 'skills__read', 'deferred': True}}}}})
        catalog = tool_catalog(data)
        self.assertEqual(catalog['declared_names'], [])
        self.assertEqual(catalog['names'], ['skills__read'])
        self.assertTrue(catalog['runtime_registry'][0]['deferred'])
        data['client_metadata'] = {}
        self.assertIn('missing_runtime_tool_registry', tool_catalog(data)['errors'])

    def test_schema_tool_property_is_not_a_catalog(self):
        data = request()
        data['tools'][0]['inputSchema']['properties']['tools'] = {'type': 'array'}
        self.assertEqual(tool_catalog(data)['errors'], [])

    def test_content_hook_and_tool_contamination(self):
        data = request()
        clean = inspect_request(data, 'control', False)
        self.assertEqual(clean['request_status'], 'supported')
        self.assertFalse(any(clean['sentinels'].values()))
        data['input'] = [{'role': 'developer', 'content': [{'text': MARKERS['global_instruction']}]}]
        data['tools'].append({'type': 'function', 'name': 'outside_source_read'})
        result = inspect_request(data, 'control', True)
        self.assertEqual(result['request_status'], 'unsupported')
        self.assertEqual(result['blockers'], ['additional_callable_tools', 'fixture_content_in_request',
                                             'fixture_hook_executed'])
        self.assertIn('outside_source_read', result['additional_tools'])

    def test_foreign_namespace_cannot_impersonate_controlled_tool(self):
        data = request()
        data['tools'].append({'type': 'namespace', 'name': 'outside',
                              'tools': [{'type': 'function', 'name': 'read_file'}]})
        self.assertIn('outside.read_file', inspect_request(data, 'control', False)['additional_tools'])

    def test_missing_model_effort_or_controlled_tools_is_not_supported(self):
        data = request()
        data['reasoning'] = {}
        self.assertIn('model_or_effort_mismatch', inspect_request(data, 'control', False)['blockers'])
        data = request([])
        self.assertEqual(inspect_request(data, 'control', False)['request_status'], 'incomplete')

    def test_instructions_are_hashed_and_not_exported(self):
        data = request()
        private_text = 'PRIVATE_INSTRUCTION_DO_NOT_EXPORT'
        data['instructions'] = private_text
        data['input'] = [{'role': 'developer', 'content': [{'text': private_text}]}]
        data['authorization'] = 'PRIVATE_CREDENTIAL_DO_NOT_EXPORT'
        result = inspect_request(data, 'control', False)
        encoded = json.dumps(result)
        self.assertNotIn(private_text, encoded)
        self.assertNotIn('PRIVATE_CREDENTIAL', encoded)
        self.assertEqual(len(result['instruction_blocks'][0]['sha256']), 64)

    def test_configurations_differ_only_by_grepglint(self):
        control = dynamic_tools('control')
        treatment = dynamic_tools('grepglint')
        self.assertEqual(treatment[:-1], control)
        self.assertEqual(treatment[-1]['name'], 'grepglint_search')
        query = treatment[-1]['inputSchema']['properties']['query']
        self.assertIn('Related words or identifiers', query['description'])
        self.assertIn('no regex or FTS operators', query['description'])
        self.assertIn('migration dependency graph', query['description'])

    def test_duplicate_json_and_non_finite_values_rejected(self):
        for value in ['{"tools":[],"tools":[1]}', '{"size":NaN}', '[[[']:
            with self.subTest(value=value), self.assertRaises(ProbeError):
                json_value(value)


class TransportTests(unittest.TestCase):
    def child(self, code, root, seconds=2, total=16 * FRAME_LIMIT):
        return Child([sys.executable, '-c', code], root, {'PATH': '/usr/bin:/bin'}, Budget(seconds, total))

    def test_process_failure_has_sanitized_error_and_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            child = self.child('import sys;sys.stderr.write("PRIVATE_DIAGNOSTIC");sys.exit(7)', root)
            try:
                with self.assertRaisesRegex(ProbeError, '^client_exited$'):
                    child.line()
                child.proc.wait(timeout=1)
            finally:
                child.close()
            self.assertEqual(child.proc.returncode, 7)
            self.assertGreater(child.counts['stderr'], 0)

    def test_timeout_terminates_child(self):
        with tempfile.TemporaryDirectory() as root:
            child = self.child('import time;time.sleep(10)', root, seconds=0.1)
            start = time.monotonic()
            try:
                with self.assertRaisesRegex(ProbeError, 'deadline_exceeded'):
                    child.line()
            finally:
                child.close()
            self.assertLess(time.monotonic() - start, 2)
            self.assertIsNotNone(child.proc.returncode)

    def test_oversized_rpc_frame_and_total_output(self):
        cases = [('import sys;sys.stdout.write("x"*1100000);sys.stdout.flush()', 16 * FRAME_LIMIT,
                  'rpc_frame_limit_exceeded'),
                 ('import sys;sys.stdout.write("{}\\n"*20000);sys.stdout.flush()', 1000,
                  'capture_limit_exceeded')]
        for code, total, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as root:
                child = self.child(code, root, total=total)
                try:
                    with self.assertRaisesRegex(ProbeError, error):
                        while True:
                            child.line()
                finally:
                    child.close()

    def test_stderr_is_streamed_and_capped(self):
        with tempfile.TemporaryDirectory() as root:
            child = self.child('import sys;sys.stderr.write("x"*1100000);sys.stderr.flush()', root)
            try:
                with self.assertRaisesRegex(ProbeError, 'stderr_limit_exceeded'):
                    child.line()
            finally:
                child.close()

    def test_owned_descendant_killed_even_when_leader_exits(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'survived'
            code = ('import os,time;pid=os.fork();'
                    '\nif pid: print(pid,flush=True);os._exit(0)'
                    '\ntime.sleep(0.5)\nopen(' + repr(str(output)) + ',"w").write("bad")\n')
            child = self.child(code, root)
            child.line()
            child.proc.wait(timeout=1)
            child.close()
            time.sleep(0.6)
            self.assertFalse(output.exists())

    def http(self, wire):
        stub = ResponsesStub(Budget(seconds=2))
        stub.start()
        address = stub.listener.getsockname()
        try:
            with socket.create_connection(address, timeout=1) as conn:
                conn.sendall(wire)
                conn.shutdown(socket.SHUT_WR)
                try:
                    while conn.recv(4096):
                        pass
                except ConnectionResetError:
                    pass
            stub.thread.join(timeout=1)
            return stub
        finally:
            stub.close()

    def test_stub_rejects_oversized_request_before_reading_body(self):
        stub = self.http(b'POST /v1/responses HTTP/1.1\r\nContent-Length: 1048577\r\n\r\n')
        self.assertEqual(stub.error, 'http_frame_limit_exceeded')
        self.assertIsNone(stub.request)

    def test_stub_rejects_credentials_without_retaining_them(self):
        stub = self.http(b'POST /v1/responses HTTP/1.1\r\nContent-Length: 2\r\n'
                         b'Authorization: Bearer PRIVATE_TOKEN\r\n\r\n{}')
        self.assertEqual(stub.error, 'credentials_in_local_request')
        self.assertTrue(stub.credentials_present)
        self.assertIsNone(stub.request)

    def test_stub_returns_deterministic_no_inference_response(self):
        body = json.dumps(request()).encode()
        stub = self.http(b'POST /v1/responses HTTP/1.1\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
        self.assertIsNone(stub.error)
        self.assertEqual(stub.request, request())

    def test_stop_before_connection_cleans_listener(self):
        stub = ResponsesStub(Budget(seconds=2))
        stub.start()
        stub.close()
        self.assertFalse(stub.thread.is_alive())


class ReceiptTests(unittest.TestCase):
    def test_private_receipt_preserves_existing_files_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'receipt.json'
            write_receipt(path, {'schema_version': 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_receipt(path, {})
            target = root / 'target'
            target.write_text('preserve')
            link = root / 'link'
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                write_receipt(link, {})
            self.assertEqual(target.read_text(), 'preserve')

    def test_public_receipt_directory_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            with self.assertRaisesRegex(ProbeError, 'directory_must_be_private'):
                write_receipt(root / 'receipt.json', {})

    def test_one_probe_at_a_time(self):
        with exclusive_probe():
            with self.assertRaisesRegex(ProbeError, 'another_preflight_is_running'):
                with exclusive_probe():
                    pass

    def test_different_tmpdirs_share_the_account_lock(self):
        with tempfile.TemporaryDirectory() as temporary, exclusive_probe():
            env = dict(os.environ, TMPDIR=temporary)
            program = ('import sys;sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1]))
                       + ');from codex_preflight import exclusive_probe\nwith exclusive_probe(): pass\n')
            result = subprocess.run([sys.executable, '-c', program], env=env, capture_output=True,
                                    text=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('another_preflight_is_running', result.stderr)

    @unittest.skipUnless(sys.platform == 'linux', 'real client probe is Linux only')
    def test_sigterm_cleans_client_descendants_and_fixtures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'started.json'
            survived = root / 'survived'
            fake = root / 'codex'
            fake.write_text('#!' + sys.executable + '\n'
                'import json,os,subprocess,sys,time\n'
                'if "--version" in sys.argv: print("codex-cli 0.154.0");sys.exit()\n'
                'child=subprocess.Popen([sys.executable,"-c",'
                + repr('import time;time.sleep(2);open(' + repr(str(survived)) + ',"w").write("bad")') + '])\n'
                'open(' + repr(str(marker)) + ',"w").write(json.dumps({"pid":os.getpid(),'
                '"descendant":child.pid,"source":os.getcwd()}))\n'
                'time.sleep(30)\n')
            fake.chmod(0o700)
            command = Path(__file__).resolve().parents[1] / 'codex_preflight.py'
            controller = subprocess.Popen([sys.executable, str(command), '--codex', str(fake),
                '--receipt', str(root / 'receipt.json')], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
            info = None
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), 'Fixture client did not start')
                info = json.loads(marker.read_text())
                controller.send_signal(signal.SIGTERM)
                stdout, stderr = controller.communicate(timeout=4)
                self.assertEqual(controller.returncode, 3, stderr.decode())
                self.assertEqual(json.loads(stdout)['status'], 'incomplete')
                receipt = json.loads((root / 'receipt.json').read_text())
                self.assertEqual(receipt['errors'], ['cancelled'])
                self.assertEqual(receipt['probes'], [])
                self.assertFalse(Path(info['source']).parents[1].exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(info['pid'], 0)
                # Orphaned killed descendants can briefly remain as zombies.
                proc_stat = Path('/proc') / str(info['descendant']) / 'stat'
                if proc_stat.exists():
                    self.assertEqual(proc_stat.read_text().split(') ', 1)[1].split()[0], 'Z')
                time.sleep(2.1)
                self.assertFalse(survived.exists())
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.communicate(timeout=2)
                if info is not None:
                    try:
                        os.killpg(info['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_child_environment_excludes_credentials_and_proxies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = child_environment(root, root / 'codex')
            self.assertFalse(any('KEY' in k or 'TOKEN' in k or 'PROXY' in k for k in env))
            self.assertEqual(env['CODEX_HOME'], str(root / 'codex'))

    @unittest.skipUnless(sys.platform == 'linux', 'real client probe is Linux only')
    def test_missing_client_returns_incomplete_receipt(self):
        result = run(Path('/missing-preflight-client'))
        self.assertEqual(result['status'], 'incomplete')
        self.assertFalse(result['inference_performed'])
        self.assertEqual(result['probes'], [])

    @unittest.skipUnless(sys.platform == 'linux', 'real client probe is Linux only')
    def test_client_start_failure_is_sanitized(self):
        with patch('_codex_capture.subprocess.Popen',
                   side_effect=subprocess.SubprocessError('PRIVATE_START_FAILURE')):
            result = run(Path(sys.executable))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['errors'], ['client_or_fixture_unavailable'])
        self.assertNotIn('PRIVATE_START_FAILURE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
