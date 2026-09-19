"""Explicit account readiness and single-confirmation run entry points."""
from _codex_capture import ProbeError
from _codex_isolation import prerequisites, check_user_manager
from _paired_proof import require_proof
from codex_preflight import exclusive_probe, write_receipt
import _paired_store as store


def selected_live(args):
    import json
    from paired import implementation_hash, launch, export
    if args.provider != 'chatgpt':
        raise ProbeError('explicit_chatgpt_selection_required')
    if (bool(args.readiness) == bool(args.execute) or args.fake or args.prove
            or args.task or args.all or args.dry_run or args.repetitions is not None
            or args.seed is not None or args.fake_scenario != 'success'):
        raise ProbeError('select_one_live_action_without_selection_overrides')
    if not args.snapshots:
        raise ProbeError('prepared_snapshots_required_no_implicit_fetch')
    args.codex, args.grepglint = args.codex.resolve(), args.grepglint.resolve()
    args.snapshots, args.auth = args.snapshots.resolve(), args.auth.resolve()
    prerequisites(args.codex, args.snapshots)
    check_user_manager()
    with exclusive_probe():
        with store.execution_lock():
            if args.readiness:
                if args.confirm:
                    raise ProbeError('readiness_is_not_authorization')
                args.proof = args.readiness.resolve()
                require_proof(args.proof, args.codex, args.grepglint, implementation_hash())
                planned = store.read(args.proof / 'plan.json')
                planned.update(schema_version=2, simulation=False, execution='chatgpt')
                run = store.create(args.artifacts.absolute(), planned)
                args.live_action = 'readiness'
            else:
                run = args.execute.resolve()
                owner = store.owned(run)
                status = store.read(run / 'run.json')
                if owner.get('sealed') or status['status'] != 'ready' or not args.confirm:
                    raise ProbeError('unused_ready_run_and_confirmation_required')
                from pathlib import Path
                args.proof = Path(status['proof_run'])
                args.live_action = 'execute'
        try:
            result = launch(args, run)
        except (OSError, ProbeError):
            print(json.dumps({'status': 'incomplete', 'run': str(run),
                'error': 'run_publication_or_cleanup_failed',
                'next_action': 'Preserve this run and its attempt ledger. Inspect the recorded service and publication state; do not replay the confirmation.'}))
            return 3
        if args.export and result['status'] != 'ready':
            write_receipt(args.export, export(run))
    output = {'status': result['status'], 'run': str(run), 'inference_performed': result['inference_performed'],
              'errors': result['errors'], 'next_action': 'Inspect retained artifacts. A stopped run cannot resume; any new run needs a new readiness check and approval.'}
    if result['status'] == 'ready':
        output.update(plan=store.read(run / 'plan.json'), authorization=result['authorization'],
                      quota=result['readiness']['weekly'], previous_attempt_history=result['previous_attempt_history'],
                      next_action='Approve this entire displayed run once by passing its confirmation to --execute RUN --provider chatgpt. This readiness check started no model turn.')
    print(json.dumps(output, indent=2))
    return 0 if result['status'] in ('completed', 'ready') else 3
