"""Reuse the E1/E2 standard-library process and bundle primitives, not its design."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE / 'shared'
if not SHARED.is_dir():
    SHARED = HERE.parents[1] / 'gate-performance' / 'harness'
sys.path.append(str(SHARED))
from common import (canonical, child, digest, files, json_equal, read_json, seal, sha,
                    timestamp, utc, verify, write_json)  # noqa: E402
from execution import defer_interrupts, execute, succeeded  # noqa: E402

SEED = 'weavegate-e5-v1-20261019'
PROTOCOL_SHA256 = 'e2d1b47e705665772fca53d3f53f209778f4790af8b6c269c8e113379fbd8959'
ARMS = ('B0', 'B1', 'B2', 'T-R', 'T-E')
CAPS = dict(zip(ARMS, (120, 600, 900, 600, 900)))
WORKLOADS = {
    'matching': ('spring-matching', 'concurrent-assign', 'active-assignment-is-unique',
                 'concurrent-assign', 'MatchingApplication', 6, 'sch_7dcb74b1e506'),
    'quota': ('spring-quota', 'concurrent-reserve', 'quota-is-conserved',
              'overlap', 'QuotaApplication', 70, 'sch_71c5162c990b'),
    'job': ('spring-job-claim', 'complete-and-reassign', 'completion-requires-current-lease',
            'overlap', 'JobClaimApplication', 70, 'sch_739dbe2cd669'),
}
STATUSES = ('complete', 'quality_failed', 'operational_failed', 'budget_exhausted',
            'preparation_failed', 'unsupported', 'not_started')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def order():
    blocks = []
    rows = (('B0', 'B1', 'T', 'B2'), ('B1', 'B2', 'B0', 'T'),
            ('B2', 'T', 'B1', 'B0'), ('T', 'B0', 'B2', 'B1'))
    def hashed(parts, suffix):
        return sha('|'.join((SEED, *parts, suffix)).encode())
    for workload in WORKLOADS:
        for variant in ('vulnerable', 'fixed'):
            for cache in ('cold', 'warm'):
                cell = (workload, variant, cache)
                ids = ['%02d' % n for n in range(1, 11)]
                for rank, block in enumerate(sorted(ids, key=lambda b: (hashed((*cell, b), 'arm-order'), b))):
                    blocks.append(((*cell, block), rows[rank % 4]))
    slots = []
    for key, arms in sorted(blocks, key=lambda b: (hashed(b[0], 'sequence'), b[0])):
        for arm in arms:
            subarms = (('T-R', 'T-E') if int(key[3]) % 2 else ('T-E', 'T-R')) if arm == 'T' else (arm,)
            for subarm in subarms:
                slots.append(dict(zip(('workload', 'variant', 'cache', 'block'), key),
                                  arm=subarm, id='-'.join((*key, subarm)),
                                  ordinal=len(slots) + 1, attempt=1, cap_seconds=CAPS[subarm]))
    return slots


def inventory(root):
    return {str(p.relative_to(root)): digest(p) for p in files(root)}


def check_inventory(root, expected):
    require(inventory(root) == expected, 'retained evidence changed: ' + str(root))


def command_valid(spec):
    require(isinstance(spec, dict) and isinstance(spec.get('argv'), list) and spec['argv'], 'command argv required')
    require(all(isinstance(a, str) and '\0' not in a for a in spec['argv']), 'invalid argv')
    require(Path(spec['argv'][0]).is_absolute(), 'absolute executable required')
    require(Path(spec['argv'][0]).is_file(), 'executable file required')
    require(isinstance(spec.get('cwd'), str) and Path(spec['cwd']).is_absolute(), 'absolute cwd required')
    require(isinstance(spec.get('env'), dict) and all(isinstance(k, str) and k and '=' not in k
            and isinstance(v, str) and '\0' not in k + v for k, v in spec['env'].items()), 'allowlisted env required')
    require(type(spec.get('timeout_seconds')) in (int, float) and spec['timeout_seconds'] > 0, 'positive timeout required')


def live_pins(manifest):
    for name, expected in manifest['pins'].items():
        require(Path(name).is_absolute() and Path(name).is_file(), 'missing live pin: ' + name)
        require(digest(name) == expected, 'live pin changed: ' + name)


def candidates(workload):
    """Independently check the Go-exported lexicographic inventory and IDs."""
    import json
    points = (['after_read_request', 'before_insert_assignment'] if workload == 'matching'
              else ['before_read', 'after_read', 'before_write', 'after_commit'])
    result = []
    def walk(nexts, steps):
        if nexts == [len(points)] * 2:
            encoded = json.dumps(steps, separators=(',', ':')).encode()
            result.append({'id': 'sch_' + sha(encoded)[:12], 'steps': steps})
        for i in range(2):
            if nexts[i] < len(points):
                following = nexts[:]; following[i] += 1
                walk(following, steps + [{'worker': 'w%d' % (i+1), 'point': points[nexts[i]]}])
    walk([0, 0], [])
    return result
