import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paired
import _paired_contract as contract
import _paired_store as store
import _paired_source as source
import _codex_handler as handler
from _paired_trial import TrialAudit, FakeCodex
from _codex_capture import Budget, ProbeError
from _codex_session import definitions
from prepare import initialize
from validate import git_tree

CORPUS = Path(__file__).resolve().parents[1]


def fixture(root):
    src = root / 'source'
    src.mkdir()
    text = b'# README\nfixture copyright\n'
    (src / 'README.md').write_bytes(text)
    import hashlib
    files = [{'path': 'README.md', 'mode': '100644', 'bytes': len(text),
              'git_blob': hashlib.sha1(b'blob ' + str(len(text)).encode() + b'\0' + text).hexdigest()}]
    tree = git_tree(files)
    initialize(src, tree)
    corpus = root / 'corpus'
    (corpus / 'coverage').mkdir(parents=True)
    (corpus / 'coverage/test.json').write_text(json.dumps({'files': files}))
    return src, corpus, {'id': 'test', 'commit': 'a' * 40, 'tree': tree, 'upstream_commit': 'a' * 40, 'upstream_tree': tree}


def tiny_plan():
    result = contract.plan(CORPUS, ['ccx-crossorg-217'])
    result.update(implementation_sha256='a' * 64, grepglint_sha256='b' * 64)
    return result


class Planning(unittest.TestCase):
    def test_all_80_deterministic_equal_prompts(self):
        first = contract.plan(CORPUS, [], True, seed=123)
        second = contract.plan(CORPUS, [], True, seed=123)
        self.assertEqual(first, second)
        self.assertEqual(first['total_trials'], 80)
        self.assertLess(first['artifact_reservation_bytes'], contract.STORE_BYTES)
        for a, b in zip(first['trials'][::2], first['trials'][1::2]):
            self.assertEqual({a['configuration'], b['configuration']}, {'control', 'grepglint'})
            for key in ('source', 'prompt_sha256', 'question', 'task_id', 'repetition', 'partition'):
                self.assertEqual(a[key], b[key])
        self.assertIn('ccx-crossorg-217', {t['task_id'] for t in first['trials']})
        self.assertEqual(sum(t['partition'] == 'development' for t in first['trials']), 16)

    def test_all_planned_source_ids_validate_in_trial_records(self):
        planned = contract.plan(CORPUS, [], True)
        planned.update(implementation_sha256='a' * 64, grepglint_sha256='b' * 64)
        for trial in planned['trials']:
            with self.subTest(task=trial['task_id'], configuration=trial['configuration']):
                record = contract.initial_record(Path('run-all'), planned, trial)
                contract.validate_record(record)

    def test_source_ids_reject_paths_and_oversized_values(self):
        record = json.loads((CORPUS / 'schema-fixtures/successful.json').read_text())
        for source_id in ('', '.', '..', '../numpy', '/numpy', 'numpy/source', 'numpy\\source', 'a' * 101):
            with self.subTest(source_id=source_id), self.assertRaisesRegex(ProbeError, 'invalid_source_identity'):
                record['source']['id'] = source_id
                contract.validate_record(record)

    def test_reject_bad_selection_and_oversized_plans(self):
        for args in [([], False, 1), (['unknown'], False, 1), (['ccx-crossorg-217'] * 2, False, 1),
                     (['ccx-crossorg-217'], False, 0), ([], True, 3), ([], True, 11)]:
            with self.subTest(args=args), self.assertRaises(ProbeError):
                contract.plan(CORPUS, *args)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(paired.main([]), 0)
        self.assertIn('No account access', ' '.join(output.getvalue().split()))

    def test_catalog_only_adds_grepglint(self):
        catalog = {'tools': [{'name': 'search', 'use_when': 'REAL', 'returns': 'results',
                              'follow_up': 'read', 'side_effects': 'index'}]}
        control = contract.tools('control', catalog)
        treatment = contract.tools('grepglint', catalog)
        self.assertEqual(control, treatment[:-1])
        self.assertIn('REAL', treatment[-1]['description'])
        self.assertEqual(control, [])
        self.assertEqual(treatment[-1]['name'], 'grepglint_search')


class SourceAndAnswer(unittest.TestCase):
    def test_full_source_identity_and_post_edit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            src, corpus, identity = fixture(Path(directory))
            before = source.verify(src, identity, corpus, Budget())
            self.assertEqual(before, {**source.verify(src, identity, corpus, Budget()), 'seconds': before['seconds']})
            (src / 'README.md').write_text('changed\n')
            with self.assertRaisesRegex(ProbeError, 'source_content_mismatch'):
                source.verify(src, identity, corpus, Budget())

    def test_unknown_paths_mode_changes_config_and_history(self):
        for change in ('extra', 'mode', 'config', 'link'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                src, corpus, identity = fixture(Path(directory))
                if change == 'extra':
                    (src / 'secret').write_text('extra')
                elif change == 'mode':
                    (src / 'README.md').chmod(0o755)
                elif change == 'config':
                    (src / '.git/config').write_text('[core]\n fsmonitor = bad\n')
                else:
                    (src / 'escape').symlink_to('/etc/passwd')
                with self.assertRaises(ProbeError):
                    source.verify(src, identity, corpus, Budget())

    def test_answer_contract_does_not_grade_or_expand_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            src, _, _ = fixture(Path(directory))
            value = {'explanation': 'Unverified factual claim.', 'evidence': [{'path': 'README.md', 'start': 2, 'end': 2}]}
            raw = json.dumps(value)
            self.assertEqual(contract.answer(raw, src), {'status': 'valid', 'raw': raw, 'parsed': value})
            for path, end in [('../secret', 2), ('/etc/passwd', 2), ('README.md', 30)]:
                value['evidence'][0].update(path=path, end=end)
                self.assertEqual(contract.answer(json.dumps(value), src)['status'], 'malformed')
            self.assertEqual(contract.answer(None, src)['status'], 'missing')
            self.assertEqual(contract.answer('x' * (contract.ANSWER_BYTES + 1), src)['status'], 'oversized')

    def test_search_arguments_paths_and_regex_are_controlled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data.py').write_text('x\n')
            (root / 'escape').symlink_to('/etc')
            with patch.object(handler, 'SOURCE', root), patch.object(handler, 'command', return_value=(0, '', '')) as command:
                handler.execute('text_search', {'query': '^x|y$', 'regex': True, 'path': 'data.py'})
                self.assertEqual(command.call_args.args[0][-3:], ['--', '^x|y$', 'data.py'])
                self.assertNotIn('--fixed-strings', command.call_args.args[0])
                for value in ('../x', '/etc', 'escape/passwd', '.git/config', '-x'):
                    with self.assertRaises((ValueError, OSError)):
                        handler.execute('text_search', {'query': 'x', 'path': value})
                for arguments in ({'query': 'x', 'regex': 'true'}, {'query': 'x', 'args': ['--follow']}):
                    with self.assertRaises(ValueError):
                        handler.execute('text_search', arguments)

    def test_returned_excerpt_is_not_whole_chunk(self):
        result = {'ok': True, 'result': {'success': True, 'content': json.dumps({'results': [{
            'path': 'x', 'start_line': 1, 'end_line': 60, 'snippet_start_line': 12, 'snippet_end_line': 15}]})}}
        self.assertEqual(contract.returned_ranges('grepglint_search', result),
                         [{'path': 'x', 'start': 12, 'end': 15, 'partial_excerpt': True}])


class UsageAndFinal(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'audit.jsonl'
        self.path.touch(mode=0o600)
        self.audit = TrialAudit(self.path, Budget())

    def tearDown(self):
        self.audit.close()
        self.directory.cleanup()

    def done(self, response='r1', usage=None):
        self.audit.receive({'method': 'rawResponse/completed', 'params': {'responseId': response, 'usage': usage}}, 'control')

    def test_unknown_usage_and_dedup_do_not_double_count_inclusions(self):
        usage = {'inputTokens': 20, 'cachedInputTokens': 10, 'outputTokens': 8, 'reasoningOutputTokens': 6, 'totalTokens': 28}
        self.done(usage=usage)
        self.done(usage=usage)
        self.assertEqual(self.audit.usage_summary()['counters'], usage)
        self.done('r2')
        result = self.audit.usage_summary()
        self.assertIsNone(result['counters']['inputTokens'])
        self.assertEqual(result['response_count'], 2)
        self.assertFalse(result['complete']['reasoningOutputTokens'])
        self.assertEqual(self.audit.verify('control', [], 2)['status'], 'passed')
        with self.assertRaisesRegex(ProbeError, 'conflicting_provider_usage'):
            self.done(usage={'inputTokens': 1})

    def test_missing_required_response_still_fails(self):
        with self.assertRaisesRegex(ProbeError, 'missing_raw_response'):
            self.audit.verify('control', [])

    def test_final_reasoning_separation_and_duplicate_identity(self):
        self.audit.receive({'method': 'rawResponseItem/completed', 'params': {'item': {'type': 'reasoning', 'text': 'private'}}}, 'control')
        script = FakeCodex('control', self.audit)
        final = script.final()[0]
        event = {'method': 'rawResponseItem/completed', 'params': {'item': final}}
        self.audit.receive(event, 'control')
        self.audit.receive(event, 'control')
        self.assertEqual(len(self.audit.final_messages), 1)
        self.assertNotIn('private', next(iter(self.audit.final_messages.values())))

    def test_stream_limit_keeps_partial_artifact(self):
        self.audit.budget.limit = 100
        with self.assertRaisesRegex(ProbeError, 'capture_limit'):
            self.audit.record('test', {'text': 'x' * 100}, 'control')
        self.assertEqual(self.path.stat().st_size, 0)


class DurableArtifacts(unittest.TestCase):
    def test_reservation_partial_validation_no_expiry_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = tiny_plan()
            run = store.create(root, planned)
            self.assertEqual(store.validate_run(run)['status'], 'incomplete')
            self.assertEqual(store.owned(run)['reserved_bytes'], planned['artifact_reservation_bytes'])
            store.seal(run)
            with patch('time.time', return_value=time.time() + 365 * 86400):
                another = store.create(root, planned)
            self.assertTrue(run.exists())
            self.assertTrue(another.exists())
            store.cleanup(run)
            self.assertFalse(run.exists())

    def test_capacity_and_unknown_or_edited_artifacts_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = tiny_plan()
            run = store.create(root, planned)
            store.seal(run)
            with patch.object(store, 'STORE_BYTES', 1):
                with self.assertRaises(ProbeError):
                    store.create(root, planned)
            (run / 'run.json').write_text('{}')
            with self.assertRaisesRegex(ProbeError, 'edited_or_unsealed'):
                store.cleanup(run)
            self.assertTrue(run.exists())
            (run / 'unrelated').write_text('keep')
            with self.assertRaisesRegex(ProbeError, 'contents_changed'):
                store.cleanup(run)
            self.assertEqual((run / 'unrelated').read_text(), 'keep')

    def test_active_execution_and_unsealed_cleanup_refused(self):
        with store.execution_lock():
            with self.assertRaisesRegex(ProbeError, 'another_paired_run'):
                with store.execution_lock():
                    pass
        with tempfile.TemporaryDirectory() as directory:
            planned = tiny_plan()
            run = store.create(Path(directory), planned)
            with self.assertRaises(ProbeError):
                store.cleanup(run)

    def test_artifacts_private_and_live_provenance_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            planned = tiny_plan()
            run = store.create(Path(directory), planned)
            for file in run.iterdir():
                self.assertEqual(file.stat().st_mode & 0o077, 0)
            record = store.read(run / 't0001.json')
            record['simulation'] = False
            with self.assertRaisesRegex(ProbeError, 'cannot_be_live'):
                contract.validate_record(record)


class ScorerAndStress(unittest.TestCase):
    def test_schema_fixtures_and_export_omit_local_payloads(self):
        for path in (CORPUS / 'schema-fixtures').glob('*.json'):
            contract.validate_record(json.loads(path.read_text()))
        with tempfile.TemporaryDirectory() as directory:
            planned = tiny_plan()
            run = store.create(Path(directory), planned)
            record = store.read(run / 't0001.json')
            record['question'] = '/private/SECRET'
            record['usage']['raw_account_payload'] = 'SECRET'
            record['usage']['quota'] = {'credentials': 'SECRET'}
            record['measurements']['unknown'] = 'SECRET'
            record['errors'] = ['SECRET']
            store.save(run, 't0001.json', record)
            self.assertNotIn('SECRET', json.dumps(paired.export(run)))

    def test_sustained_disposable_queries_and_edits(self):
        from paired_stress import measure
        result = measure(epochs=6, queries=80)
        self.assertEqual(result['queries_and_edits'], 480)
        self.assertLess(result['controller_python_peak_bytes'], 8 * 1024 * 1024)
        self.assertLess(result['maximum_epoch_disk_bytes'], 64 * 1024)
        self.assertEqual(result['final_fixture_disk_bytes'], 0)


class PublicationAndValidation(unittest.TestCase):
    def test_trial_publication_failure_at_each_rename_keeps_complete_record(self):
        original = os.replace
        for failed_step in range(1, 5):
            with self.subTest(step=failed_step), tempfile.TemporaryDirectory() as directory:
                run = store.create(Path(directory), tiny_plan())
                before = store.read(run / 't0001.json')
                updated = {**before, 'state': 'attempted'}
                count = 0
                def replace(src, dst):
                    nonlocal count
                    count += 1
                    if count == failed_step:
                        raise OSError('interrupted publication')
                    return original(src, dst)
                with patch.object(store.os, 'replace', side_effect=replace):
                    with self.assertRaises(OSError):
                        store.save(run, 't0001.json', updated)
                self.assertIn(store.read(run / 't0001.json'), [before, updated])
                contents = {p.name: p.read_bytes() for p in run.iterdir()}
                self.assertEqual(store.validate_run(run)['status'], 'incomplete')
                self.assertEqual(contents, {p.name: p.read_bytes() for p in run.iterdir()})
                with self.assertRaises(ProbeError):
                    store.cleanup(run)

    def test_partial_staging_write_and_seal_failure_preserve_readable_run(self):
        for operation in ('data', 'seal'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                run = store.create(Path(directory), tiny_plan())
                previous = (run / 't0001.json').read_bytes()
                original = store.write
                def fail(path, value):
                    if path.name == ('.data.next' if operation == 'data' else '.ownership.next'):
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.write(fd, b'{partial')
                        os.close(fd)
                        raise OSError('disk write failed')
                    return original(path, value)
                with patch.object(store, 'write', side_effect=fail), self.assertRaises(OSError):
                    if operation == 'data':
                        record = store.read(run / 't0001.json')
                        store.save(run, 't0001.json', {**record, 'state': 'attempted'})
                    else:
                        store.seal(run)
                self.assertEqual((run / 't0001.json').read_bytes(), previous)
                self.assertEqual(store.validate_run(run)['status'], 'incomplete')

    def test_initialization_failure_does_not_publish_or_change_existing_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = tiny_plan()
            run = store.create(root, planned)
            store.seal(run)
            previous = {p.name: p.read_bytes() for p in run.iterdir()}
            original = store.write
            def fail(path, value):
                if path.name == 't0001.json':
                    raise OSError('initialization interrupted')
                return original(path, value)
            with patch.object(store, 'write', side_effect=fail), self.assertRaises(OSError):
                store.create(root, planned)
            pending = next(root.glob('initializing-run-*'))
            self.assertEqual(store.validate_run(pending)['status'], 'incomplete')
            self.assertEqual(previous, {p.name: p.read_bytes() for p in run.iterdir()})
            with self.assertRaisesRegex(ProbeError, 'unfinished_initialization'):
                store.create(root, planned)

    def test_completed_record_rejects_malformed_answers_missing_identities_and_bad_counts(self):
        valid = json.loads((CORPUS / 'schema-fixtures/successful.json').read_text())
        contract.validate_record(valid)
        for path in (CORPUS / 'schema-fixtures/invalid').glob('*.json'):
            with self.subTest(path=path.name), self.assertRaises(ProbeError):
                contract.validate_record(json.loads(path.read_text()))
        for key in (*contract.IDENTITY_HASHES, 'reported_model', 'catalog_sha256'):
            bad = copy.deepcopy(valid)
            del bad[key]
            with self.subTest(key=key), self.assertRaises(ProbeError):
                contract.validate_record(bad)


    def test_audit_digest_counts_results_usage_and_final_answer_are_reconciled(self):
        from _paired_trial import validate_audit
        for change in ('sha256', 'records', 'calls', 'tools', 'answer'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 't0001.jsonl'
                path.touch(mode=0o600)
                record = json.loads((CORPUS / 'schema-fixtures/successful.json').read_text())
                audit = TrialAudit(path, Budget())
                session = record['configuration']
                audit.receive({'method': 'rawResponseItem/completed', 'params': {'item': {
                    'id': 'final', 'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                    'content': [{'type': 'output_text', 'text': record['answer']['raw']}]}}}, session)
                audit.receive({'method': 'rawResponse/completed', 'params': {'responseId': 'r1', 'usage': {}}}, session)
                record['audit'].update(sha256=audit.sha.hexdigest(), records=audit.sequence, calls=0)
                record['usage'] = audit.usage_summary()
                audit.close()
                self.assertTrue(validate_audit(path, record, Budget()))
                if change in ('records', 'calls'):
                    record['audit'][change] += 1
                elif change == 'sha256':
                    record['audit'][change] = 'f' * 64
                elif change == 'tools':
                    record['tools'] = [{'result_record': 99}]
                else:
                    record['answer']['raw'] = '{}'
                with self.assertRaises(ProbeError):
                    validate_audit(path, record, Budget())


if __name__ == '__main__':
    unittest.main()
