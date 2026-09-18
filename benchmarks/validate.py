#!/usr/bin/env python3
"""Validate corpus metadata and cited source bytes offline."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from prepare import PreparationError, safe_path

MAX_JSON_BYTES = 16 * 1024 ** 2
MAX_EVIDENCE_BYTES = 8 * 1024 ** 2


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def read_bytes(root, name, limit=MAX_EVIDENCE_BYTES):
    try:
        relative = safe_path(name)
        path = root / str(relative)
        require(path.resolve().is_relative_to(root.resolve()), f'Artifact leaves corpus: {name}')
        require(not path.is_symlink() and path.is_file(), f'Artifact unavailable: {name}')
        require(path.stat().st_size <= limit, f'Artifact exceeds byte limit: {name}')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        require(len(data) <= limit, f'Artifact exceeds byte limit: {name}')
        return data
    except (OSError, PreparationError) as error:
        raise ValidationError(str(error)) from error


def load(root, name):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    try:
        return json.loads(read_bytes(root, name, MAX_JSON_BYTES), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f'Invalid JSON in {name}: {error}') from error


def sha(data):
    return hashlib.sha256(data).hexdigest()


def task_digest(task):
    return sha(json.dumps({k: v for k, v in task.items() if k != 'review'}, sort_keys=True, separators=(',', ':')).encode())


def partition(tasks):
    result = {}
    for origin, count in [('published', 6), ('authored', 2)]:
        clusters = {}
        for task in tasks:
            if task['origin']['kind'] == origin:
                clusters.setdefault(task['duplicate_cluster'], []).append(task['id'])
        ordered = sorted(clusters, key=lambda c: hashlib.sha256(('corpus-v1:' + c).encode()).hexdigest())
        possibilities = {0: []}
        for cluster in ordered:
            for n, chosen in list(possibilities.items()):
                total = n + len(clusters[cluster])
                if total <= count and total not in possibilities:
                    possibilities[total] = chosen + [cluster]
        require(count in possibilities, 'Duplicate clusters cannot satisfy development allocation')
        dev = {id for c in possibilities[count] for id in clusters[c]}
        for task in tasks:
            if task['origin']['kind'] == origin:
                result[task['id']] = 'development' if task['id'] in dev else 'held_out'
    return result


def git_tree(files):
    root = {}
    for file in files:
        parts = str(safe_path(file['path'])).split('/')
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            require(isinstance(node, dict), 'Inventory path conflicts with a directory')
        require(parts[-1] not in node, 'Duplicate inventory path')
        node[parts[-1]] = (file['mode'], file['git_blob'])
    def digest(node):
        values = []
        for name, value in node.items():
            if isinstance(value, dict):
                mode, oid = '40000', digest(value)
                key = (name + '/').encode()
            else:
                mode, oid = value
                key = name.encode()
            require(mode in ('40000', '100644', '100755', '120000', '160000'), 'Invalid Git mode')
            require(bool(re.fullmatch('[0-9a-f]{40}', oid)), 'Invalid Git object ID')
            values.append((key, mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)))
        data = b''.join(data for _, data in sorted(values))
        return hashlib.sha1(b'tree ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return digest(root)


def artifact(root, record):
    require(isinstance(record, dict) and record.get('path') and record.get('sha256'), 'Missing artifact metadata')
    data = read_bytes(root, record['path'], MAX_JSON_BYTES)
    require(sha(data) == record['sha256'], f'Artifact hash differs: {record["path"]}')
    return data


def validate(root, allow_pending=False, snapshots=None):
    manifest = load(root, 'manifest.json')
    require(manifest.get('version') == 1, 'Unsupported corpus version')
    tasks = manifest.get('tasks')
    require(isinstance(tasks, list) and len(tasks) == 40, 'Corpus requires exactly 40 tasks')
    suite = json.loads(artifact(root, manifest['suite']['artifact']))
    require(manifest['suite']['commit'] == '8d5f3c876c28a5033634facf42c21da9ebc6fcd6', 'Wrong CodeScaleBench revision')
    require(suite['suite_id'] == 'csb-v2-full-validated', 'Wrong suite')
    members = {task['task_id']: task for task in suite['tasks']}
    artifact(root, manifest['suite']['license'])
    artifact(root, manifest['suite']['derived_description_notice'])
    sources = load(root, 'sources.json')
    require(len({s['id'] for s in sources}) == len(sources), 'Duplicate source ID')
    by_source = {}
    for source in sources:
        for field in ('commit', 'tree', 'upstream_commit', 'upstream_tree'):
            require(bool(re.fullmatch('[0-9a-f]{40}', source.get(field, ''))), f'Unresolved source {field}')
        require(source.get('upstream') and source.get('identity_note'), 'Missing source identity')
        require(source.get('licenses'), 'Missing source notices')
        for notice in source['licenses']:
            require(notice.get('license'), 'Missing license identification')
            artifact(root, notice)
        coverage = load(root, 'coverage/' + source['id'] + '.json')
        files = coverage['files']
        require(git_tree(files) == source['tree'], 'Inventory does not reproduce pinned Git tree')
        require(len(files) == source['path_count'] <= 20000, 'Source path count differs or exceeds limit')
        require(sum(f['bytes'] for f in files) == source['source_bytes'] <= 1024 ** 3, 'Source byte count differs or exceeds limit')
        require(coverage['summary']['status'] in ('success', 'failed'), 'Missing actual cold-index outcome')
        require(coverage['summary']['tree'] == source['tree'], 'Coverage is for another tree')
        if coverage['summary']['status'] == 'failed':
            require(coverage['summary'].get('error'), 'Index failure needs a diagnostic')
        pin = json.loads(artifact(root, source['pin_evidence']))
        require(pin['sha'] == source['commit'] and pin['tree']['sha'] == source['tree'], 'Pin evidence differs from source identity')
        by_source[source['id']] = (source, {f['path']: f for f in files})
    seen = set(); reviewed = load(root, 'reviews.json')
    for task in tasks:
        id = task.get('id')
        require(isinstance(id, str) and id and id not in seen, 'Missing or duplicate task ID')
        seen.add(id)
        for key in ('question', 'group', 'language', 'duplicate_cluster', 'adaptation', 'reference_answer'):
            require(isinstance(task.get(key), str) and task[key].strip(), f'{id}: missing {key}')
        require(task['group'] in ('identifier', 'behavior', 'cross_file'), f'{id}: invalid group')
        origin = task['origin'];require(origin['kind'] in ('published', 'authored'), f'{id}: invalid origin')
        if origin['kind'] == 'published':
            require(origin.get('task_id') in members and origin['task_id'] == id, f'{id}: absent suite membership')
            require(origin['suite'] == members[id]['suite'], f'{id}: suite membership differs')
            expected_url = 'https://github.com/sourcegraph/CodeScaleBench/blob/' + manifest['suite']['commit'] + '/benchmarks/' + members[id]['task_dir'] + '/instruction.md'
            require(origin['source_url'] == expected_url, f'{id}: wrong original URL')
            require(origin.get('original_files'), f'{id}: missing original task evidence')
            for file in origin['original_files']:
                artifact(root, file)
            require(origin['original_wording'] in [f['path'] for f in origin['original_files']], f'{id}: missing original wording')
        else:
            require(task['source'] == 'eshop-b4a40872', f'{id}: authored source must be eShop')
        require(task['source'] in by_source, f'{id}: unavailable source')
        source, files = by_source[task['source']]
        groups = task.get('evidence_groups', [])
        require(groups and len({g['id'] for g in groups}) == len(groups), f'{id}: missing or duplicate groups')
        mandatory = set(); paths = set()
        for group in groups:
            require(type(group.get('required')) is bool and group.get('alternatives'), f'{id}: invalid alternative-evidence group')
            require(isinstance(group['alternatives'], list), f'{id}: alternatives must be a list')
            if group['required']:
                mandatory.add(group['id'])
            region_keys = set()
            for region in group['alternatives']:
                path = str(safe_path(region['path']))
                require(path in files, f'{id}: source path unavailable: {path}')
                file = files[path]
                require(file['mode'] in ('100644', '100755'), f'{id}: evidence must be a regular file')
                start, end = region['start'], region['end']
                require(type(start) is int and type(end) is int and 1 <= start <= end <= file['lines'], f'{id}: invalid source range')
                repo = source.get('mirror') or source['upstream']
                expected_url = f'https://github.com/{repo}/blob/{source["commit"]}/{path}#L{start}-L{end}'
                require(region.get('source_url') == expected_url, f'{id}: citation URL differs')
                key=(path,start,end);require(key not in region_keys, f'{id}: duplicate alternative');region_keys.add(key)
                data = read_bytes(root, 'evidence/' + task['source'] + '/' + path)
                require(sha(data) == file['sha256'], f'{id}: source evidence hash differs')
                oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                require(oid == file['git_blob'], f'{id}: evidence differs from pinned Git blob')
                require(sha(b''.join(data.splitlines(keepends=True)[start-1:end])) == region['excerpt_sha256'], f'{id}: excerpt hash differs')
                if snapshots is not None:
                    actual = read_bytes(snapshots / task['source'], path)
                    require(actual == data, f'{id}: prepared source evidence differs')
                if group['required']:
                    paths.add(path)
        require(mandatory, f'{id}: no required evidence')
        claims=task.get('claims', [])
        require(claims and len({c['id'] for c in claims}) == len(claims), f'{id}: missing or duplicate claims')
        used=set()
        for claim in claims:
            require(claim.get('text') and claim.get('evidence_groups'), f'{id}: incomplete claim')
            require(set(claim['evidence_groups']) <= mandatory, f'{id}: claim references absent or optional evidence')
            used.update(claim['evidence_groups'])
        require(used == mandatory, f'{id}: required evidence has no factual claim')
        if task['group'] == 'cross_file':
            require(len(paths) >= 2, f'{id}: cross-file task requires multiple files')
        if not allow_pending:
            require(task.get('review', {}).get('status') == 'accepted', f'{id}: independent review pending')
            record = reviewed.get('tasks', {}).get(id)
            require(record and record.get('task_sha256') == task_digest(task), f'{id}: missing or outdated review evidence')
            require(record.get('reviewer') and record.get('source_audit') and record.get('verdict') == 'accepted', f'{id}: incomplete source audit')
    require(Counter(t['origin']['kind'] for t in tasks) == {'published':30,'authored':10}, 'Origin allocation differs')
    require({t['group'] for t in tasks} == {'identifier','behavior','cross_file'}, 'Missing question group')
    require(sum(t['language'] in ('JavaScript','TypeScript') for t in tasks) >= 2, 'Insufficient JS/TS evidence')
    require(sum(any(Path(r['path']).suffix in ('.js','.jsx','.ts','.tsx','.mjs','.cjs','.mts','.cts')
                    for g in t['evidence_groups'] if g['required'] for r in g['alternatives'])
                for t in tasks) >= 2, 'Insufficient required JS/TS source files')
    require(any(t['language']=='C#' for t in tasks) and any(t['language'] in ('Python','Go') for t in tasks), 'Missing non-JS/TS coverage')
    expected = partition(tasks)
    require(all(t['split'] == expected[t['id']] for t in tasks), 'Partition differs or duplicate cluster leaks')
    clusters = {}
    for task in tasks:
        clusters.setdefault(task['duplicate_cluster'], set()).add(task['split'])
    require(all(len(splits) == 1 for splits in clusters.values()), 'Duplicate cluster leaks across partitions')
    dev = [t['id'] for t in tasks if t['origin']['kind']=='published' and t['split']=='development']
    calibration = min(dev, key=lambda id: hashlib.sha256(('calibration-v1:' + id).encode()).hexdigest())
    require(manifest['partition']['calibration_task'] == calibration, 'Calibration task differs from deterministic rule')
    return {'tasks':len(tasks),'origins':dict(Counter(t['origin']['kind'] for t in tasks)),
            'groups':dict(Counter(t['group'] for t in tasks)), 'languages':dict(Counter(t['language'] for t in tasks)),
            'splits':dict(Counter(t['split'] for t in tasks)), 'sources':len(sources),
            'review_status':'pending allowed' if allow_pending else 'accepted', 'calibration_task':calibration}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=Path(__file__).parent)
    parser.add_argument('--snapshots',type=Path)
    parser.add_argument('--allow-pending-review',action='store_true',help='Authoring check only; does not establish acceptance')
    args=parser.parse_args()
    try:
        print(json.dumps(validate(args.corpus,args.allow_pending_review,args.snapshots),indent=2))
    except (ValidationError,PreparationError,KeyError,TypeError,ValueError) as error:
        parser.exit(1,f'Corpus validation failed: {error}\n')


if __name__=='__main__':
    main()
