"""Reuse the E1/E2 standard-library process and bundle primitives, not its design."""
import os
import sys
import fcntl
import math
import re
import zipfile
import copy
import ctypes
import tempfile
import runpy
from contextlib import contextmanager, ExitStack
from pathlib import Path
from urllib.parse import urlsplit
_imports = runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
                         init_globals={'dependencies': ('fixture_contract', 'snapshot')})
from fixture_contract import FIXTURE_INPUTS, FIXTURE_REVISION, REFERENCE_BUILD
from snapshot import frozen_launch, runtime_identity

HERE = Path(__file__).resolve().parent
SHARED = HERE / 'shared'
if not SHARED.is_dir():
    SHARED = HERE.parents[1] / 'gate-performance' / 'harness'
SHARED = SHARED.resolve()
SHARED_MODULES = _imports['SHARED_MODULES']
sys.path.insert(0, str(SHARED))
# Check cached modules before execution can import them transitively, too.
for name in SHARED_MODULES:
    module = sys.modules.get(name)
    if module is not None and Path(getattr(module, '__file__', '') or '').resolve() != SHARED / (name + '.py'):
        raise ValueError('shared module location differs: ' + name)
for name in SHARED_MODULES:
    module = _imports['load_source'](name, SHARED, 'shared')
    if Path(getattr(module, '__file__', '') or '').resolve() != SHARED / (name + '.py'):
        raise ValueError('shared module location differs: ' + name)
# The shared harness also has a tool.py. Restore runner import precedence once
# its required modules (including redaction's plan dependency) are loaded.
sys.path.pop(0)
from common import (canonical, child, digest, files, json_equal, positive, read_json, seal, sha,
                    timestamp, utc, verify as shared_verify, write_bytes, write_json)  # noqa: E402
from execution import defer_interrupts, execute as shared_execute, succeeded  # noqa: E402

RUNNER_SOURCES = _imports['RUNNER_SOURCES']

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
EXPECTED_DOMAIN_ROWS = {
    'matching': [{'project_request_id': '42', 'active_assignment_count': '2'}],
    'quota': [{'account_id': '42', 'capacity': '1', 'remaining': '0', 'accepted_units': '2'}],
    'job': [{'job_id': '42', 'epoch': '2', 'status': 'DONE', 'completed_epoch': '1'}],
}
STATUSES = ('complete', 'quality_failed', 'operational_failed', 'budget_exhausted',
            'preparation_failed', 'unsupported', 'not_started')
EVIDENCE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def campaign_deviations(manifest):
    value = manifest.get('deviations', [])
    require(isinstance(value, list) and all(isinstance(d, str) and d.strip() for d in value),
            'deviations must be a list of nonempty descriptions')
    return value


def campaign_id(manifest):
    value = manifest.get('campaign_id')
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value),
            'campaign ID must start with an ASCII letter or digit and contain only letters, digits, dot, underscore or hyphen')
    return value


def unsupported_declarations(manifest):
    declarations = manifest.get('unsupported', {})
    require(isinstance(declarations, dict), 'unsupported must be a workload/arm mapping')
    keys = {w + '/' + a for w in WORKLOADS for a in ARMS}
    for key, value in declarations.items():
        require(key in keys, 'unsupported workload/arm is not registered: ' + str(key))
        require(isinstance(value, dict) and set(value) == {'issue_url', 'reason'},
                'unsupported declaration requires issue_url and reason')
        require(isinstance(value['reason'], str) and value['reason'].strip(), 'unsupported reason required')
        url = value['issue_url']
        require(isinstance(url, str) and url and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url),
                'unsupported issue URL invalid')
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == 'https' and parsed.hostname and parsed.path not in ('', '/')
                     and parsed.username is None and parsed.password is None
                     and (parsed.port is None or parsed.port > 0))
        except ValueError:
            valid = False
        require(valid, 'unsupported issue URL must identify an HTTPS issue without credentials')
    return declarations


RESOURCE_COUNTERS = ('cpu_seconds', 'memory_bytes', 'container_creations', 'evidence_bytes', 'cache_bytes')


def unavailable_resources(reason):
    return {name: {'value': None, 'reason': reason} for name in RESOURCE_COUNTERS}


def resource_observations(value):
    """Validate disclosures identically during collection and offline reduction."""
    require(isinstance(value, dict) and set(value) == set(RESOURCE_COUNTERS),
            'resource counter inventory differs')
    def text(record, name):
        return isinstance(record.get(name), str) and bool(record[name].strip())
    for name, record in value.items():
        require(isinstance(record, dict) and 'value' in record, 'resource value required: ' + name)
        observed = record['value']
        if observed is None:
            require(text(record, 'reason'), 'resource null reason required: ' + name)
            require(set(record) == {'value', 'reason'}, 'resource null metadata differs: ' + name)
            continue
        numeric = type(observed) in ((int, float) if name == 'cpu_seconds' else (int,))
        require(numeric and observed >= 0 and (type(observed) is int or math.isfinite(observed)),
                'resource value invalid: ' + name)
        require(text(record, 'scope'), 'resource counter scope required: ' + name)
        fields = {'value', 'scope'}
        if name == 'memory_bytes':
            require(record.get('method') in ('sampled', 'high_water_mark'), 'resource memory method required')
            fields.add('method')
            if record['method'] == 'sampled':
                require(type(record.get('cadence_ms')) is int and record['cadence_ms'] == 100
                        and text(record, 'limitation'), 'resource memory cadence/limitation required')
                fields.update(('cadence_ms', 'limitation'))
        require(set(record) == fields, 'resource counter metadata differs: ' + name)
    return value


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


def verify(root):
    root = Path(root)
    require(not root.is_symlink() and root.is_dir(), 'bundle directory missing or symlink: ' + str(root))
    shared_verify(root)


def frozen_sources(bundle):
    expected = read_json(bundle / 'frozen' / 'manifest.json').get('source_inventory')
    names = set(RUNNER_SOURCES) | {'shared/' + m + '.py' for m in SHARED_MODULES}
    require(isinstance(expected, dict) and set(expected) == names, 'registered source inventory differs')
    _imports['check_source_inventory'](bundle / 'frozen' / 'source')
    for directory, actual, names in (('source', HERE, RUNNER_SOURCES),
                                     ('source/shared', SHARED, tuple(m + '.py' for m in SHARED_MODULES))):
        retained = bundle / 'frozen' / directory
        require({p.name for p in retained.glob('*.py')} == set(names), 'frozen source inventory differs')
        for name in names:
            require(digest(actual / name) == digest(retained / name), 'collector/analyzer source changed after freeze')
            key = str((retained / name).relative_to(bundle / 'frozen' / 'source'))
            require(digest(retained / name) == expected[key], 'registered source changed after freeze')


@contextmanager
def created_child(parent, name, check_parent):
    """Pin a private staging inode before publishing a new directory name.

    mkdir has no FD-returning form. Bootstrap in a private random namespace,
    check its identity and emptiness, and publish the retained inode with
    renameat2(RENAME_NOREPLACE). Never reopen the public name to acquire ownership.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    check_parent()
    temporary = tempfile.mkdtemp(prefix='.e5-new-', dir=Path('/proc/self/fd') / str(parent))
    staging = Path(temporary).name
    descriptor = None
    owned = False
    published = False
    try:
        identity = os.stat(staging, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(staging, flags, dir_fd=parent)
        require(os.path.samestat(identity, os.fstat(descriptor)) and not os.listdir(descriptor),
                'new directory replaced before ownership: ' + name)
        owned = True
        check_parent()
        require(os.path.samestat(os.fstat(descriptor), os.stat(staging, dir_fd=parent, follow_symlinks=False)),
                'staging directory pathname replaced: ' + name)
        publish_directory(parent, staging, name)
        published = True
        def check():
            check_parent()
            try:
                if os.path.samestat(os.fstat(descriptor), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    return
            except OSError:
                pass
            raise ValueError('directory pathname replaced: ' + name)
        check()
        try:
            yield check, Path('/proc/self/fd') / str(descriptor)
        finally:
            check()
    finally:
        # An untrusted/replaced staging pathname is never recursively removed.
        if not published and owned:
            try:
                if os.path.samestat(os.fstat(descriptor), os.stat(staging, dir_fd=parent, follow_symlinks=False)):
                    os.rmdir(staging, dir_fd=parent)
            except OSError:
                pass
        if descriptor is not None: os.close(descriptor)


def publish_directory(parent, source, destination):
    """Linux no-clobber rename; fail closed if the filesystem lacks support."""
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent, os.fsencode(source), parent, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), destination)


@contextmanager
def created_directory(directory):
    """Create through retained, no-follow ancestors, then publish via the leaf FD.

    Checking a resolved pathname before mkdir is insufficient: an ancestor can
    be redirected in between. Keep every edge of the directory chain pinned
    through creation and sealing, including parents created by this operation.
    """
    directory = Path(directory)
    require(directory.is_absolute() and '..' not in directory.parts, 'absolute output directory required')
    if directory == Path(directory.anchor):
        raise FileExistsError('analysis output already exists: ' + str(directory))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(directory.anchor, flags)]
    edges = []
    def check():
        for parent, name, descriptor in edges:
            try:
                if os.path.samestat(os.fstat(descriptor), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    continue
            except OSError:
                pass
            raise ValueError('directory pathname replaced: ' + str(directory))
    try:
        with ExitStack() as ownership:
            for index, name in enumerate(directory.parts[1:], 1):
                check()
                parent = descriptors[-1]
                try:
                    if index == len(directory.parts) - 1: raise FileNotFoundError()
                    identity = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    _, retained = ownership.enter_context(created_child(parent, name, check))
                    # Duplicate our own handle, never reopen the published pathname.
                    descriptor = os.dup(int(retained.name))
                else:
                    descriptor = os.open(name, flags, dir_fd=parent)
                    if not os.path.samestat(identity, os.fstat(descriptor)):
                        os.close(descriptor)
                        raise ValueError('directory pathname replaced: ' + str(directory))
                descriptors.append(descriptor)
                edges.append((parent, name, descriptor))
            check()
            try:
                yield check, Path('/proc/self/fd') / str(descriptors[-1])
            finally:
                check()
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


@contextmanager
def pinned_directory(directory, identity=None):
    """Own the original inode and never redirect collector writes after a rename.

    /proc/self/fd is collector-local; child argv/cwd keep their registered paths.
    The handle stays open until command containment and final receipts complete.
    A lost pathname leaves the existing active checkpoint unrecoverable.
    """
    directory = Path(directory)
    if identity is None:
        metadata = os.stat(directory, follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def check():
        try:
            if os.path.samestat(os.fstat(descriptor), os.stat(directory, follow_symlinks=False)):
                return
        except OSError:
            pass
        raise ValueError('directory pathname replaced: ' + str(directory))
    try:
        metadata = os.fstat(descriptor)
        require((metadata.st_dev, metadata.st_ino) == identity,
                'directory pathname replaced before open: ' + str(directory))
        check()
        try:
            yield check, Path('/proc/self/fd') / str(descriptor)
        finally:
            check()
    finally:
        os.close(descriptor)


def execute(spec, context, directory, step, secrets):
    with pinned_directory(directory, context.get('directory_identity')) as (_, retained):
        if 'execution_pins' not in context:
            return shared_execute(spec, context, retained, step, secrets)
        with frozen_launch(context['execution_pins'], context['execution_runtime'],
                           context.get('execution_directories', ()),
                           (spec['cwd'], context.get('trial_dir'), context.get('campaign_dir'),
                            spec['env'].get('HOME'), spec['env'].get('TMPDIR')),
                           timeout=spec['timeout_seconds']) as launch:
            return shared_execute(spec, context, retained, step, secrets, launch=launch)


def normalized_fixture_config(config):
    """Only deployment paths, the registered image, and selected variant vary."""
    config = copy.deepcopy(config)
    target = config['Target']
    target['DB'] = '<mysql-image>'
    target['Schema'] = {k: '<'+k.lower()+'>' for k in target['Schema']}
    target['SUT']['Variant'] = '<variant>'
    target['SUT']['External'].update(Java='<java>', JAR='<jar>')
    return config


def fixture_revision_inputs(workload, variant, expected):
    reference = FIXTURE_INPUTS[workload]
    require(json_equal(normalized_fixture_config(expected['config']), reference['config'])
            and expected['config']['Target']['SUT']['Variant'] == variant,
            'fixture config differs from reviewed revision')
    schema = expected['config']['Target']['Schema']
    require(Path(schema['Migrations']).is_dir()
            and inventory(Path(schema['Migrations'])) == reference['migrations'],
            'fixture migrations differ from reviewed revision')
    require(digest(schema['Seed']) == reference['seed_sha256'], 'fixture seed differs from reviewed revision')
    require(expected['business_classes'] == reference['business_classes'],
            'fixture business classes differ from reviewed revision')


def domain_expectations(manifest):
    if manifest['phase'] != 'measurement': return
    for workload in WORKLOADS:
        for variant in ('vulnerable', 'fixed'):
            require(json_equal(manifest['inputs'][workload][variant].get('expected_domain_rows'),
                               EXPECTED_DOMAIN_ROWS[workload]),
                    'domain row expectation differs from protocol')


PREREQUISITES = ('go_acceptance', 'java_acceptance', 'paired_acceptance',
                 'corpus_acceptance', 'baseline_feasibility')


def prerequisite_markers(kind):
    if kind == 'baseline_feasibility':
        return ['E5_BASELINE_RESULT workload=%s variant=%s executions=20 proxy=original hooks=inactive oracle=existing resets=checked cleanup=proved'
                % (workload, variant) for workload in WORKLOADS for variant in ('vulnerable', 'fixed')]
    if kind == 'corpus_acceptance':
        markers = []
        for fixture in ('spring-quota', 'spring-job-claim'):
            for order in ('serial', 'overlap'):
                for variant in ('vulnerable', 'fixed'):
                    violation = order == 'overlap' and variant == 'vulnerable'
                    markers.append('SPRING_CORPUS_RESULT fixture=%s order=%s variant=%s repeat=20 exit=%d violation_runs=%d fingerprints=1 requested=realized timeouts=0 resets=checked jvm=reaped connections=closed'
                                   % (fixture, order, variant, 2 if violation else 0, 20 if violation else 0))
        return markers
    return []


def prerequisite_completion(manifest, retained=None):
    """Check retained completion records; execution provenance still needs audit."""
    if manifest['phase'] != 'measurement': return
    candidate = manifest['candidate']
    require(re.fullmatch(r'[0-9a-f]{40}', candidate.get('source_revision', '')) is not None
            and candidate['source_revision'] == manifest['e1_registration'].get('source_revision'),
            'candidate source revision differs from E1 registration')
    require(set(manifest['prerequisites']) == set(PREREQUISITES), 'prerequisite inventory differs')
    def pinned(path):
        require(path in manifest['pins'], 'prerequisite file is unpinned: ' + str(path))
        actual = Path(path) if retained is None else retained / manifest['pins'][path]
        require(digest(actual) == manifest['pins'][path], 'prerequisite bytes differ')
        return actual
    for kind in PREREQUISITES:
        record = read_json(pinned(manifest['prerequisites'][kind]))
        require(type(record.get('schema')) is int and record['schema'] == 1
                and record.get('kind') == kind, 'prerequisite completion record required: ' + kind)
        require(json_equal(record.get('candidate'), candidate)
                and record.get('fixture_revision') == manifest['fixture_revision'],
                'prerequisite candidate/fixture identity differs: ' + kind)
        command = read_json(pinned(record['command']))
        require(succeeded(command) and command.get('containment_proven') is True,
                'prerequisite command did not complete successfully: ' + kind)
        output = pinned(record['stdout']).read_text()
        for marker in prerequisite_markers(kind):
            require(marker in output, 'prerequisite result marker missing: ' + marker)
        if kind in ('go_acceptance', 'java_acceptance', 'paired_acceptance'):
            target = kind.split('_')[0]
            argv = command['argv']
            require('--require-complete' in argv
                    and any(Path(a).name == 'check-external-sut-acceptance.py' for a in argv)
                    and argv.count('--results') == 1
                    and argv.index('--results') + 1 < len(argv)
                    and argv[argv.index('--results') + 1] == record['results'],
                    'prerequisite require-complete command missing: ' + kind)
            require(re.search(r'EXTERNAL_SUT_ACCEPTANCE_RESULT target=' + target
                              + r' manifest=valid acceptance=complete pass=[1-9][0-9]* fail=0 incomplete=0(?:\s|$)',
                              output), 'prerequisite acceptance is incomplete: ' + kind)
            result = read_json(pinned(record['results']))
            require(result.get('target') == target and result['run']['revision'] == candidate['source_revision']
                    and type(result['run']['repetitions']) is int and result['run']['repetitions'] >= 20,
                    'prerequisite acceptance identity/repetitions differ: ' + kind)
            rows = result['results']
            require(rows and all(r['status'] == 'pass' and r['checks']
                                and all(c['status'] == 'pass' for c in r['checks'].values())
                                for r in rows.values()), 'prerequisite acceptance checks incomplete: ' + kind)
            artifacts = result['run']['artifacts']
            require(isinstance(artifacts, dict) and artifacts, 'prerequisite original artifacts missing')
            for artifact in artifacts.values():
                path = Path(record['results']).parent / artifact['path']
                require(not Path(artifact['path']).is_absolute() and '..' not in Path(artifact['path']).parts,
                        'prerequisite artifact escapes evidence directory')
                require(digest(pinned(str(path))) == artifact['sha256'], 'prerequisite artifact digest differs')


def command_valid(spec):
    require(isinstance(spec, dict) and isinstance(spec.get('argv'), list) and spec['argv'], 'command argv required')
    require(all(isinstance(a, str) and '\0' not in a for a in spec['argv']), 'invalid argv')
    require(Path(spec['argv'][0]).is_absolute(), 'absolute executable required')
    require(Path(spec['argv'][0]).is_file(), 'executable file required')
    require(isinstance(spec.get('cwd'), str) and Path(spec['cwd']).is_absolute(), 'absolute cwd required')
    require(isinstance(spec.get('env'), dict) and all(isinstance(k, str) and k and '=' not in k
            and isinstance(v, str) and '\0' not in k + v for k, v in spec['env'].items()), 'allowlisted env required')
    for field in ('timeout_seconds', 'cancel_grace_seconds'):
        if field == 'timeout_seconds' or field in spec:
            require(positive(spec.get(field)), field + ' must be a finite positive number')


@contextmanager
def campaign_lock(bundle, shared=False):
    """Keep exclusion even when a hook replaces the compatibility lock file."""
    bundle = Path(bundle)
    descriptor = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        fcntl.flock(descriptor, mode)
        marker = os.open(bundle / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(marker, mode)
            def check():
                try:
                    if (os.path.samestat(os.fstat(descriptor), os.stat(bundle, follow_symlinks=False))
                            and os.path.samestat(os.fstat(marker), os.stat(bundle / '.lock', follow_symlinks=False))):
                        return
                except OSError:
                    pass
                raise ValueError('campaign lock pathname replaced')
            with ExitStack() as stack:
                directories = {name: stack.enter_context(pinned_directory(bundle / name))
                               for name in ('frozen', 'progress', 'slots')}
                def check_all(*, marker=True):
                    # The directory lock still excludes peers if only .lock was
                    # replaced. Safe storage may retain that failure's cleanup
                    # and stop checkpoint without trusting the replaced marker.
                    require(os.path.samestat(os.fstat(descriptor), os.stat(bundle, follow_symlinks=False)),
                            'campaign directory pathname replaced')
                    for check_directory, _ in directories.values(): check_directory()
                    if marker: check()
                check_all.directories = {name: retained for name, (_, retained) in directories.items()}
                check_all()
                try:
                    yield check_all
                finally:
                    check_all()
        finally:
            os.close(marker)
    finally:
        os.close(descriptor)


def config_document(config):
    """Encode the Go effective snapshot as a JSON-subset YAML config.

    Only struct fields change spelling. Scenario names and worker argument keys
    belong to the fixture and must retain their original case and punctuation.
    """
    def convert(value):
        if isinstance(value, list): return [convert(v) for v in value]
        if not isinstance(value, dict): return value
        result = {}
        for name, item in value.items():
            field = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
            field = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', field).lower()
            result[field] = ({k: convert(v) for k, v in item.items()} if name == 'Scenarios'
                             else item if name == 'Args' else convert(item))
        return result
    return convert(config)


def command_config(command, expected):
    """Bind the actual --config bytes to every field of the frozen snapshot."""
    argv, paths = command['argv'], []
    require('--' not in argv, 'config command option terminator is unsupported')
    for index, arg in enumerate(argv):
        if arg in ('--config', '-config'):
            require(index + 1 < len(argv), 'config path required')
            paths.append(argv[index + 1])
        elif arg.startswith(('--config=', '-config=')):
            paths.append(arg.split('=', 1)[1])
    require(len(paths) == 1 and paths[0] and '{' not in paths[0], 'one explicit frozen config required')
    path = Path(command['cwd']) / paths[0]
    try:
        document = read_json(path)
    except (ValueError, OSError) as error:
        raise ValueError('command config must be JSON-subset YAML: ' + str(path)) from error
    require(json_equal(document, config_document(expected['config'])),
            'command config differs from frozen effective config')


def command_output(command):
    """Both launchers must write only to the current invocation's raw tree."""
    argv, paths = command['argv'], []
    require('--' not in argv, 'output command option terminator is unsupported')
    for index, arg in enumerate(argv):
        if arg in ('--out', '-out'):
            require(index + 1 < len(argv), 'output path required')
            paths.append(argv[index + 1])
        elif arg.startswith(('--out=', '-out=')):
            paths.append(arg.split('=', 1)[1])
    # An exact placeholder avoids cwd, traversal and other-slot interpretations,
    # and expands identically for measured slots and their warm-up invocations.
    require(paths == ['{trial_dir}/raw'], 'one output destination {trial_dir}/raw required')


def live_pins(manifest):
    for name, expected in manifest['pins'].items():
        require(Path(name).is_absolute() and Path(name).is_file(), 'missing live pin: ' + name)
        require(digest(name) == expected, 'live pin changed: ' + name)
    if manifest.get('phase') == 'measurement':
        measurement_dependencies(manifest)


def execution_directories(manifest):
    directories = set()
    specs = list(manifest.get('hooks', {}).values())
    specs += [c for variants in manifest.get('commands', {}).values()
              for commands in variants.values() for c in commands.values()]
    for spec in specs:
        values = spec['argv'] + [v for k, v in spec['env'].items() if k in ('CLASSPATH', 'PYTHONPATH')]
        for value in values:
            argument = value.split('=', 1)[-1]
            if '://' in argument: continue
            for name in argument.split(os.pathsep):
                if not name or '{' in name: continue
                path = Path(os.path.abspath(Path(spec['cwd']) / name))
                if path.name == '*': path = path.parent
                if path.is_dir(): directories.add(str(path))
    for variants in manifest.get('inputs', {}).values():
        for expected in variants.values():
            path = expected['config']['Target'].get('Schema', {}).get('Migrations')
            if path: directories.add(path)
    return sorted(directories)


def file_dependencies(spec, pins, label, inputs=()):
    """Check visible file inputs; dynamic imports/subprocesses remain audited pins."""
    values = [(v, True, False, True) for v in spec['argv']]
    values += [(v, k in ('CLASSPATH', 'PYTHONPATH'), False,
                k not in ('PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP')) for k, v in spec['env'].items()]
    values += [(v, True, True, True) for v in inputs]
    for value, include_directory, required, literal_paths in values:
        require(isinstance(value, str) and (value or not required), label + ' dependency path required')
        # Options, classpath lists and Java lib/* wildcards are input paths.
        # Runtime receipt/output placeholders are not frozen inputs.
        argument = value.split('=', 1)[-1]
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', argument) and not required:
            continue  # Infrastructure URLs are not local file dependencies.
        for name in argument.split(os.pathsep):
            if not name or '{' in name:
                require(not required, label + ' dependency must be a frozen path')
                continue
            path = Path(os.path.abspath(Path(spec['cwd']) / name))
            wildcard = path.name == '*' and path.parent.is_dir()
            if wildcard:
                dependencies = sorted(p for p in path.parent.iterdir() if p.suffix.lower() == '.jar' and p.is_file())
            else:
                dependencies = (files(path) if include_directory and path.is_dir()
                                else [path] if path.is_file() else [])
            literal = literal_paths and (Path(name).is_absolute() or name.startswith(('./', '../')))
            require(not (required or literal) or path.is_file() or path.is_dir() or wildcard,
                    label + ' dependency missing: ' + name)
            require(all(str(p) in pins for p in dependencies), label + ' file dependency is unpinned: ' + name)


def measurement_dependencies(manifest):
    prerequisite_completion(manifest)
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
                command_output(command)
                file_dependencies(command, manifest['pins'], workload + '/' + variant + '/' + arm, inputs)
                command_config(command, expected)
                if arm.startswith('B'):
                    require(not any(a.split('=', 1)[0] in ('--java', '-java', '--jar', '-jar', '--image', '-image')
                                    for a in command['argv']), 'baseline runtime override is unsupported')
                    classpath_identity(command, expected, workload)
            fixture_identity(expected, workload)
            fixture_revision_inputs(workload, variant, expected)
    for name, hook in manifest['hooks'].items():
        file_dependencies(hook, manifest['pins'], name)


BUSINESS_PREFIX = 'io/github/weavegate/fixture/'
SDK_PREFIX = 'io/github/weavegate/sdk/'
LAUNCHER_CLASSES = {'io/github/weavegate/experiment/BaselineMain.class',
                    'io/github/weavegate/experiment/BaselineMain$Command.class'}


def library_identity(libraries, expected, workload, label):
    # The SDK is registered by E1, independently of the reference compiler's
    # SDK build. Every other dependency must retain the reviewed library bytes.
    def sdk(name): return name.startswith('weavegate-spring-') and name.endswith('.jar')
    require([checksum for name, checksum in libraries.items() if sdk(name)] == [expected['sdk_sha256']],
            label + ' SDK library identity differs')
    reference = {n:h for n,h in REFERENCE_BUILD['libraries'][workload].items() if not sdk(n)}
    require({n:h for n,h in libraries.items() if not sdk(n)} == reference,
            label + ' libraries differ from reference build')


def fixture_identity(expected, workload):
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
        require({n[len(prefix):] for n in names if n.startswith(prefix) and not n.endswith('/')}
                == set(expected['business_classes']), 'fixture classpath contains unreviewed entries')
        sdk = [n for n in names if n.startswith('BOOT-INF/lib/weavegate-spring-') and n.endswith('.jar')]
        require(len(sdk) == 1 and sha(archive.read(sdk[0])) == expected['sdk_sha256'],
                'fixture SDK identity differs')
        libraries = {n[len('BOOT-INF/lib/'):]: sha(archive.read(n)) for n in names
                     if n.startswith('BOOT-INF/lib/') and not n.endswith('/')}
        library_identity(libraries, expected, workload, 'fixture')


def classpath_identity(command, expected, workload):
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
    classes, sdk_sources, libraries = {}, [], {}
    directory_entries = set()
    for path in paths:
        selected = {}
        if path.is_dir():
            for file in files(path):
                name = str(file.relative_to(path))
                require(name in expected['business_classes'] or name in LAUNCHER_CLASSES,
                        'classpath entry outside reviewed fixture/launcher: ' + name)
                require(name not in directory_entries, 'duplicate classpath entry: ' + name)
                directory_entries.add(name)
                if name in expected['business_classes']: selected[name] = digest(file)
        else:
            require(path.is_file() and zipfile.is_zipfile(path), 'classpath JAR missing/invalid: ' + str(path))
            require(path.name not in libraries, 'duplicate classpath library: ' + path.name)
            libraries[path.name] = digest(path)
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
    library_identity(libraries, expected, workload, 'classpath')


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
        for key, name in (('manifest_sha256', 'manifest.json'), ('order_sha256', 'order.json')):
            require(value.get(key) == digest(bundle / 'frozen' / name),
                    'registration hash differs: ' + name)
    return value


def retained_progress(bundle, manifest, slots):
    """Fail closed on gaps, lost suffixes, crashes, or continuation after a stop."""
    declarations = unsupported_declarations(manifest)
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
        declaration = declarations.get(slot['workload'] + '/' + slot['arm'])
        require(json_equal(state.get('unsupported'), declaration), 'unsupported slot declaration differs')
        if declaration is not None:
            require(state['unsupported'] == declaration
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
