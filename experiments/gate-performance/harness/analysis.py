"""Rebuild accounting and paired estimates from a sanitized, sealed bundle."""
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from common import (E1_RESOURCES, E2_RESOURCES, child, counter, digest, json_equal, order,
                    read_json, sha, verify, write_csv, write_json)
from collector import receipt_valid, succeeded, validate_manifest


def quantile(values, p):
    values = sorted(values)
    index = (len(values) - 1) * p
    low = math.floor(index)
    high = math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def estimate(m, workload, metric, pairs, dates):
    """pairs: (date slot, block ID, baseline, candidate); no imputation."""
    pairs = sorted(pairs)
    if not pairs:
        return {'n': 0, 'ratio': None, 'difference': None, 'interval': None}
    ratio_defined = all(b > 0 and w > 0 for _, _, b, w in pairs)
    logs = [math.log(w) - math.log(b) for _, _, b, w in pairs] if ratio_defined else []
    differences = [w - b for _, _, b, w in pairs]
    result = {'n': len(pairs), 'ratio': math.exp(math.fsum(logs) / len(logs)) if logs else None,
              'difference': math.fsum(differences) / len(differences), 'interval': None}
    for arm, offset in [('baseline', 2), ('candidate', 3)]:
        values = [p[offset] for p in pairs]
        result[arm] = {'median': quantile(values, .5),
                       'q1': quantile(values, .25), 'q3': quantile(values, .75)}
    result['improvement'] = 1 - result['ratio'] if ratio_defined else None
    strata = {date: [p for p in pairs if p[0] == date] for date in dates}
    result['by_date'] = {
        date: {'n': len(ps), 'ratio': math.exp(math.fsum(math.log(w) - math.log(b)
                 for _, _, b, w in ps) / len(ps)) if ps and all(b > 0 and w > 0
                 for _, _, b, w in ps) else None,
               'difference': math.fsum(w - b for _, _, b, w in ps) / len(ps) if ps else None}
        for date, ps in strata.items()}
    if any(len(ps) < 2 for ps in strata.values()):
        result['interval_reason'] = 'fewer_than_two_valid_blocks_in_planned_date'
        return result
    ratios, diffs = [], []
    for replicate in range(10000):
        sample = []
        for date, ps in sorted(strata.items()):
            for draw in range(len(ps)):
                key = '|'.join([m['seed'], m['experiment'], m['phase'], workload,
                                metric, date, '%05d' % replicate, '%05d' % draw, 'bootstrap'])
                index = int(sha(key.encode()), 16) * len(ps) // (1 << 256)
                sample.append(ps[index])
        if ratio_defined:
            ratios.append(math.exp(math.fsum(math.log(w) - math.log(b) for _, _, b, w
                                             in sample) / len(sample)))
        diffs.append(math.fsum(w - b for _, _, b, w in sample) / len(sample))
    result['interval'] = {'ratio': [quantile(ratios, .025), quantile(ratios, .975)]
                          if ratios else None,
                          'difference': [quantile(diffs, .025), quantile(diffs, .975)]}
    if ratios:
        lo, hi = result['interval']['ratio']
        result['interval']['improvement'] = [1 - hi, 1 - lo]
    return result


def trial(root, m, slot, arm):
    directory = root / 'blocks' / slot['block'] / arm
    path = directory / 'trial.json'
    missing = dict(slot, arm=arm, attempt=1, started=False, classification='not_started',
                   reason='slot_not_collected', metrics={}, facts={}, exit=None,
                   elapsed_seconds=None, cleanup_proven=False)
    if not path.exists():
        missing['reason'] = 'interrupted_collection' if directory.parent.exists() else 'slot_not_collected'
        return missing
    row = read_json(path)
    if any(row.get(key) != value for key, value in dict(slot, arm=arm, attempt=1).items()):
        raise ValueError('trial identity differs from registered slot')
    if row['classification'] == 'complete':
        try:
            if not row['started'] or not row['cleanup_proven']:
                raise ValueError('completion lacks start/cleanup')
            for step in ('prepare', 'observe', 'check', 'cleanup'):
                if not succeeded(read_json(directory / (step + '.command.json'))):
                    raise ValueError('command failed: ' + step)
            run = read_json(directory / 'run.command.json')
            if (run['timeout'] or run['error'] or run['exit'] != row['exit'] or
                    run['exit'] not in m['expected_exit'][slot['workload']][arm] or
                    run['seconds'] != row['elapsed_seconds']):
                raise ValueError('run command receipt differs')
            receipt = read_json(directory / 'receipt.json')
            receipt_valid(m, row, receipt, directory)
            if row['metrics'] != receipt['metrics'] or row['facts'] != receipt['facts']:
                raise ValueError('ledger and original receipt differ')
            if m['experiment'] == 'E1' and row['metrics'].get(
                    'adoption_seconds' if m['phase'] == 'adoption-cold' else 'gate_seconds') != run['seconds']:
                raise ValueError('gate duration must use collector monotonic clock')
            proof = read_json(directory / 'cleanup.json')
            if proof.get('proven') is not True or not proof.get('evidence'):
                raise ValueError('missing cleanup proof')
            for name, checksum in proof['evidence'].items():
                if digest(child(directory, name)) != checksum:
                    raise ValueError('cleanup evidence mismatch')
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            row.update(classification='quality_failure', reason=str(error))
    return row


def analyze(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError('analysis output must be outside the immutable input bundle')
    verify(root)
    m = read_json(root / 'campaign.json')
    validate_manifest(m)
    slots = order(m)
    block_dir = root / 'blocks'
    if block_dir.exists() and any(p.name not in {s['block'] for s in slots} for p in block_dir.iterdir()):
        raise ValueError('unregistered block in evidence bundle')
    if read_json(root / 'order.json') != slots:
        raise ValueError('order file differs from frozen assignment')
    rows = [trial(root, m, slot, arm) for slot in slots for arm in slot['arms']]
    blocks = defaultdict(dict)
    for row in rows:
        blocks[row['block']][row['arm']] = row
    if any(r['started'] for r in rows):
        from common import timestamp
        reg = read_json(root / 'registration.json')
        if reg['campaign_sha256'] != digest(root / 'campaign.json') or reg['order_sha256'] != digest(root / 'order.json'):
            raise ValueError('registration hash mismatch')
        for command in root.glob('blocks/*/*/*.command.json'):
            if timestamp(reg['registered_at']) >= timestamp(read_json(command)['started_at']):
                raise ValueError('measurement predates registration')
    pair_reasons = {}
    for block, arms in blocks.items():
        b, w = arms['baseline'], arms['candidate']
        reasons = [r['reason'] or r['classification'] for r in (b, w)
                   if r['classification'] != 'complete']
        if not reasons and any(not json_equal(b['facts'][key], w['facts'][key]) for key in m['compare_facts']):
            reasons.append('paired_facts_mismatch')
        if not reasons and m['experiment'] == 'E2':
            first, second = [arms[a] for a in b['arms']]
            try:
                from common import timestamp
                lag = (timestamp(second['facts']['ci_created_at']) -
                       timestamp(first['facts']['ci_completed_at'])).total_seconds()
                if not 0 <= lag <= 300:
                    reasons.append('pairing_window_missed')
            except (KeyError, ValueError):
                reasons.append('pairing_window_missing')
        pair_reasons[block] = reasons
    accounting = {}
    results = {}
    for workload, counts in sorted(m['strata'].items()):
        selected = [r for r in rows if r['workload'] == workload]
        accounting[workload] = {}
        for arm in ('baseline', 'candidate'):
            arm_rows = [r for r in selected if r['arm'] == arm]
            accounting[workload][arm] = dict(Counter(r['classification'] for r in arm_rows),
                                            planned=len(arm_rows), started=sum(r['started'] for r in arm_rows),
                                            reasons=dict(Counter(r['reason'] for r in arm_rows if r['reason'])))
        valid = [(b['baseline'], b['candidate']) for block, b in blocks.items()
                 if b['baseline']['workload'] == workload and not pair_reasons[block]]
        metrics = sorted(set(m['resource_metrics']) | set(m.get('analysis_metrics', [])))
        if m['experiment'] in ('E1', 'E2'):
            metrics.append('gate_seconds' if m['experiment'] == 'E1' else 'validation_seconds')
        if m['experiment'] == 'E1' and m['phase'] == 'adoption-cold':
            metrics.append('adoption_seconds')
        results[workload] = {}
        for metric in sorted(set(metrics)):
            pairs = [(b['date_slot'], b['block_id'], b['metrics'][metric], w['metrics'][metric])
                     for b, w in valid if counter(b['metrics'].get(metric)) and
                     counter(w['metrics'].get(metric))]
            dates = ['%02d' % i for i in range(1, len(counts) + 1)]
            results[workload][metric] = estimate(m, workload, metric, pairs, dates)
    decision = {'status': 'descriptive', 'reasons': [], 'higher_resource_cost': [], 'undefined_resource_ratios': []}
    if m['experiment'] in ('E1', 'E2') and m['phase'] == 'confirmation':
        primary = 'example-fixed-replay' if m['experiment'] == 'E1' else 'external-acceptance'
        metric = 'gate_seconds' if m['experiment'] == 'E1' else 'validation_seconds'
        threshold = .85 if m['experiment'] == 'E1' else .80
        endpoint = results[primary][metric]
        decision['threshold'] = threshold
        if any(pair_reasons.values()):
            decision['reasons'].append('incomplete_or_invalid_planned_pairs')
        if m.get('deviations'):
            decision['reasons'].append('registered_deviation_requires_descriptive_reporting')
        resource_names = E1_RESOURCES if m['experiment'] == 'E1' else E2_RESOURCES
        primary_count = sum(m['strata'][primary])
        if any(results[primary][name]['n'] != primary_count for name in resource_names):
            decision['reasons'].append('resource_measurements_incomplete')
        cost_metrics = ('cpu_seconds', 'peak_memory_bytes', 'disk_write_bytes') if m['experiment'] == 'E1' else ('runner_minutes',)
        for name in cost_metrics:
            ratio = results[primary][name]['ratio']
            if ratio is None:
                decision['undefined_resource_ratios'].append(name)
            elif ratio > 1.10:
                decision['higher_resource_cost'].append(name)
        interval = endpoint.get('interval')
        if (endpoint['ratio'] is None or not interval or not interval['ratio'] or
                endpoint['ratio'] > threshold or interval['ratio'][1] >= 1):
            decision['reasons'].append('practical_improvement_not_demonstrated')
        decision['status'] = 'no_positive_claim' if decision['reasons'] else 'improvement_demonstrated'
    output.mkdir(mode=0o700)  # Never overwrite an earlier analyzer's results.
    result = {'campaign': m['campaign_id'], 'experiment': m['experiment'], 'phase': m['phase'],
              'input_sha256sums': digest(root / 'SHA256SUMS'), 'accounting': accounting,
              'paired_missingness': pair_reasons, 'estimates': results, 'decision': decision}
    for source in sorted(Path(__file__).parent.glob('*.py')):
        target = output / 'source' / source.name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(source.read_bytes())
    write_json(output / 'results.json', result)
    write_json(output / 'trials.json', rows)
    write_csv(output / 'trials.csv', [{k: r[k] for k in ('block', 'workload', 'date_slot', 'arm',
              'attempt', 'started', 'classification', 'reason', 'exit', 'elapsed_seconds')}
              for r in rows], ['block', 'workload', 'date_slot', 'arm', 'attempt', 'started',
                               'classification', 'reason', 'exit', 'elapsed_seconds'])
    lines = ['# Campaign analysis', '', 'Campaign: `%s` (%s / %s).' %
             (m['campaign_id'], m['experiment'], m['phase']), '',
             'Decision: **%s**.' % decision['status'], '']
    def display(number):
        return 'N/A' if number is None else '%.9g' % number
    def interval_display(value, key):
        bounds = (value.get('interval') or {}).get(key)
        return '[%s, %s]' % tuple(map(display, bounds)) if bounds else 'N/A'
    def interval_reason(value):
        if not value.get('interval'):
            return value.get('interval_reason', 'no_valid_pairs' if not value['n'] else 'interval_unavailable')
        if value['interval'].get('ratio') is None:
            return 'undefined_ratio'
        return ''
    if 'threshold' in decision:
        lines += ['Primary endpoint: `%s` / `%s`.' % (primary, metric), '',
                  'Primary ratio: %s; 95%% bootstrap interval: %s.' %
                  (display(endpoint['ratio']), interval_display(endpoint, 'ratio')), '',
                  'Confirmatory timing rule: ratio <= %s and upper 95%% interval bound < 1; '
                  'all planned quality/resource evidence must be complete.' % display(threshold), '']
        reason = interval_reason(endpoint)
        if reason:
            lines += ['Primary interval unavailable: `%s`.' % reason, '']
        lines += ['Decision reasons: %s.' % (', '.join('`%s`' % reason for reason in decision['reasons'])
                                           or 'none'), '']
    if decision['higher_resource_cost']:
        if decision['status'] == 'improvement_demonstrated':
            qualification = ('faster with higher measured resource cost' if m['experiment'] == 'E1'
                             else 'faster feedback with higher runner cost')
            lines += ['Timing result: **%s**.' % qualification, '']
        lines += ['Higher measured resource cost (ratio > 1.10): %s.' %
                  ', '.join('`%s`' % name for name in decision['higher_resource_cost']), '']
    if decision['undefined_resource_ratios']:
        lines += ['Undefined resource ratios: %s; no resource-efficiency claim follows.' %
                  ', '.join('`%s`' % name for name in decision['undefined_resource_ratios']), '']
    def cell(value):
        return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace(
            '|', '&#124;').replace('\n', '<br>').replace('\r', '')
    stopped = read_json(root / 'stopped.json') if (root / 'stopped.json').exists() else None
    lines += ['Registered arms: %s.' % cell(json.dumps(m['arms'], sort_keys=True)), '',
              'Quality inventory: %s.' % ', '.join(cell(name) for name in m['quality_checks']), '',
              'Collection status: %s.' % ('stopped — ' + cell(json.dumps(stopped, sort_keys=True))
                                         if stopped else 'not stopped'), '',
              'Planned pairs: %d; valid pairs: %d; excluded pairs: %d.' %
              (len(blocks), sum(not reasons for reasons in pair_reasons.values()),
               sum(bool(reasons) for reasons in pair_reasons.values())), '',
              '| Workload | Arm | Planned | Started | Complete | Operational failure | Quality failure | Not started |',
              '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for workload, arms in accounting.items():
        for arm, counts in arms.items():
            lines.append('| %s | %s | %d | %d | %d | %d | %d | %d |' %
                         (cell(workload), arm, counts['planned'], counts['started'],
                          counts.get('complete', 0), counts.get('operational_failure', 0),
                          counts.get('quality_failure', 0), counts.get('not_started', 0)))
    lines += ['', '| Workload | Arm | Failure or missingness reason | Count |',
              '| --- | --- | --- | ---: |']
    for workload, arms in accounting.items():
        for arm, counts in arms.items():
            for reason, count in sorted(counts['reasons'].items()):
                lines.append('| %s | %s | %s | %d |' % (cell(workload), arm, cell(reason), count))
    if not any(counts['reasons'] for arms in accounting.values() for counts in arms.values()):
        lines += ['| — | — | none | 0 |']
    lines += ['', '| Excluded block | Pair reasons |', '| --- | --- |']
    for block, reasons in sorted(pair_reasons.items()):
        if reasons:
            lines.append('| %s | %s |' % (cell(block), '; '.join(cell(reason) for reason in reasons)))
    if not any(pair_reasons.values()):
        lines += ['| — | none |']
    lines += ['', 'Registered deviations:', '']
    deviations = m.get('deviations', [])
    lines += ['- ' + cell(value) for value in deviations] if deviations else ['None.']
    lines += ['']
    lines += ['| Workload | Metric | Pairs | Geometric ratio | Ratio 95% interval | Mean difference | Difference 95% interval | Interval limitation |',
              '| --- | --- | ---: | ---: | --- | ---: | --- | --- |']
    for workload, metrics in results.items():
        for metric, value in metrics.items():
            lines.append('| %s | %s | %d | %s | %s | %s | %s | %s |' %
                         (workload, metric, value['n'], display(value['ratio']),
                          interval_display(value, 'ratio'), display(value['difference']),
                          interval_display(value, 'difference'), interval_reason(value)))
    lines += ['', 'Full intervals, per-date estimates, arm summaries, all failure counts and',
              'pair reasons are in `results.json`; every planned slot is in `trials.csv`.',
              '', 'Secondary endpoints are descriptive. Results are conditional on the',
              'registered dates, environments, caches and workload. No p99, labor savings',
              'or broad application claim follows from this analysis.', '']
    (output / 'report.md').write_text('\n'.join(lines))
    write_json(output / 'command.json', {'argv': [sys.executable, str(Path(__file__).with_name('tool.py')),
                                                'analyze', str(root), str(output)]})
    from common import seal
    seal(output)
    return result
