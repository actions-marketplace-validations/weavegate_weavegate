"""Rebuild accounting and paired estimates from a sanitized, sealed bundle."""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from common import (E1_RESOURCES, E2_RESOURCES, campaign_lock, child, counter, digest, json_equal, order,
                    read_json, sha, verify, write_csv, write_json)
from evidence import trial
from plan import validate_manifest, validate_registration


def quantile(values, p):
    values = sorted(values)
    index = (len(values) - 1) * p
    low = math.floor(index)
    high = math.ceil(index)
    weight = index - low
    delta = values[high] - values[low]
    if not math.isfinite(delta):
        return values[low] * (1 - weight) + values[high] * weight
    return values[low] + delta * weight


def mean(values):
    try:
        return math.fsum(values) / len(values)
    except OverflowError:
        # A finite mean can have an overflowing intermediate sum. Use exact
        # arithmetic only on that exceptional path, retaining every observation.
        return float(sum(Fraction(value) for value in values) / len(values))


def ratio_from_logs(logs):
    try:
        ratio = math.exp(mean(logs))
    except OverflowError:
        return None
    return ratio if ratio > 0 else None  # Underflow must not invent a zero ratio.


def estimate(m, workload, metric, pairs, dates):
    """pairs: (date slot, block ID, baseline, candidate); no imputation."""
    pairs = sorted(pairs)
    if not pairs:
        return {'n': 0, 'ratio': None, 'difference': None, 'interval': None}
    ratio_defined = all(b > 0 and w > 0 for _, _, b, w in pairs)
    logs = [math.log(w) - math.log(b) for _, _, b, w in pairs] if ratio_defined else []
    differences = [w - b for _, _, b, w in pairs]
    result = {'n': len(pairs), 'ratio': ratio_from_logs(logs) if logs else None,
              'difference': mean(differences), 'interval': None}
    if logs and result['ratio'] is None:
        result['ratio_reason'] = 'unrepresentable_ratio'
    for arm, offset in [('baseline', 2), ('candidate', 3)]:
        values = [p[offset] for p in pairs]
        result[arm] = {'median': quantile(values, .5),
                       'q1': quantile(values, .25), 'q3': quantile(values, .75)}
    result['improvement'] = 1 - result['ratio'] if result['ratio'] is not None else None
    strata = {date: [p for p in pairs if p[0] == date] for date in dates}
    result['by_date'] = {
        date: {'n': len(ps), 'ratio': ratio_from_logs([math.log(w) - math.log(b)
                 for _, _, b, w in ps]) if ps and all(b > 0 and w > 0
                 for _, _, b, w in ps) else None,
               'difference': mean([w - b for _, _, b, w in ps]) if ps else None}
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
            ratio = ratio_from_logs([math.log(w) - math.log(b) for _, _, b, w in sample])
            if ratio is not None:
                ratios.append(ratio)
        diffs.append(mean([w - b for _, _, b, w in sample]))
    result['interval'] = {'ratio': [quantile(ratios, .025), quantile(ratios, .975)]
                          if len(ratios) == 10000 else None,
                          'difference': [quantile(diffs, .025), quantile(diffs, .975)]}
    if ratio_defined and len(ratios) != 10000:
        result['ratio_interval_reason'] = 'unrepresentable_bootstrap_ratio'
    if result['interval']['ratio']:
        lo, hi = result['interval']['ratio']
        result['interval']['improvement'] = [1 - hi, 1 - lo]
    return result


def analyze(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError('analysis output must be outside the immutable input bundle')
    with campaign_lock(root, shared=True):
        return analyze_locked(root, output)


def analyze_locked(root, output):
    verify(root)
    if read_json(root / 'analysis/dependencies.json').get('bundle_format') != 5:
        raise ValueError('unsupported bundle format; use the bundle archived analyzer')
    m = read_json(root / 'campaign.json')
    validate_manifest(m)
    slots = order(m)
    block_dir = root / 'blocks'
    if block_dir.exists() and any(p.name not in {s['block'] for s in slots} for p in block_dir.iterdir()):
        raise ValueError('unregistered block in evidence bundle')
    if read_json(root / 'order.json') != slots:
        raise ValueError('order file differs from frozen assignment')
    registration = root / 'registration.json'
    if (registration.exists() or any(block_dir.glob('*/*/*.command.json'))
            or any(block_dir.glob('*/*/attempt.json'))
            or any(block_dir.glob('*/collection-command.json'))):
        reg = read_json(registration)
        validate_registration(reg)
        if reg.get('campaign_sha256') != digest(root / 'campaign.json') or reg.get('order_sha256') != digest(root / 'order.json'):
            raise ValueError('registration hash mismatch')
    rows = [trial(root, m, slot, arm) for slot in slots for arm in slot['arms']]
    blocks = defaultdict(dict)
    for row in rows:
        blocks[row['block']][row['arm']] = row
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
        if m['experiment'] == 'E2':
            metrics.extend(('queue_inclusive_seconds', 'initial_queue_seconds'))
        if m['experiment'] == 'E1' and m['phase'] == 'adoption-cold':
            metrics.append('adoption_seconds')
        results[workload] = {}
        if m['experiment'] not in ('E1', 'E2'):
            continue  # Collection is reusable; future estimands are not defined here.
        for metric in sorted(set(metrics)):
            pairs = [(b['date_slot'], b['block_id'], b['metrics'][metric], w['metrics'][metric])
                     for b, w in valid if counter(b['metrics'].get(metric)) and
                     counter(w['metrics'].get(metric))]
            dates = ['%02d' % i for i in range(1, len(counts) + 1)]
            results[workload][metric] = estimate(m, workload, metric, pairs, dates)
    decision = {'status': 'descriptive', 'reasons': [], 'higher_resource_cost': [], 'undefined_resource_ratios': []}
    if m['experiment'] not in ('E1', 'E2'):
        decision.update(status='accounting_only', reasons=['protocol_analysis_not_implemented'])
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
        cost_metrics = E1_RESOURCES if m['experiment'] == 'E1' else ('runner_minutes',)
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
    def inline_code(value):
        text = str(value).replace('\n', ' ').replace('\r', ' ')
        fence = '`' * (max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
        return fence + ' ' + text + ' ' + fence
    lines = ['# Campaign analysis', '', 'Campaign: %s (%s / %s).' %
             (inline_code(m['campaign_id']), m['experiment'], inline_code(m['phase'])), '',
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
            return value.get('ratio_interval_reason', 'undefined_ratio')
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
    failures = [row for row in rows if row['classification'] != 'complete' and
                (row['classification'] != 'not_started' or row['started'] or
                 (root / 'blocks' / row['block'] / row['arm']).exists())]
    lines += ['', 'Retained failed-attempt evidence (excluded from paired estimates):', '',
              '| Block | Workload | Arm | Outcome | Reason | Started | Exit | Elapsed seconds (run/spawn) |',
              '| --- | --- | --- | --- | --- | --- | ---: | ---: |']
    for row in failures:
        lines.append('| %s | %s | %s | %s | %s | %s | %s | %s |' %
                     (cell(row['block']), cell(row['workload']), row['arm'], row['classification'],
                      cell(row['reason']), str(row['started']).lower(), display(row['exit']),
                      display(row['elapsed_seconds'])))
    if not failures:
        lines.append('| — | — | — | none | — | — | N/A | N/A |')
    lines += ['', '| Block | Arm | Retained metric (quality may be invalid) | Observed value |',
              '| --- | --- | --- | ---: |']
    for row in failures:
        names = set(m['resource_metrics']) | set(m.get('analysis_metrics', [])) | set(row['metrics'])
        for name in sorted(names):
            lines.append('| %s | %s | %s | %s |' %
                         (cell(row['block']), row['arm'], cell(name), display(row['metrics'].get(name))))
    if not failures:
        lines.append('| — | — | none | N/A |')
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
                         (cell(workload), cell(metric), value['n'], display(value['ratio']),
                          interval_display(value, 'ratio'), display(value['difference']),
                          interval_display(value, 'difference'), interval_reason(value)))
    lines += ['', '| Workload | Metric | Pairs | Improvement (1 - ratio) | Improvement 95% interval | Baseline median | Baseline IQR [Q1, Q3] | Candidate median | Candidate IQR [Q1, Q3] |',
              '| --- | --- | ---: | ---: | --- | ---: | --- | ---: | --- |']
    for workload, metrics in results.items():
        for metric, value in metrics.items():
            def arm_summary(arm):
                summary = value.get(arm, {})
                return (display(summary.get('median')), '[%s, %s]' %
                        (display(summary['q1']), display(summary['q3'])) if summary else 'N/A')
            lines.append('| %s | %s | %d | %s | %s | %s | %s | %s | %s |' %
                         (cell(workload), cell(metric), value['n'], display(value.get('improvement')),
                          interval_display(value, 'improvement'), *arm_summary('baseline'),
                          *arm_summary('candidate')))
    lines += ['', '| Workload | Metric | Date slot | UTC date | Planned pairs | Valid pairs | Geometric ratio | Mean difference |',
              '| --- | --- | --- | --- | ---: | ---: | ---: | ---: |']
    for workload, metrics in results.items():
        for metric, value in metrics.items():
            for index, planned in enumerate(m['strata'][workload]):
                slot = '%02d' % (index + 1)
                summary = value.get('by_date', {}).get(slot, {})
                lines.append('| %s | %s | %s | %s | %d | %d | %s | %s |' %
                             (cell(workload), cell(metric), slot, m['dates'][index], planned,
                              summary.get('n', 0), display(summary.get('ratio')),
                              display(summary.get('difference'))))
    lines += ['', 'Exact estimates, intervals, arm summaries, date estimates, all failure counts',
              'and pair reasons are also in `results.json`; every planned slot is in `trials.csv`.',
              '', 'Secondary endpoints are descriptive. Results are conditional on the',
              'registered dates, environments, caches and workload. No p99, labor savings',
              'or broad application claim follows from this analysis.', '']
    (output / 'report.md').write_text('\n'.join(lines))
    write_json(output / 'command.json', {'argv': [sys.executable, '-B', str(Path(__file__).with_name('tool.py')),
                                                'analyze', str(root), str(output)]})
    from common import seal
    seal(output)
    return result
