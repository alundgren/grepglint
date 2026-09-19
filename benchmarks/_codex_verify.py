"""Offline stock-client verification; account turns require the separate smoke mode."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

from _codex_artifacts import create, export, save, seal
from _codex_audit import Audit, CALL_LIMIT, ARGUMENT_BYTES, RESPONSE_BYTES
from _codex_capture import Budget, Child, FRAME_LIMIT, TOTAL_LIMIT, ProbeError, json_value
from _codex_isolation import (CLIENT_SHA256, JAVASCRIPT_HOST_SHA256, FREE_RESERVE, MEMORY_BYTES, TASKS,
                              UnsupportedHost, cgroup_limits, check_user_manager, diagnostic_command,
                              file_hash, prerequisites, service_command)
from _codex_session import BASE, QUEUE_LIMIT, local_session
from codex_preflight import (CLIENT_VERSION, EFFORT, MARKERS, MODEL, ProbeCancelled,
                             cancellation_signals, exclusive_probe, run_probe, write_receipt)
from prepare import command

CONTRACT = 'stock-codex-local-v2'


def next_action(receipt):
    if receipt['status'] == 'passed':
        return 'Run the explicit human smoke check.'
    errors = receipt.get('errors', [])
    if 'systemd_user_manager_unavailable' in errors or 'verification_service_unavailable' in errors:
        return 'Use a working systemd user manager that can start transient services, then rerun verification.'
    if any(error.startswith('cgroup_') or error == 'aggregate_resource_limits_not_enforced' for error in errors):
        return 'Enable cgroup v2 memory, CPU and task controllers for the systemd user manager, then rerun verification.'
    if 'handler_namespace_unavailable' in errors:
        return 'Check Bubblewrap, user namespaces and --size support on this host, then rerun verification.'
    if any(error.startswith('landlock_') or error == 'seccomp_unavailable' for error in errors):
        return 'Use a Linux kernel that permits Landlock and seccomp restrictions, then rerun verification.'
    return 'Inspect the failed check and documented host prerequisites; do not start a provider turn.'


def source_hash(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            content = b'link:' + os.readlink(path).encode()
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        result.update(relative + b'\0' + content + b'\0')
    return result.hexdigest()


def prepare_fixture(root):
    source = root / 'source'
    source.mkdir(mode=0o700)
    (source / 'example.py').write_text('def example():\n    return 1\n')
    (source / 'AGENTS.md').write_text(MARKERS['project_instruction'] + '\n')
    (source / 'execute.sh').write_text('#!/bin/sh\nprintf forbidden > /cache/repository-executed\n')
    (source / 'execute.sh').chmod(0o755)
    outside = root / 'outside'
    outside.mkdir(mode=0o700)
    for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
        (outside / name).write_text('PRIVATE_' + name.upper() + '_SENTINEL\n')
    (source / 'escape').symlink_to(outside / 'source')
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    for args in (['init', '--quiet', '--template='], ['add', '.'],
                 ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                  'commit', '--quiet', '-m', 'Prepared fixture']):
        command(['git', '-c', 'core.hooksPath=/dev/null', *args], source, timeout=3, env=env)
    return source


def resource_fixtures(root, budget):
    before = (root / 'memory.events').read_text()
    allocation = subprocess.Popen([sys.executable, '-I', '-c',
        'x=bytearray(1100*1024*1024); print(len(x))'], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        allocation.wait(timeout=min(6, budget.remaining()))
    finally:
        if allocation.poll() is None:
            allocation.kill()
            allocation.wait()
    after = (root / 'memory.events').read_text()
    def counters(text):
        return {line.split()[0]: int(line.split()[1]) for line in text.splitlines()}
    memory = counters(after)['oom_kill'] > counters(before)['oom_kill'] and allocation.returncode == -9
    before_cpu = counters((root / 'cpu.stat').read_text())
    code = 'import time; end=time.process_time()+0.3\nwhile time.process_time()<end: pass'
    children = [subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
    try:
        for child in children:
            child.wait(timeout=min(3, budget.remaining()))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
    after_cpu = counters((root / 'cpu.stat').read_text())
    cpu = after_cpu['nr_throttled'] > before_cpu['nr_throttled'] and all(c.returncode == 0 for c in children)
    if not memory or not cpu:
        raise ProbeError('resource_enforcement_fixture_failed')
    return {'allocation_oom_denied': memory, 'concurrent_cpu_loops_throttled': cpu,
            'allocation_requested_bytes': 1100 * 1024 * 1024,
            'memory_peak_including_fixture': int((root / 'memory.peak').read_text()),
            'cpu_usec': after_cpu['usage_usec'] - before_cpu['usage_usec']}


def implementation_hash():
    sha = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted([root / 'codex_preflight.py', root / 'prepare.py',
                        root / 'verification-evidence.json', *root.glob('_codex_*.py')]):
        sha.update(path.name.encode() + b'\0' + path.read_bytes())
    return sha.hexdigest()


def receipt_template():
    return {'schema_version': 2, 'contract': CONTRACT, 'status': 'incomplete',
        'inference_performed': False, 'client': {'version': CLIENT_VERSION, 'binary_sha256': CLIENT_SHA256,
            'javascript_host_sha256': JAVASCRIPT_HOST_SHA256,
            'source_commit': '6b9826e3aa83b1a5947db50f4332cb9c65f1b340'},
        'implementation_sha256': implementation_hash(), 'sessions': [],
        'local_isolation': {'status': 'incomplete'}, 'tool_policy': {'status': 'incomplete'},
        'audit': {'status': 'incomplete'},
        'provider_verification': {'status': 'pending', 'provider': 'chatgpt',
            'reason': 'local_request_construction_does_not_verify_account_provider'},
        'limits': {'deadline_seconds': 60, 'frame_bytes': FRAME_LIMIT, 'total_bytes': TOTAL_LIMIT,
            'tool_calls': CALL_LIMIT, 'argument_bytes': ARGUMENT_BYTES, 'response_bytes': RESPONSE_BYTES,
            'handler_queue': QUEUE_LIMIT, 'handler_concurrency': 1, 'clients': 1,
            'aggregate_memory_bytes': MEMORY_BYTES, 'aggregate_swap_bytes': 0,
            'aggregate_cpu_cores': 1, 'processes_and_threads': TASKS,
            'cache_tmpfs_bytes': 896 * 1024 ** 2, 'client_tmpfs_bytes': 64 * 1024 ** 2,
            'free_disk_reserve_bytes': FREE_RESERVE, 'retention_seconds': 3600, 'retained_runs': 4}}


def verify_worker(binary, grepglint, run):
    budget = Budget(50)
    receipt = receipt_template()
    audit = None
    root = None
    started = time.monotonic()
    try:
        receipt['worker_started'] = True
        save(run, receipt)
        cg, limits = cgroup_limits()
        prerequisites(binary, run)
        version = Child([str(binary), '--version'], run, {'PATH': '/usr/bin:/bin'}, budget)
        try:
            if version.line().decode().strip() != CLIENT_VERSION:
                raise ProbeError('client_version_mismatch')
        finally:
            version.close()
        receipt['client']['pin_verified'] = True
        receipt['local_isolation']['enforced_cgroup'] = limits
        receipt['local_isolation']['resource_fixtures'] = resource_fixtures(cg, budget)
        audit = Audit(run / 'audit.jsonl', budget)
        with tempfile.TemporaryDirectory(prefix='grepglint-verify-') as directory:
            root = Path(directory)
            source = prepare_fixture(root)
            before = source_hash(source)
            config = root / 'configuration'
            config.mkdir(mode=0o700)
            (config / 'base.md').write_text(BASE)
            catalog = json_value(command([str(grepglint), 'tools', '--json'], source, timeout=3))
            receipt['grepglint_sha256'] = file_hash(grepglint)
            receipt['grepglint_catalog_sha256'] = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
            for treatment in ('control', 'grepglint'):
                session = local_session(binary, grepglint, source, config, catalog, treatment, audit, budget)
                if source_hash(source) != before:
                    raise ProbeError('source_changed')
                session.update(source_sha256=before, source_unchanged=True, cache='fresh_private_tmpfs')
                receipt['sessions'].append(session)
                save(run, receipt)
            # Keep the original diagnostic's real positive contamination controls.
            contamination = []
            for treatment in ('control', 'grepglint'):
                fixture = root / ('contaminated-' + treatment)
                fixture.mkdir(mode=0o700)
                result = run_probe(binary, fixture, treatment, True, budget,
                    launcher=lambda args, env: diagnostic_command(binary, fixture, args, env))
                if (result.get('request_status') != 'unsupported' or not result.get('hook_ran')
                        or not all(result.get('sentinels', {}).get(name) for name in
                                   ('global_instruction', 'skill', 'hook', 'configuration', 'outside_source'))):
                    raise ProbeError('contamination_not_detected')
                contamination.append(result)
            receipt['contamination_controls'] = contamination
        first, second = receipt['sessions']
        if (first['configuration_sha256'] != second['configuration_sha256']
                or first['prompt_sha256'] != second['prompt_sha256']
                or not first['cache_identity'] or not second['cache_identity']
                or first['cache_identity'] == second['cache_identity']
                or set(second['observed']['names']) - set(first['observed']['names']) != {'grepglint_search'}):
            raise ProbeError('configuration_pair_mismatch')
        receipt['local_isolation']['status'] = 'passed'
        receipt['tool_policy'] = {'status': 'passed', 'source_evidence': 'benchmarks/verification-evidence.json',
                                  'same_configuration_and_prompt': True, 'only_grepglint_added': True}
        receipt['audit'] = {'status': 'passed', 'sha256': audit.sha.hexdigest(),
                            'records': audit.sequence, 'calls': len(audit.calls),
                            'outer_cell_association': 'unavailable'}
        receipt['checks_completed'] = True
    except ProbeCancelled:
        receipt['errors'] = ['cancelled']
    except UnsupportedHost as error:
        receipt.update(status='unsupported', errors=[str(error)])
        receipt['local_isolation']['status'] = 'unsupported'
    except ProbeError as error:
        receipt['errors'] = [str(error)]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        receipt['errors'] = ['verification_process_failure']
    finally:
        if audit:
            audit.close()
        receipt['captured_bytes'] = budget.used
        receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
        receipt['cleanup'] = {'temporary_files_removed': root is None or not root.exists(),
                              'owned_client_groups_stopped': False}
        save(run, receipt)
        seal(run)
    return receipt


def launch(binary, grepglint, run):
    unit = 'grepglint-verify-' + secrets.token_hex(12)
    budget = Budget(56)
    child = None
    service_requested = False
    receipt = receipt_template()
    try:
        prerequisites(binary, run)
        check_user_manager()
        if not grepglint.is_file():
            raise ProbeError('grepglint_binary_required')
        args = [sys.executable, str(Path(__file__).resolve()), '--worker', str(run),
                '--codex', str(binary), '--grepglint', str(grepglint)]
        receipt['runtime_unit'] = unit
        save(run, receipt)
        service_requested = True
        child = Child(service_command(unit, args, seconds=52), run, os.environ.copy(), budget)
        status = json_value(child.line())
        if status.get('status') not in ('passed', 'incomplete', 'unsupported'):
            raise ProbeError('invalid_worker_status')
        receipt = json_value((run / 'receipt.json').read_bytes())
        if not receipt.get('worker_started'):
            raise ProbeError('worker_receipt_not_saved')
    except ProbeCancelled:
        receipt = json_value((run / 'receipt.json').read_bytes())
        receipt.update(status='incomplete', errors=['cancelled'])
    except UnsupportedHost as error:
        receipt.update(status='unsupported', errors=[str(error)])
        receipt['local_isolation']['status'] = 'unsupported'
    except (ProbeError, OSError) as error:
        if child:
            receipt = json_value((run / 'receipt.json').read_bytes())
        if isinstance(error, ProbeError) and str(error) == 'client_exited' and not receipt.get('worker_started'):
            receipt.update(status='unsupported', errors=['verification_service_unavailable'])
            receipt['local_isolation']['status'] = 'unsupported'
        else:
            receipt.update(status='unsupported' if child is None else 'incomplete',
                           errors=[str(error) if isinstance(error, ProbeError) else 'linux_launch_unavailable'])
    finally:
        if child:
            child.close()
        # The service is owned by this invocation, even if its transport died.
        receipt['runtime_unit'] = unit
        stopped = not service_requested
        if service_requested:
            try:
                subprocess.run(['systemctl', '--user', 'stop', unit],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
                active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
                stopped = active.returncode in (3, 4)
            except (OSError, subprocess.TimeoutExpired):
                stopped = False
        receipt.setdefault('cleanup', {})['service_stopped'] = stopped
        receipt['cleanup']['owned_client_groups_stopped'] = stopped
        if not receipt['cleanup']['service_stopped']:
            receipt['status'] = 'incomplete'
            receipt.setdefault('errors', []).append('service_cleanup_failed')
        elif receipt.get('checks_completed') and not receipt.get('errors') and all(receipt['cleanup'].values()):
            receipt['status'] = 'passed'
        save(run, receipt)
        seal(run)
    return receipt


def main(argv=None):
    if argv and argv[0] == 'smoke':
        from _codex_smoke import main as smoke_main
        return smoke_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=['verify', 'smoke'], default='verify')
    parser.add_argument('--codex', type=Path, default=Path(shutil.which('codex') or 'codex'))
    parser.add_argument('--grepglint', type=Path, default=Path(__file__).resolve().parents[1] / 'target/release/grepglint')
    parser.add_argument('--artifacts', type=Path, default=Path.home() / '.local/state/grepglint/codex-verification')
    parser.add_argument('--export', type=Path)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run = None
    try:
        with cancellation_signals():
            if args.worker:
                receipt = verify_worker(args.codex.resolve(), args.grepglint.resolve(), args.worker)
                print(json.dumps({'status': receipt['status']}), flush=True)
                return 0
            with exclusive_probe():
                run = create(args.artifacts.resolve())
                receipt = launch(args.codex.resolve(), args.grepglint.resolve(), run)
                if args.export:
                    write_receipt(args.export, export(receipt))
        print(json.dumps({'status': receipt['status'], 'receipt': str(run / 'receipt.json'),
                          'errors': receipt.get('errors', []), 'chatgpt_verification': 'pending',
                          'next_action': next_action(receipt)}))
        return {'passed': 0, 'unsupported': 2, 'incomplete': 3}[receipt['status']]
    except (ProbeError, OSError, ProbeCancelled) as error:
        print(json.dumps({'status': 'incomplete', 'errors': [str(error) if isinstance(error, ProbeError)
                          else 'artifact_or_launch_failure'], 'inference_performed': False,
                          'receipt': str(run / 'receipt.json') if run else None,
                          'next_action': 'Inspect the failed check and any retained receipt. '
                                         'Use an owned private directory for artifacts and exports.'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
