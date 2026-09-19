import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import score
from _score_input import Corpus, records, digest, read
from _score_metrics import coverage, correctness, judgment_template, quota
from _codex_capture import Budget, ProbeError
from _paired_trial import TrialAudit
import _paired_contract as contract
import _paired_store as store
from _codex_session import inspect_policy, configuration
from codex_preflight import dynamic_tools
from test_preflight import request

CORPUS = Path(__file__).resolve().parents[1]
TASK = 'ccx-crossorg-217'


def completed_run(root, corpus, repetitions=1, seal=True):
    plan = contract.plan(CORPUS, [TASK], repetitions=repetitions)
    plan.update(implementation_sha256='a' * 64, grepglint_sha256='b' * 64)
    run = store.create(root, plan)
    fixture = json.loads((CORPUS / 'schema-fixtures/successful.json').read_text())
    task = corpus.tasks[TASK]
    evidence = [{k: g['alternatives'][0][k] for k in ('path', 'start', 'end')} for g in task['evidence_groups']]
    for planned in plan['trials']:
        record = {**copy.deepcopy(fixture), **store.read(run / (planned['trial_id'] + '.json'))}
        record.update(state='completed', reported_model=record['requested_model'], reported_effort=record['requested_effort'])
        record['measurements'] = fixture['measurements']
        record['tools'] = []
        record['answer'] = {'status': 'valid', 'parsed': {'explanation': task['reference_answer'] if record['configuration'] == 'control' else 'Wrong explanation with identical files.', 'evidence': evidence}}
        record['answer']['raw'] = json.dumps(record['answer']['parsed'])
        mode = record['configuration']
        dynamic = dynamic_tools(mode)
        req = request(dynamic)
        req.update(instructions=contract.BASE, input=[{'role': 'user', 'content': task['question']}])
        observed = inspect_policy(req, mode, contract.BASE, task['question'])
        record['catalog_sha256'] = digest(dynamic)
        record['configuration_sha256'] = digest(configuration('<loopback>'))
        record['session'] = {**observed, 'normalized_configuration': configuration('<loopback>'), 'configuration_sha256': record['configuration_sha256']}
        audit = TrialAudit(run / (record['trial_id'] + '.jsonl'), Budget())
        audit.record('rpc.sent', {'method': 'thread/start', 'params': {'dynamicTools': dynamic}}, mode)
        audit.record('responses.request', req, mode)
        audit.receive({'method': 'rawResponseItem/completed', 'params': {'item': {'id': 'final', 'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': record['answer']['raw']}]}}}, mode)
        event = {'method': 'rawResponse/completed', 'params': {'responseId': 'r1', 'usage': {'inputTokens': 20, 'cachedInputTokens': 10, 'outputTokens': 8, 'reasoningOutputTokens': 6, 'totalTokens': 28}}}
        audit.receive(event, mode)
        audit.receive(event, mode)
        record['audit'] = {'status': 'passed', 'sha256': audit.sha.hexdigest(), 'records': audit.sequence, 'calls': 0, 'path': record['trial_id'] + '.jsonl'}
        record['usage'] = audit.usage_summary()
        audit.close()
        store.save(run, record['trial_id'] + '.json', record)
    status = store.read(run / 'run.json')
    status.update(status='completed', cleanup={'service_stopped': True})
    store.save(run, 'run.json', status)
    if seal:
        store.seal(run)
    return run


class Retrieval(unittest.TestCase):
    def setUp(self):
        self.files = {p: {'mode': '100644', 'lines': 100} for p in ('a', 'b', 'unrelated')}
        self.groups = [{'id': 'g1', 'required': True, 'alternatives': [{'path': 'a', 'start': 10, 'end': 19}, {'path': 'b', 'start': 1, 'end': 10}]},
                       {'id': 'optional', 'required': False, 'alternatives': [{'path': 'a', 'start': 80, 'end': 90}]}]

    def test_union_alternatives_partial_chunks_and_invalid_paths(self):
        evidence = [{'path': 'a', 'start': 10, 'end': 12}, {'path': 'a', 'start': 11, 'end': 14}] * 2
        result = coverage(evidence, self.groups, self.files)
        self.assertEqual(result['group_overlap'], {'g1': .5})
        self.assertEqual(result['lines'], 5)
        self.assertEqual(result['region_recall'], 1)
        alternative = coverage([{'path': 'b', 'start': 1, 'end': 10}], self.groups, self.files)
        self.assertEqual(alternative['group_overlap'], {'g1': 1})
        evidence += [{'path': '../secret', 'start': 1, 'end': 1}, {'path': 'unrelated', 'start': 1, 'end': 100}]
        result = coverage(evidence, self.groups, self.files)
        self.assertEqual(result['file_precision'], 1 / 3)
        self.assertEqual(result['invalid_ranges'], 1)

    def test_whole_chunk_metadata_does_not_expand_excerpt(self):
        result = coverage([{'path': 'a', 'start': 10, 'end': 10, 'partial_excerpt': True, 'end_line': 100}], self.groups, self.files)
        self.assertEqual(result['group_overlap']['g1'], .1)
        self.assertEqual(result['lines'], 1)


class Scoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = Corpus(CORPUS)

    def test_blind_grade_offline_reproduce_and_usage_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = completed_run(root / 'runs', self.corpus)
            items = records([run], self.corpus)
            tasks, oracle = self.corpus.rubric(None)
            key, form = score.prepare_review(items, tasks, oracle)
            self.assertNotIn('configuration', json.dumps(form))
            self.assertNotIn('t0001', json.dumps(form))
            for entry in form['answers']:
                good = 'Wrong explanation' not in entry['answer']
                for claim in entry['judgment']['claims'].values():
                    claim.update(supported=good, evidence_supported=good, reason='Checked the cited source.')
                entry['judgment'].update(material_contradiction=not good, reason='Compared all claims.')
            graded = score.judgments(items, tasks, oracle, key, form)
            result = score.report(items, tasks, oracle, self.corpus, graded)
            self.assertEqual(result['summary']['pass'], 1)
            self.assertEqual(result['summary']['fail'], 1)
            self.assertEqual(result['trials'][0]['cited'], result['trials'][1]['cited'])
            self.assertEqual(result['trials'][0]['usage']['counters']['totalTokens'], 28)
            self.assertEqual(result['summary']['excluded_from_live_comparison'], 2)
            score.write_outputs(root / 'review', {'private-key.json': key, 'review.json': form})
            argv = ['report', '--run', str(run), '--key', str(root / 'review/private-key.json'), '--review', str(root / 'review/review.json')]
            for name in ('report1', 'report2'):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(score.main(argv + ['--output', str(root / name)]), 0)
            for name in ('report.json', 'report.md'):
                self.assertEqual((root / 'report1' / name).read_bytes(), (root / 'report2' / name).read_bytes())
            self.assertEqual((root / 'report1/report.json').stat().st_mode & 0o777, 0o600)
            bad = copy.deepcopy(form)
            bad['answers'][0]['answer'] = 'edited answer'
            with self.assertRaisesRegex(ProbeError, 'review_answer_or_rubric_edited'):
                score.judgments(items, tasks, oracle, key, bad)
            with self.assertRaisesRegex(ProbeError, 'duplicate_trial_id'):
                records([run, run], self.corpus)

    def test_effective_identity_tampering_and_pair_mismatch_rejected(self):
        for field in ('configuration_sha256', 'catalog_sha256', 'prompt_sha256', 'reported_model', 'session'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                run = completed_run(Path(tmp), self.corpus, seal=False)
                record = store.read(run / 't0001.json')
                record[field] = {} if field == 'session' else 'f' * 64
                store.save(run, 't0001.json', record)
                with self.assertRaises(ProbeError):
                    records([run], self.corpus)

    def test_every_provider_request_identity_and_conversation_growth(self):
        import hashlib
        from _codex_audit import encoded
        for change in ('valid', 'instructions', 'catalog', 'model', 'effort', 'extra_instruction'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                run = completed_run(Path(tmp), self.corpus, seal=False)
                record = store.read(run / 't0001.json')
                path = run / 't0001.jsonl'
                events = [json.loads(line) for line in path.read_text().splitlines()]
                req = copy.deepcopy(next(e['value'] for e in events if e['kind'] == 'responses.request'))
                req['input'].insert(0, {'type': 'message', 'role': 'assistant', 'content': 'Working through the evidence.'})
                req['input'].append({'type': 'function_call_output', 'call_id': 'previous', 'output': 'source text'})
                if change == 'instructions':
                    req['instructions'] = 'Different instructions'
                elif change == 'catalog':
                    req['tools'][0]['description'] = 'Different search semantics'
                elif change == 'model':
                    req['model'] = 'different-model'
                elif change == 'effort':
                    req['reasoning']['effort'] = 'different-effort'
                elif change == 'extra_instruction':
                    req['input'].append({'role': 'developer', 'content': '<permissions instructions>different rules</permissions instructions>'})
                event = {'sequence': len(events), 'kind': 'responses.request', 'session': record['configuration'], 'value': req}
                with path.open('ab') as stream:
                    stream.write(encoded(event) + b'\n')
                record['audit'].update(records=len(events) + 1, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                store.save(run, 't0001.json', record)
                if change == 'valid':
                    self.assertEqual(len(records([run], self.corpus)), 2)
                else:
                    with self.assertRaises(ProbeError):
                        records([run], self.corpus)

    def test_invalid_citations_empty_evidence_and_missing_grades_never_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = completed_run(Path(tmp), self.corpus)
            items = records([run], self.corpus)
            tasks, oracle = self.corpus.rubric(None)
            record = items[0]
            task = tasks[TASK]
            grade = judgment_template(task)
            for claim in grade['claims'].values():
                claim.update(supported=True, evidence_supported=True, reason='Manual judgment.')
            grade.update(material_contradiction=False, reason='No contradiction.')
            for evidence in ([], [{'path': '/home/private/source', 'start': 1, 'end': 2}],
                             [{'path': task['evidence_groups'][0]['alternatives'][0]['path'], 'start': 1, 'end': 999999}]):
                record['answer']['raw'] = json.dumps({'explanation': 'claim', 'evidence': evidence})
                row = score.trial_result(record, task, self.corpus, grade)
                self.assertEqual(row['correctness'], 'unscored')
            grade['claims'][task['claims'][0]['id']]['supported'] = None
            self.assertEqual(correctness(grade, task, True), 'unscored')

    def test_incomplete_missing_malformed_timeout_and_unknown_stay_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = contract.plan(CORPUS, [TASK])
            plan.update(implementation_sha256='a' * 64, grepglint_sha256='b' * 64)
            run = store.create(Path(tmp), plan)
            trial = plan['trials'][0]['trial_id']
            record = store.read(run / (trial + '.json'))
            record.update(state='failed', errors=['deadline', 'source_unavailable'])
            record['answer'] = {'status': 'malformed', 'raw': 'not JSON', 'parsed': None}
            store.save(run, trial + '.json', record)
            before = {p.name: p.read_bytes() for p in run.iterdir()}
            items = records([run], self.corpus)
            tasks, oracle = self.corpus.rubric(None)
            result = score.report(items, tasks, oracle, self.corpus)
            self.assertEqual(result['summary']['failed'], 1)
            self.assertEqual(result['summary']['unscored'], 2)
            self.assertEqual(result['summary']['not_started'], 1)
            self.assertEqual(result['pairs'][0]['status'], 'incomplete')
            self.assertIsNone(result['pairs'][0]['token_delta_grepglint_minus_control']['inputTokens'])
            self.assertEqual(result['trials'][0]['errors'], ['deadline', 'source_unavailable'])
            self.assertEqual(result['trials'][0]['cold_index_reference']['sqlite_error_class'], 'SQLITE_FULL')
            self.assertEqual(before, {p.name: p.read_bytes() for p in run.iterdir()})
            record['source']['commit'] = '0' * 40
            store.save(run, trial + '.json', record)
            with self.assertRaises(ProbeError):
                records([run], self.corpus)

    def test_correction_applies_both_and_invalidates_old_judgments(self):
        tasks, oracle = self.corpus.rubric(None)
        task = tasks[TASK]
        original = task['evidence_groups'][0]['alternatives'][0]
        path = next(p for p, f in self.corpus.files[task['source']].items() if f['mode'] == '100644' and f['lines'] and p != original['path'])
        region = {'path': path, 'start': 1, 'end': 1}
        correction = {'schema_version': 1, 'version': 2, 'manifest_sha256': self.corpus.manifest_hash, 'amendments': [
            {'task_id': TASK, 'reason': 'Reviewed alternative behavior at pinned source.', 'source_citation': {k: region[k] for k in ('path', 'start', 'end')},
             'add_alternatives': [{'group_id': task['evidence_groups'][0]['id'], **{k: region[k] for k in ('path', 'start', 'end')}}], 'claim_texts': {}}]}
        revised, corrected = self.corpus.rubric(correction)
        self.assertEqual(len(revised[TASK]['evidence_groups'][0]['alternatives']), len(task['evidence_groups'][0]['alternatives']) + 1)
        with tempfile.TemporaryDirectory() as tmp:
            run = completed_run(Path(tmp), self.corpus)
            items = records([run], self.corpus)
            for item in items:
                item['answer']['raw'] = json.dumps({'explanation': 'Reviewed alternative answer.', 'evidence': [region]})
            before = score.report(items, tasks, oracle, self.corpus)
            key, form = score.prepare_review(items, tasks, oracle)
            with self.assertRaisesRegex(ProbeError, 'review_oracle_changed'):
                score.judgments(items, revised, corrected, key, form)
            report = score.report(items, revised, corrected, self.corpus)
            self.assertEqual(report['trials'][0]['cited'], report['trials'][1]['cited'])
            self.assertEqual(report['oracle']['version'], 2)
            group = task['evidence_groups'][0]['id']
            self.assertEqual(before['trials'][0]['cited']['group_overlap'][group], 0)
            self.assertEqual(report['trials'][0]['cited']['group_overlap'][group], 1)

    def test_repeats_do_not_become_independent_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = completed_run(Path(tmp), self.corpus, repetitions=2)
            tasks, oracle = self.corpus.rubric(None)
            result = score.report(records([run], self.corpus), tasks, oracle, self.corpus)
            self.assertEqual(result['summary']['tasks'], 1)
            self.assertEqual(result['task_variation'][0]['pairs'], 2)
            self.assertEqual(result['task_variation'][0]['independent_tasks'], 1)
            self.assertIn('One task cannot establish a general benefit.', result['limitations'])

    def test_no_judgment_or_unrelated_file_dump_cannot_auto_pass(self):
        task = self.corpus.tasks[TASK]
        blank = judgment_template(task)
        self.assertEqual(correctness(blank, task, True), 'unscored')
        for claim in blank['claims'].values():
            claim.update(supported=True, evidence_supported=False, reason='Dump does not support the claim.')
        blank.update(material_contradiction=False, reason='No contradiction.')
        self.assertEqual(correctness(blank, task, True), 'fail')

    def test_export_omits_free_text_paths_and_account_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = completed_run(Path(tmp), self.corpus)
            tasks, oracle = self.corpus.rubric(None)
            result = score.report(records([run], self.corpus), tasks, oracle, self.corpus)
            row = result['trials'][0]
            secret = '/home/private/account-auth-transcript'
            row['errors'] = [secret]
            row['measurements']['memory_method'] = secret
            row['usage']['account'] = secret
            row['judgment']['reason'] = secret
            row['cited']['ranges'][secret] = [[1, 2]]
            exported = score.shareable(result)
            self.assertNotIn(secret, json.dumps(exported))
            self.assertNotIn(secret, score.markdown(exported))

    def test_oversized_duplicate_json_and_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file'
            path.write_text('{"a":1,"a":2}')
            with self.assertRaises(ProbeError):
                read(path)
            with self.assertRaises(ProbeError):
                read(path, limit=1)
            with self.assertRaisesRegex(ProbeError, 'output_exists'):
                score.write_outputs(Path(tmp), {'x': {}})


class Quota(unittest.TestCase):
    def observation(self):
        return {'before': {'remaining_percent': 90, 'observed_at': '2026-09-18T10:00:00Z', 'bucket': 'weekly', 'reset_at': '2026-09-20T00:00:00Z'},
                'after': {'remaining_percent': 90, 'observed_at': '2026-09-18T10:05:00Z', 'bucket': 'weekly', 'reset_at': '2026-09-20T00:00:00Z'}, 'concurrent_activity': False}

    def test_rounding_reset_concurrency_and_simulation(self):
        observation = self.observation()
        value = quota(observation, False)
        self.assertEqual(value['change_percentage_points'], 0)
        self.assertIn('not zero cost', value['interpretation'])
        self.assertEqual(value['status'], 'comparable')
        self.assertEqual(quota(observation, True)['status'], 'not_comparable')
        observation['concurrent_activity'] = True
        self.assertEqual(quota(observation, False)['status'], 'not_comparable')
        observation['concurrent_activity'] = False
        observation['after']['reset_at'] = '2026-09-27T00:00:00Z'
        self.assertIn('reset_crossing', quota(observation, False)['reasons'])
        self.assertEqual(quota(None, False)['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
