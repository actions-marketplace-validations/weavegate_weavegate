#!/usr/bin/env python3
"""Descriptive local timing observations; never an E1 confirmation campaign."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import time
from datetime import datetime, timezone


DETERMINISTIC = ('scenario.json', 'observation.json', 'trace.json',
                 'schedule.json', 'report.md')
STAGES = {'preflight', 'fixture_provision', 'fixture_reset', 'jar_snapshot',
          'process_start_to_ready', 'block_recognition', 'step_wait',
          'schedule_execute', 'oracle_evaluate', 'adapter_stop', 'fixture_teardown'}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pin_path(pin):
    return pin['path'].replace('{repository}/', str(Path.cwd()) + '/')


def utc():
    return datetime.now(timezone.utc).isoformat()


def inventory(root):
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*'))
            if p.is_file() and p != root / 'SHA256SUMS.json'}


def verify(root):
    if read(root / 'SHA256SUMS.json') != inventory(root):
        raise ValueError('retained evidence checksum mismatch')


def command(argv):
    return subprocess.check_output(argv, text=True, timeout=30).strip()


def containers():
    return sorted(command(['docker', 'ps', '-aq', '--no-trunc']).split())


def run_command(argv, cwd, env, trial):
    receipt = {'argv': argv, 'cwd': cwd, 'env': env, 'started_at': utc(),
               'started': False, 'exit': None, 'duration_ns': None, 'timed_out': False,
               'interrupted': False, 'error': None}
    write(trial / 'command.json', receipt)
    child = None
    start = time.monotonic_ns()
    try:
        with (trial / 'stdout.log').open('wb') as out, (trial / 'stderr.log').open('wb') as err:
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out,
                                     stderr=err, start_new_session=True)
            receipt['started'] = True
            write(trial / 'command.json', receipt)
            try:
                receipt['exit'] = child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                receipt['timed_out'] = True
            except KeyboardInterrupt:
                receipt['interrupted'] = True
            if receipt['timed_out'] or receipt['interrupted']:
                try:
                    os.killpg(child.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    receipt['exit'] = child.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    receipt['exit'] = child.wait(timeout=5)
    except BaseException as error:
        receipt['error'] = type(error).__name__ + ': ' + str(error)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            receipt['exit'] = child.wait(timeout=5)
    finally:
        receipt['duration_ns'] = time.monotonic_ns() - start
        receipt['ended_at'] = utc()
        write(trial / 'command.json', receipt)
    return receipt


def processes():
    # PID + kernel start tick prevents PID reuse from hiding a new child.
    # Only identify the experiment's snapshot JVMs; never retain environments.
    result = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            data = path.read_bytes()
            if b'weavegate-jar-' in data and b'java' in data:
                stat = (path.parent / 'stat').read_text().rsplit(')', 1)[1].split()
                result.append([int(path.parent.name), stat[19]])
        except (OSError, IndexError):
            continue
    return sorted(result)


def decomposition(spans, wall_ns):
    """Partition wall coverage, counting overlapping intervals only once."""
    by_id = {s['id']: s for s in spans}
    roots = [s for s in spans if s['parent_id'] == 0]
    if len(by_id) != len(spans) or len(roots) != 1 or roots[0]['name'] != 'command':
        raise ValueError('invalid timing tree')
    root = roots[0]
    if root['duration_ns'] > wall_ns:
        raise ValueError('instrumented command exceeds whole-process duration')
    cumulative = {}
    categorized = []
    for s in spans:
        if s['start_ns'] < 0 or s['duration_ns'] < 0:
            raise ValueError('negative interval')
        cumulative[s['name']] = cumulative.get(s['name'], 0) + s['duration_ns']
        ancestry = [s]
        while ancestry[-1]['parent_id']:
            parent = by_id[ancestry[-1]['parent_id']]
            child = ancestry[-1]
            if parent in ancestry:
                raise ValueError('cyclic timing tree')
            if (parent['start_ns'] > child['start_ns'] or
                    parent['start_ns'] + parent['duration_ns'] < child['start_ns'] + child['duration_ns']):
                raise ValueError('child outside parent interval')
            ancestry.append(parent)
        category = next((a['name'] for a in ancestry if a['name'] in STAGES), 'bookkeeping')
        categorized.append((s, len(ancestry), category))
    edges = sorted({t for s in spans for t in (s['start_ns'], s['start_ns'] + s['duration_ns'])})
    exclusive = {'outside_timing': wall_ns - root['duration_ns']}
    for start, end in zip(edges, edges[1:]):
        active = [(depth, category) for s, depth, category in categorized
                  if s['start_ns'] <= start and end <= s['start_ns'] + s['duration_ns']]
        if not active:
            continue
        depth = max(d for d, _ in active)
        names = sorted({name for d, name in active if d == depth})
        name = names[0] if len(names) == 1 else 'overlap:' + '+'.join(names)
        exclusive[name] = exclusive.get(name, 0) + end - start
    if sum(exclusive.values()) != wall_ns:
        raise ValueError('wall coverage does not reconcile')
    return {'exclusive_ns': exclusive, 'cumulative_ns': cumulative,
            'schedule_runs': sum(s['name'] == 'schedule_run' for s in spans),
            'schedule_ids': [s['schedule_id'] for s in spans if s['name'] == 'schedule_run']}


def inspect_trial(path, spec, arm):
    receipt = read(path / 'command.json')
    problems = []
    if receipt['exit'] != spec['exit'] or receipt['timed_out'] or receipt['interrupted'] or receipt['error']:
        problems.append('unexpected_exit_or_timeout')
    runs = list((path / 'output').glob('runs/*/manifest.json'))
    if len(runs) != 1:
        return {'problems': problems + ['missing_unique_run'], 'duration_ns': receipt['duration_ns']}
    run = runs[0].parent
    manifest, observation, scenario = [read(run / n) for n in
                                        ('manifest.json', 'observation.json', 'scenario.json')]
    expected = spec['observation']
    for key, value in expected.items():
        if observation.get(key) != value:
            problems.append('observation:' + key)
    if manifest['cleanup_failed']:
        problems.append('fixture_cleanup_failed')
    if scenario.get('schedule', {}).get('id') != spec['schedule_id']:
        problems.append('schedule_id')
    diagnostics = observation['diagnostics']
    if spec['exit'] == 2 and not any(d['code'] == 'WG001' and
            d.get('assertion') == 'one-active-reservation-per-seat' for d in diagnostics):
        problems.append('expected_WG001_missing')
    if spec['exit'] == 0 and observation['assertion_violations']:
        problems.append('unexpected_assertion_rows')
    cleanup = read(path / 'cleanup.json')
    if not cleanup['no_new_containers'] or not cleanup['no_new_snapshot_jvms']:
        problems.append('cleanup_not_observed')
    hashes = {n: sha(run / n) if (run / n).exists() else None for n in DETERMINISTIC}
    if any(hashes[n] is None for n in DETERMINISTIC if n != 'schedule.json'):
        problems.append('missing_artifact')
    result = {'problems': problems, 'duration_ns': receipt['duration_ns'],
              'deterministic_sha256': hashes, 'artifact_bytes': sum(p.stat().st_size for p in run.iterdir()),
              'manifest_facts': {k: manifest[k] for k in ('schema_version', 'seed_data', 'isolation_level',
                                                        'engine', 'adapter', 'variant', 'image', 'sut_sha256')}}
    if arm == 'Bplus':
        timing = manifest.get('timings', {})
        if timing.get('scope') != 'before_artifact_write':
            problems.append('missing_timing')
        else:
            result['stages'] = decomposition(timing['spans'], receipt['duration_ns'])
            if result['stages']['schedule_runs'] != spec['schedule_runs']:
                problems.append('timed_execution_count')
            if result['stages']['schedule_ids'] != spec['schedule_ids']:
                problems.append('timed_candidate_order')
    return result


def collect(plan_path, destination, registration):
    plan_path, root = Path(plan_path).resolve(), Path(destination).resolve()
    plan = read(plan_path)
    # Admission happens before creating a destination or starting a gate.
    if command(['git', 'rev-parse', registration + ':' + str(plan_path.relative_to(Path.cwd()))]) != command(
            ['git', 'hash-object', str(plan_path)]):
        raise ValueError('plan differs from the published registration')
    for pin in plan['execution_pins']:
        if sha(pin_path(pin)) != pin['sha256']:
            raise ValueError('changed input: ' + pin['path'])
    root.mkdir()
    write(root / 'plan.json', plan)
    write(root / 'registration.json', {'revision': registration, 'plan_sha256': sha(plan_path),
                                      'collection_started_at': utc()})
    stopped = None
    try:
        for block in plan['order']:
            if stopped:
                break
            spec = plan['workloads'][block['workload']]
            paired = {}
            for arm in block['arms']:
                trial = root / (block['id'] + '-' + arm)
                trial.mkdir()
                for pin in plan['execution_pins']:
                    if sha(pin_path(pin)) != pin['sha256']:
                        raise ValueError('changed input: ' + pin['path'])
                before = containers()
                before_jvms = processes()
                argv = [plan['binaries'][arm], *spec['argv'], '--out', str(trial / 'output')]
                env = dict(plan['environment_allowlist'])
                receipt = run_command(argv, spec['cwd'], env, trial)
                after_exit = containers()
                waits = []
                # Event-coordinated wait for the CLI's reaper container to exit.
                # Never delete or stop other applications' containers.
                for cid in sorted(set(after_exit) - set(before)):
                    try:
                        result = subprocess.run(['docker', 'wait', cid], capture_output=True,
                                                text=True, timeout=30)
                        waits.append({'id': cid, 'exit': result.returncode, 'stdout': result.stdout,
                                      'stderr': result.stderr})
                    except subprocess.TimeoutExpired:
                        waits.append({'id': cid, 'timeout': True})
                after = containers()
                after_jvms = processes()
                write(trial / 'cleanup.json', {'before_containers': before, 'after_exit_containers': after_exit,
                      'after_containers': after, 'waits': waits, 'before_snapshot_jvms': before_jvms,
                      'after_snapshot_jvms': after_jvms,
                      'no_new_containers': not (set(after) - set(before)),
                      'no_new_snapshot_jvms': not any(p not in before_jvms for p in after_jvms)})
                paired[arm] = inspect_trial(trial, spec, arm)
                print(block['id'], arm, receipt['exit'], round(receipt['duration_ns'] / 1e9, 3), paired[arm]['problems'], flush=True)
                if not read(trial / 'cleanup.json')['no_new_containers'] or not read(trial / 'cleanup.json')['no_new_snapshot_jvms']:
                    stopped = 'cleanup_not_observed'
                    break
                if receipt['interrupted']:
                    stopped = 'operator_interruption'
                    break
            if len(paired) == 2:
                if any(t['problems'] for t in paired.values()):
                    stopped = 'artifact_or_execution_check_failed'
                elif (paired['B']['deterministic_sha256'] != paired['Bplus']['deterministic_sha256'] or
                      paired['B']['manifest_facts'] != paired['Bplus']['manifest_facts']):
                    stopped = 'paired_evidence_mismatch'
            write(root / 'status.json', {'stopped': stopped, 'last_block': block['id'], 'updated_at': utc()})
    except BaseException as error:
        write(root / 'status.json', {'stopped': type(error).__name__ + ': ' + str(error), 'updated_at': utc()})
        raise
    finally:
        write(root / 'SHA256SUMS.json', inventory(root))


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    def quantile(p):
        i = (len(ordered) - 1) * p
        low = int(i)
        return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (i - low)
    return {'n': len(values), 'mean': statistics.mean(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values), 'iqr': quantile(.75) - quantile(.25),
            'sample_sd': statistics.stdev(values) if len(values) > 1 else None}


def analyze(root, output):
    root = Path(root)
    verify(root)
    plan = read(root / 'plan.json')
    rows, estimates = [], {}
    for workload, spec in plan['workloads'].items():
        pairs, stages = [], []
        for block in [b for b in plan['order'] if b['workload'] == workload]:
            trials = {}
            for arm in block['arms']:
                path = root / (block['id'] + '-' + arm)
                if (path / 'command.json').exists() and (path / 'cleanup.json').exists():
                    trials[arm] = inspect_trial(path, spec, arm)
                rows.append({'block': block['id'], 'workload': workload, 'arm': arm,
                             'result': trials.get(arm), 'state': 'observed' if arm in trials else 'not_completed'})
            if 'Bplus' in trials and 'stages' in trials['Bplus'] and not trials['Bplus']['problems']:
                stages.append(trials['Bplus'])
            if len(trials) == 2 and not any(t['problems'] for t in trials.values()):
                if trials['B']['deterministic_sha256'] == trials['Bplus']['deterministic_sha256'] and trials['B']['manifest_facts'] == trials['Bplus']['manifest_facts']:
                    pairs.append(trials)
                else:
                    rows[-1]['pair_problem'] = 'paired_evidence_mismatch'
        ratios = [p['Bplus']['duration_ns'] / p['B']['duration_ns'] for p in pairs]
        stage_names = sorted({k for t in stages for k in t['stages']['exclusive_ns']})
        estimates[workload] = {
            'planned_pairs': sum(b['workload'] == workload for b in plan['order']),
            'artifact_matched_pairs': len(pairs),
            'B_seconds': summary([p['B']['duration_ns'] / 1e9 for p in pairs]),
            'Bplus_seconds': summary([p['Bplus']['duration_ns'] / 1e9 for p in pairs]),
            'Bplus_over_B_geometric_mean': math.exp(statistics.mean(map(math.log, ratios))) if ratios else None,
            'log_ratio_sample_sd': statistics.stdev(map(math.log, ratios)) if len(ratios) > 1 else None,
            'exclusive_stage_seconds': {k: summary([t['stages']['exclusive_ns'].get(k, 0) / 1e9 for t in stages]) for k in stage_names},
            'exclusive_stage_wall_fraction': {k: summary([t['stages']['exclusive_ns'].get(k, 0) / t['duration_ns'] for t in stages]) for k in stage_names}}
    write(output, {'scope': 'descriptive local tuning; no E1 quality or improvement claim',
                   'estimates': estimates, 'trials': rows, 'status': read(root / 'status.json')})
    print('LOCAL_PILOT_ANALYSIS_RESULT checksums=verified planned_pairs=' + str(len(plan['order'])))


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt('signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    collect_parser = sub.add_parser('collect')
    collect_parser.add_argument('plan')
    collect_parser.add_argument('destination')
    collect_parser.add_argument('--registration', required=True)
    analyze_parser = sub.add_parser('analyze')
    analyze_parser.add_argument('bundle')
    analyze_parser.add_argument('output')
    args = parser.parse_args()
    if args.command == 'collect':
        collect(args.plan, args.destination, args.registration)
    else:
        analyze(args.bundle, args.output)
