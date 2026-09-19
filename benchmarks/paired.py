#!/usr/bin/env python3
"""Plan paired discovery trials. No account access without explicit ChatGPT readiness or execution."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from _codex_audit import encoded
from _codex_capture import Budget, Child, ProbeError, json_value
from _codex_isolation import (UnsupportedHost, cgroup_limits, check_user_manager, file_hash,
                              prerequisites, service_command)
from _paired_session import local_session
from _paired_skill import SKILL_SOURCE, skill_enabled
from codex_preflight import (cancellation_signals, exclusive_probe, ProbeCancelled,
                             write_receipt, digest, MODEL, EFFORT)
from _paired_contract import (CONTRACT, LIMITS, MAX_TRIALS, plan, tools, answer,
                              validate_record, GUIDANCE_MODES, trial_instructions)
from _paired_source import verify as verify_source
from _paired_trial import TrialAudit, FakeCodex
import _paired_store as store

CORPUS = Path(__file__).resolve().parent


def implementation_hash():
    result = __import__('hashlib').sha256()
    for path in sorted([CORPUS / 'paired.py', CORPUS / 'codex_preflight.py', CORPUS / 'prepare.py',
                        CORPUS / 'validate.py', SKILL_SOURCE, *CORPUS.glob('_codex_*.py'), *CORPUS.glob('_paired_*.py')]):
        result.update(path.name.encode() + b'\0' + path.read_bytes())
    return result.hexdigest()


def trial_worker(run, trial, args, catalog, cg, *, live=None):
    record = store.read(run / (trial['trial_id'] + '.json'))
    record['state'] = 'attempted'
    store.save(run, trial['trial_id'] + '.json', record)
    budget = Budget(LIMITS['trial_seconds'], total=LIMITS['events_output_bytes'])
    audit = None
    started = time.monotonic()
    before = None
    source = args.snapshots / trial['source']['id']
    try:
        before = verify_source(source, trial['source'], CORPUS, budget)
        record['source_before'] = before
        audit = TrialAudit(run / (trial['trial_id'] + '.jsonl'), budget, simulation=live is None, native=True)
        configuration = trial['configuration']
        definitions = tools(configuration, catalog)
        from _paired_proof import CorpusProbe
        script = (CorpusProbe(configuration, audit, source) if getattr(args, 'prove', False) or args.fake_scenario == 'adversarial'
                  else FakeCodex(configuration, audit, args.fake_scenario))
        result = live.session(trial, audit, budget, record) if live else local_session(
            args.codex, args.grepglint, source, catalog, configuration, audit, budget,
            script, trial['question'], before['sha256'], base=trial_instructions(trial), skill=skill_enabled(trial))
        record['session'] = result
        record['reported_model'] = result['reported_model']
        record['reported_effort'] = result['reported_effort']
        record['catalog_sha256'] = digest(encoded(definitions))
        record['configuration_sha256'] = result['configuration_sha256']
        record['answer'] = answer(next(iter(audit.final_messages.values()), None), source)
        if record['answer']['status'] != 'valid':
            raise ProbeError('invalid_final_answer')
        record['state'] = 'completed'
    except ProbeCancelled:
        record.update(state='failed', errors=['cancelled'])
    except (ProbeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        record.update(state='failed', errors=[str(error) if isinstance(error, ProbeError) else 'trial_process_failure'])
    finally:
        if before:
            try:
                after = verify_source(source, trial['source'], CORPUS, budget)
                record['source_after'] = after
                if after['sha256'] != before['sha256']:
                    raise ProbeError('source_changed')
            except (ProbeError, OSError, ValueError, subprocess.SubprocessError):
                record['state'] = 'failed'
                record['errors'].append('source_postcheck_failed')
        if audit:
            if not audit.output.closed:
                audit.close()
            if audit.final_messages and record['answer']['raw'] is None:
                record['answer'] = answer(next(iter(audit.final_messages.values())), source)
            if audit.final_status == 'oversized':
                record['answer'] = {'status': 'oversized', 'raw': None, 'parsed': None}
            record['usage'] = audit.usage_summary()
            record['tools'] = audit.tools
            record['grepglint'] = audit.observations()
            record['audit'] = {'status': 'passed' if record['state'] == 'completed' else 'incomplete',
                'sha256': audit.sha.hexdigest(), 'records': audit.sequence, 'calls': len(audit.calls),
                'path': trial['trial_id'] + '.jsonl'}
        record['measurements'] = {'trial_wall_seconds': time.monotonic() - started,
            'captured_bytes': budget.used, 'source_verification_seconds': before['seconds'] if before else None,
            'source_preparation_seconds': None, 'source_preparation_method': 'separate command; not timed as model usage',
            'memory_peak_bytes': int((cg / 'memory.peak').read_text()),
            'memory_method': 'cgroup memory.peak; aggregate high-water since run service start, includes cache pages',
            'tool_seconds': sum(t['elapsed_seconds'] for t in record.get('tools', []))}
        validate_record(record)
        store.save(run, trial['trial_id'] + '.json', record)
    return record


def worker(args):
    run = args.worker
    status = store.read(run / 'run.json')
    try:
        if os.environ.get('RUNTIME_DIRECTORY') != status.get('runtime_directory'):
            raise ProbeError('native_temporary_directory_unavailable')
        cg, limits = cgroup_limits(memory=LIMITS['aggregate_memory_bytes'])
        prerequisites(args.codex, run)
        if file_hash(args.grepglint) != store.read(run / 'plan.json')['grepglint_sha256']:
            raise ProbeError('build_identity_changed')
        status['resource_limits'] = limits
        status['worker_started'] = True
        store.save(run, 'run.json', status)
        planned = store.read(run / 'plan.json')
        budget = Budget(10)
        child = Child([str(args.grepglint), 'tools', '--json'], run, {'PATH': '/usr/bin:/bin'}, budget)
        try:
            data = bytearray()
            while True:
                try:
                    data.extend(child.line() + b'\n')
                except ProbeError as error:
                    if str(error) != 'client_exited':
                        raise
                    break
            catalog = json_value(data)
        finally:
            child.close()
        if args.live_action:
            if args.provider != 'chatgpt':
                raise ProbeError('explicit_chatgpt_selection_required')
            from _paired_live import worker as live_worker
            status = live_worker(args, catalog, cg)
        else:
            for trial in planned['trials']:
                result = trial_worker(run, trial, args, catalog, cg)
                if result['state'] != 'completed':
                    status['errors'] = result['errors']
                    break
            else:
                status['status'] = 'completed'
    except ProbeCancelled:
        status = store.read(run / 'run.json')
        status.update(status='incomplete', errors=['cancelled'])
    except (ProbeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        status = store.read(run / 'run.json')
        status.update(status='incomplete', errors=[str(error) if isinstance(error, ProbeError) else 'worker_failure'])
    finally:
        store.save(run, 'run.json', status)
    print(json.dumps({'status': status['status']}), flush=True)
    return 0


def launch(args, run):
    count = len(store.read(run / 'plan.json')['trials'])
    seconds = count * (LIMITS['trial_seconds'] + LIMITS['cleanup_seconds']) + 30
    unit = 'grepglint-paired-' + secrets.token_hex(12)
    temporary = Path('/run/user') / str(os.getuid()) / unit
    status = store.read(run / 'run.json')
    status['runtime_unit'] = unit
    status['runtime_directory'] = str(temporary)
    store.save(run, 'run.json', status)
    child = None
    requested = False
    errors = []
    stopped = False
    try:
        prerequisites(args.codex, run)
        check_user_manager()
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(run),
                   '--snapshots', str(args.snapshots), '--codex', str(args.codex),
                   '--grepglint', str(args.grepglint), '--fake-scenario', args.fake_scenario]
        if args.prove:
            command += ['--prove']
        if args.live_action:
            command += ['--provider', 'chatgpt', '--live-action', args.live_action, '--proof', str(args.proof), '--auth', str(args.auth)]
            if args.confirm:
                command += ['--confirm', args.confirm]
        requested = True
        child = Child(service_command(unit, command, seconds=seconds, memory=LIMITS['aggregate_memory_bytes'], runtime=unit), run, os.environ.copy(), Budget(seconds + 5))
        json_value(child.line())
    except ProbeCancelled:
        errors = ['cancelled']
    except (ProbeError, OSError) as error:
        errors = [str(error) if isinstance(error, ProbeError) else 'service_launch_failed']
    finally:
        try:
            if child:
                child.close()
        finally:
            if requested:
                try:
                    subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=3)
                    result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], capture_output=True, timeout=2)
                    stopped = result.returncode in (3, 4)
                except (OSError, subprocess.TimeoutExpired):
                    stopped = False
            else:
                stopped = True
        status = store.read(run / 'run.json')
        status['errors'].extend(errors)
        status['cleanup'] = {'service_stopped': stopped, 'temporary_files_removed': not temporary.exists()}
        if not stopped:
            status['errors'].append('service_cleanup_failed')
        if temporary.exists():
            status['errors'].append('native_temporary_cleanup_failed')
        if status['errors'] or not status.get('worker_started'):
            status['status'] = 'incomplete'
        for trial in store.read(run / 'plan.json')['trials']:
            record = store.read(run / (trial['trial_id'] + '.json'))
            status['inference_performed'] |= record['inference_performed']
            if record['state'] == 'attempted':
                record.update(state='failed', errors=errors or ['worker_interrupted'])
                store.save(run, trial['trial_id'] + '.json', record)
        store.save(run, 'run.json', status)
        if stopped and status['status'] != 'ready':
            store.seal(run)
    return status


def export(run):
    import re
    status = store.read(run / 'run.json')
    planned = store.read(run / 'plan.json')
    def safe_id(value):
        return value if isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,100}', value) else None
    def sha(value, length=64):
        return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) else None
    result = {'schema_version': planned['schema_version'], 'contract': CONTRACT, 'simulation': planned['simulation'],
              'inference_performed': status['inference_performed'], 'run_id': safe_id(run.name),
              'status': store.validate_run(run)['status'],
              'seed': planned['seed'] if type(planned['seed']) is int else None,
              'manifest_sha256': sha(planned['manifest_sha256']), 'trials': []}
    for trial in planned['trials']:
        record = store.read(run / (trial['trial_id'] + '.json'))
        validate_record(record)
        item = {key: safe_id(record[key]) for key in ('trial_id', 'pair_id', 'task_id', 'partition',
            'configuration', 'state')}
        item.update({key: record[key] for key in ('repetition', 'order') if type(record[key]) is int})
        item['guidance'] = record.get('guidance', 'description-only')
        item['source'] = {key: sha(record['source'].get(key), 40) for key in ('commit', 'tree', 'upstream_commit', 'upstream_tree')}
        item['source']['id'] = safe_id(record['source'].get('id'))
        for key in ('client_sha256', 'grepglint_sha256', 'implementation_sha256'):
            item[key] = sha(record.get(key))
        item['answer_status'] = record['answer']['status'] if record['answer']['status'] in ('missing', 'valid', 'malformed', 'oversized') else 'missing'
        item['calls'] = record['audit'].get('calls') if type(record['audit'].get('calls')) is int else None
        usage = record['usage']
        counters = usage.get('counters') or {}
        item['usage'] = {'simulation': record['simulation'], 'quota': None, 'counters': {key: counters.get(key)
            if type(counters.get(key)) is int and counters[key] >= 0 else None
            for key in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')}}
        allowed = ('trial_wall_seconds', 'captured_bytes', 'source_verification_seconds',
                   'source_preparation_seconds', 'memory_peak_bytes', 'tool_seconds')
        if not record['simulation']:
            from _paired_live import public_quota
            item['usage']['quota'] = public_quota(record.get('quota_observations', []))
        item['measurements'] = {key: value for key, value in record['measurements'].items()
                                if key in allowed and type(value) in (int, float) and value >= 0}
        result['trials'].append(item)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--task', action='append', default=[])
    selection.add_argument('--all', action='store_true')
    parser.add_argument('--repetitions', type=int)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--guidance', choices=GUIDANCE_MODES,
                        help='Select description-only or optional persistent exploration guidance for treatment.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--prove', action='store_true', help='Run selected corpus capability probes offline.')
    parser.add_argument('--provider', choices=['chatgpt'])
    parser.add_argument('--readiness', type=Path, metavar='PROOF_RUN')
    parser.add_argument('--execute', type=Path, metavar='RUN')
    parser.add_argument('--confirm')
    parser.add_argument('--auth', type=Path, default=Path.home() / '.codex/auth.json')
    parser.add_argument('--proof', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--live-action', choices=['readiness', 'execute'], help=argparse.SUPPRESS)
    parser.add_argument('--fake-scenario', choices=['success', 'non-use', 'malformed', 'missing', 'oversized', 'transport-loss', 'adversarial'], default='success')
    parser.add_argument('--snapshots', type=Path)
    parser.add_argument('--codex', type=Path, default=Path(shutil.which('codex') or 'codex'))
    parser.add_argument('--grepglint', type=Path, default=CORPUS.parent / 'target/release/grepglint')
    parser.add_argument('--artifacts', type=Path, default=Path.home() / '.local/state/grepglint/paired-runs')
    parser.add_argument('--validate', type=Path, metavar='RUN')
    parser.add_argument('--validate-record', type=Path, metavar='JSON')
    parser.add_argument('--cleanup', type=Path, metavar='RUN')
    parser.add_argument('--export', type=Path, metavar='FILE')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run = None
    try:
        with cancellation_signals():
            if args.worker:
                with store.execution_lock():
                    return worker(args)
            if args.validate_record:
                with args.validate_record.open('rb') as stream:
                    data = stream.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ProbeError('trial_metadata_limit_exceeded')
                value = json_value(data)
                print(json.dumps({'state': validate_record(value), 'simulation': value['simulation']}))
                return 0
            if args.validate:
                result = store.validate_run(args.validate)
                if args.export:
                    write_receipt(args.export, export(args.validate))
                print(json.dumps(result))
                return 0 if result['status'] == 'completed' else 3
            if args.cleanup:
                with exclusive_probe(), store.execution_lock():
                    status = store.read(args.cleanup / 'run.json')
                    if status.get('status') == 'ready' and status.get('cleanup', {}).get('service_stopped') is True:
                        from _paired_attempts import Attempts
                        ledger = Attempts()
                        try:
                            ledger.append({'action': 'cancelled', 'run_id': args.cleanup.name})
                            store.seal(args.cleanup)
                        finally:
                            ledger.close()
                    store.cleanup(args.cleanup)
                print('Removed owned paired run. Smoke attempt accounting is unchanged.')
                return 0
            if args.readiness or args.execute:
                from _paired_cli import selected_live
                return selected_live(args)
            if args.confirm or args.provider or args.live_action:
                raise ProbeError('select_explicit_readiness_or_execution')
            if not args.task and not args.all:
                parser.print_help()
                return 0
            planned = plan(CORPUS, args.task, args.all, args.repetitions if args.repetitions is not None else 1,
                           args.seed if args.seed is not None else 0, args.guidance or 'description-only')
            planned['implementation_sha256'] = implementation_hash()
            planned['execution'] = 'proof' if args.prove else 'simulation'
            planned['aggregate_limits'] = {
                'wall_seconds': planned['total_trials'] * (LIMITS['trial_seconds'] + LIMITS['cleanup_seconds']) + 30,
                'tool_calls': planned['total_trials'] * LIMITS['tool_calls'],
                'events_output_bytes': planned['total_trials'] * LIMITS['events_output_bytes']}
            planned['grepglint_sha256'] = file_hash(args.grepglint.resolve()) if args.grepglint.is_file() else None
            if args.snapshots:
                args.snapshots = args.snapshots.resolve()
                planned['prepared_sources'] = {}
                budget = Budget(300)
                for trial in planned['trials']:
                    source = trial['source']
                    if source['id'] not in planned['prepared_sources']:
                        planned['prepared_sources'][source['id']] = verify_source(args.snapshots / source['id'], source, CORPUS, budget)
            else:
                planned['prepared_sources'] = 'not_checked; prepare pinned source separately and pass --snapshots before execution'
            if args.dry_run or not (args.fake or args.prove):
                print(json.dumps(planned, indent=2))
                return 0
            if not args.snapshots:
                raise ProbeError('prepared_snapshots_required_no_implicit_fetch')
            args.codex, args.grepglint = args.codex.resolve(), args.grepglint.resolve()
            prerequisites(args.codex, args.artifacts.parent if args.artifacts.parent.exists() else Path.home())
            check_user_manager()
            with exclusive_probe():
                with store.execution_lock():
                    pass
                run = store.create(args.artifacts.absolute(), planned)
                result = launch(args, run)
                if args.export:
                    write_receipt(args.export, export(run))
            print(json.dumps({'status': result['status'], 'simulation': True, 'run': str(run),
                'errors': result['errors'], 'next_action': 'Validate with --validate RUN; inspect failed records locally. No automatic retry. Use --cleanup RUN to reclaim sealed artifacts.'}))
            return 0 if result['status'] == 'completed' else 3
    except (ProbeError, OSError, ValueError, KeyError, ProbeCancelled) as error:
        print(json.dumps({'status': 'incomplete', 'simulation': True, 'inference_performed': False,
            'error': str(error) if isinstance(error, ValueError) else 'artifact_or_launch_failure',
            'run': str(run) if run else None,
            'next_action': 'Inspect the failure and retained artifacts. Prepare source separately; use --cleanup RUN only for an inactive owned run. No retry occurred.'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
