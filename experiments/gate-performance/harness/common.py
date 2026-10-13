"""Shared bundle primitives; Python standard library only."""
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

SEED = 'weavegate-e1-e2-v1-20261013'
PROTOCOLS = ('README.md', 'e1-gate.md', 'e2-ci.md')
INVENTORIES = ('arms', 'inputs', 'runtime', 'environment', 'observation', 'sources')
QUALITY = ('outcome', 'assertions', 'schedules', 'repetitions', 'normalized_evidence',
           'isolation', 'lifecycle', 'resources', 'environment')
E1_RESOURCES = ('cpu_seconds', 'peak_memory_bytes', 'containers_created',
                'peak_containers', 'disk_write_bytes', 'artifact_bytes', 'cache_bytes')
E2_RESOURCES = ('runner_minutes', 'peak_runner_concurrency')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    return sha(Path(path).read_bytes())


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def json_equal(left, right):
    """Compare JSON facts recursively without Python's bool/number coercion."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right  # Both are JSON numbers.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    return left == right


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def write_json(path, value):
    Path(path).write_bytes(canonical(value))


def utc():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be a UTC string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('timestamp must use UTC: ' + value)
    return parsed


def child(root, name):
    """Bundle paths are portable, relative and never traverse symlinks."""
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
        raise ValueError('unsafe relative path: ' + str(name))
    result = Path(root)
    for part in path.parts:
        result /= part
        if result.is_symlink():
            raise ValueError('symlink in bundle: ' + str(name))
    return result


def files(root):
    result = []
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in bundle: ' + str(path))
        if path.is_file():
            result.append(path)
    return result


def seal(root):
    root = Path(root)
    entries = {str(p.relative_to(root)): digest(p) for p in files(root)
               if p != root / 'SHA256SUMS'}
    # JSON avoids ambiguous filenames in line-oriented checksum formats.
    write_json(root / 'SHA256SUMS', entries)


def verify(root):
    root = Path(root)
    expected = read_json(root / 'SHA256SUMS')
    actual = {str(p.relative_to(root)): digest(p) for p in files(root)
              if p != root / 'SHA256SUMS'}
    if expected != actual:
        raise ValueError('bundle checksum inventory differs')


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def counter(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def write_csv(path, rows, fields):
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def sampling(experiment, phase):
    if experiment == 'E1':
        example = ['example-fixed-replay', 'example-vulnerable-replay',
                   'example-fixed-explore', 'example-vulnerable-explore']
        if phase == 'pilot':
            return {w: [4, 4] for w in example}
        if phase == 'confirmation':
            return dict({w: [10, 10, 10] for w in example},
                        **{w: [4, 3, 3] for w in
                           ['spring-fixed-replay', 'spring-vulnerable-replay']})
        if phase == 'adoption-cold':
            return {'example-fixed-replay': [4, 4]}
    if experiment == 'E2':
        if phase == 'pilot':
            return {'external-acceptance': [3, 3]}
        if phase == 'confirmation':
            return {'external-acceptance': [4, 4, 4]}
    raise ValueError('unregistered experiment/phase')


def order(manifest):
    rows = []
    for workload, counts in sorted(manifest['strata'].items()):
        for date_index, count in enumerate(counts, 1):
            slot = '%02d' % date_index
            ids = ['%03d' % i for i in range(1, count + 1)]
            def key(block, suffix):
                return sha('|'.join([manifest['seed'], manifest['experiment'],
                                    manifest['phase'], workload, slot, block, suffix]).encode())
            half = count // 2 + (count % 2 if date_index % 2 else 0)
            first = set(sorted(ids, key=lambda b: (key(b, 'order'), b))[:half])
            for block in sorted(ids, key=lambda b: (key(b, 'sequence'), b)):
                rows.append({'block': '%s-%s-%s' % (workload, slot, block),
                             'block_id': block, 'workload': workload, 'date_slot': slot,
                             'date': manifest['dates'][date_index - 1],
                             'arms': ['baseline', 'candidate'] if block in first else
                                     ['candidate', 'baseline']})
    return rows
