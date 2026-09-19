"""Bind offline stock-client capability checks to selected corpus prompts and sources."""
import json
from pathlib import Path

from _codex_audit import encoded
from _codex_capture import ProbeError
from _codex_session import Script, check_script, function, configuration
from _codex_isolation import CLIENT_SHA256, JAVASCRIPT_HOST_SHA256, file_hash
from codex_preflight import digest
from _paired_contract import BASE, LIMITS, tools

PROVIDER_EVIDENCE = 'https://github.com/alundgren/grepglint/issues/39#issuecomment-5732530859'


class CorpusProbe(Script):
    def __init__(self, configuration, audit, source):
        super().__init__(configuration, audit, configuration)
        # Probe calls are independent of the question and reference answers.
        self.steps = self.steps[:10]
        # Keep catalog probes bounded even for a large prepared repository.
        import re
        candidates = [p.name for p in source.iterdir() if re.fullmatch(r'[A-Za-z0-9_.-]+', p.name)
                      and p.is_file() and not p.is_symlink() and 2 <= p.stat().st_size <= 512 * 1024]
        if not candidates:
            raise ProbeError('corpus_probe_regular_root_file_required')
        path = sorted(candidates)[0]
        self.steps = json.loads(json.dumps(self.steps).replace('example.py', path)
                                .replace('*.py', '__grepglint_probe_absent__')
                                .replace('example', '__grepglint_probe_absent__'))
        self.steps += [[function('scoped_regex', 'functions.text_search',
                                {'query': 'a^', 'regex': True, 'path': '.'}),
                       function('invalid_regex', 'functions.text_search',
                                {'query': '[', 'regex': True, 'path': '.'})]]

    def __call__(self, request):
        if self.step >= len(self.steps):
            return [{'type': 'message', 'id': 'proof_final', 'role': 'assistant', 'phase': 'final_answer',
                     'content': [{'type': 'output_text', 'text': json.dumps({
                         'explanation': 'SIMULATED capability proof, not an answer to the question.', 'evidence': []})}]}]
        return super().__call__(request)

    def check(self, audit, session):
        checks = check_script(self, audit, session, check_grepglint=False)
        for call, expected in (('scoped_regex', True), ('invalid_regex', False)):
            checks[call] = audit.dynamic[(session, call)]['handler']['response']['result']['success'] is expected
        if not all(checks.values()):
            raise ProbeError('corpus_capability_probe_failed')
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
            or store.validate_run(run)['status'] != 'completed'):
        raise ProbeError('matching_selected_offline_proof_required')
    corpus = Path(__file__).resolve().parent
    if (planned['manifest_sha256'] != digest((corpus / 'manifest.json').read_bytes())
            or planned['sources_sha256'] != digest((corpus / 'sources.json').read_bytes())):
        raise ProbeError('corpus_identity_changed')
    required = {'orchestrator_skills', 'executor_skills', 'missing_skill', 'hidden_skill',
                'shell', 'patch', 'delegation', 'resource', 'invalid_name', 'absolute_path',
                'traversal', 'symlink', 'history', 'javascript_restrictions', 'nested_catalog',
                'discarded_calls_retained', 'deterministic_input', 'yield_and_wait',
                'scoped_regex', 'invalid_regex'}
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
            'checks': {key: True for key in ('runtime_registrations', 'normalized_configuration',
                       'prompt_and_catalog', 'direct_skill_handlers', 'nested_aliases', 'negative_capabilities')}}
