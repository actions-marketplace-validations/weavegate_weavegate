"""Reuse the E1/E2 standard-library process and bundle primitives, not its design."""
import os
import sys
import importlib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE / 'shared'
if not SHARED.is_dir():
    SHARED = HERE.parents[1] / 'gate-performance' / 'harness'
SHARED = SHARED.resolve()
SHARED_MODULES = ('common', 'plan', 'redaction', 'execution')
sys.path.insert(0, str(SHARED))
# Check cached modules before execution can import them transitively, too.
for name in SHARED_MODULES:
    module = sys.modules.get(name)
    if module is not None and Path(getattr(module, '__file__', '') or '').resolve() != SHARED / (name + '.py'):
        raise ValueError('shared module location differs: ' + name)
for name in SHARED_MODULES:
    module = importlib.import_module(name)
    if Path(getattr(module, '__file__', '') or '').resolve() != SHARED / (name + '.py'):
        raise ValueError('shared module location differs: ' + name)
# The shared harness also has a tool.py. Restore runner import precedence once
# its required modules (including redaction's plan dependency) are loaded.
sys.path.pop(0)
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
    if manifest.get('phase') == 'measurement':
        measurement_dependencies(manifest)


def file_dependencies(spec, pins, label, inputs=()):
    """Check visible file inputs; dynamic imports/subprocesses remain audited pins."""
    values = [(v, True, False) for v in spec['argv']]
    values += [(v, k in ('CLASSPATH', 'PYTHONPATH'), False) for k, v in spec['env'].items()]
    values += [(v, True, True) for v in inputs]
    for value, include_directory, required in values:
        require(isinstance(value, str) and (value or not required), label + ' dependency path required')
        # Options, classpath lists and Java lib/* wildcards are input paths.
        # Runtime receipt/output placeholders are not frozen inputs.
        for name in value.split('=', 1)[-1].split(os.pathsep):
            if not name or '{' in name:
                require(not required, label + ' dependency must be a frozen path')
                continue
            path = Path(os.path.abspath(Path(spec['cwd']) / name))
            if path.name == '*' and path.parent.is_dir():
                dependencies = sorted(p for p in path.parent.iterdir() if p.suffix.lower() == '.jar' and p.is_file())
            else:
                dependencies = (files(path) if include_directory and path.is_dir()
                                else [path] if path.is_file() else [])
            require(not required or path.is_file() or path.is_dir(), label + ' dependency missing: ' + name)
            require(all(str(p) in pins for p in dependencies), label + ' file dependency is unpinned: ' + name)


def measurement_dependencies(manifest):
    for workload, variants in manifest['commands'].items():
        for variant, commands in variants.items():
            expected = manifest['inputs'][workload][variant]
            target = expected['config']['Target']
            require(target.get('DB') == manifest['environment']['mysql_image'],
                    'effective config image differs from registered MySQL image')
            external = target['SUT']['External']
            schema = target['Schema']
            inputs = [external['Java'], external['JAR'], schema['Migrations'], schema['Seed']]
            for arm, command in commands.items():
                file_dependencies(command, manifest['pins'], workload + '/' + variant + '/' + arm, inputs)
                if arm.startswith('B'):
                    classpath_identity(command, expected)
            fixture_identity(expected)
    for name, hook in manifest['hooks'].items():
        file_dependencies(hook, manifest['pins'], name)


BUSINESS_PREFIX = 'io/github/weavegate/fixture/'
SDK_PREFIX = 'io/github/weavegate/sdk/'


def fixture_identity(expected):
    """Bind declared business/SDK identities to the pinned product application."""
    jar = expected['config']['Target']['SUT']['External']['JAR']
    require(digest(jar) == expected['jar_sha256'], 'fixture JAR identity differs')
    with zipfile.ZipFile(jar) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'duplicate fixture JAR entries')
        prefix = 'BOOT-INF/classes/'
        business = {n[len(prefix):]: sha(archive.read(n)) for n in names
                    if n.startswith(prefix + BUSINESS_PREFIX) and n.endswith('.class')}
        require(business and business == expected['business_classes'], 'fixture business-class identity differs')
        sdk = [n for n in names if n.startswith('BOOT-INF/lib/weavegate-spring-') and n.endswith('.jar')]
        require(len(sdk) == 1 and sha(archive.read(sdk[0])) == expected['sdk_sha256'],
                'fixture SDK identity differs')


def classpath_identity(command, expected):
    """Check every protected class source, rejecting shadow copies as well as drift."""
    argv = command['argv']
    values = []
    for i, arg in enumerate(argv):
        if arg in ('--classpath', '-classpath'):
            require(i+1 < len(argv), 'classpath value required')
            values.append(argv[i+1])
        elif arg.startswith(('--classpath=', '-classpath=')):
            values.append(arg.split('=',1)[1])
    require(len(values) == 1 and values[0], 'one explicit baseline classpath required')
    paths = []
    for name in values[0].split(os.pathsep):
        require(name and '{' not in name, 'classpath must use frozen paths')
        path = Path(command['cwd']) / name
        if path.name == '*':
            require(path.parent.is_dir(), 'classpath wildcard directory missing')
            paths.extend(sorted(p for p in path.parent.iterdir() if p.suffix.lower() == '.jar'))
        else: paths.append(path)
    classes, sdk_sources = {}, []
    for path in paths:
        selected = {}
        if path.is_dir():
            for prefix in (BUSINESS_PREFIX, SDK_PREFIX):
                for file in (path / prefix).rglob('*.class'):
                    selected[str(file.relative_to(path))] = digest(file)
        else:
            require(path.is_file() and zipfile.is_zipfile(path), 'classpath JAR missing/invalid: ' + str(path))
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.endswith('.class') or not any(p in name for p in (BUSINESS_PREFIX, SDK_PREFIX)): continue
                    require(name.startswith((BUSINESS_PREFIX, SDK_PREFIX)), 'versioned/nested identity class in classpath')
                    require(name not in selected, 'duplicate classpath identity class: ' + name)
                    selected[name] = sha(archive.read(name))
        require(not set(classes).intersection(selected), 'duplicate business/SDK class in classpath')
        classes.update(selected)
        if any(n.startswith(SDK_PREFIX) for n in selected):
            require(path.is_file(), 'SDK must use the registered JAR')
            sdk_sources.append(digest(path))
    require({n:h for n,h in classes.items() if n.startswith(BUSINESS_PREFIX)} == expected['business_classes']
            and expected['business_classes'], 'classpath business-class identity differs')
    require(sdk_sources == [expected['sdk_sha256']], 'classpath SDK identity differs')


def cleanup_dependencies(manifest):
    """Check literal file inputs as well as the declared transitive inventory.

    Hooks are trusted adapters: imports and dynamically opened files still need
    explicit pins and audit. Do not silently omit a script or config in argv/env.
    """
    hook = manifest['hooks']['cleanup']
    pins = hook.get('execution_pins')
    require(isinstance(pins, list) and pins and all(isinstance(p, str) for p in pins),
            'cleanup dependency subset required')
    require(len(pins) == len(set(pins)) and set(pins) <= set(manifest['pins'])
            and hook['argv'][0] in pins, 'cleanup dependencies must include executable and be pinned')
    file_dependencies(hook, pins, 'cleanup')
    return pins


def save_progress(bundle, consumed, active=None, stopped=None):
    checkpoint = bundle / 'progress'
    registration = None
    if checkpoint.exists():
        verify(checkpoint)
        registration = read_json(checkpoint / 'checkpoint.json')['registration']
    checkpoint.mkdir(exist_ok=True)
    write_json(checkpoint / 'checkpoint.json', {
        'slots': consumed, 'active': active, 'stopped': stopped,
        'registration': registration,
        'complete': len(consumed) == len(order()) and active is None and stopped is None,
    })
    seal(checkpoint)


def retained_registration(bundle, progress):
    value = progress['registration']
    path = bundle / 'registration.json'
    if value is not None:
        require(path.is_file(), 'retained registration missing')
        require(json_equal(read_json(path), value), 'registration differs from sealed checkpoint')
    return value


def retained_progress(bundle, manifest, slots):
    """Fail closed on gaps, lost suffixes, crashes, or continuation after a stop."""
    paths = list((bundle / 'slots').iterdir())
    consumed = {p.name for p in paths}
    require(consumed == {s['id'] for s in slots[:len(consumed)]},
            'consumed slots are not an ordered prefix')
    for path in paths: verify(path)
    verify(bundle / 'progress')
    progress = read_json(bundle / 'progress' / 'checkpoint.json')
    registration = retained_registration(bundle, progress)
    if manifest['phase'] == 'measurement' and (consumed or progress['active'] is not None):
        require(registration is not None, 'registration missing from consumed checkpoint')
    require(progress['slots'] == {p.name: digest(p / 'SHA256SUMS') for p in paths},
            'retained slots differ from progress checkpoint')
    require(progress['active'] is None, 'unfinished checkpoint; no replacement attempts')
    stopped = None
    for index, slot in enumerate(slots[:len(consumed)]):
        state = read_json(bundle / 'slots' / slot['id'] / 'slot.json')
        require(state.get('slot') == slot, 'slot identity differs')
        if state.get('unsupported'):
            require(state['unsupported'] == manifest.get('unsupported', {}).get(slot['workload'] + '/' + slot['arm'])
                    and not (bundle / 'slots' / slot['id'] / 'run.command.json').exists(),
                    'unsupported slot was executed/unregistered')
        else:
            require(state.get('cleanup_proven') is True or state.get('stop'),
                    'allocation cleanup unproven without sealed stop checkpoint')
        if state.get('stop'):
            stopped = state['stop']
            require(stopped.get('slot') == slot['id'] and stopped.get('reason')
                    and index == len(consumed) - 1, 'slot follows sealed stop checkpoint')
    require(json_equal(progress['stopped'], stopped), 'stop differs from sealed checkpoint')
    require(progress['complete'] is (len(consumed) == len(slots) and stopped is None),
            'completion checkpoint differs from retained slots')
    if (bundle / 'stopped.json').exists():
        require(json_equal(read_json(bundle / 'stopped.json'), stopped), 'stop marker differs from sealed checkpoint')
    return progress


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
