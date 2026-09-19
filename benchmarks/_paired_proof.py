"""Bind offline stock-client capability checks to selected corpus prompts and sources."""
import json
from pathlib import Path

from _codex_audit import encoded
from _codex_capture import ProbeError
from _codex_session import function, javascript, output_text
from _paired_session import configuration
from _codex_isolation import CLIENT_SHA256, JAVASCRIPT_HOST_SHA256, file_hash
from codex_preflight import digest
from _paired_contract import BASE, LIMITS, tools

PROVIDER_EVIDENCE = 'https://github.com/alundgren/grepglint/issues/39#issuecomment-5732530859'


class CorpusProbe:
    def __init__(self, configuration, audit, source):
        self.configuration, self.audit, self.source = configuration, audit, source
        self.step = 0
        self.expected = []

    def __call__(self, request):
        import shlex
        step = self.step
        self.step += 1
        if step == 0:
            items = [function('recursive_listing', 'functions.exec_command', {'cmd': 'rg --files'})]
        elif step == 1:
            items = [function('large_output', 'functions.exec_command', {
                'cmd': "python3 -c \"print('ordinary output line\\\\n' * 10000)\""})]
        elif step == 2:
            program = """import errno, json, os, pathlib, socket
checks = {}
for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
    try:
        pathlib.Path(OUTSIDE, name).read_bytes()
        checks[name] = False
    except (OSError, PermissionError):
        checks[name] = True
try:
    pathlib.Path(AUTH).read_bytes()
    checks['auth'] = False
except OSError:
    checks['auth'] = True
p = pathlib.Path('.grepglint-native-write-probe')
try:
    with p.open('x') as stream: stream.write('probe')
    p.unlink()
    checks['source_read_only'] = False
except OSError:
    checks['source_read_only'] = True
try:
    sock = socket.socket()
    sock.settimeout(0.2)
    sock.connect(('198.51.100.1', 9))
    checks['network'] = False
except OSError as error:
    checks['network'] = error.errno in (errno.EPERM, errno.EACCES, errno.ENETUNREACH)
print(json.dumps(checks))
""".replace('OUTSIDE', repr(str(self.temporary / 'outside'))).replace('AUTH', repr(str(Path.home() / '.codex/auth.json')))
            items = [function('isolation', 'functions.exec_command', {'cmd': 'python3 -c ' + shlex.quote(program)})]
        elif step == 3:
            items = [javascript('nested_native', "await tools.exec_command({cmd: \"python3 -c 'print(6 * 7)'\"}); text('discarded-command-complete');")]
        elif step == 4:
            items = [function('yielded_command', 'functions.exec_command', {'cmd': 'sleep 0.4; echo native-yield-complete', 'yield_time_ms': 1})]
        elif step == 5:
            import re
            # The next HTTP request can arrive before the controller drains its
            # raw-event pipe. Use the result actually delivered to the model.
            result = next(item['output'] for item in request['input']
                          if item.get('call_id') == 'yielded_command' and 'output' in item)
            match = re.search(r'Process running with session ID (\d+)', result)
            if not match:
                raise ProbeError('native_yield_not_observed')
            items = [function('waited_command', 'functions.write_stdin', {'session_id': int(match[1]), 'chars': '', 'yield_time_ms': 1000})]
        elif step == 6:
            items = [function('orchestrator_skills', 'skills.list', {'authority': {'kind': 'orchestrator'}}),
                     function('executor_skills', 'skills.list', {'authority': {'kind': 'executor'}})]
        elif step == 7:
            items = [function('input_request', 'functions.request_user_input', {'questions': [{
                'id': 'hint', 'header': 'Help', 'question': 'Give a hint.', 'options': [
                    {'label': 'Yes', 'description': 'A hint.'}, {'label': 'No', 'description': 'No hint.'}]}]})]
        elif step == 8:
            items = [function('background_command', 'functions.exec_command', {'cmd': 'sleep 30', 'yield_time_ms': 1})]
        else:
            return [{'type': 'message', 'id': 'proof_final', 'role': 'assistant', 'phase': 'final_answer',
                     'content': [{'type': 'output_text', 'text': json.dumps({
                         'explanation': 'SIMULATED native capability proof, not an answer to the question.', 'evidence': []})}]}]
        self.expected.extend(i['call_id'] for i in items)
        return items

    def check(self, audit, session):
        from _codex_capture import json_value
        from _codex_session import NO_ASSISTANCE
        isolation = output_text(audit, session, 'isolation').split('Output:\n')[-1].strip()
        checks = {'native_listing': 'Process exited with code 0' in output_text(audit, session, 'recursive_listing'),
            'native_truncation_continues': 'truncated' in output_text(audit, session, 'large_output'),
            'native_isolation': json_value(isolation) == {key: True for key in (
                'source', 'oracle', 'credentials', 'history', 'controller', 'auth', 'source_read_only', 'network')},
            'native_nested_execution': any(k[0] == session and (v.get('completion', {}).get('aggregatedOutput') or '').strip() == '42'
                                           for k, v in audit.native_items.items()),
            'native_yield_and_wait': 'native-yield-complete' in output_text(audit, session, 'waited_command'),
            'native_background_cleanup': 'completion' in audit.native_items.get((session, 'background_command'), {}),
            'empty_skills': all('unsupported call' in output_text(audit, session, name)
                                for name in ('orchestrator_skills', 'executor_skills')),
            'deterministic_input': NO_ASSISTANCE in output_text(audit, session, 'input_request')}
        if not all(checks.values()):
            raise ProbeError('native_probe_failed_' + next(k for k,v in checks.items() if not v))
        return checks


def stable_plan(planned):
    """Exclude only measured preparation time and explicit execution provenance."""
    result = json.loads(encoded(planned))
    for key in ('simulation', 'schema_version', 'execution', 'inference_performed'):
        result.pop(key, None)
    for value in result.get('prepared_sources', {}).values():
        value.pop('seconds', None)
    return result


def plan_hash(planned):
    return digest(encoded(stable_plan(planned)))


def require_proof(run, binary, grepglint, implementation):
    import _paired_store as store
    store.owned(run, sealed=True)
    planned = store.read(run / 'plan.json')
    if (planned.get('execution') != 'proof' or planned['implementation_sha256'] != implementation
            or planned['limits'] != LIMITS or planned['answer_instructions'] != BASE
            or planned['client_sha256'] != CLIENT_SHA256 or file_hash(binary) != CLIENT_SHA256
            or file_hash(binary.parent / 'codex-code-mode-host') != JAVASCRIPT_HOST_SHA256
            or planned['grepglint_sha256'] != file_hash(grepglint)
            or store.read(run / 'run.json').get('cleanup', {}).get('temporary_files_removed') is not True
            or store.validate_run(run)['status'] != 'completed'):
        raise ProbeError('matching_selected_offline_proof_required')
    corpus = Path(__file__).resolve().parent
    if (planned['manifest_sha256'] != digest((corpus / 'manifest.json').read_bytes())
            or planned['sources_sha256'] != digest((corpus / 'sources.json').read_bytes())):
        raise ProbeError('corpus_identity_changed')
    required = {'native_listing', 'native_truncation_continues', 'native_isolation',
                'native_nested_execution', 'native_yield_and_wait', 'native_background_cleanup',
                'empty_skills', 'deterministic_input'}
    sessions = {}
    for trial in planned['trials']:
        record = store.read(run / (trial['trial_id'] + '.json'))
        session = record['session']
        checks = session.get('negative_checks', {})
        if (not all(checks.get(key) is True for key in required)
                or session['configuration_sha256'] != digest(encoded(configuration('<loopback>')))
                or not session.get('observed', {}).get('runtime_registry')
                or session['observed'].get('errors') or not all(session['isolation_checks'].values())
                or record['source_before']['sha256'] != planned['prepared_sources'][trial['source']['id']]['sha256']):
            raise ProbeError('selected_capability_proof_incomplete')
        sessions[trial['trial_id']] = {key: record[key] for key in ('catalog_sha256', 'prompt_sha256')}
    return {'plan_sha256': plan_hash(planned),
            'receipt_sha256': store.hash_file(run / 'ownership.json', 1024 * 1024),
            'javascript_host_sha256': JAVASCRIPT_HOST_SHA256,
            'provider_evidence': PROVIDER_EVIDENCE, 'sessions': sessions,
            'checks': {key: True for key in ('native_tools', 'native_isolation', 'native_temporary_cleanup', 'prompt_and_catalog')}}
