"""One evidence reducer for collection and offline analysis; no product verdicts."""
from pathlib import Path

from common import (child, counter, digest, json_equal, positive, read_json, timestamp)
from execution import expand_command, succeeded
from plan import STEPS
from storage import COMMAND_FILES, CONTROL


def observation(m, row, directory, elapsed_seconds, receipt=None):
    """Validate at the observation boundary and again during offline reduction."""
    if receipt is None:
        receipt = read_json(directory / 'receipt.json')
    if not isinstance(receipt, dict) or not isinstance(receipt.get('metrics'), dict):
        raise ValueError('missing/malformed observation receipt')
    # Join the clock in memory; never rewrite the observer's retained bytes.
    joined = dict(receipt, metrics=dict(receipt['metrics']))
    if m['experiment'] == 'E1':
        metric = 'adoption_seconds' if m['phase'] == 'adoption-cold' else 'gate_seconds'
        joined['metrics'][metric] = elapsed_seconds
    receipt_valid(m, row, joined, directory)


def receipt_valid(m, row, receipt, directory):
    if not isinstance(receipt, dict):
        raise ValueError('observation receipt must be an object')
    for name in ('checks', 'facts', 'metrics', 'evidence'):
        if not isinstance(receipt.get(name), dict):
            raise ValueError('observation ' + name + ' must be an object')
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
    hashes_valid(directory, evidence)
    metric = 'gate_seconds' if m['experiment'] == 'E1' else 'validation_seconds'
    if m['experiment'] == 'E2':
        from ci import endpoint, workflow_bounds
        if receipt.get('ci_snapshot') not in evidence:
            raise ValueError('CI snapshot must be included in original evidence')
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


def hashes_valid(directory, evidence):
    directory = Path(directory)
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError('missing original evidence')
    for name, expected in evidence.items():
        path = child(directory, name)
        if path.parent == directory and path.name in CONTROL | COMMAND_FILES:
            raise ValueError('collector-owned file is not original evidence: ' + name)
        if not isinstance(expected, str) or digest(path) != expected:
            raise ValueError('retained evidence hash mismatch: ' + str(name))


def cleanup_inventory(directory):
    """Capture paths and inode identities before teardown can introduce aliases."""
    from common import bundle_inventory
    paths = bundle_inventory(directory)
    # Keep all pre-cleanup files, including controls. The collector defers their
    # replacement until proof validation so freed inodes cannot be reused by
    # new cleanup originals while these identities are authoritative.
    identities = []
    for name, value in paths.items():
        path = child(directory, name)
        if value is not None:
            stat = path.stat()
            identities.append([stat.st_dev, stat.st_ino])
    return {'paths': paths, 'identities': identities}


def cleanup_valid(directory):
    command = read_json(directory / 'cleanup.command.json')
    if not succeeded(command):
        raise ValueError('cleanup command failed')
    proof = read_json(directory / 'cleanup.json')
    if not isinstance(proof, dict) or proof.get('proven') is not True:
        raise ValueError('missing cleanup proof')
    hashes_valid(directory, proof.get('evidence'))
    inputs = read_json(directory / 'cleanup-inputs.json')
    if (not isinstance(inputs, dict) or not isinstance(inputs.get('paths'), dict)
            or not inputs['paths'] or not isinstance(inputs.get('identities'), list)):
        raise ValueError('missing cleanup input inventory')
    identities = inputs['identities']
    inputs = inputs['paths']
    # The inventory cannot record its own inode before it is written. Reject
    # aliases of live controls too, including cleanup-inputs.json itself.
    for name in CONTROL:
        path = directory / name
        if path.exists():
            stat = path.stat()
            identities.append([stat.st_dev, stat.st_ino])
    if 'cleanup.json' in inputs or any(
            str(child(directory, name).relative_to(directory)) in inputs for name in proof['evidence']):
        raise ValueError('cleanup proof must retain originals created by cleanup')
    for name in proof['evidence']:
        stat = child(directory, name).stat()
        if [stat.st_dev, stat.st_ino] in identities:
            raise ValueError('cleanup proof aliases pre-cleanup evidence')


EVIDENCE_ERRORS = (ValueError, OSError, KeyError, TypeError, AttributeError)


def command_valid(command, spec, context, step):
    """Bind a retained receipt to the frozen invocation, not just its exit."""
    argv, cwd, env = expand_command(spec, context)
    env = {k: v for k, v in env.items() if k not in context['redact_env']}
    expected = {'step': step, 'argv': argv, 'cwd': cwd, 'env': env,
                'redacted_env_names': context['redact_env']}
    if any(not json_equal(command.get(k), v) for k, v in expected.items()):
        raise ValueError('command differs from frozen invocation: ' + step)
    if (any(type(command.get(k)) is not bool for k in
            ('started', 'timeout', 'interrupted', 'finished', 'containment_proven')) or
            command['exit'] is not None and type(command['exit']) is not int or
            command['error'] is not None and not isinstance(command['error'], str)):
        raise ValueError('invalid command outcome: ' + step)
    begin = timestamp(command['started_at'])
    if begin <= context['registered_at']:
        raise ValueError('measurement predates registration: ' + step)
    if command['finished']:
        if not counter(command.get('seconds')) or timestamp(command['ended_at']) < begin:
            raise ValueError('invalid command timing: ' + step)
        if (command['started'] and not command['timeout'] and not command['interrupted']
                and command['error'] is None and command['seconds'] >= spec['timeout_seconds']):
            raise ValueError('command exceeded registered budget: ' + step)
    if step in ('prepare', 'run') and begin.date().isoformat() != context['date']:
        raise ValueError('command ran outside registered UTC date: ' + step)
    return begin


def trial(root, m, slot, arm):
    """Reduce raw receipts for every outcome; trial.json is only a cached view."""
    directory = root / 'blocks' / slot['block'] / arm
    row = dict(slot, arm=arm, attempt=1, started=False, classification='not_started',
               reason='slot_not_collected', metrics={}, facts={}, exit=None,
               elapsed_seconds=None, cleanup_proven=False)
    if not (directory / 'attempt.json').exists():
        if directory.parent.exists():
            row['reason'] = 'interrupted_collection'
        return row
    state = read_json(directory / 'attempt.json')
    if not isinstance(state, dict):
        raise ValueError('invalid attempt state')
    context = dict(trial_dir=Path(state['collection_root']) / 'blocks' / slot['block'] / arm,
                   campaign_dir=state['collection_root'], block_id=slot['block'], arm=arm,
                   redact_env=m['redact_env'], date=slot['date'],
                   registered_at=timestamp(read_json(root / 'registration.json')['registered_at']))
    commands = {}
    validation_error = None
    previous_end = None
    for step in STEPS:
        path = directory / (step + '.command.json')
        if path.exists():
            try:
                commands[step] = read_json(path)
                begin = command_valid(commands[step], m['commands'][slot['workload']][arm][step], context, step)
                if previous_end is not None and begin < previous_end:
                    raise ValueError('command chronology differs from registered sequence: ' + step)
                if commands[step]['finished']:
                    previous_end = timestamp(commands[step]['ended_at'])
            except EVIDENCE_ERRORS as error:
                validation_error = str(error)
    run = commands.get('run', {})
    if not isinstance(run, dict):
        run = {}
    row.update(started=run.get('started') is True,
               exit=run.get('exit') if type(run.get('exit')) is int else None,
               elapsed_seconds=run.get('seconds') if counter(run.get('seconds')) else None)
    receipt = None
    try:
        receipt = read_json(directory / 'receipt.json')
        if isinstance(receipt, dict):
            metrics = receipt.get('metrics', {})
            if isinstance(metrics, dict):
                row['metrics'] = {k: v for k, v in metrics.items() if counter(v)}
            if isinstance(receipt.get('facts'), dict):
                row['facts'] = receipt['facts']
    except EVIDENCE_ERRORS:
        pass
    if m['experiment'] == 'E1' and row['elapsed_seconds'] is not None:
        metric = 'adoption_seconds' if m['phase'] == 'adoption-cold' else 'gate_seconds'
        row['metrics'][metric] = row['elapsed_seconds']
    try:
        if state.get('cleanup_inputs_valid') is True and all(
                isinstance(c, dict) and c.get('containment_proven') is True for c in commands.values()):
            cleanup_valid(directory)
            row['cleanup_proven'] = True
    except EVIDENCE_ERRORS:
        pass
    # An arm skipped before preparation has no allocation to clean up.
    if state.get('reason') in ('registered_date_expired', 'cleanup_unproven') and not commands:
        row['reason'] = state['reason']
        return row
    row.update(classification='operational_failure', reason='interrupted_collection')
    if not row['cleanup_proven']:
        row.update(classification='quality_failure', reason='cleanup_unproven')
    elif validation_error:
        row.update(classification='quality_failure', reason=validation_error)
    elif state.get('observation_error'):
        row.update(classification='quality_failure', reason=state['observation_error'])
    elif state.get('reason') == 'registered_date_expired' and not run:
        row.update(classification='not_started', reason='registered_date_expired')
    elif state.get('reason'):
        row['reason'] = state['reason']
    elif not succeeded(commands.get('prepare', {})):
        row['reason'] = 'prepare_failed'
    elif not run or run.get('interrupted') or not run.get('finished'):
        pass
    elif run.get('timeout'):
        row['reason'] = 'budget_expired'
    elif run.get('error') or not row['started']:
        row['reason'] = 'execution_failed' if row['started'] else 'spawn_failed'
    elif run['exit'] not in m['expected_exit'][slot['workload']][arm]:
        row['reason'] = 'unexpected_exit'
        if run['exit'] in (0, 2, 3):
            row.update(classification='quality_failure',
                       reason='flaky_exit' if run['exit'] == 3 else 'unexpected_outcome')
    else:
        row.update(classification='quality_failure', reason='evidence_incomplete')
        try:
            if not all(succeeded(commands.get(step, {})) for step in ('observe', 'check')):
                return row
            if state.get('state') != 'finished':
                raise ValueError('interrupted_collection')
            observation(m, row, directory, row['elapsed_seconds'], receipt)
            row.update(classification='complete', reason='')
        except EVIDENCE_ERRORS as error:
            row['reason'] = str(error)
    return row
