"""Shared bundle primitives; Python standard library only."""
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import tempfile
from contextlib import contextmanager
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
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite JSON number: ' + value)
        return number
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs, parse_float=finite_float,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def write_json(path, value):
    write_bytes(path, canonical(value))


def write_bytes(path, data):
    # A reader sees either the previous complete checkpoint or the next one.
    # Replacement also detaches hard-linked retained copies from external files.
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    if not isinstance(name, str) or not name or '\0' in name:
        raise ValueError('unsafe relative path: ' + str(name))
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
        raise ValueError('unsafe relative path: ' + str(name))
    result = Path(root)
    for part in path.parts:
        result /= part
        if result.is_symlink():
            raise ValueError('symlink in bundle: ' + str(name))
    return result


def entries(root):
    """Inventory directories too: even empty entries can change accounting."""
    result = []
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in bundle: ' + str(path))
        if not path.is_file() and not path.is_dir():
            raise ValueError('non-regular entry in bundle: ' + str(path))
        result.append(path)
    return result


def files(root):
    return [path for path in entries(root) if path.is_file()]


def bundle_inventory(root):
    # Null authenticates directory presence/type; files retain SHA-256 values.
    return {str(path.relative_to(root)): None if path.is_dir() else digest(path)
            for path in entries(root)}


def seal(root):
    root = Path(root)
    inventory = bundle_inventory(root)
    inventory.pop('SHA256SUMS', None)
    # JSON avoids ambiguous filenames in line-oriented checksum formats.
    write_json(root / 'SHA256SUMS', inventory)


def verify(root):
    root = Path(root)
    expected = read_json(root / 'SHA256SUMS')
    actual = bundle_inventory(root)
    actual.pop('SHA256SUMS', None)
    if expected != actual:
        raise ValueError('bundle checksum inventory differs')


def positive(value):
    return finite_number(value) and value > 0


def counter(value):
    return finite_number(value) and value >= 0


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def write_csv(path, rows, fields):
    with io.StringIO(newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        write_bytes(path, stream.getvalue().encode())


@contextmanager
def campaign_lock(root, shared=False):
    # The directory inode keeps exclusion intact even if a hook replaces the
    # lock marker. Keep the file lock too for archived format-4 readers.
    root = Path(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another collector or analyzer owns this campaign') from None
        with (root / 'collection.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('another collector or analyzer owns this campaign') from None
            def check_paths():
                if (not os.path.samestat(os.fstat(descriptor), os.stat(root, follow_symlinks=False))
                        or not os.path.samestat(os.fstat(lock.fileno()),
                                               os.stat(root / 'collection.lock', follow_symlinks=False))):
                    raise ValueError('campaign lock pathname replaced')
            check_paths()
            try:
                yield
            finally:
                check_paths()
    finally:
        os.close(descriptor)


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
