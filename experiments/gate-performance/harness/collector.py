"""Freeze campaigns and collect one preassigned pair on its allocated host."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from common import (E1_RESOURCES, E2_RESOURCES, INVENTORIES, PROTOCOLS, QUALITY,
                    SEED, canonical, child, counter, digest, files, json_equal, order,
                    positive, read_json, sampling, seal, sha, timestamp, utc,
                    verify, write_csv, write_json)

STEPS = ('prepare', 'run', 'observe', 'check', 'cleanup')


class GroupTerminationError(OSError):
    """An owned command group did not terminate within its KILL deadline."""


def string_inventory(value, name, allow_empty=False):
    if (not isinstance(value, list) or (not value and not allow_empty) or
            any(not isinstance(item, str) or not item.strip() for item in value) or
            len(set(value)) != len(value)):
        raise ValueError(name + ' must be a list of unique nonempty strings')


def validate_manifest(m):
    if type(m.get('schema')) is not int or m['schema'] != 1 or m.get('experiment') not in ('E1', 'E2', 'E3', 'E4'):
        raise ValueError('expected schema 1 and E1/E2/E3/E4')
    if (not isinstance(m.get('campaign_id'), str) or not m['campaign_id'].strip()
            or type(m.get('protocol_revision')) is not int or m['protocol_revision'] < 1):
        raise ValueError('campaign identity and protocol revision required')
    if not isinstance(m.get('seed'), str) or not m['seed'].strip():
        raise ValueError('seed must be a nonempty string')
    if not isinstance(m.get('phase'), str) or not m['phase'].strip():
        raise ValueError('phase must be a nonempty string')
    for name in INVENTORIES:
        if not isinstance(m.get(name), dict) or not m[name]:
            raise ValueError('missing inventory: ' + name)
    if not m.get('pins') or not m.get('protocols') or not m.get('execution_pins'):
        raise ValueError('protocol and artifact pins required')
    for name in ('quality_checks', 'resource_metrics', 'compare_facts', 'redact_env'):
        string_inventory(m.get(name), name, allow_empty=name == 'redact_env')
    if any('=' in name or '\0' in name for name in m['redact_env']):
        raise ValueError('invalid redaction environment name')
    string_inventory(m.get('analysis_metrics', []), 'analysis_metrics', allow_empty=True)
    deviations = m.get('deviations', [])
    if not isinstance(deviations, list) or any(not isinstance(value, str) for value in deviations):
        raise ValueError('deviations must be a list of strings')
    if m['experiment'] in ('E1', 'E2'):
        if m['protocol_revision'] != 1 or m['seed'] != SEED:
            raise ValueError('E1/E2 revision 1 uses the published seed')
        if m['strata'] != sampling(m['experiment'], m['phase']):
            raise ValueError('sample counts/workloads differ from the frozen protocol')
        if set(m['protocols']) != set(PROTOCOLS):
            raise ValueError('all three shared protocol files required')
        if not set(QUALITY) <= set(m['quality_checks']):
            raise ValueError('incomplete quality inventory')
        resources = E1_RESOURCES if m['experiment'] == 'E1' else E2_RESOURCES
        if not set(resources) <= set(m['resource_metrics']):
            raise ValueError('incomplete resource inventory')
        if m['experiment'] == 'E2':
            from ci import validate_inventory
            validate_inventory(m.get('ci'))
    else:
        if not m.get('seed') or not m.get('quality_checks'):
            raise ValueError('future protocol must supply its seed and quality inventory')
    if not m['strata'] or any(not counts or any(type(n) is not int or n < 1 for n in counts)
                              for counts in m['strata'].values()):
        raise ValueError('strata require positive integer counts')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) for name in m['strata']):
        raise ValueError('workload IDs must be safe lowercase identifiers')
    dates = m['dates']
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise ValueError('date slots must be distinct and increasing')
    for date in dates:
        if (not isinstance(date, str) or
                datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d') != date):
            raise ValueError('date slots must use canonical UTC YYYY-MM-DD')
    if max(map(len, m['strata'].values())) != len(dates):
        raise ValueError('date count differs from strata')
    if set(m['commands']) != set(m['strata']) or set(m['expected_facts']) != set(m['strata']):
        raise ValueError('commands and expected facts required for every workload')
    for workload, arms in m['commands'].items():
        if set(arms) != {'baseline', 'candidate'}:
            raise ValueError('both arm commands required: ' + workload)
        for arm, commands in arms.items():
            if set(commands) != set(STEPS):
                raise ValueError('prepare/run/observe/check/cleanup commands required')
            exits = m['expected_exit'][workload][arm]
            if not isinstance(exits, list) or not exits or any(type(code) is not int for code in exits):
                raise ValueError('expected exits must be a nonempty list of integers')
            expected = m['expected_facts'][workload][arm]
            if not isinstance(expected, dict) or not expected:
                raise ValueError('expected facts must be nonempty objects')
            for step, spec in commands.items():
                if 'execution_pins' in spec:
                    string_inventory(spec['execution_pins'], 'cleanup execution_pins')
                    if (step != 'cleanup' or not set(spec['execution_pins']) <=
                            {pin['path'] for pin in m['execution_pins']}):
                        raise ValueError('cleanup execution_pins must reference registered live inputs')
                if (not isinstance(spec.get('argv'), list) or not spec['argv']
                        or not all(isinstance(a, str) and '\0' not in a for a in spec['argv'])
                        or not spec['argv'][0]
                        or not isinstance(spec.get('cwd'), str) or not spec['cwd'] or '\0' in spec['cwd']
                        or not positive(spec.get('timeout_seconds'))
                        or not isinstance(spec.get('env'), dict)
                        or not all(isinstance(k, str) and isinstance(v, str)
                                   and '=' not in k and '\0' not in k and '\0' not in v
                                   for k, v in spec['env'].items())
                        or not positive(spec.get('cancel_grace_seconds', 25))):
                    raise ValueError('invalid command: ' + step)
                if m['experiment'] in ('E1', 'E2') and step == 'run':
                    if m['experiment'] == 'E2':
                        if spec['timeout_seconds'] <= 5400:
                            raise ValueError('E2 wrapper watchdog must reserve overhead beyond the provider cap')
                    elif spec['timeout_seconds'] != (1800 if m['phase'] == 'adoption-cold' else 600):
                        raise ValueError('run observation budget differs from protocol')
            if m['experiment'] == 'E1':
                expected_code = 2 if 'vulnerable' in workload else 0
                if m['expected_exit'][workload][arm] != [expected_code]:
                    raise ValueError('expected exit differs from the registered workload')
                runs = 21 if workload == 'example-vulnerable-explore' else (
                    18 if workload == 'example-fixed-explore' else 20)
                facts = m['expected_facts'][workload][arm]
                if facts.get('schedule_runs') != runs or facts.get('flaky') is not False:
                    raise ValueError('schedule-run count/flakiness expectation differs from protocol')
            elif m['experiment'] == 'E2' and m['expected_exit'][workload][arm] != [0]:
                raise ValueError('completed workflow wrapper must exit 0')


def freeze(source, destination):
    source, destination = Path(source).resolve(), Path(destination)
    m = read_json(source)
    validate_manifest(m)
    # Validate every prerequisite before creating a campaign.
    verify_execution_pins(m)
    contents = {}
    for pin in m['pins']:
        path = child(source.parent, pin['path'])
        data = path.read_bytes()
        if sha(data) != pin['sha256'] or not pin.get('role'):
            raise ValueError('artifact pin mismatch: ' + pin['path'])
        name = 'pins/' + pin['path']
        if name in contents:
            raise ValueError('duplicate pin: ' + pin['path'])
        contents[name] = data
    for name, pin in m['protocols'].items():
        if '\0' in name:
            raise ValueError('invalid protocol filename')
        child(destination / 'protocol', name)  # Check output paths before creating any bundle.
        data = child(source.parent, pin['path']).read_bytes()
        if sha(data) != pin['sha256']:
            raise ValueError('protocol pin mismatch: ' + name)
        if m['experiment'] in ('E1', 'E2'):
            current = Path(__file__).resolve().parents[1] / name
            if data != current.read_bytes():
                raise ValueError('protocol differs from this collector revision: ' + name)
        contents['protocol/' + name] = data
    targets = {child(destination, name) for name in contents}
    if len(targets) != len(contents) or any(parent in targets for path in targets for parent in path.parents):
        raise ValueError('conflicting retained artifact paths')
    rows = order(m)
    campaign_bytes = canonical(m)
    destination.mkdir(mode=0o700)  # Never overwrite or extend a frozen campaign.
    for name, data in contents.items():
        path = child(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / 'collection.lock').touch(mode=0o600)
    write_json(destination / 'freeze-command.json', {'argv': [sys.executable,
               str(Path(__file__).with_name('tool.py')), 'freeze', str(source), str(destination.resolve())]})
    (destination / 'campaign.json').write_bytes(campaign_bytes)
    write_json(destination / 'order.json', rows)
    write_csv(destination / 'order.csv', [dict(r, arms=','.join(r['arms'])) for r in rows],
              ['block', 'block_id', 'workload', 'date_slot', 'date', 'arms'])
    # Archive the executable implementation; analysis requires no live checkout.
    for path in sorted(Path(__file__).parent.glob('*.py')):
        target = destination / 'analysis' / 'source' / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    write_json(destination / 'analysis' / 'dependencies.json',
               {'runtime': 'Python >= 3.9', 'dependencies': 'standard library only'})
    inventory(destination, m)
    seal(destination)


def sanitize(root, secrets):
    """Sanitize retained bytes before computing evidence hashes."""
    removed = 0
    for path in files(root):
        data = path.read_bytes()
        if path.suffix == '.json':
            try:
                value = read_json(path)
            except (ValueError, UnicodeError):
                pass  # Non-JSON originals still receive byte-level redaction.
            else:
                replacements = [0]
                redacted = redact_metadata(value, secrets, path.name, replacements)
                if replacements[0]:
                    path.write_bytes(canonical(redacted))
                removed += replacements[0]
                continue
        variants = set(secrets)
        for value in secrets:
            variants.add(json.dumps(value.decode(), ensure_ascii=True)[1:-1].encode())
            variants.add(json.dumps(value.decode(), ensure_ascii=False)[1:-1].encode())
        for value in sorted(variants, key=len, reverse=True):
            removed += data.count(value)
            data = data.replace(value, b'[REDACTED]')
        path.write_bytes(data)
    return removed


def group_running(group):
    """Linux group liveness excludes exited zombies, which parents must reap."""
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            continue  # A process exited during the kernel inventory read.
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            return True
    return False


def wait_group(group, deadline):
    # Poll only termination state; this does not coordinate measured workloads.
    wake = threading.Event()
    while group_running(group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        wake.wait(min(.01, remaining))
    return True


def kill_group(process, grace):
    deadline = time.monotonic() + grace
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise GroupTerminationError('command process group leader remains alive after SIGKILL') from None
    if not wait_group(process.pid, deadline):
        raise GroupTerminationError('command process group remains alive after SIGKILL')


def execute(spec, context, directory, step, secrets):
    def expand(value):
        for key, replacement in context.items():
            value = value.replace('{' + key + '}', str(replacement))
        return value
    argv = [expand(a) for a in spec['argv']]
    cwd = expand(spec['cwd'])
    env = {key: expand(value) for key, value in spec['env'].items()}
    # Secrets are passed by named environment variable, never logged as values.
    for key in context['redact_env']:
        env[key] = os.environ[key]
    started = utc()
    start = time.monotonic_ns()
    result = {'step': step, 'argv': argv, 'cwd': cwd,
              'env': {k: v for k, v in env.items() if k not in context['redact_env']},
              'redacted_env_names': context['redact_env'], 'started_at': started,
              'started': False, 'exit': None, 'timeout': False, 'error': None}
    process = None
    try:
        with (directory / (step + '.stdout')).open('wb') as out, \
                (directory / (step + '.stderr')).open('wb') as err:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                       start_new_session=True)
            result['started'] = True
            try:
                try:
                    remaining = spec['timeout_seconds'] - (time.monotonic_ns() - start) / 1e9
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, spec['timeout_seconds'])
                    result['exit'] = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    result['timeout'] = True
                    grace = spec.get('cancel_grace_seconds', 25)
                    deadline = time.monotonic() + grace
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        result['exit'] = process.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
                    if not wait_group(process.pid, deadline):
                        kill_group(process, grace)
                    result['exit'] = process.wait(timeout=0)
            except GroupTerminationError:
                raise  # The KILL deadline has already expired; do not restart its budget.
            except BaseException:
                kill_group(process, spec.get('cancel_grace_seconds', 25))
                raise
    except OSError as error:
        result['error'] = str(error)
    result['seconds'] = (time.monotonic_ns() - start) / 1e9
    result['ended_at'] = utc()
    # Result values can include expanded environment references; redact them too.
    result = redact_metadata(result, secrets, step + '.command.json')
    write_json(directory / (step + '.command.json'), result)
    return result


def succeeded(command):
    return command['exit'] == 0 and not command['timeout'] and command['error'] is None


def receipt_valid(m, row, receipt, directory):
    required = m['quality_checks']
    if any(receipt.get('checks', {}).get(name) is not True for name in required):
        raise ValueError('missing or failed quality checks')
    facts = receipt.get('facts', {})
    expected = m['expected_facts'][row['workload']][row['arm']]
    if any(key not in facts or not json_equal(facts[key], value) for key, value in expected.items()):
        raise ValueError('observed facts differ from registered expectations')
    if any(key not in facts for key in m['compare_facts']):
        raise ValueError('missing paired comparison facts')
    if any(not counter(receipt.get('metrics', {}).get(name)) for name in m['resource_metrics']):
        raise ValueError('missing/invalid resource measurements')
    if (not isinstance(receipt.get('environment'), dict) or not receipt['environment']
            or not isinstance(receipt.get('resource_method'), str) or not receipt['resource_method'].strip()):
        raise ValueError('observed environment and resource provenance required')
    evidence = receipt.get('evidence', {})
    if not evidence:
        raise ValueError('missing original evidence')
    for name, expected_hash in evidence.items():
        if digest(child(directory, name)) != expected_hash:
            raise ValueError('retained evidence hash mismatch: ' + name)
    metric = 'gate_seconds' if m['experiment'] == 'E1' else 'validation_seconds'
    if m['experiment'] == 'E2':
        from ci import endpoint, workflow_bounds
        provider = read_json(child(directory, receipt['ci_snapshot']))
        derived = endpoint(provider, dict(m['ci'], tasks=m['ci']['tasks'][row['arm']]))
        if any(not json_equal(receipt['metrics'].get(k), v) for k, v in derived.items()):
            raise ValueError('CI receipt disagrees with provider timestamps')
        for key, boundary in zip(('ci_created_at', 'ci_completed_at'), workflow_bounds(provider)):
            if timestamp(facts.get(key)) != boundary:
                raise ValueError('CI pairing timestamp disagrees with provider: ' + key)
    elif m['experiment'] in ('E3', 'E4'):
        return
    if m['experiment'] == 'E1' and m['phase'] == 'adoption-cold':
        timer = receipt.get('gate_timer', {})
        if (timer.get('source') != 'monotonic' or timer.get('boundary') != 'cli_spawn_to_exit'
                or not json_equal(timer.get('seconds'), receipt['metrics'].get('gate_seconds'))
                or timer.get('evidence') not in evidence):
            raise ValueError('cold adoption requires a separate nested CLI timer receipt')
    if not positive(receipt['metrics'].get(metric)):
        raise ValueError('primary duration missing')


def collect(root, block, registration):
    with exclusive(root):
        return collect_locked(root, block, registration)


def validate_registration(reg):
    """Check provenance syntax, not public availability or timestamp authenticity."""
    if (not isinstance(reg, dict) or
            any(not isinstance(reg.get(key), str) or not reg[key].strip()
                for key in ('public_url', 'revision'))):
        raise ValueError('invalid public registration receipt')
    url = reg['public_url']
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError('invalid public registration URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('invalid public registration URL')
    parsed.port  # Reject malformed or out-of-range ports without fetching the URL.


def collect_locked(root, block, registration):
    root = Path(root).resolve()
    verify(root)
    entry_hashes = read_json(root / 'SHA256SUMS')
    if (root / 'stopped.json').exists():
        raise ValueError('campaign stopped; register a new campaign')
    m = read_json(root / 'campaign.json')
    validate_manifest(m)
    verify_execution_pins(m)
    for source in sorted(Path(__file__).parent.glob('*.py')):
        if digest(source) != digest(root / 'analysis/source' / source.name):
            raise ValueError('collector source changed since campaign freeze')
    if read_json(root / 'order.json') != order(m):
        raise ValueError('registered order differs')
    reg = read_json(registration)
    validate_registration(reg)
    if (reg['campaign_sha256'] != digest(root / 'campaign.json') or
            reg['order_sha256'] != digest(root / 'order.json') or
            timestamp(reg['registered_at']) >= timestamp(utc())):
        raise ValueError('invalid public registration receipt')
    receipt_path = root / 'registration.json'
    if receipt_path.exists() and read_json(receipt_path) != reg:
        raise ValueError('registration receipt cannot change')
    slots = [r for r in order(m) if r['block'] == block]
    if len(slots) != 1:
        raise ValueError('unknown block')
    slot = slots[0]
    prior = [r for r in order(m) if r['workload'] == slot['workload']
             and r['date_slot'] == slot['date_slot']]
    for earlier in prior[:prior.index(slot)]:
        if any(not (root / 'blocks' / earlier['block'] / arm / 'trial.json').exists()
               for arm in earlier['arms']):
            raise ValueError('earlier assigned block is missing/incomplete')
    if slot['date'] != datetime.now(timezone.utc).strftime('%Y-%m-%d'):
        raise ValueError('block must execute on its registered UTC date')
    if timestamp(reg['registered_at']).date().isoformat() > slot['date']:
        raise ValueError('registration postdates block')
    # Public audit identifiers; credential values are read separately below.
    redaction_env_names = m['redact_env']
    if any(not os.environ.get(name) for name in redaction_env_names):
        raise ValueError('missing required redaction environment variable')
    secrets = [os.environ[name].encode() for name in redaction_env_names]
    pair_dir = root / 'blocks' / block
    pair_dir.parent.mkdir(exist_ok=True)
    pair_dir.mkdir(mode=0o700)  # Existing or interrupted blocks cannot be rerun.
    write_json(receipt_path, reg)
    write_json(pair_dir / 'collection-command.json', {'argv': [sys.executable,
               str(Path(__file__).with_name('tool.py')), 'collect', str(root), '--block', block,
               '--registration', str(receipt_path)]})
    write_json(pair_dir / 'slot.json', slot)
    seal(root)  # A crash leaves a visible started block, never a replacement slot.
    aggregates = {'trials.csv', 'commands.jsonl', 'events.jsonl', 'environments.jsonl'}
    protected = {name: checksum for name, checksum in entry_hashes.items() if name not in aggregates}
    protected['registration.json'] = digest(receipt_path)
    protected.update({str(path.relative_to(root)): digest(path) for path in files(pair_dir)})
    checksum_inventory = digest(root / 'SHA256SUMS')
    allowed = set(protected) | aggregates | {'SHA256SUMS'}
    active_directory = None
    integrity_error = None
    def check_protected():
        if digest(root / 'SHA256SUMS') != checksum_inventory:
            raise ValueError('sealed checksum inventory changed during collection')
        for name, checksum in protected.items():
            path = child(root, name)
            if not path.is_file() or digest(path) != checksum:
                raise ValueError('protected evidence changed during collection: ' + name)
        for path in files(root):
            name = str(path.relative_to(root))
            if name not in allowed and (active_directory is None or active_directory not in path.parents):
                raise ValueError('unregistered evidence added during collection: ' + name)
    def protect():
        nonlocal integrity_error
        if integrity_error is not None:
            raise ValueError(integrity_error)
        try:
            check_protected()
        except (OSError, ValueError) as error:
            integrity_error = str(error)
            raise
    def command(spec, context, directory, step):
        result = execute(spec, context, directory, step, secrets)
        protect()
        return result
    safe = True
    redaction_counts = {}
    def retain_redactions(directory, removed):
        redaction_counts[directory] = redaction_counts.get(directory, 0) + removed
        write_json(directory / 'redactions.json', {'replacements': redaction_counts[directory]})
    def sanitize_arm(directory):
        retain_redactions(directory, sanitize(directory, secrets))
    try:
        for arm in slot['arms']:
            if active_directory is not None:
                protect()
                snapshot = {str(path.relative_to(root)): digest(path) for path in files(active_directory)}
                protected.update(snapshot)
                allowed.update(snapshot)
                checkpoint = pair_dir / 'completed-arm-hashes.json'
                write_json(checkpoint, snapshot)
                name = str(checkpoint.relative_to(root))
                protected[name] = digest(checkpoint)
                allowed.add(name)
            directory = pair_dir / arm
            active_directory = directory
            directory.mkdir(mode=0o700)
            row = dict(slot, arm=arm, attempt=1, classification='not_started',
                       reason='cleanup_unproven', exit=None, elapsed_seconds=None,
                       started=False, cleanup_proven=False, metrics={}, facts={},
                       collection_root=str(root))
            write_json(directory / 'trial.json', row)
            if not safe:
                continue
            if slot['date'] != datetime.now(timezone.utc).strftime('%Y-%m-%d'):
                row['reason'] = 'registered_date_expired'
                write_json(directory / 'trial.json', row)
                continue
            context = {'campaign_dir': root, 'trial_dir': directory,
                       'block_id': block, 'arm': arm, 'redact_env': redaction_env_names}
            commands = m['commands'][slot['workload']][arm]
            row['classification'] = 'operational_failure'
            row['reason'] = 'interrupted_collection'
            write_json(directory / 'trial.json', row)
            try:
                prepare = command(commands['prepare'], context, directory, 'prepare')
                row['reason'] = 'prepare_failed'
                if succeeded(prepare):
                    verify_execution_pins(m)
                    if slot['date'] != datetime.now(timezone.utc).strftime('%Y-%m-%d'):
                        row.update(classification='not_started', reason='registered_date_expired')
                        continue  # Preparation still receives its normal cleanup below.
                    row['started'] = True
                    write_json(directory / 'trial.json', row)
                    run = command(commands['run'], context, directory, 'run')
                    row.update(started=run['started'], exit=run['exit'], elapsed_seconds=run['seconds'],
                               reason='unexpected_exit')
                    observed = command(commands['observe'], context, directory, 'observe')
                    sanitize_arm(directory)
                    checked = command(commands['check'], context, directory, 'check')
                    evidence = None
                    try:
                        evidence = read_json(directory / 'receipt.json')
                        if m['experiment'] == 'E1':
                            metric = 'adoption_seconds' if m['phase'] == 'adoption-cold' else 'gate_seconds'
                            evidence.setdefault('metrics', {})[metric] = run['seconds']
                            write_json(directory / 'receipt.json', evidence)
                        row['metrics'] = {k: v for k, v in evidence.get('metrics', {}).items() if counter(v)}
                        row['facts'] = evidence.get('facts', {})
                    except (ValueError, OSError, KeyError, TypeError, AttributeError):
                        pass  # Unknown counters stay unknown; originals remain retained.
                    if run['timeout'] or run['error']:
                        row['reason'] = ('budget_expired' if run['timeout'] else
                                         'execution_failed' if run['started'] else 'spawn_failed')
                    elif run['exit'] in (0, 2, 3) and run['exit'] not in m['expected_exit'][slot['workload']][arm]:
                        row.update(classification='quality_failure',
                                   reason='flaky_exit' if run['exit'] == 3 else 'unexpected_outcome')
                    elif run['exit'] in m['expected_exit'][slot['workload']][arm]:
                        row.update(classification='quality_failure', reason='evidence_incomplete')
                        if succeeded(observed) and succeeded(checked):
                            if evidence is None:
                                raise ValueError('missing/malformed observation receipt')
                            receipt_valid(m, row, evidence, directory)
                            row.update(classification='complete', reason='',
                                       metrics=evidence['metrics'], facts=evidence['facts'])
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
                row.update(classification='quality_failure', reason=str(error))
            finally:
                # Cleanup is independent of run exit, timeout, receipt parsing and Ctrl-C.
                safe = False
                try:
                    pins_match = True
                    try:
                        verify_execution_pins(m)
                    except (OSError, ValueError):
                        pins_match = False
                    # A separately registered teardown can still release resources
                    # after unrelated inputs drift. Drift never permits reuse.
                    verify_execution_pins(m, commands['cleanup'].get('execution_pins'))
                    cleanup = command(commands['cleanup'], context, directory, 'cleanup')
                    sanitize_arm(directory)
                    proof = read_json(directory / 'cleanup.json')
                    safe = succeeded(cleanup) and proof.get('proven') is True
                    if safe:
                        if not proof.get('evidence'):
                            safe = False
                        for name, checksum in proof.get('evidence', {}).items():
                            if digest(child(directory, name)) != checksum:
                                safe = False
                    safe = safe and pins_match
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    safe = False
                except BaseException:
                    safe = False
                    raise
                finally:
                    row['cleanup_proven'] = safe
                    if not safe:
                        row.update(classification='quality_failure', reason='cleanup_unproven')
                    sanitize_arm(directory)
                    replacements = [0]
                    write_json(directory / 'trial.json', redact_metadata(row, secrets, 'trial.json', replacements))
                    retain_redactions(directory, replacements[0])
    finally:
        if not safe:
            if (root / 'stopped.json').exists():
                integrity_error = integrity_error or 'hook wrote collector-owned stopped.json'
            else:
                write_json(root / 'stopped.json', {'reason': 'cleanup_unproven', 'block': block})
                protected['stopped.json'] = digest(root / 'stopped.json')
                allowed.add('stopped.json')
        # BaseException paths must never seal unsanitized hook output. A failed
        # sanitization leaves the bundle unsealed rather than publishing raw bytes.
        removed = sanitize(pair_dir, secrets)
        if active_directory is not None:
            retain_redactions(active_directory, removed)
        try:
            protect()
        except (OSError, ValueError) as error:
            # Preserve the old inventory and the damaged evidence for audit.
            write_json(pair_dir / 'integrity-error.json', {'reason': str(error)})
            raise ValueError('protected campaign evidence changed; refusing to reseal') from error
        inventory(root, m)
        seal(root)
    return safe


def inventory(root, m):
    """Include every planned arm, including blocks never collected."""
    rows, commands, events, environments = [], [], [], []
    for slot in order(m):
        for arm in slot['arms']:
            directory = root / 'blocks' / slot['block'] / arm
            path = directory / 'trial.json'
            row = read_json(path) if path.exists() else dict(
                slot, arm=arm, attempt=1, classification='not_started',
                reason='slot_not_collected', exit=None, elapsed_seconds=None)
            rows.append({key: row[key] for key in ('block', 'workload', 'date_slot', 'arm',
                         'attempt', 'classification', 'reason', 'exit', 'elapsed_seconds')})
            for step in STEPS:
                path = directory / (step + '.command.json')
                if path.exists():
                    command = dict(read_json(path), block=slot['block'], arm=arm)
                    commands.append(command)
                    events.append({key: command[key] for key in
                                   ('block', 'arm', 'step', 'seconds', 'started_at', 'ended_at')})
            path = directory / 'receipt.json'
            if path.exists():
                try:
                    receipt = read_json(path)
                    if isinstance(receipt, dict):
                        environments.append({'block': slot['block'], 'arm': arm,
                                             'observed': receipt.get('environment')})
                except ValueError:
                    pass
    write_csv(root / 'trials.csv', rows, ['block', 'workload', 'date_slot', 'arm', 'attempt',
                                        'classification', 'reason', 'exit', 'elapsed_seconds'])
    for name, items in [('commands', commands), ('events', events), ('environments', environments)]:
        (root / (name + '.jsonl')).write_text(''.join(
            json.dumps(item, sort_keys=True, allow_nan=False) + '\n' for item in items))


def verify_execution_pins(m, selected=None):
    for pin in m['execution_pins']:
        if selected is not None and pin['path'] not in selected:
            continue
        path = Path(pin['path'])
        if not path.is_absolute() or not pin.get('role') or digest(path) != pin['sha256']:
            raise ValueError('live execution pin mismatch: ' + pin['path'])


@contextmanager
def exclusive(root):
    with (Path(root) / 'collection.lock').open('r+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another collector owns this campaign') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def redact_metadata(value, secrets, filename, replacements=None):
    """Keep numeric types and collector-owned public identity/control fields."""
    if not isinstance(value, dict):
        return redact_value(value, secrets, replacements)
    if filename in ('slot.json', 'collection-command.json', 'completed-arm-hashes.json'):
        return value  # Protected collector-owned public audit identifiers.
    if filename == 'trial.json':
        public = {'block', 'block_id', 'workload', 'date_slot', 'date', 'arms', 'arm',
                  'attempt', 'classification', 'collection_root', 'metrics', 'started',
                  'exit', 'elapsed_seconds', 'cleanup_proven'}
        known = public | {'reason', 'facts'}
    elif filename.endswith('.command.json'):
        public = {'step', 'argv', 'cwd', 'env', 'redacted_env_names', 'started_at',
                  'ended_at', 'started', 'exit', 'timeout', 'seconds'}
        known = public | {'error'}
    elif filename == 'redactions.json':
        public = known = {'replacements'}
    else:
        return redact_value(value, secrets, replacements)
    return {key if key in known else redact_value(key, secrets, replacements):
            item if key in public else redact_value(item, secrets, replacements)
            for key, item in value.items()}


def redact_value(value, secrets, replacements=None):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if replacements is not None:
                replacements[0] += value.count(secret.decode())
            value = value.replace(secret.decode(), '[REDACTED]')
        return value
    if isinstance(value, list):
        return [redact_value(item, secrets, replacements) for item in value]
    if isinstance(value, dict):
        return {redact_value(key, secrets, replacements): redact_value(item, secrets, replacements)
                for key, item in value.items()}
    return value
