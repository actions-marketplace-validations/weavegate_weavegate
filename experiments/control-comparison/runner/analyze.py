"""Reduce original Oracle/product evidence. Never infer a verdict from SQL or exits."""
from collections import Counter, defaultdict
import html
import runpy
import shlex
import signal
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support',)})
from support import *


def stats(values):
    values = sorted(v for v in values if v is not None)
    def q(p):
        if not values: return None
        at = (len(values) - 1) * p
        low = int(at)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (at - low)
    return {'n': len(values), 'median': q(.5), 'p90': q(.9),
            'min': min(values) if values else None, 'max': max(values) if values else None}


def bound(local_end, local_duration, whole):
    if any(type(v) not in (int, float) for v in (local_end, local_duration, whole)):
        return None
    if not 0 <= local_end <= local_duration <= whole:
        return None
    return [local_end, local_end + whole - local_duration]


def membership(interval, cap):
    if interval is None: return 'unknown'
    if interval[1] <= cap: return 'in_budget'
    if interval[0] > cap: return 'late'
    return 'budget_membership_unknown'


def stages(spans, seconds, allow_missing=False):
    """Intervals stay on their own clock; nested coverage uses union length."""
    require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0,
            'invalid invocation timing duration')
    totals = defaultdict(list)
    intervals = []
    roots = [s for s in spans if s['name'] == 'command']
    if allow_missing and not spans: return {'missing': 'command timing unavailable'}
    require(len(roots) == 1, 'one command timing root required')
    root = roots[0]
    for span in spans:
        require(all(type(span.get(key)) is int and span[key] >= 0
                    for key in ('id', 'parent_id', 'start_ns', 'duration_ns')),
                'invalid timing clock/hierarchy value')
    require(root['id'] == 1 and root['parent_id'] == 0, 'invalid command timing root')
    require(root['duration_ns'] / 1e9 <= seconds, 'command timing exceeds invocation')
    start, end = root['start_ns'], root['start_ns'] + root['duration_ns']
    for index, s in enumerate(spans):
        require(s['id'] == index + 1 and 0 <= s['parent_id'] < s['id'], 'invalid timing hierarchy')
        require(index == 0 or s['parent_id'] > 0, 'unrooted timing span')
        a, b = s['start_ns'], s['start_ns'] + s['duration_ns']
        require(0 <= s['duration_ns'] and start <= a <= b <= end, 'invalid timing interval')
        if s['parent_id']:
            parent = spans[s['parent_id'] - 1]
            require(parent['start_ns'] <= a <= b <= parent['start_ns'] + parent['duration_ns'], 'span outside parent')
        totals[s['name']].append(s['duration_ns'] / 1e9)
        if s['name'] not in ('command', 'execution', 'schedule_run', 'replay', 'explore_pass'):
            intervals.append((a, b))
    union, previous = 0, start
    for a, b in sorted(intervals):
        union += max(0, b - max(a, previous))
        previous = max(previous, b)
    remainder = seconds - union / 1e9
    require(remainder >= 0, 'negative outer remainder')
    return {'durations_seconds': dict(totals), 'exclusive_covered_seconds': union / 1e9,
            'remainder_seconds': remainder, 'clock': 'arm-process-monotonic'}


def domain_rows_match(violations, assertion, expected):
    actual = [{k: str(v) for k, v in row.items()} for violation in violations
              if violation['oracle_id'] == assertion for row in violation['rows']]
    return bool(expected.get('expected_domain_rows')) and actual == expected['expected_domain_rows']


def baseline(root, slot, command, expected):
    result = {'valid_evaluations': 0, 'domain_violations': 0, 'attempted': 0,
              'quality_reasons': [], 'operational_reasons': [], 'first_event_seconds': None,
              'stable_confirmation': False, 'cleanup_proven': False, 'executions': []}
    raw = root / 'raw'
    final_receipt = (raw / 'run.json').is_file()
    cap_killed = (not final_receipt and command['timeout']
                  and command['exit'] in (-signal.SIGTERM, -signal.SIGKILL,
                                          128 + signal.SIGTERM, 128 + signal.SIGKILL)
                  and command['started'] and command['finished'] and not command['interrupted']
                  and command['error'] is None and command['containment_proven'])
    if final_receipt:
        run = read_json(raw / 'run.json')
        require(run.get('arm') == slot['arm'] and run.get('variant') == slot['variant'],
                'baseline identity differs from requested slot')
        require(run.get('environment', {}).get('image') == expected['config']['Target']['DB'],
                'baseline image differs from effective config')
    else:
        # A killed process may have already published valid evaluations. Keep
        # those observations; missing final cleanup/timing cannot erase them.
        run = {'cleanup_proven': False, 'timings': {'spans': []}}
        if cap_killed:
            # Allocation cleanup is independently required by reduce_slot.
            # A killed driver cannot publish its deferred final receipt.
            result['evidence_absence'] = 'collection_cap_before_publication'
        else:
            result['operational_reasons'].append('missing final baseline receipt')
    result['cleanup_proven'] = run.get('cleanup_proven') is True
    if not result['cleanup_proven'] and not cap_killed:
        result['operational_reasons'].append('fixture cleanup unproven')
    if run.get('error') and not (command['timeout'] and run.get('cancellation_only') is True):
        result['operational_reasons'].append(run['error'])
    if command['exit'] != 0 and not command['timeout']:
        result['operational_reasons'].append('baseline operational exit')
    whole = command['seconds']
    result['stages'] = stages(run['timings']['spans'], whole, allow_missing=not final_receipt)
    if slot['arm'] == 'B0':
        if final_receipt:
            require(run['executions'] == 0 and run.get('seed') is not None, 'B0 lifecycle evidence missing')
        else:
            require(not list(raw.glob('execution-*.json')), 'B0 contains execution evidence')
        return result
    assertion = WORKLOADS[slot['workload']][2]
    names = [a['ID'] for a in expected['config']['Oracle']['Assertions']]
    executions = sorted(raw.glob('execution-*.json'))
    for number, path in enumerate(executions, 1):
        value = read_json(path)
        require(value['index'] == number, 'execution gap or duplicate')
        result['attempted'] += 1
        evaluation = value.get('evaluation')
        valid = (evaluation is not None and (value.get('error') is None or value.get('cancellation_only') is True)
                 and value.get('child_reaped') is True and value.get('connections_closed') is True
                 and value.get('reset_verified') is True)
        if 'seed' in run:
            require(value['seed'] == run['seed'], 'reset seed differs')
        if evaluation is not None:
            require([r['oracle_id'] for r in evaluation['results']] == names, 'Oracle inventory differs')
            require(isinstance(evaluation['fingerprint'], str) and len(evaluation['fingerprint']) == 64,
                    'missing Oracle fingerprint')
            violations = [v for r in evaluation['results'] for v in r['violations']]
        else:
            violations = []
        domain = any(v['oracle_id'] == assertion and v['rows'] for v in violations)
        if domain and not domain_rows_match(violations, assertion, expected):
            result['quality_reasons'].append('domain assertion rows differ')
        if any(v['oracle_id'] != assertion for v in violations):
            result['quality_reasons'].append('receipt assertion failed')
        if domain and slot['variant'] == 'fixed': result['quality_reasons'].append('fixed domain violation')
        interval = None
        if evaluation and 'process_elapsed_ns' in run:
            ends = value['oracle_ends_ns']
            require(type(value['oracle_end_ns']) is int and type(run['process_elapsed_ns']) is int
                    and 0 <= value['oracle_end_ns'] <= run['process_elapsed_ns']
                    and isinstance(ends, dict) and set(ends) == set(names)
                    and all(type(v) is int and 0 <= v <= value['oracle_end_ns'] for v in ends.values()),
                    'invalid per-assertion completion times')
            require([ends[name] for name in names] == sorted(ends.values()),
                    'per-assertion completion order differs')
            interval = bound(ends[assertion] / 1e9, run['process_elapsed_ns'] / 1e9, whole)
        if valid and domain and result['first_event_seconds'] is None: result['first_event_seconds'] = interval
        result['executions'].append({'index': number, 'valid': valid, 'domain': domain,
                                     'fingerprint': evaluation['fingerprint'] if evaluation else None,
                                     'event_seconds': interval, 'budget_membership': membership(interval, slot['cap_seconds'])})
        result['valid_evaluations'] += int(valid)
        result['domain_violations'] += int(valid and domain)
        if not valid and not (command['timeout'] and value.get('cancellation_only') is True
                              and value.get('child_reaped') and value.get('connections_closed')):
            result['operational_reasons'].append('execution incomplete')
    if 'executions' in run and len(executions) != run['executions']:
        result['operational_reasons'].append('missing execution evidence')
    planned = expected['counts'][slot['arm']]
    result['planned_executions'] = planned
    result['unobserved'] = planned - result['valid_evaluations']
    result['unobserved_slots'] = [
        {'index': i, 'reason': ('execution_incomplete' if i <= len(executions) else
                               'collection_cap' if command['timeout'] else 'invocation_stopped')}
        for i in range(1, planned + 1)
        if i > len(result['executions']) or not result['executions'][i-1]['valid']]
    result['x_over_m'] = (result['domain_violations'] / result['valid_evaluations']
                          if result['valid_evaluations'] else None)
    result['x_over_planned'] = result['domain_violations'] / planned
    require(0 <= len(executions) <= planned, 'execution count exceeds plan')
    if len(executions) != planned and not command['timeout']: result['operational_reasons'].append('missing planned executions')
    result['usable_first_event_seconds'] = None
    if (root / 'stream.json').exists():
        stream = read_json(root / 'stream.json')
        require(stream['clock'] == 'collector-monotonic' and stream['error'] is None
                and all(type(v) is int and v >= 0 for v in
                        (stream['start_monotonic_ns'], stream['envelope_ns'], command['collector_start_ns'])),
                'stream observation clock invalid')
        offset = command['collector_start_ns'] - stream['start_monotonic_ns']
        require(0 <= offset and whole <= (stream['envelope_ns'] - offset) / 1e9,
                'stream observation does not contain invocation')
        require(isinstance(stream['receipts'], dict), 'stream receipts invalid')
        execution_names = {str(p.relative_to(root)) for p in executions}
        for name, receipt in stream['receipts'].items():
            require(name in execution_names, 'stream receipt execution missing')
            require(type(receipt['received_ns']) is int
                    and offset <= receipt['received_ns'] <= stream['envelope_ns'], 'stream receipt time invalid')
            require(digest(root / name) == receipt['sha256'], 'streamed evidence changed')
        for item in result['executions']:
            name = 'raw/execution-%03d.json' % item['index']
            receipt = stream['receipts'].get(name)
            if receipt and item['valid'] and item['domain']:
                t = (receipt['received_ns'] - offset) / 1e9
                result['usable_first_event_seconds'] = [t, t]
                break
    return result


def validate_trace(trace, saved, slot):
    """Validate intents with the product's existing blocking/terminal semantics."""
    require(trace['schedule_ref'] == saved['id'], 'trace schedule differs')
    require(len(trace['terminals']) == 2 and all(t['state'] == 'done' and t['failure_class'] == 'none'
                                               for t in trace['terminals']), 'terminal failure')
    workers = [t.get('worker') for t in trace['terminals']]
    require(all(isinstance(w, str) and w for w in workers) and len(set(workers)) == len(workers)
            and set(workers) == {s['worker'] for s in saved['steps']}, 'terminal workers differ from schedule')
    events = trace['events']
    require([e['seq'] for e in events] == list(range(1, len(events) + 1)), 'trace sequence differs')
    require(not any(e['kind'] == 'worker_failed' for e in events), 'worker failure')
    resolved = [e for e in events if e['kind'] in ('point_released', 'step_terminal_skipped')]
    require([{'worker': e['worker'], 'point': e['point']} for e in resolved] == saved['steps'],
            'requested/resolved schedule differs')
    require([e['step'] for e in resolved] == list(range(len(saved['steps']))), 'resolved step index differs')
    for event in resolved:
        if event['kind'] == 'step_terminal_skipped':
            # Matching fixed returns after observing the assignment made by the
            # lock holder. Existing paired acceptance expects that shorter path.
            require(slot['workload'] == 'matching' and slot['variant'] == 'fixed'
                    and event['point'] == 'before_insert_assignment'
                    and any(e['kind'] == 'worker_done' and e['worker'] == event['worker']
                            and e['seq'] < event['seq'] for e in events), 'unexplained skipped step')
    for event in events:
        if event['kind'] == 'point_timeout':
            require(event['status'] == 'timeout_inferred'
                    and any(e['kind'] in ('point_released', 'step_terminal_skipped')
                            and e['worker'] == event['worker'] and e['point'] == event['point']
                            and e['seq'] > event['seq'] for e in events), 'unresolved inferred block')
    return {'inferred_blocks': sum(e['kind'] == 'point_timeout' for e in events),
            'terminal_skips': sum(e['kind'] == 'step_terminal_skipped' for e in events)}


def product(root, slot, command, expected):
    result = {'quality_reasons': [], 'operational_reasons': [], 'cleanup_proven': False,
              'first_event_seconds': None, 'stable_confirmation': False}
    manifests = list((root / 'raw').glob('runs/*/manifest.json'))
    runs = root / 'raw' / 'runs'
    if (not manifests and (not runs.exists() or not any(runs.iterdir()))
            and command['timeout'] and command['exit'] == 130
            and command['started'] and command['finished'] and not command['interrupted']
            and command['error'] is None and command['containment_proven']):
        # The CLI cancels before WriteRun. Allocation cleanup still requires
        # independent proof in the slot; absence is never a PASS or detection.
        result['evidence_absence'] = 'collection_cap_before_publication'
        stderr = root / 'run.stderr'
        if stderr.exists() and b'weavegate: warning: cleanup failed:' in stderr.read_bytes():
            result['operational_reasons'].append('product cleanup failed before publication')
        return result
    require(len(manifests) == 1, 'one original product run required')
    directory = manifests[0].parent
    m = read_json(manifests[0]); o = read_json(directory / 'observation.json')
    require(all(type(o.get(key)) is int and o[key] >= 0
                for key in ('repeat', 'violation_runs', 'schedules_explored', 'explore_passes')),
            'invalid product execution counts')
    require(type(o.get('flaky')) is bool and isinstance(o.get('fingerprints'), dict)
            and all(isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}', key)
                    and type(count) is int and count > 0 for key, count in o['fingerprints'].items()),
            'invalid product fingerprint counts')
    scenario = read_json(directory / 'scenario.json'); trace = read_json(directory / 'trace.json')
    merged = read_json(directory / 'report.json')
    require(merged['manifest'] == m and merged['scenario'] == scenario and merged['observation'] == o,
            'merged product evidence differs')
    require((directory / 'report.md').is_file(), 'missing product report')
    require(m['variant'] == slot['variant'] and m['adapter'] == 'external', 'product identity differs')
    require(m.get('image') == expected['config']['Target']['DB'], 'product image differs from effective config')
    require(o.get('mode') == {'T-R': 'replay', 'T-E': 'explore'}[slot['arm']],
            'product mode differs from requested arm')
    if slot['arm'] == 'T-R':
        require(o['schedules_explored'] == 0 and o['explore_passes'] == 0,
                'replay contains exploration')
    require(m['sut_sha256'] == 'sha256:' + expected['jar_sha256'] and m['isolation_level'] == 'REPEATABLE-READ', 'product input/isolation differs')
    require(o['oracles'] == [{'id': a['ID'], 'sql': a['SQL'], 'expect_rows': a['ExpectRows']}
                             for a in expected['config']['Oracle']['Assertions']], 'product assertions differ')
    result['cleanup_proven'] = m['cleanup_failed'] is False
    if not result['cleanup_proven']: result['operational_reasons'].append('product cleanup failed')
    result.update(violation_runs=o['violation_runs'], fingerprints=o['fingerprints'],
                  exploration_executions=o['schedules_explored'], discovery_fingerprint=o.get('discovery_fingerprint'),
                  selected_schedule=scenario.get('schedule', {}).get('id'), flaky=o['flaky'])
    spans = m.get('timings', {}).get('spans', [])
    result['stages'] = stages(spans, command['seconds'])
    assertion = WORKLOADS[slot['workload']][2]
    violations = o['assertion_violations']
    domain = any(v['oracle_id'] == assertion and v['rows'] for v in violations)
    outcome_reasons = []
    if any(v['oracle_id'] != assertion for v in violations): result['quality_reasons'].append('receipt assertion failed')
    if domain and slot['variant'] == 'fixed': outcome_reasons.append('fixed domain violation')
    if o['flaky'] or command['exit'] == 3: result['quality_reasons'].append('flaky')
    if command['exit'] not in (0, 2, 3) and not command['timeout']: result['operational_reasons'].append('product operational exit')
    repeat = expected['counts']['T-R']
    result['planned_confirmation_executions'] = repeat
    saved = scenario.get('schedule')
    result['search_exhausted'] = slot['arm'] == 'T-E' and not saved
    result['unexpected_miss'] = False
    if saved:
        require(saved in expected['candidates'], 'unregistered schedule')
        if slot['arm'] == 'T-R': require(saved['id'] == WORKLOADS[slot['workload']][6], 'replay schedule differs')
        elif not 1 <= o['explore_passes'] <= expected['config']['Run']['ExplorePasses']:
            result['quality_reasons'].append('discovery exceeds registered exploration pass limit')
        trace_valid = True
        try:
            result['trace_observations'] = validate_trace(trace, saved, slot)
        except ValueError as error:
            trace_valid = False
            result['quality_reasons'].append(str(error))
        stable = o['repeat'] == repeat and list(o['fingerprints'].values()) == [repeat] and not o['flaky']
        if not stable: result['quality_reasons'].append('repeat/fingerprint mismatch')
        want = repeat if slot['variant'] == 'vulnerable' else 0
        if o['violation_runs'] != want or (want > 0) != domain:
            # A fully reproduced violation in a fixed control is an unexpected
            # outcome, but remains valid detection evidence. Contradictory
            # counts/outcomes still invalidate the receipt.
            reasons = (outcome_reasons if slot['variant'] == 'fixed' and domain
                       and o['violation_runs'] == repeat else result['quality_reasons'])
            reasons.append('unexpected domain outcome')
        if slot['arm'] == 'T-E' and (not domain or not o.get('discovery_fingerprint')):
            result['quality_reasons'].append('saved exploration schedule lacks discovery evidence')
        if domain and not domain_rows_match(violations, assertion, expected):
            result['quality_reasons'].append('domain assertion rows differ')
        result['stable_confirmation'] = (stable and trace_valid and domain
                                         and o['violation_runs'] == repeat and slot['arm'] == 'T-E')
    elif slot['arm'] == 'T-R': result['quality_reasons'].append('missing replay')
    else:
        if o['schedules_explored'] != len(expected['candidates']) * 3 or o['explore_passes'] != 3:
            result['quality_reasons'].append('incomplete exhaustive search')
        if o['fingerprints'] or o.get('discovery_fingerprint') is not None:
            result['quality_reasons'].append('exhausted search contains confirmation/discovery fingerprints')
        if o['violation_runs'] != 0 or violations or o['flaky'] or command['exit'] != 0:
            result['quality_reasons'].append('exhausted search has non-clean outcome')
    if (command['exit'] == 0 and (domain or o['flaky'])) or (command['exit'] == 2 and (not domain or o['flaky'])):
        result['quality_reasons'].append('exit and retained outcome disagree')
    executions = [s for s in spans if s['name'] == 'schedule_run']
    if len(executions) != o['schedules_explored'] + (o['repeat'] if saved else 0):
        result['quality_reasons'].append('execution timing count differs')
    result['evaluated_executions'] = len(executions)
    for execution in executions:
        names = Counter(s['name'] for s in spans if s['parent_id'] == execution['id'])
        if any(names[n] != 1 for n in ('fixture_reset', 'adapter_create', 'adapter_start', 'schedule_execute', 'oracle_evaluate', 'adapter_stop')):
            result['quality_reasons'].append('execution stage missing/duplicated')
    byid = {s['id']: s for s in spans}
    passes = [s for s in spans if s['name'] == 'explore_pass']
    if len(passes) != o['explore_passes']:
        result['quality_reasons'].append('exploration pass count differs')
    for index, exploration in enumerate(passes):
        visited = [s.get('schedule_id') for s in executions if s['parent_id'] == exploration['id']]
        registered = [c['id'] for c in expected['candidates']]
        exhausted = not saved or index < len(passes) - 1
        if visited != registered[:len(visited)] or (exhausted and visited != registered):
            result['quality_reasons'].append('candidate traversal differs from frozen order')
    replayed = [s for s in executions if byid.get(s['parent_id'], {}).get('name') == 'replay']
    if saved and (len(replayed) != repeat or any(s.get('schedule_id') != saved['id'] for s in replayed)):
        result['quality_reasons'].append('confirmation execution identity/count differs')
        result['stable_confirmation'] = False
    if slot['arm'] == 'T-R' and domain and replayed and len(o['fingerprints']) == 1 and not o['flaky']:
        evaluations = [s for s in spans if s['name'] == 'oracle_evaluate' and s['parent_id'] == replayed[0]['id']]
        commands = [s for s in spans if s['name'] == 'command']
        if len(evaluations) == len(commands) == 1:
            event, cmd = evaluations[0], commands[0]
            result['first_event_seconds'] = bound((event['start_ns'] + event['duration_ns'] - cmd['start_ns']) / 1e9,
                                                  cmd['duration_ns'] / 1e9, command['seconds'])
    if slot['arm'] == 'T-E' and saved:
        # Discovery is the last schedule_run inside an explore_pass; replay is a sibling.
        byid = {s['id']: s for s in spans}
        discovery = [s for s in spans if s['name'] == 'schedule_run'
                     and byid.get(s['parent_id'], {}).get('name') == 'explore_pass']
        mapped = (len(discovery) == o['schedules_explored'] and discovery and passes
                  and discovery[-1]['parent_id'] == passes[-1]['id']
                  and discovery[-1].get('schedule_id') == saved['id'])
        if not mapped:
            result['quality_reasons'].append('discovery schedule differs from final explored execution')
        fingerprint_matches = o.get('discovery_fingerprint') in o['fingerprints']
        if not fingerprint_matches:
            result['quality_reasons'].append('discovery fingerprint differs from confirmation')
        result['stable_confirmation'] = bool(result['stable_confirmation'] and mapped and fingerprint_matches)
        # With zero violating replays the CLI retains the discovery's violations.
        # Otherwise a different replay fingerprint can leave attribution ambiguous.
        discovery_evidence = fingerprint_matches or o['violation_runs'] == 0
        if mapped and domain and o.get('discovery_fingerprint') and discovery_evidence:
            evaluations = [s for s in spans if s['name'] == 'oracle_evaluate' and s['parent_id'] == discovery[-1]['id']]
            commands = [s for s in spans if s['name'] == 'command']
            if len(evaluations) == len(commands) == 1:
                event, cmd = evaluations[0], commands[0]
                result['first_event_seconds'] = bound((event['start_ns'] + event['duration_ns'] - cmd['start_ns']) / 1e9,
                                                      cmd['duration_ns'] / 1e9, command['seconds'])
    result['receipt_valid'] = not result['quality_reasons']
    result['quality_reasons'].extend(outcome_reasons)
    result['unexpected_miss'] = (result['search_exhausted'] and slot['variant'] == 'vulnerable'
                                and not result['quality_reasons'] and not result['operational_reasons'])
    # Operational cleanup does not undo confirmation. Inconsistent domain,
    # traversal or execution evidence cannot establish a validated confirmation.
    result['stable_confirmation'] = result['stable_confirmation'] and not result['quality_reasons']
    return result


def command_receipt(value):
    """Validate every field consumed by reduction before trusting any metrics."""
    require(isinstance(value, dict), 'object required')
    for key in ('started', 'finished', 'interrupted', 'timeout', 'containment_proven'):
        require(type(value[key]) is bool, 'invalid ' + key)
    require(type(value['seconds']) in (int, float) and math.isfinite(value['seconds'])
            and value['seconds'] >= 0, 'invalid seconds')
    require(value['exit'] is None or type(value['exit']) is int, 'invalid exit')
    require(value['error'] is None or isinstance(value['error'], str), 'invalid error')
    if 'collector_start_ns' in value:
        require(type(value['collector_start_ns']) is int and value['collector_start_ns'] >= 0,
                'invalid collector clock')
    return value


def reduce_slot(root, slot, manifest):
    result = dict(slot, status='not_started', reasons=[], started=False)
    unsupported = unsupported_declarations(manifest).get(slot['workload'] + '/' + slot['arm'])
    if unsupported and not root.exists():
        result.update(status='unsupported', reasons=[unsupported]); return result
    if not root.exists(): return result
    verify(root)
    state = read_json(root / 'slot.json')
    require(state.get('slot') == slot, 'slot identity differs')
    require(json_equal(state.get('unsupported'), unsupported), 'unsupported slot declaration differs')
    # Lifecycle failures apply even when preparation never spawned the arm.
    operational = state.get('integrity_errors', [])[:] + state.get('containment_errors', [])
    for key in ('error', 'cleanup_error'):
        if state.get(key): operational.append(state[key])
    if state.get('interrupted'): operational.append('collector interrupted')
    if state.get('cleanup_proven') is not True:
        operational.append('allocation cleanup unproven')
    if state.get('unsupported'):
        require(state['unsupported'] == unsupported and not (root / 'run.command.json').exists(), 'unsupported slot was executed/unregistered')
        result.update(status='unsupported', reasons=[state['unsupported']] + operational); return result
    if not (root / 'run.command.json').exists():
        result.update(status='preparation_failed', cleanup_proven=state.get('cleanup_proven') is True,
                      reasons=operational if state.get('error') else ['preparation incomplete'] + operational)
        return result
    command = None
    evidence_valid = False
    result.update(invocation_seconds=None, exit=None, budget_expired=None, command_receipt_valid=False)
    expected = manifest['inputs'][slot['workload']][slot['variant']]
    try:
        raw_command = read_json(root / 'run.command.json')
        if isinstance(raw_command, dict): result['started'] = raw_command.get('started') is True
        command = command_receipt(raw_command)
    except EVIDENCE_ERRORS as error:
        evidence = {'operational_reasons': ['missing/invalid command receipt: ' + str(error)],
                    'quality_reasons': [], 'cleanup_proven': False, 'first_event_seconds': None}
    else:
        result.update(command_receipt_valid=True, started=command['started'], invocation_seconds=command['seconds'],
                      exit=command['exit'], budget_expired=command['timeout'] or command['seconds'] > slot['cap_seconds'])
        try:
            evidence = (baseline if slot['arm'].startswith('B') else product)(root, slot, command, expected)
            evidence_valid = (not evidence.get('evidence_absence')
                              and evidence.get('receipt_valid', not evidence['quality_reasons']))
        except EVIDENCE_ERRORS as error:
            evidence = {'operational_reasons': ['missing/invalid evidence: ' + str(error)],
                        'quality_reasons': [], 'cleanup_proven': False, 'first_event_seconds': None}
    result.update(evidence)
    operational = evidence['operational_reasons'] + operational
    if command and (command['error'] or not command['started'] or not command['containment_proven']):
        operational.append('command spawn/containment failure')
    if command and (command['finished'] is not True or command['interrupted'] is not False):
        operational.append('command unfinished/interrupted')
    received = state.get('evidence_received_seconds') if command is not None else None
    if command is not None and 'evidence_received_seconds' in state:
        try:
            require(isinstance(received, list) and len(received) == 2
                    and all(type(v) in (int, float) and math.isfinite(v) for v in received)
                    and command['seconds'] <= received[0] == received[1], 'invalid collector receipt time')
        except (ValueError, TypeError, OverflowError):
            operational.append('invalid collector receipt time')
            received = None
    result['resources'] = unavailable_resources('aggregate counters unavailable in feasibility collection')
    if 'resources' in state or manifest['phase'] == 'measurement':
        try:
            result['resources'] = resource_observations(state.get('resources'))
        except ValueError as error:
            operational.append(str(error))
            result['resources'] = unavailable_resources('resource observation missing/invalid; see sealed slot evidence')
    result['allocation_cleanup_proven'] = state.get('cleanup_proven') is True
    result['status'] = ('operational_failed' if operational else 'quality_failed' if evidence['quality_reasons']
                        else 'budget_exhausted' if result['budget_expired'] else 'complete')
    result['reasons'] = operational + evidence['quality_reasons']
    result['first_event_membership'] = membership(result.get('first_event_seconds'), slot['cap_seconds'])
    # Later observation/cleanup failures cannot erase an already validated
    # receipt. Its timing and clean final completion are separate observations.
    result['usable_evidence_seconds'] = received if evidence_valid else None
    result['usable_evidence_membership'] = membership(result['usable_evidence_seconds'], slot['cap_seconds'])
    return result


def detected_in_budget(row):
    usable = (membership(row.get('usable_first_event_seconds'), row['cap_seconds'])
              if row['arm'].startswith('B') else row.get('usable_evidence_membership'))
    return row.get('first_event_membership') == 'in_budget' and usable == 'in_budget'


def analysis_source(bundle, manifest, progress):
    # A fixed sibling destination keeps repeated reductions byte-identical. It
    # must be absent when replaying; callers can select another new destination.
    argv = [sys.executable, '-B', str(bundle/'frozen/source/tool.py'), 'analyze',
            str(bundle), str(bundle.with_name(bundle.name + '-analysis-replay'))]
    return {
        'manifest_sha256': digest(bundle/'frozen/manifest.json'),
        'order_sha256': digest(bundle/'frozen/order.json'),
        'frozen_sha256': digest(bundle/'frozen/SHA256SUMS'),
        'checkpoint_sha256': digest(bundle/'progress/checkpoint.json'),
        'registration': progress['registration'], 'candidate': manifest.get('candidate'),
        'e1_registration': manifest.get('e1_registration'),
        'fixture_revision': manifest.get('fixture_revision'),
        'source_revision': manifest.get('source_revision'),
        'replay': {'argv': argv, 'command': shlex.join(argv)},
    }


def analyze(bundle, output):
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    require(output != bundle and bundle not in output.parents, 'analysis output must be outside campaign')
    # Match collection's lock for the entire snapshot and output publication.
    with campaign_lock(bundle, shared=True) as check_lock:
        return analyze_locked(bundle, output, check_lock)


def analyze_locked(bundle, output, check_lock):
    bundle, output = Path(bundle), Path(output)
    verify(bundle / 'frozen')
    frozen_sources(bundle)
    verify(bundle / 'progress')
    retained_registration(bundle, read_json(bundle / 'progress' / 'checkpoint.json'))
    manifest = read_json(bundle / 'frozen' / 'manifest.json')
    campaign_id(manifest)
    domain_expectations(manifest)
    prerequisite_completion(manifest, bundle / 'frozen' / 'inputs')
    if manifest['phase'] == 'measurement':
        require(all(inp['config']['Target'].get('DB') == manifest['environment']['mysql_image']
                    for variants in manifest['inputs'].values() for inp in variants.values()),
                'effective config image differs from registered MySQL image')
    slots = read_json(bundle / 'frozen' / 'order.json')
    require(slots == order(), 'order differs from protocol')
    progress = retained_progress(bundle, manifest, slots)
    rows = [reduce_slot(bundle / 'slots' / s['id'], s, manifest) for s in slots]
    stopped = progress['stopped']
    if stopped:
        for r in rows:
            if r['status'] == 'not_started': r['reasons'] = ['campaign stopped: ' + stopped['reason']]
    cells = []
    for workload in WORKLOADS:
        for variant in ('vulnerable', 'fixed'):
            for cache in ('cold', 'warm'):
                selected = [r for r in rows if (r['workload'], r['variant'], r['cache']) == (workload, variant, cache)]
                for arm in ARMS:
                    values = [r for r in selected if r['arm'] == arm]
                    complete = [r for r in values if r['status'] == 'complete']
                    stage_values = defaultdict(list)
                    for r in complete:
                        for name, durations in r.get('stages', {}).get('durations_seconds', {}).items():
                            stage_values[name].extend(durations)
                        if 'remainder_seconds' in r.get('stages', {}):
                            stage_values['remainder'].append(r['stages']['remainder_seconds'])
                    cells.append({'workload': workload, 'variant': variant, 'cache': cache, 'arm': arm,
                        'planned': 10, 'started': sum(r['started'] for r in values),
                        'statuses': {s: sum(r['status'] == s for r in values) for s in STATUSES},
                        'invocation_seconds': stats([r.get('invocation_seconds') for r in complete]),
                        'all_started_seconds': stats([r.get('invocation_seconds') for r in values if r['started']]),
                        'stage_seconds': {name: stats(values) for name, values in sorted(stage_values.items())},
                        'amortized_execution_seconds': stats([
                            r['invocation_seconds'] / r['evaluated_executions'] for r in complete
                            if r.get('evaluated_executions', 0) > 0]) if arm == 'T-E' else
                            stats([r['invocation_seconds'] / manifest['inputs'][workload][variant]['counts'][arm]
                                   for r in complete]) if arm in ('B1','T-R') else None,
                        'observed_detections': sum(detected_in_budget(r) for r in values),
                        'valid_exposure_blocks': sum(r.get('valid_evaluations', 0) > 0 for r in values),
                        'stable_confirmations': sum(r.get('stable_confirmation', False) for r in values),
                        'first_lower_seconds_conditional': stats([r['first_event_seconds'][0] for r in values if r.get('first_event_seconds')]),
                        'first_upper_seconds_conditional': stats([r['first_event_seconds'][1] for r in values if r.get('first_event_seconds')])})
    pairs = []
    grouped = defaultdict(dict)
    for r in rows: grouped[(r['workload'], r['variant'], r['cache'], r['block'])][r['arm']] = r
    for key, arms in sorted(grouped.items()):
        b, t = arms['B1'], arms['T-R']
        valid = b['status'] == t['status'] == 'complete'
        x, y = b.get('invocation_seconds'), t.get('invocation_seconds')
        pairs.append(dict(zip(('workload', 'variant', 'cache', 'block'), key),
                          difference_seconds=y-x if valid else None,
                          ratio=y/x if valid and x else None,
                          reason=None if valid and x else 'incomplete pair or zero denominator',
                          t_complete=all(arms[a]['status'] == 'complete' for a in ('T-R','T-E'))))
    cost_cells = []
    for key in sorted({(p['workload'],p['variant'],p['cache']) for p in pairs}):
        selected = [p for p in pairs if (p['workload'],p['variant'],p['cache']) == key]
        cost_cells.append(dict(zip(('workload','variant','cache'),key),
                              difference_seconds=stats([p['difference_seconds'] for p in selected]),
                              ratio=stats([p['ratio'] for p in selected]),
                              t_complete=sum(p['t_complete'] for p in selected)))
    payload = {'schema': 1, 'phase': manifest['phase'], 'campaign_id': manifest['campaign_id'], 'stopped': stopped,
               'deviations': campaign_deviations(manifest),
               'source': analysis_source(bundle, manifest, progress),
               'claim': 'same-host descriptive observations only', 'slots': rows, 'cells': cells,
               'pairs': pairs, 'cost_cells': cost_cells}
    check_lock()
    resolved_output = output.resolve()
    require(resolved_output != bundle and bundle not in resolved_output.parents,
            'analysis output must be outside campaign')
    lines = ['# E5 retained evidence', '', 'Phase: ' + manifest['phase'] + '. Same-host descriptive data only.',
             'p90 uses interpolation; at n=10 it is not a population tail estimate.', '']
    source = payload['source']
    lines += ['## Registered deviations', '']
    lines += ['- ' + html.escape(d).replace('\n', ' ') for d in payload['deviations']] or ['None registered.']
    lines += ['', '## Authenticated source', '', 'Campaign: ' + manifest['campaign_id'], '',
              '```json', canonical(source).decode().rstrip(), '```', '',
              'Reproduce from the retained bundle with its frozen tool (destination must be new):', '',
              '```sh', source['replay']['command'], '```', '',
              '## Accounting', '',
             '| Workload | Variant | Cache | Arm | Complete / planned | Failures / missing | Median seconds (n) | p90 | Detection / 10 | Baseline exposure / 10 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    def fmt(v): return 'N/A' if v is None else '%.6f' % v
    for c in cells:
        s = c['invocation_seconds']
        lines.append('| %s | %s | %s | %s | %d / 10 | %s | %s (%d) | %s | %d / 10 | %s |' % (
            c['workload'], c['variant'], c['cache'], c['arm'], c['statuses']['complete'],
            ', '.join(k + '=' + str(v) for k,v in c['statuses'].items() if k != 'complete' and v),
            fmt(s['median']), s['n'], fmt(s['p90']), c['observed_detections'],
            '%d / 10' % c['valid_exposure_blocks'] if c['arm'] in ('B1','B2') else 'N/A'))
    lines += ['', '## Paired cost', '',
              '| Workload | Variant | Cache | T-R minus B1 median seconds (n) | Ratio median (n) | T complete / 10 |',
              '| --- | --- | --- | --- | --- | --- |']
    for c in cost_cells:
        d, r = c['difference_seconds'], c['ratio']
        lines.append('| %s | %s | %s | %s (%d) | %s (%d) | %d / 10 |' % (
            c['workload'], c['variant'], c['cache'], fmt(d['median']), d['n'], fmt(r['median']), r['n'], c['t_complete']))
    lines += ['', '## Descriptive cost per evaluated execution', '',
              'Whole invocation divided by actual evaluations, including T-E discovery and confirmation. Complete slots only; variable search work is not equal-work throughput.', '',
              '| Workload | Variant | Cache | Arm | Median seconds (n) | p90 | Min | Max |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for c in cells:
        s = c['amortized_execution_seconds']
        if s is None: continue
        lines.append('| %s | %s | %s | %s | %s (%d) | %s | %s | %s |' % (
            c['workload'], c['variant'], c['cache'], c['arm'], fmt(s['median']), s['n'],
            fmt(s['p90']), fmt(s['min']), fmt(s['max'])))
    lines += ['', '## Observed search slots', '',
              'Every unstarted slot and reason remains in the accounting above and results.json.',
              'Fixed-row detections are unexpected domain violations, not intended discoveries.', '',
              'B2 retains uncontrolled exposure; T-E retains explored candidates and automatic confirmation separately.', '',
              '| Slot | Status | Attempted / valid | Violations / valid | Violations / planned | First domain bound seconds |',
              '| --- | --- | --- | --- | --- | --- |']
    def first_bound(r):
        event = r.get('first_event_seconds')
        return ('[%s, %s] %s' % (fmt(event[0]), fmt(event[1]), r['first_event_membership'])
                if event else 'unknown / absent')
    for r in rows:
        if r['arm'] != 'B2' or not r['started']: continue
        lines.append('| %s | %s | %s / %s | %s | %s | %s |' % (
            r['id'],r['status'],r.get('attempted','N/A'),r.get('valid_evaluations','N/A'),
            fmt(r.get('x_over_m')),fmt(r.get('x_over_planned')),first_bound(r)))
    lines += ['', '| Slot | Status | Explored candidates | Violating confirmations / planned | Fingerprint counts | Selected schedule | First domain bound seconds | Stable confirmation |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        if r['arm'] != 'T-E' or not r['started']: continue
        confirmation = ('not run (no discovery)' if r.get('search_exhausted') else
                        '%s / %s' % (r.get('violation_runs', 'N/A'), r.get('planned_confirmation_executions', 'N/A')))
        fingerprints = ', '.join('%s=%d' % item for item in sorted(r.get('fingerprints', {}).items())) or 'none retained'
        lines.append('| %s | %s | %s | %s | %s | %s | %s | %s |' % (
            r['id'], r['status'], r.get('exploration_executions', 'N/A'), confirmation,
            fingerprints, r.get('selected_schedule') or 'none retained', first_bound(r),
            r.get('stable_confirmation', False)))
    lines += ['', '## Stage observations', '',
              'Inclusive named spans; overlapping parents are never added together. Remainder is unmatched invocation time, one value per complete slot. Guest startup is not inferred by subtraction.', '',
              '| Workload | Variant | Cache | Arm | Stage | Median seconds (n) | p90 | Min | Max |',
              '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for c in cells:
        for name, s in c['stage_seconds'].items():
            lines.append('| %s | %s | %s | %s | %s | %s (%d) | %s | %s | %s |' % (
                c['workload'],c['variant'],c['cache'],c['arm'],name,fmt(s['median']),s['n'],fmt(s['p90']),fmt(s['min']),fmt(s['max'])))
    lines += ['', 'All first-event bounds, per-execution outcomes, failures, resources and within-block pairs are in results.json.',
              'T-R minus B1 is total controlled-path cost, not isolated sync-point overhead.', '']
    with created_directory(output) as (check_output, retained):
        check_lock()
        write_json(retained / 'results.json', payload)
        check_output()
        write_bytes(retained / 'report.md', '\n'.join(lines).encode())
        check_output()
        check_lock()
        seal(retained)
        check_output()
        check_lock()
    return payload
