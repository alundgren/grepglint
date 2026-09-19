"""Bounded offline inputs tied to frozen corpus and audited runner records."""
import copy
import hashlib
import os
from pathlib import Path
import stat

from _codex_audit import encoded
from _codex_capture import ProbeError, json_value
from _paired_contract import (BASE, LEGACY_BASE, TOOL_ENVIRONMENT, CONTRACT, MAX_TRIALS, initial_record,
                              NATIVE_FRAME_BYTES as FRAME_LIMIT, CAPTURE_BYTES as TOTAL_LIMIT)
import _paired_store as store
from _score_metrics import require, valid_range
from validate import load, validate, read_bytes

MAX_BYTES = 32 * 1024 ** 2


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'unsafe_or_oversized_score_input')
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'score_input_limit_exceeded')
    return json_value(data)


class Corpus:
    def __init__(self, root):
        validate(root)
        self.root = root
        self.manifest = load(root, 'manifest.json')
        self.tasks = {t['id']: t for t in self.manifest['tasks']}
        self.sources = {s['id']: s for s in load(root, 'sources.json')}
        self.manifest_hash = hashlib.sha256(read_bytes(root, 'manifest.json', MAX_BYTES)).hexdigest()
        self.sources_hash = hashlib.sha256(read_bytes(root, 'sources.json', MAX_BYTES)).hexdigest()
        self.inventories = {name: load(root, 'coverage/' + name + '.json') for name in self.sources}
        self.files = {name: {f['path']: f for f in inventory['files']} for name, inventory in self.inventories.items()}

    def rubric(self, correction):
        tasks = copy.deepcopy(self.tasks)
        if correction is None:
            correction = {'schema_version': 1, 'version': 1, 'manifest_sha256': self.manifest_hash, 'amendments': []}
        require(isinstance(correction, dict) and set(correction) == {'schema_version', 'version', 'manifest_sha256', 'amendments'}, 'invalid_oracle_correction')
        require(correction['schema_version'] == 1 and correction['manifest_sha256'] == self.manifest_hash, 'correction_manifest_mismatch')
        require(type(correction['version']) is int and correction['version'] >= 1, 'invalid_oracle_version')
        require(isinstance(correction['amendments'], list) and len(correction['amendments']) <= 100, 'too_many_oracle_amendments')
        require(not correction['amendments'] or correction['version'] >= 2, 'correction_requires_new_version')
        for change in correction['amendments']:
            require(isinstance(change, dict) and set(change) == {'task_id', 'reason', 'source_citation', 'add_alternatives', 'claim_texts'}, 'invalid_oracle_amendment')
            require(change['task_id'] in tasks, 'unknown_corrected_task')
            task = tasks[change['task_id']]
            require(isinstance(change['reason'], str) and 0 < len(change['reason'].strip()) <= 4000, 'correction_requires_reason')
            require(valid_range(change['source_citation'], self.files[task['source']]), 'invalid_correction_citation')
            groups = {g['id']: g for g in task['evidence_groups']}
            require(isinstance(change['add_alternatives'], list) and len(change['add_alternatives']) <= 100, 'invalid_corrected_alternatives')
            for alternative in change['add_alternatives']:
                require(isinstance(alternative, dict) and set(alternative) == {'group_id', 'path', 'start', 'end'}, 'invalid_corrected_alternative')
                require(alternative['group_id'] in groups and valid_range(alternative, self.files[task['source']]), 'invalid_corrected_alternative')
                groups[alternative['group_id']]['alternatives'].append({k: alternative[k] for k in ('path', 'start', 'end')})
            texts = change['claim_texts']
            require(isinstance(texts, dict) and set(texts) <= {c['id'] for c in task['claims']}, 'invalid_corrected_claims')
            for claim in task['claims']:
                if claim['id'] in texts:
                    value = texts[claim['id']]
                    require(isinstance(value, str) and 0 < len(value.strip()) <= 4000, 'invalid_corrected_claim_text')
                    claim['text'] = value
        return tasks, correction


def request_tools(value):
    catalogs = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('tools', 'additional_tools'):
                catalogs.append(child)
            elif key not in ('parameters', 'inputSchema', 'input_schema'):
                catalogs.extend(request_tools(child))
    elif isinstance(value, list):
        for child in value:
            catalogs.extend(request_tools(child))
    return catalogs


def request_instructions(value):
    return [{k: v for k, v in block.items() if k != 'location'} for block in value['instruction_blocks']]


def effective_identity(path, record, task):
    from _codex_session import inspect_policy
    if record.get('tool_environment') == TOOL_ENVIRONMENT:
        from _paired_session import inspect_policy
    base = BASE if record.get('tool_environment') == TOOL_ENVIRONMENT else LEGACY_BASE
    session = record.get('session')
    require(isinstance(session, dict), 'missing_effective_session_identity')
    require(digest(session.get('normalized_configuration')) == record['configuration_sha256'], 'effective_configuration_hash_mismatch')
    require(session.get('configuration_sha256') == record['configuration_sha256'], 'session_configuration_mismatch')
    inspected, dynamic_hash, consumed = None, None, 0
    first_tools = None
    with path.open('rb') as stream:
        while line := stream.readline(FRAME_LIMIT + 1):
            consumed += len(line)
            require(len(line) <= FRAME_LIMIT and consumed <= TOTAL_LIMIT, 'audit_input_limit_exceeded')
            event = json_value(line)
            if event.get('kind') == 'responses.request':
                current = inspect_policy(event['value'], record['configuration'], base, task['question'])
                if inspected is None:
                    inspected = current
                    first_tools = digest(request_tools(event['value']))
                else:
                    # Assistant/tool messages can grow the conversation and move
                    # instruction locations without changing their contents.
                    require(digest(request_tools(event['value'])) == first_tools
                            and current['observed']['runtime_registry'] == inspected['observed']['runtime_registry']
                            and request_instructions(current) == request_instructions(inspected), 'provider_request_identity_changed')
            if event.get('kind') == 'rpc.sent' and event['value'].get('method') == 'thread/start':
                require(dynamic_hash is None, 'duplicate_thread_start')
                dynamic_hash = digest(event['value']['params']['dynamicTools'])
    require(inspected is not None and dynamic_hash == record['catalog_sha256'], 'missing_or_mismatched_effective_catalog')
    for field in ('catalog_sha256', 'prompt_sha256', 'instruction_blocks', 'observed', 'model', 'effort'):
        require(session.get(field) == inspected[field], 'effective_request_identity_mismatch')
    return first_tools, digest(request_instructions(inspected))



def live_identity(path, record, task, plan, status):
    from _codex_smoke import live_configuration
    if record.get('tool_environment') == TOOL_ENVIRONMENT:
        from _paired_session import live_configuration
    from _paired_proof import plan_hash
    authorization = status.get('authorization', {})
    binding = authorization.get('binding', {})
    proof = binding.get('proofs', {})
    require(record.get('authorization_sha256') == authorization.get('confirmation')
            and binding.get('plan_sha256') == plan_hash(plan)
            and proof.get('plan_sha256') == plan_hash(plan), 'live_authorization_identity_mismatch')
    selected = proof.get('sessions', {}).get(record['trial_id'], {})
    require(all(selected.get(key) == record.get(key) for key in ('catalog_sha256', 'prompt_sha256')),
            'live_selected_proof_mismatch')
    require(record.get('offline_proof', {}).get('receipt_sha256') == proof.get('receipt_sha256'),
            'live_proof_receipt_mismatch')
    session = record.get('session', {})
    require(session.get('normalized_configuration') == live_configuration()
            and digest(live_configuration()) == record['configuration_sha256'], 'live_configuration_mismatch')
    sent, consumed = {}, 0
    with path.open('rb') as stream:
        while line := stream.readline(FRAME_LIMIT + 1):
            consumed += len(line)
            require(len(line) <= FRAME_LIMIT and consumed <= TOTAL_LIMIT, 'audit_input_limit_exceeded')
            event = json_value(line)
            message = event.get('value', {})
            if event.get('kind') == 'rpc.sent' and message.get('method') in ('thread/start', 'turn/start'):
                method = message['method']
                require(method not in sent, 'duplicate_live_submission')
                sent[method] = message['params']
    thread, turn = sent.get('thread/start', {}), sent.get('turn/start', {})
    require(digest(thread.get('dynamicTools')) == record['catalog_sha256']
            and thread.get('ephemeral') is True
            and turn.get('input') == [{'type': 'text', 'text': task['question']}]
            and turn.get('model') == record['requested_model']
            and turn.get('effort') == record['requested_effort'], 'live_submission_identity_mismatch')
    return record['catalog_sha256'], record['prompt_sha256']

def records(runs, corpus):
    require(1 <= len(runs) <= 8, 'select_one_to_eight_runs')
    result, seen, consumed = [], set(), 0
    signatures = {}
    for run in runs:
        status = store.validate_run(run)
        require('trials' in status, 'run_initialization_incomplete_no_published_plan')
        plan = store.read(run / 'plan.json')
        require(plan.get('contract') == CONTRACT and plan.get('schema_version') in (1, 2), 'unsupported_run_contract')
        require(plan.get('manifest_sha256') == corpus.manifest_hash and plan.get('sources_sha256') == corpus.sources_hash, 'run_corpus_identity_mismatch')
        require(len(result) + len(plan['trials']) <= MAX_TRIALS, 'score_trial_limit_exceeded')
        run_status = store.read(run / 'run.json')
        for planned in plan['trials']:
            path = run / (planned['trial_id'] + '.json')
            consumed += path.stat().st_size
            require(consumed <= MAX_BYTES, 'aggregate_trial_metadata_limit_exceeded')
            record = store.read(path) if path.stat().st_size else initial_record(run, plan, planned)
            key = (record['run_id'], record['trial_id'])
            require(key not in seen, 'duplicate_trial_id')
            seen.add(key)
            task = corpus.tasks.get(record['task_id'])
            require(task is not None, 'unknown_record_task')
            source = corpus.sources[task['source']]
            require(record['source'] == {k: source[k] for k in ('id', 'commit', 'tree', 'upstream_commit', 'upstream_tree')}, 'record_source_revision_mismatch')
            require(record['partition'] == task['split'], 'record_partition_mismatch')
            hashes = {'manifest_sha256': corpus.manifest_hash, 'sources_sha256': corpus.sources_hash,
                      'task_sha256': digest(task), 'prompt_sha256': hashlib.sha256(((BASE if record.get('tool_environment') == TOOL_ENVIRONMENT else LEGACY_BASE) + '\n' + task['question']).encode()).hexdigest()}
            require(all(record.get(k) == v for k, v in hashes.items()), 'record_task_or_prompt_mismatch')
            require(record.get('tool_environment', 'controlled-handlers-v1') == plan.get('tool_environment', 'controlled-handlers-v1'), 'tool_environment_mismatch')
            for field in ('implementation_sha256', 'client_sha256', 'grepglint_sha256', 'requested_model', 'requested_effort'):
                require(record.get(field) == plan.get(field), 'record_run_identity_mismatch')
            if record['state'] == 'completed':
                # Configuration includes normalized instructions and client settings;
                # tool catalogs differ by treatment, but must be stable across repeats.
                effective = (effective_identity(run / (record['trial_id'] + '.jsonl'), record, task)
                             if record['simulation'] else live_identity(
                                 run / (record['trial_id'] + '.jsonl'), record, task, plan, run_status))
                sig = tuple(record.get(k) for k in ('requested_model', 'requested_effort', 'client_sha256',
                            'implementation_sha256', 'grepglint_sha256', 'configuration_sha256', 'catalog_sha256')) + (effective[0],)
                previous = signatures.setdefault(record['configuration'], sig)
                require(previous == sig, 'incompatible_run_configuration')
                record['_effective_instructions'] = effective[1]
            record['_run_status'] = 'completed' if status['status'] == run_status.get('status') == 'completed' else 'incomplete'
            record['_run_errors'] = run_status.get('errors', [])
            record['_run_cleanup'] = run_status.get('cleanup', {})
            result.append(record)
    pairs = {}
    for record in result:
        pairs.setdefault((record['run_id'], record['pair_id']), []).append(record)
    for pair in pairs.values():
        require(len(pair) == 2 and {r['configuration'] for r in pair} == {'control', 'grepglint'}, 'invalid_planned_pair')
        for field in ('task_id', 'repetition', 'source', 'prompt_sha256', 'requested_model', 'requested_effort', 'client_sha256', 'implementation_sha256', 'grepglint_sha256'):
            require(pair[0][field] == pair[1][field], 'pair_identity_mismatch')
        if all(r['state'] == 'completed' for r in pair):
            require(pair[0]['configuration_sha256'] == pair[1]['configuration_sha256'] and pair[0]['_effective_instructions'] == pair[1]['_effective_instructions'], 'pair_effective_configuration_mismatch')
    return sorted(result, key=lambda r: (r['run_id'], r['order']))
