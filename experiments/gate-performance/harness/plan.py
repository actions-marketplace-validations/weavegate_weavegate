"""Validate the complete frozen plan before collection can consume a slot."""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from common import (E1_RESOURCES, E2_RESOURCES, INVENTORIES, PROTOCOLS, QUALITY,
                    SEED, child, digest, positive, sampling, timestamp)

STEPS = ('prepare', 'run', 'observe', 'check', 'cleanup')


def string_inventory(value, name, allow_empty=False):
    if (not isinstance(value, list) or (not value and not allow_empty) or
            any(not isinstance(item, str) or not item.strip() for item in value) or
            len(set(value)) != len(value)):
        raise ValueError(name + ' must be a list of unique nonempty strings')


def validate_manifest(m):
    validate_shapes(m)
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
    # The numeric formats are minimum widths: large slots/counts add digits.
    for workload, counts in m['strata'].items():
        for date_index, count in enumerate(counts, 1):
            if len(('%s-%02d-%03d' % (workload, date_index, count)).encode()) > 255:
                raise ValueError('composed block name exceeds 255 bytes')
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
                                   and k and '=' not in k and '\0' not in k and '\0' not in v
                                   for k, v in spec['env'].items())
                        or not positive(spec.get('cancel_grace_seconds', 25))):
                    raise ValueError('invalid command: ' + step)
                if (not Path(spec['cwd']).is_absolute() and not any(
                        spec['cwd'] == token or spec['cwd'].startswith(token + '/')
                        for token in ('{campaign_dir}', '{trial_dir}'))):
                    raise ValueError('command cwd must expand to an absolute directory: ' + step)
                executable = spec['argv'][0]
                if (any(token in executable for token in
                        ('{campaign_dir}', '{trial_dir}', '{block_id}', '{arm}')) or
                        not Path(executable).is_absolute() and
                        ('/' in executable or 'PATH' not in spec['env'])):
                    raise ValueError('command executable requires an absolute path or explicit PATH: ' + step)
                if Path(executable).is_absolute():
                    pins = spec.get('execution_pins', [pin['path'] for pin in m['execution_pins']])
                    if executable not in pins:
                        raise ValueError('command executable must have an execution pin: ' + step)
                elif any(not Path(entry).is_absolute() for entry in spec['env']['PATH'].split(os.pathsep)):
                    raise ValueError('command executable PATH entries must be absolute: ' + step)
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


def validate_shapes(m):
    """Validate containers before indexing them; malformed plans consume no slot."""
    if not isinstance(m, dict):
        raise ValueError('manifest must be an object')
    for name in ('strata', 'commands', 'expected_facts', 'expected_exit', 'protocols'):
        if not isinstance(m.get(name), dict) or not m[name]:
            raise ValueError(name + ' must be a nonempty object')
    if not isinstance(m.get('dates'), list) or not m['dates'] or any(
            not isinstance(date, str) for date in m['dates']):
        raise ValueError('dates must be a nonempty list of UTC dates')
    for workload, counts in m['strata'].items():
        if not isinstance(counts, list) or not counts or any(type(n) is not int or n < 1 for n in counts):
            raise ValueError('strata require positive integer counts')
    for name in ('commands', 'expected_facts', 'expected_exit'):
        if set(m[name]) != set(m['strata']):
            raise ValueError(name + ' must cover exactly the registered workloads')
        for arms in m[name].values():
            if not isinstance(arms, dict) or set(arms) != {'baseline', 'candidate'}:
                raise ValueError(name + ' requires both arms')
    for arms in m['commands'].values():
        for commands in arms.values():
            if not isinstance(commands, dict) or set(commands) != set(STEPS):
                raise ValueError('prepare/run/observe/check/cleanup commands required')
            if any(not isinstance(spec, dict) for spec in commands.values()):
                raise ValueError('command specifications must be objects')
    for name in ('pins', 'execution_pins'):
        pins = m.get(name)
        if not isinstance(pins, list) or not pins:
            raise ValueError(name + ' must be a nonempty pin list')
        seen = set()
        for pin in pins:
            validate_pin(pin, name == 'execution_pins')
            path = pin['path']
            if path in seen:
                raise ValueError('duplicate pin: ' + path)
            seen.add(path)
    for name, pin in m['protocols'].items():
        child(Path('/protocol'), name)
        validate_pin(pin, False, role=False)


def validate_pin(pin, absolute, role=True):
    if (not isinstance(pin, dict) or not isinstance(pin.get('path'), str)
            or not pin['path'] or '\0' in pin['path']
            or not isinstance(pin.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', pin['sha256'])
            or role and (not isinstance(pin.get('role'), str) or not pin['role'].strip())):
        raise ValueError('invalid artifact pin')
    if absolute:
        if not Path(pin['path']).is_absolute():
            raise ValueError('execution pins must be absolute')
    else:
        child(Path('/pins'), pin['path'])


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
    timestamp(reg.get('registered_at'))


def verify_execution_pins(m, selected=None):
    executables = {spec['argv'][0] for arms in m['commands'].values()
                   for commands in arms.values() for spec in commands.values()}
    for pin in m['execution_pins']:
        if selected is not None and pin['path'] not in selected:
            continue
        path = Path(pin['path'])
        if pin['path'] in executables and (not path.is_file() or not os.access(path, os.X_OK)):
            raise ValueError('command executable is not executable: ' + str(path))
        if not path.is_absolute() or not pin.get('role') or digest(path) != pin['sha256']:
            raise ValueError('live execution pin mismatch: ' + pin['path'])


def bind_executables(m):
    """Freeze PATH lookup to an absolute argv[0]; offline analysis needs no host."""
    for arms in m['commands'].values():
        for commands in arms.values():
            for spec in commands.values():
                executable = spec['argv'][0]
                if not Path(executable).is_absolute():
                    executable = shutil.which(executable, path=spec['env']['PATH'])
                    if executable is None:
                        raise ValueError('command executable not found in explicit PATH')
                    spec['argv'][0] = executable
