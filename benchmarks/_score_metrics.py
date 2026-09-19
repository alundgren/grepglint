"""Deterministic retrieval diagnostics and explicit human judgments."""
from datetime import datetime
import math

from _codex_capture import ProbeError


def require(value, message):
    if not value:
        raise ProbeError(message)


def valid_range(item, files):
    if not isinstance(item, dict) or not isinstance(item.get('path'), str):
        return False
    file = files.get(item['path'])
    return bool(file and file['mode'] in ('100644', '100755')
                and type(item.get('start')) is int and type(item.get('end')) is int
                and 1 <= item['start'] <= item['end'] <= file['lines'])


def merge(ranges):
    result = []
    for start, end in sorted(set(ranges)):
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def coverage(items, groups, files):
    require(isinstance(items, list) and len(items) <= 10000, 'too_many_evidence_ranges')
    paths = {i['path'] for i in items if isinstance(i, dict) and isinstance(i.get('path'), str)}
    good = [i for i in items if valid_range(i, files)]
    ranges = {p: merge((i['start'], i['end']) for i in good if i['path'] == p)
              for p in {i['path'] for i in good}}
    accepted = {a['path'] for g in groups for a in g['alternatives']}
    fractions, file_hits = {}, 0
    for group in groups:
        if not group['required']:
            continue
        file_hits += any(a['path'] in ranges for a in group['alternatives'])
        fractions[group['id']] = max(sum(max(0, min(end, a['end']) - max(start, a['start']) + 1)
            for start, end in ranges.get(a['path'], [])) / (a['end'] - a['start'] + 1)
            for a in group['alternatives'])
    count = len(fractions)
    return {'file_precision': len(set(ranges) & accepted) / len(paths) if paths else 0,
            'file_recall': file_hits / count if count else 0,
            'matched_files': len(set(ranges) & accepted), 'distinct_paths': len(paths),
            'valid_files': len(ranges), 'invalid_ranges': len(items) - len(good),
            'lines': sum(end - start + 1 for rs in ranges.values() for start, end in rs),
            'region_recall': sum(v > 0 for v in fractions.values()) / count if count else 0,
            'group_overlap': fractions, 'ranges': ranges}


def judgment_template(task):
    return {'claims': {c['id']: {'supported': None, 'evidence_supported': None, 'reason': ''}
                       for c in task['claims']}, 'material_contradiction': None, 'reason': ''}


def correctness(judgment, task, eligible):
    require(isinstance(judgment, dict) and set(judgment) == {'claims', 'material_contradiction', 'reason'}, 'invalid_judgment')
    require(isinstance(judgment['claims'], dict) and set(judgment['claims']) == {c['id'] for c in task['claims']}, 'judgment_claims_mismatch')
    decisions = []
    for claim in judgment['claims'].values():
        require(isinstance(claim, dict) and set(claim) == {'supported', 'evidence_supported', 'reason'}, 'invalid_claim_judgment')
        for field in ('supported', 'evidence_supported'):
            require(claim[field] is None or type(claim[field]) is bool, 'invalid_claim_decision')
            decisions.append(claim[field])
        require(isinstance(claim['reason'], str) and len(claim['reason']) <= 4000, 'invalid_judgment_reason')
        require(all(v is None for v in (claim['supported'], claim['evidence_supported'])) or claim['reason'].strip(), 'judgment_requires_reason')
    contradiction = judgment['material_contradiction']
    require(contradiction is None or type(contradiction) is bool, 'invalid_contradiction_decision')
    require(isinstance(judgment['reason'], str) and len(judgment['reason']) <= 4000, 'invalid_judgment_reason')
    require(contradiction is None or judgment['reason'].strip(), 'judgment_requires_reason')
    if not eligible or contradiction is None or any(v is None for v in decisions):
        return 'unscored'
    return 'pass' if all(decisions) and not contradiction else 'fail'


def quota(observation, simulation):
    if observation is None:
        return {'status': 'unavailable', 'before': None, 'after': None, 'change_percentage_points': None}
    require(isinstance(observation, dict) and set(observation) == {'before', 'after', 'concurrent_activity'}, 'invalid_quota_observation')
    require(observation['concurrent_activity'] is None or type(observation['concurrent_activity']) is bool, 'invalid_quota_activity')
    times = []
    for entry in (observation['before'], observation['after']):
        require(isinstance(entry, dict) and set(entry) == {'remaining_percent', 'observed_at', 'bucket', 'reset_at'}, 'invalid_quota_snapshot')
        value = entry['remaining_percent']
        require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100, 'invalid_remaining_percent')
        require(entry['bucket'] == 'weekly', 'unsupported_quota_bucket')
        pair = []
        for key in ('observed_at', 'reset_at'):
            require(isinstance(entry[key], str) and len(entry[key]) <= 40, 'invalid_quota_timestamp')
            try:
                stamp = datetime.fromisoformat(entry[key].replace('Z', '+00:00'))
            except ValueError as error:
                raise ProbeError('invalid_quota_timestamp') from error
            require(stamp.tzinfo is not None, 'quota_timestamp_requires_timezone')
            pair.append(stamp)
        times.append(pair)
    require(times[1][0] >= times[0][0], 'quota_observations_out_of_order')
    before, after = observation['before'], observation['after']
    delta = before['remaining_percent'] - after['remaining_percent']
    reasons = []
    if simulation:
        reasons.append('simulation_not_attributable')
    if times[0][1] != times[1][1] or times[0][0] >= times[0][1] or times[1][0] >= times[0][1]:
        reasons.append('reset_crossing')
    if observation['concurrent_activity'] is not False:
        reasons.append('concurrent_or_unknown_account_activity')
    if delta < 0:
        reasons.append('remaining_percentage_increased')
    return {**observation, 'status': 'not_comparable' if reasons else 'comparable', 'reasons': reasons,
            'change_percentage_points': delta,
            'interpretation': 'no observable percentage-point change; not zero cost' if delta == 0 else 'observed change only; no token or price conversion'}
