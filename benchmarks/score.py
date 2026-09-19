#!/usr/bin/env python3
"""Prepare blind human reviews and report saved discovery trials offline."""
import argparse
from collections import Counter, defaultdict
import copy
import json
import os
from pathlib import Path
import secrets
import sys

from _codex_capture import ProbeError, json_value
from _score_input import Corpus, MAX_BYTES, digest, read, records
from _score_metrics import coverage, correctness, judgment_template, quota, require

CORPUS = Path(__file__).resolve().parent
TOKENS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')


def answer(record):
    raw = record['answer'].get('raw')
    if raw is None:
        return None
    try:
        value = json_value(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def review_entry(record, task, label):
    return {'label': label, 'task_id': task['id'], 'question': task['question'],
            'source': record['source'], 'answer': record['answer'].get('raw'),
            'claims': task['claims'], 'accepted_evidence_groups': task['evidence_groups'],
            'judgment': judgment_template(task)}


def prepare_review(items, tasks, oracle):
    shuffled = list(items)
    secrets.SystemRandom().shuffle(shuffled)
    labels = {'answer-' + secrets.token_hex(8): [r['run_id'], r['trial_id']] for r in shuffled}
    key = {'schema_version': 1, 'input_sha256': digest(items), 'oracle_sha256': digest(oracle), 'labels': labels}
    by_id = {(r['run_id'], r['trial_id']): r for r in items}
    form = {'schema_version': 1, 'oracle_sha256': digest(oracle),
            'instructions': 'Judge every claim and its cited evidence. Set booleans and record reasons. Leave unknown judgments null. Do not consult the private key or trial artifacts until grading is complete.',
            'answers': [review_entry(by_id[tuple(value)], tasks[by_id[tuple(value)]['task_id']], label)
                        for label, value in labels.items()]}
    return key, form


def judgments(items, tasks, oracle, key, form):
    require(key.get('schema_version') == 1 and form.get('schema_version') == 1, 'unsupported_review_version')
    require(key.get('input_sha256') == digest(items), 'review_inputs_changed_regenerate_review')
    require(key.get('oracle_sha256') == form.get('oracle_sha256') == digest(oracle), 'review_oracle_changed_regenerate_review')
    labels = key.get('labels')
    require(isinstance(labels, dict) and len(labels) == len(items), 'invalid_review_key')
    require(isinstance(form.get('answers'), list) and len(form['answers']) == len(items), 'incomplete_review_form')
    by_id = {(r['run_id'], r['trial_id']): r for r in items}
    result = {}
    for entry in form['answers']:
        require(isinstance(entry, dict) and entry.get('label') in labels, 'unknown_review_label')
        ref = labels[entry['label']]
        require(isinstance(ref, list) and len(ref) == 2 and all(isinstance(x, str) for x in ref), 'invalid_review_reference')
        ref = tuple(ref)
        require(ref in by_id and ref not in result, 'duplicate_or_unknown_review_trial')
        record = by_id[ref]
        expected = review_entry(record, tasks[record['task_id']], entry['label'])
        require({k: v for k, v in entry.items() if k != 'judgment'} == {k: v for k, v in expected.items() if k != 'judgment'}, 'review_answer_or_rubric_edited')
        result[ref] = entry.get('judgment')
    return result


def trial_result(record, task, corpus, judgment):
    parsed = answer(record)
    citations = parsed.get('evidence', []) if parsed else []
    if not isinstance(citations, list):
        citations = []
    files = corpus.files[task['source']]
    cited = coverage(citations, task['evidence_groups'], files)
    tools = record.get('tools', [])
    from _paired_contract import LIMITS, TOOL_ENVIRONMENT
    require(isinstance(tools, list) and len(tools) <= LIMITS['tool_calls'], 'invalid_saved_tools')
    retrieved = []
    for tool in tools:
        require(isinstance(tool, dict) and isinstance(tool.get('returned_ranges'), list), 'invalid_saved_tool_ranges')
        retrieved.extend(tool['returned_ranges'])
        require(len(retrieved) <= 10000, 'too_many_returned_ranges')
    returned = coverage(retrieved, task['evidence_groups'], files)
    if record.get('tool_environment') == TOOL_ENVIRONMENT:
        returned = {key: None for key in returned}
        returned['status'] = 'unavailable_native_shell_ranges'
    eligible = (record['state'] == 'completed' and record['answer']['status'] == 'valid'
                and cited['invalid_ranges'] == 0 and cited['valid_files'] > 0)
    decision = judgment if judgment is not None else judgment_template(task)
    result = {k: record[k] for k in ('run_id', 'trial_id', 'pair_id', 'task_id', 'partition', 'configuration', 'repetition', 'order', 'state', 'source')}
    result.update(group=task['group'], language=task['language'], source_bytes=corpus.sources[task['source']]['source_bytes'],
        simulation=record['simulation'], excluded_from_live_comparison=record['simulation'],
        run_status=record['_run_status'], run_errors=record['_run_errors'], run_cleanup=record['_run_cleanup'],
        answer_status=record['answer']['status'], errors=record.get('errors', []),
        tool_environment=record.get('tool_environment', 'controlled-handlers-v1'),
        correctness=correctness(decision, task, eligible), judgment=decision,
        cited=cited, retrieved=returned,
        measurements=record['measurements'], usage=record['usage'],
        quota_observations=__import__('_paired_live').public_quota(record.get('quota_observations', [])),
        measurement_validation='audited' if record['state'] == 'completed' else 'partial_unverified',
        tool_calls=record.get('audit', {}).get('calls', len(tools)),
        returned_bytes=sum(t['returned_bytes'] for t in tools) if all(t.get('returned_bytes') is not None for t in tools) else None,
        grepglint_calls=sum(t.get('name') == 'grepglint_search' for t in tools),
        grepglint=record.get('grepglint'),
        cold_index_reference=corpus.inventories[task['source']]['summary'],
        identities={k: record.get(k) for k in ('manifest_sha256', 'sources_sha256', 'task_sha256', 'prompt_sha256',
            'client_sha256', 'implementation_sha256', 'grepglint_sha256', 'catalog_sha256', 'configuration_sha256',
            'requested_model', 'reported_model', 'requested_effort', 'reported_effort')})
    return result


def summary(rows):
    return {'trials': len(rows), 'tasks': len({r['task_id'] for r in rows}),
            'attempted': sum(r['state'] != 'not-started' for r in rows),
            'failed': sum(r['state'] == 'failed' for r in rows),
            'not_started': sum(r['state'] == 'not-started' for r in rows),
            'unscored': sum(r['correctness'] == 'unscored' for r in rows),
            'pass': sum(r['correctness'] == 'pass' for r in rows),
            'fail': sum(r['correctness'] == 'fail' for r in rows),
            'excluded_from_live_comparison': sum(r['excluded_from_live_comparison'] for r in rows)}


def delta(a, b):
    return b - a if type(a) in (int, float) and type(b) in (int, float) else None


def report(items, tasks, oracle, corpus, graded=None, quota_file=None):
    rows = [trial_result(r, tasks[r['task_id']], corpus, (graded or {}).get((r['run_id'], r['trial_id']))) for r in items]
    observations = {}
    if quota_file is not None:
        require(isinstance(quota_file, dict) and set(quota_file) == {'schema_version', 'input_sha256', 'pairs'}, 'invalid_quota_file')
        require(quota_file['schema_version'] == 1 and quota_file['input_sha256'] == digest(items), 'quota_input_mismatch')
        require(isinstance(quota_file['pairs'], list) and len(quota_file['pairs']) <= len(items) // 2, 'invalid_quota_pairs')
        for entry in quota_file['pairs']:
            require(isinstance(entry, dict) and set(entry) == {'run_id', 'pair_id', 'observation'}, 'invalid_quota_pair')
            key = (entry['run_id'], entry['pair_id'])
            require(key not in observations, 'duplicate_quota_pair')
            observations[key] = entry['observation']
    groups = defaultdict(list)
    for row in rows:
        groups[(row['run_id'], row['pair_id'])].append(row)
    require(set(observations) <= set(groups), 'unknown_quota_pair')
    pairs = []
    for key, members in sorted(groups.items()):
        ordered = {r['configuration']: r for r in members}
        a, b = ordered['control'], ordered['grepglint']
        counters_a, counters_b = a['usage'].get('counters') or {}, b['usage'].get('counters') or {}
        pairs.append({'run_id': key[0], 'pair_id': key[1], 'task_id': a['task_id'], 'repetition': a['repetition'],
            'status': 'completed' if all(r['state'] == 'completed' and r['run_status'] == 'completed' for r in members) else 'incomplete',
            'correctness': {r['configuration']: r['correctness'] for r in members},
            'token_delta_grepglint_minus_control': {k: delta(counters_a.get(k), counters_b.get(k)) for k in TOKENS},
            'wall_seconds_delta_grepglint_minus_control': delta(a['measurements'].get('trial_wall_seconds'), b['measurements'].get('trial_wall_seconds')),
            'quota': quota(observations.get(key), any(r['simulation'] for r in members))})
    partitions = {}
    for partition in ('development', 'held_out'):
        subset = [r for r in rows if r['partition'] == partition]
        partitions[partition] = {}
        for dimension in ('configuration', 'group', 'language', 'source_bytes'):
            values = sorted({str(r[dimension]) for r in subset})
            partitions[partition][dimension] = {value: summary([r for r in subset if str(r[dimension]) == value]) for value in values}
    variation = []
    for task_id in sorted({r['task_id'] for r in rows}):
        task_pairs = [p for p in pairs if p['task_id'] == task_id]
        values = [p['wall_seconds_delta_grepglint_minus_control'] for p in task_pairs if p['wall_seconds_delta_grepglint_minus_control'] is not None]
        variation.append({'task_id': task_id, 'pairs': len(task_pairs), 'independent_tasks': 1,
                          'observed_wall_delta_min': min(values) if values else None,
                          'observed_wall_delta_max': max(values) if values else None,
                          'by_configuration': {c: summary([r for r in rows if r['task_id'] == task_id and r['configuration'] == c]) for c in ('control', 'grepglint')}})
    return {'schema_version': 1, 'contract': 'discovery-score-v1', 'input_sha256': digest(items),
            'oracle': oracle, 'oracle_sha256': digest(oracle), 'summary': summary(rows),
            'limitations': ['Simulation results are workflow diagnostics, excluded from live correctness, consumption and model-efficiency comparisons.',
                'One task cannot establish a general benefit.' if len(variation) == 1 else 'Repeats measure task-level variation and are not independent tasks.',
                'Retrieval overlap does not establish factual correctness. Missing judgments remain unscored.',
                'Cached input is included in input; reasoning is included in output. Counters are not added twice.',
                'Evidence covers the pinned client constructed request and protocol-visible provider calls, not undisclosed provider-side instructions or capabilities.'],
            'trials': rows, 'pairs': pairs, 'partitions': partitions, 'task_variation': variation}


def shareable(value):
    # Construct from safe values, never redact free-form text after copying it.
    result = {k: copy.deepcopy(value[k]) for k in ('schema_version', 'contract', 'input_sha256', 'oracle_sha256', 'summary', 'limitations', 'partitions', 'task_variation', 'pairs')}
    result['oracle_version'] = value['oracle']['version']
    result['trials'] = []
    for row in value['trials']:
        item = {k: row[k] for k in ('run_id', 'trial_id', 'pair_id', 'task_id', 'partition', 'configuration', 'repetition', 'order', 'state',
            'group', 'language', 'source_bytes', 'simulation', 'excluded_from_live_comparison', 'answer_status', 'correctness', 'tool_calls', 'returned_bytes', 'grepglint_calls', 'tool_environment')}
        for name in ('retrieved', 'cited'):
            item[name] = {k: v for k, v in row[name].items() if k != 'ranges'}
        item['measurements'] = {k: v for k, v in row['measurements'].items() if k in ('trial_wall_seconds', 'captured_bytes', 'source_verification_seconds', 'source_preparation_seconds', 'memory_peak_bytes', 'tool_seconds') and (v is None or type(v) in (int, float))}
        item['usage'] = {'counters': {k: v if type(v := (row['usage'].get('counters') or {}).get(k)) is int and v >= 0 else None for k in TOKENS}, 'simulation': row['simulation']}
        observation = row.get('grepglint') or {}
        item['grepglint'] = {k: observation.get(k) for k in ('used', 'non_use') if type(observation.get(k)) is bool}
        item['grepglint']['indexing_seconds'] = observation.get('indexing_seconds') if type(observation.get('indexing_seconds')) in (int, float) else None
        item['grepglint']['disk_peak'] = {k: v for k, v in (observation.get('disk_peak') or {}).items() if k in ('cache_bytes', 'database_bytes', 'journal_bytes') and type(v) is int and v >= 0}
        item['grepglint']['error_count'] = len(observation.get('errors', []))
        item['grepglint']['fallback_call_count'] = len(observation.get('fallback_calls', []))
        item['memory_method'] = 'cgroup aggregate high-water; includes cache pages' if row['measurements'].get('memory_method') == 'cgroup memory.peak; aggregate high-water since run service start, includes cache pages' else 'unavailable or simulated method; inspect local report'
        result['trials'].append(item)
    return result


def markdown(value):
    lines = ['# Discovery comparison', '', *value['limitations'], '',
        f"Oracle version {value.get('oracle_version', value.get('oracle', {}).get('version'))}. Input `{value['input_sha256']}`.", '',
        '| Trials | Attempted | Failed | Not started | Unscored | Pass | Fail | Excluded from live comparison |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |',
        '| ' + ' | '.join(str(value['summary'][k]) for k in ('trials', 'attempted', 'failed', 'not_started', 'unscored', 'pass', 'fail', 'excluded_from_live_comparison')) + ' |', '',
        '| Trial | Task | Split | Configuration | State / answer | Judgment | Retrieved region recall | Cited region recall | Input / cached / output / reasoning | Wall seconds | Calls / bytes / Grepglint |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    def number(v):
        return 'unknown' if v is None else str(round(v, 6)) if isinstance(v, float) else str(v)
    for row in value['trials']:
        counters = row['usage'].get('counters') or {}
        lines.append('| ' + ' | '.join([row['run_id'] + '/' + row['trial_id'], row['task_id'], row['partition'], row['configuration'],
            row['state'] + ' / ' + row['answer_status'], row['correctness'], number(row['retrieved']['region_recall']), number(row['cited']['region_recall']),
            ' / '.join(number(counters.get(k)) for k in TOKENS[:4]), number(row['measurements'].get('trial_wall_seconds')),
            f"{row['tool_calls']} / {number(row['returned_bytes'])} / {row['grepglint_calls']}"]) + ' |')
    lines += ['', '## Resource observations', '',
        '| Trial | Indexing seconds | Non-use | Index errors / fallback calls | Memory bytes | Cache / database / journal bytes |',
        '| --- | --- | --- | --- | --- | --- |']
    for row in value['trials']:
        observation = row.get('grepglint') or {}
        disk = observation.get('disk_peak') or {}
        lines.append('| ' + ' | '.join([row['run_id'] + '/' + row['trial_id'], number(observation.get('indexing_seconds')),
            str(observation.get('non_use', 'unknown')), str(observation.get('error_count', len(observation.get('errors', [])))) + ' / ' + str(observation.get('fallback_call_count', len(observation.get('fallback_calls', [])))),
            number(row['measurements'].get('memory_peak_bytes')), ' / '.join(number(disk.get(k)) for k in ('cache_bytes', 'database_bytes', 'journal_bytes'))]) + ' |')
    lines += ['', 'Indexing time is the first search wall time, including cold indexing. Disk sizes are sampled lower bounds. Memory is the run cgroup high-water mark, including cache pages; simulated fixtures may use a simulated method. These are not per-trial incremental memory measurements.', '']
    for partition, dimensions in value['partitions'].items():
        lines += ['## ' + partition.replace('_', ' ').capitalize(), '']
        for dimension, groups in dimensions.items():
            for name, counts in groups.items():
                lines.append(f"- {dimension} {name}: {counts['tasks']} tasks, {counts['trials']} trials, {counts['pass']} pass, {counts['fail']} fail, {counts['unscored']} unscored, {counts['failed']} failed, {counts['excluded_from_live_comparison']} excluded from live comparison.")
    lines += ['', '## Pair observations', '']
    for pair in value['pairs']:
        q = pair['quota']
        lines.append(f"- {pair['run_id']}/{pair['pair_id']}, task {pair['task_id']}, repetition {pair['repetition']}: {pair['status']}; weekly quota {q['status']}; observed change {number(q['change_percentage_points'])} percentage points.")
        if q['before'] is not None:
            for when in ('before', 'after'):
                entry = q[when]
                lines.append(f"  {when}: {entry['remaining_percent']}% at {entry['observed_at']}, bucket {entry['bucket']}, reset {entry['reset_at']}.")
            lines.append('  ' + q['interpretation'] + '; ' + ', '.join(q['reasons']))
    lines += ['', '## Task variation', '']
    for task in value['task_variation']:
        lines.append(f"- {task['task_id']}: {task['pairs']} pair(s), one task; observed wall-time delta range {number(task['observed_wall_delta_min'])} to {number(task['observed_wall_delta_max'])} seconds.")
    lines += ['', 'Detailed file/range coverage, partition/group/language/source-size counts, indexing, memory methods, disk observations, failures and judgments are in the companion JSON. Unknown measurements remain unknown.', '']
    return '\n'.join(lines)


def write_outputs(directory, files):
    require(not directory.exists(), 'output_exists_choose_new_directory')
    payloads = {name: (value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode() for name, value in files.items()}
    require(sum(map(len, payloads.values())) <= MAX_BYTES, 'score_output_limit_exceeded')
    directory.mkdir(mode=0o700)
    for name, data in payloads.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'report'))
    parser.add_argument('--run', action='append', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, default=CORPUS)
    parser.add_argument('--corrections', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--quota', type=Path)
    parser.add_argument('--shareable', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(bool(args.key) == bool(args.review), 'supply_both_key_and_review')
        require(args.command == 'report' or not any((args.key, args.review, args.quota, args.shareable)), 'prepare_accepts_only_runs_corpus_corrections_output')
        corpus = Corpus(args.corpus)
        tasks, oracle = corpus.rubric(read(args.corrections) if args.corrections else None)
        items = records(args.run, corpus)
        if args.command == 'prepare':
            key, form = prepare_review(items, tasks, oracle)
            write_outputs(args.output, {'private-key.json': key, 'review.json': form})
        else:
            graded = judgments(items, tasks, oracle, read(args.key), read(args.review)) if args.review else None
            value = report(items, tasks, oracle, corpus, graded, read(args.quota) if args.quota else None)
            if args.shareable:
                value = shareable(value)
            write_outputs(args.output, {'report.json': value, 'report.md': markdown(value)})
        print('Created local offline scoring artifacts. Existing inputs were not changed.')
        return 0
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        code = str(error) if isinstance(error, ProbeError) else 'invalid_or_unavailable_score_input'
        print('Scoring stopped: ' + code + '. Preserve inputs, correct the indicated input, and use a new output directory.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
