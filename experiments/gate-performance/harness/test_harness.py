#!/usr/bin/env python3
"""Constructed evidence tests; these are not E1/E2 measurements."""
import copy
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from analysis import analyze, estimate, quantile
from ci import TASKS, endpoint, snapshot
from collector import collect, execute, freeze, receipt_valid, validate_manifest
from common import (E1_RESOURCES, E2_RESOURCES, PROTOCOLS, QUALITY, SEED, canonical,
                    digest, order, read_json, sampling, seal, sha, verify, write_json)

HERE = Path(__file__).resolve().parent


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.hook = self.source / 'hook.py'
        self.hook.write_text('''import sys,json,hashlib,os
from pathlib import Path
p=Path(sys.argv[2]); step=sys.argv[1]
if step=='run':
 print(os.environ.get('TEST_REDACTION_SECRET','safe'))
 print(json.dumps(os.environ.get('TEST_REDACTION_SECRET','safe')))
 sys.exit(int(os.environ.get('RUN_EXIT','0')))
if step=='observe':
 data=b'synthetic oracle and cleanup evidence\\n'
 (p/'evidence.log').write_bytes(data)
 evidence={'evidence.log':hashlib.sha256(data).hexdigest()}
 receipt={'checks':{k:True for k in json.loads(sys.argv[3])},
 'facts':dict(json.loads(sys.argv[6]),host='test',allocation=sys.argv[4]),
 'metrics':{k:2 for k in json.loads(sys.argv[5])},
 'environment':{'host':'test','allocation':sys.argv[4]},
 'resource_method':'constructed test only','evidence':evidence}
 (p/'receipt.json').write_text(json.dumps(receipt))
if step=='cleanup':
 data=b'observed cleanup in constructed test\\n'
 (p/'cleanup.log').write_bytes(data)
 (p/'cleanup.json').write_text(json.dumps({'proven':os.environ.get('UNSAFE')!='1',
 'evidence':{'cleanup.log':hashlib.sha256(data).hexdigest()}}))
''')
        for name in PROTOCOLS:
            (self.source / name).write_bytes((HERE.parent / name).read_bytes())
        self.manifest = self.make_manifest()

    def make_manifest(self, experiment='E1', phase='pilot'):
        strata = sampling(experiment, phase)
        dates = [datetime.now(timezone.utc).strftime('%Y-%m-%d'), '2099-01-01']
        if phase == 'confirmation':
            dates.append('2099-01-02')
        resources = list(E1_RESOURCES if experiment == 'E1' else E2_RESOURCES)
        def facts(w):
            return {'schedule': 'sch_constructed', 'flaky': False,
                    'schedule_runs': 21 if w == 'example-vulnerable-explore' else (
                        18 if w == 'example-fixed-explore' else 20)}
        command = lambda step, w: {'argv': [sys.executable, str(self.hook), step, '{trial_dir}',
                                         json.dumps(QUALITY), '{block_id}', json.dumps(resources), json.dumps(facts(w))],
                                 'cwd': str(self.source), 'env': {'RUN_EXIT': str(2 if 'vulnerable' in w else 0)},
                                 'timeout_seconds': 600 if step == 'run' else 5}
        commands = {w: {arm: {step: command(step, w) for step in
                              ('prepare', 'run', 'observe', 'check', 'cleanup')}
                        for arm in ('baseline', 'candidate')} for w in strata}
        m = {'schema': 1, 'campaign_id': 'constructed', 'experiment': experiment,
             'phase': phase, 'protocol_revision': 1, 'seed': SEED, 'strata': strata,
             'dates': dates, 'quality_checks': list(QUALITY), 'resource_metrics': resources,
             'redact_env': [], 'compare_facts': ['schedule', 'host'], 'commands': commands,
             'expected_exit': {w: {a: [2 if 'vulnerable' in w else 0] for a in ('baseline', 'candidate')} for w in strata},
             'expected_facts': {w: {a: facts(w) for a in
                                    ('baseline', 'candidate')} for w in strata},
             'pins': [{'path': 'hook.py', 'sha256': digest(self.hook), 'role': 'checker'}],
             'execution_pins': [{'path': str(self.hook), 'sha256': digest(self.hook), 'role': 'checker'}],
             'protocols': {name: {'path': name, 'sha256': digest(self.source / name)} for name in PROTOCOLS}}
        for section in ('arms', 'inputs', 'runtime', 'environment', 'observation', 'sources'):
            m[section] = {'constructed': 'test only'}
        return m

    def frozen(self, m=None):
        m = m or self.manifest
        manifest = self.source / 'manifest.json'
        write_json(manifest, m)
        bundle = self.root / 'bundle'
        freeze(manifest, bundle)
        registration = self.root / 'registration.json'
        write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                 'order_sha256': digest(bundle / 'order.json'),
                                 'registered_at': '2020-01-01T00:00:00Z',
                                 'revision': 'constructed-receipt',
                                 'public_url': 'https://example.invalid/constructed'})
        return bundle, registration

    def e2_manifest(self):
        m = self.make_manifest('E2')
        m['ci'] = {'repository': 'owner/repo', 'execution_sha': 'a' * 40,
                   'tasks': {arm: {task: {'job': 'acceptance', 'step': 'check'}
                                   for task in TASKS} for arm in ('baseline', 'candidate')}}
        for workload, arms in m['commands'].items():
            for arm, specs in arms.items():
                specs['run']['timeout_seconds'] = 5400
                m['expected_exit'][workload][arm] = [0]
        return m

    def block(self, bundle):
        return read_json(bundle / 'order.json')[0]['block']

    def test_fixed_order_matches_independent_hash_algorithm(self):
        m = self.manifest
        rows = order(m)
        self.assertEqual(len(rows), 32)
        for workload in m['strata']:
            for slot in ('01', '02'):
                group = [r for r in rows if r['workload'] == workload and r['date_slot'] == slot]
                self.assertEqual(sum(r['arms'][0] == 'baseline' for r in group), 2)
                def hash_for(row, suffix):
                    import hashlib
                    value = f'{SEED}|E1|pilot|{workload}|{slot}|{row["block_id"]}|{suffix}'
                    return hashlib.sha256(value.encode()).digest()
                expected = sorted(group, key=lambda row: hash_for(row, 'sequence'))
                self.assertEqual(group, expected)
                first = sorted(group, key=lambda row: hash_for(row, 'order'))[:2]
                self.assertTrue(all(r['arms'][0] == 'baseline' for r in first))
        spring = order(self.make_manifest(phase='confirmation'))
        for slot, first_count in [('02', 1), ('03', 2)]:
            group = [r for r in spring if r['workload'] == 'spring-fixed-replay' and r['date_slot'] == slot]
            self.assertEqual(sum(r['arms'][0] == 'baseline' for r in group), first_count)

    def test_freeze_rejects_counts_protocol_drift_and_bad_pins(self):
        for mutation in ('count', 'pin', 'protocol', 'path', 'empty_quality'):
            m = copy.deepcopy(self.manifest)
            if mutation == 'count':
                m['strata']['example-fixed-replay'] = [5, 4]
            elif mutation == 'pin':
                m['pins'][0]['sha256'] = '0' * 64
            elif mutation == 'protocol':
                (self.source / 'e1-gate.md').write_text('changed protocol')
                m['protocols']['e1-gate.md']['sha256'] = digest(self.source / 'e1-gate.md')
            elif mutation == 'path':
                m['pins'][0]['path'] = '../hook.py'
            else:
                m['quality_checks'] = []
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                self.frozen(m)
            (self.source / 'e1-gate.md').write_bytes((HERE.parent / 'e1-gate.md').read_bytes())
            self.assertFalse((self.root / 'bundle').exists())

    def test_collect_and_analysis_replay_every_slot(self):
        bundle, registration = self.frozen()
        self.assertTrue(collect(bundle, self.block(bundle), registration))
        verify(bundle)
        with self.assertRaises(FileExistsError):
            collect(bundle, self.block(bundle), registration)
        one = analyze(bundle, self.root / 'one')
        # Copy input bundle away from source and invoke its archived analyzer.
        moved = self.root / 'moved'
        shutil.copytree(bundle, moved)
        two = analyze(moved, self.root / 'two')
        self.assertEqual(one, two)
        self.assertEqual((self.root / 'one/results.json').read_bytes(),
                         (self.root / 'two/results.json').read_bytes())
        self.assertEqual(len(read_json(self.root / 'one/trials.json')), 64)
        total = one['accounting']['example-fixed-explore']
        self.assertEqual(total['baseline']['planned'], 8)
        self.assertIn('commands.jsonl', read_json(bundle / 'SHA256SUMS'))
        self.assertIn('fewer_than_two_valid_blocks_in_planned_date',
                      (self.root / 'one/report.md').read_text())

    def test_freeze_rejects_noncanonical_dates_before_creating_bundle(self):
        for date in ('2099-1-1', '2099-01-1', '2099-1-01', '2099-02-30'):
            m = copy.deepcopy(self.manifest)
            m['dates'][1] = date
            with self.subTest(date=date), self.assertRaises(ValueError):
                self.frozen(m)
            self.assertFalse((self.root / 'bundle').exists())
        self.frozen()  # Canonical dates still freeze.

    def test_freeze_requires_string_working_directories(self):
        for step in ('prepare', 'run', 'observe', 'check', 'cleanup'):
            for index, cwd in enumerate((42, True, ['path'], {'path': 'cwd'}, '', None)):
                m = copy.deepcopy(self.manifest)
                m['commands']['example-fixed-replay']['baseline'][step]['cwd'] = cwd
                source = self.source / 'cwd-manifest.json'
                write_json(source, m)
                destination = self.root / ('cwd-%s-%d' % (step, index))
                with self.subTest(step=step, cwd=cwd):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        self.frozen()  # Valid string directories still freeze.

    def test_freeze_requires_nonempty_argv_lists(self):
        for step in ('prepare', 'run', 'observe', 'check', 'cleanup'):
            for index, argv in enumerate(('/bin/true', {'/bin/true': 1}, 1, True,
                                          None, [], [1], [None])):
                m = copy.deepcopy(self.manifest)
                m['commands']['example-fixed-replay']['baseline'][step]['argv'] = argv
                source = self.source / 'argv-manifest.json'
                write_json(source, m)
                destination = self.root / ('argv-%s-%d' % (step, index))
                with self.subTest(step=step, argv=argv):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        self.frozen()  # Valid argv lists still freeze.

    def test_freeze_rejects_unspawnable_environments(self):
        for step in ('prepare', 'run', 'observe', 'check', 'cleanup'):
            for index, env in enumerate(({'BAD=NAME': 'x'}, {'BAD\0NAME': 'x'},
                                         {'NAME': 'bad\0value'})):
                with self.subTest(step=step, env=env):
                    with self.assertRaises(ValueError):
                        subprocess.Popen([sys.executable, '-c', 'pass'], env=env)
                    m = copy.deepcopy(self.manifest)
                    m['commands']['example-fixed-replay']['baseline'][step]['env'] = env
                    source = self.source / 'env-manifest.json'
                    write_json(source, m)
                    destination = self.root / ('env-%s-%d' % (step, index))
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        self.frozen()

    def test_freeze_requires_integer_versions(self):
        for field in ('schema', 'protocol_revision'):
            for index, value in enumerate((True, False, 1.0, '1', None, 0, -1)):
                m = copy.deepcopy(self.manifest)
                m[field] = value
                source = self.source / 'version-manifest.json'
                write_json(source, m)
                destination = self.root / ('version-%s-%d' % (field, index))
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        self.frozen()

    def test_future_protocol_freeze_requires_string_seed(self):
        for experiment in ('E3', 'E4'):
            for index, seed in enumerate((1, True, None, [], {}, '', '   ')):
                m = copy.deepcopy(self.manifest)
                m.update(experiment=experiment, seed=seed)
                source = self.source / 'seed-manifest.json'
                write_json(source, m)
                destination = self.root / ('seed-%s-%d' % (experiment, index))
                with self.subTest(experiment=experiment, seed=seed):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
            m.update(seed='future-protocol-seed', protocol_revision=2)
            write_json(source, m)
            bundle = self.root / ('valid-seed-' + experiment)
            freeze(source, bundle)
            self.assertEqual(read_json(bundle / 'order.json'), order(m))
            analyze(bundle, self.root / ('future-analysis-' + experiment))

    def test_collection_rejects_malformed_registration_before_entering_slot(self):
        bundle, registration = self.frozen()
        valid = read_json(registration)
        bad_urls = (True, 1, {}, [], None, '', '   ', 'relative/path',
                    'ftp://example.invalid/archive', 'https:///archive',
                    'https://', 'https://example.invalid:bad/archive',
                    'https://example.invalid/a\nb', 'https://user:pass@example.invalid/archive')
        for field, values in (('public_url', bad_urls),
                              ('revision', (True, 1, {}, [], None, '', '   '))):
            for value in values:
                reg = dict(valid, **{field: value})
                write_json(registration, reg)
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        collect(bundle, self.block(bundle), registration)
                    self.assertFalse((bundle / 'blocks').exists())
                    self.assertFalse((bundle / 'registration.json').exists())
                    verify(bundle)
        write_json(registration, valid)
        self.assertTrue(collect(bundle, self.block(bundle), registration))
        write_json(bundle / 'registration.json', dict(valid, public_url=True))
        seal(bundle)
        with self.assertRaisesRegex(ValueError, 'registration'):
            analyze(bundle, self.root / 'invalid-registration-analysis')
        self.assertFalse((self.root / 'invalid-registration-analysis').exists())

    def test_report_preserves_campaign_metadata_without_markdown_injection(self):
        m = copy.deepcopy(self.manifest)
        m.update(experiment='E3', campaign_id='campaign`\n# injected <tag>|**bold**',
                 phase='phase``\r\n## injected')
        bundle, _ = self.frozen(m)
        result = analyze(bundle, self.root / 'metadata')
        report = (self.root / 'metadata/report.md').read_text()
        self.assertEqual(result['campaign'], m['campaign_id'])
        self.assertEqual(result['phase'], m['phase'])
        header = report.splitlines()[2]
        self.assertEqual(header, 'Campaign: `` campaign` # injected <tag>|**bold** `` '
                         '(E3 / ``` phase``  ## injected ```).')
        self.assertNotIn('\n# injected', report)
        self.assertNotIn('\n## injected', report)

    def test_report_includes_arm_distributions_and_every_planned_date(self):
        m = self.e2_manifest()
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        state = {'missing_second_date': False}
        def constructed_row(root, manifest, slot, arm):
            index = int(slot['block_id'])
            baseline = (10 if slot['date_slot'] == '01' else 20) + index
            value = baseline if arm == 'baseline' else baseline / (2 if slot['date_slot'] == '01' else 4)
            missing = state['missing_second_date'] and slot['date_slot'] == '02'
            return dict(slot, arm=arm, attempt=1, started=not missing, cleanup_proven=not missing,
                        classification='not_started' if missing else 'complete',
                        reason='slot_not_collected' if missing else '', exit=0, elapsed_seconds=1,
                        metrics={'validation_seconds': value, 'runner_minutes': value,
                                 'peak_runner_concurrency': 0},
                        facts={'schedule': 'sch_constructed', 'host': 'test',
                               'ci_created_at': '2026-10-13T00:00:00Z',
                               'ci_completed_at': '2026-10-13T00:00:00Z'})
        for missing in (False, True):
            state['missing_second_date'] = missing
            output = self.root / ('distributions-' + str(missing))
            with patch('analysis.trial', side_effect=constructed_row):
                result = analyze(bundle, output)
            report = (output / 'report.md').read_text()
            self.assertIn('Improvement 95% interval', report)
            self.assertIn('Baseline median', report)
            self.assertIn('Candidate median', report)
            self.assertIn('IQR [Q1, Q3]', report)
            endpoint = result['estimates']['external-acceptance']['validation_seconds']
            display = lambda v: 'N/A' if v is None else '%.9g' % v
            self.assertIn('| validation_seconds | %d | %s | %s |' %
                          (endpoint['n'], display(endpoint['improvement']),
                           '[%s, %s]' % tuple(map(display, endpoint['interval']['improvement']))
                           if endpoint['interval'] else 'N/A'), report)
            for arm in ('baseline', 'candidate'):
                summary = endpoint[arm]
                self.assertIn('%s | [%s, %s]' % (display(summary['median']),
                              display(summary['q1']), display(summary['q3'])), report)
            for slot, date in enumerate(m['dates'], 1):
                summary = endpoint['by_date']['%02d' % slot]
                self.assertIn('| external-acceptance | validation_seconds | %02d | %s | 3 | %d | %s | %s |' %
                              (slot, date, summary['n'], display(summary['ratio']),
                               display(summary['difference'])), report)
            self.assertIn('| peak_runner_concurrency |', report)
            if missing:
                self.assertIn('| validation_seconds | 3 | 0.5 | N/A |', report)
            else:
                self.assertIsNone(result['estimates']['external-acceptance']['peak_runner_concurrency']['improvement'])

    def test_freeze_requires_deviation_string_lists(self):
        for index, deviations in enumerate((1, True, None, 'deviation', {},
                                            [1], [True], [None], [{}], [[]])):
            m = copy.deepcopy(self.manifest)
            m['deviations'] = deviations
            source = self.source / 'deviations-manifest.json'
            write_json(source, m)
            destination = self.root / ('deviations-%d' % index)
            with self.subTest(deviations=deviations):
                with self.assertRaises(ValueError):
                    freeze(source, destination)
                self.assertFalse(destination.exists())
        for index, deviations in enumerate(([], ['constructed deviation', 'constructed deviation'])):
            m['deviations'] = deviations
            write_json(source, m)
            bundle = self.root / ('valid-deviations-%d' % index)
            freeze(source, bundle)
            output = self.root / ('deviations-analysis-%d' % index)
            analyze(bundle, output)
            report = (output / 'report.md').read_text()
            self.assertIn('- constructed deviation' if deviations else 'None.', report)

    def test_report_escapes_custom_metric_names(self):
        m = copy.deepcopy(self.manifest)
        names = ['latency|p95', 'custom\nrow\r<value>&']
        m['analysis_metrics'] = names[:1]
        m['resource_metrics'] += names[1:]
        bundle, _ = self.frozen(m)
        result = analyze(bundle, self.root / 'escaped-metrics')
        report = (self.root / 'escaped-metrics/report.md').read_text()
        self.assertIn('| latency&#124;p95 |', report)
        self.assertIn('| custom<br>row&lt;value&gt;&amp; |', report)
        self.assertNotIn('custom\nrow', report)
        for metrics in result['estimates'].values():
            self.assertTrue(set(names) <= set(metrics))
        tables = [section for section in report.split('\n\n')
                  if section.startswith('| Workload | Metric |')]
        self.assertEqual(len(tables), 3)
        for table, separators in zip(tables, (9, 10, 9)):
            self.assertIn('| latency&#124;p95 |', table)
            self.assertIn('| custom<br>row&lt;value&gt;&amp; |', table)
            for line in table.splitlines():
                self.assertEqual(line.count('|'), separators)

    def test_freeze_requires_lowercase_execution_sha(self):
        m = self.e2_manifest()
        m['ci']['execution_sha'] = 'A' * 40
        source = self.source / 'sha-manifest.json'
        write_json(source, m)
        with self.assertRaises(ValueError):
            freeze(source, self.root / 'uppercase')
        self.assertFalse((self.root / 'uppercase').exists())
        m['ci']['execution_sha'] = 'a' * 40
        self.frozen(m)

    def test_freeze_validates_optional_analysis_metric_names(self):
        for index, metrics in enumerate((None, 'cpu', {}, [1], [''], [' '], ['cpu', 'cpu'])):
            m = copy.deepcopy(self.manifest)
            m['analysis_metrics'] = metrics
            source = self.source / 'metrics-manifest.json'
            write_json(source, m)
            destination = self.root / ('metrics-%d' % index)
            with self.subTest(metrics=metrics):
                with self.assertRaises(ValueError):
                    freeze(source, destination)
                self.assertFalse(destination.exists())
        m['analysis_metrics'] = []
        validate_manifest(m)
        m['analysis_metrics'] = ['custom_seconds']
        bundle, _ = self.frozen(m)
        result = analyze(bundle, self.root / 'optional-metrics')
        self.assertIn('custom_seconds', result['estimates']['example-fixed-replay'])

    def test_freeze_requires_integer_expected_exit_codes(self):
        for experiment in ('E1', 'E2'):
            m = self.e2_manifest() if experiment == 'E2' else copy.deepcopy(self.manifest)
            workload = 'external-acceptance' if experiment == 'E2' else 'example-fixed-replay'
            for index, exits in enumerate(([False], [0.0], [True], ['0'], 0, [], None)):
                m['expected_exit'][workload]['baseline'] = exits
                source = self.source / 'exits-manifest.json'
                write_json(source, m)
                destination = self.root / ('exits-%s-%d' % (experiment, index))
                with self.subTest(experiment=experiment, exits=exits):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
            m['expected_exit'][workload]['baseline'] = [0]
            validate_manifest(m)

    def test_freeze_requires_expected_fact_objects_for_every_arm(self):
        for experiment in ('E1', 'E2', 'E3', 'E4'):
            base = self.e2_manifest() if experiment == 'E2' else copy.deepcopy(self.manifest)
            base['experiment'] = experiment
            workload = next(iter(base['strata']))
            for arm in ('baseline', 'candidate'):
                for index, value in enumerate((1, True, 'facts', ['facts'], {}, None)):
                    m = copy.deepcopy(base)
                    m['expected_facts'][workload][arm] = value
                    source = self.source / 'fact-manifest.json'
                    write_json(source, m)
                    destination = self.root / ('facts-%s-%s-%d' % (experiment, arm, index))
                    with self.subTest(experiment=experiment, arm=arm, facts=value):
                        with self.assertRaises(ValueError):
                            freeze(source, destination)
                        self.assertFalse(destination.exists())

    def test_freeze_requires_unique_string_inventory_lists(self):
        for name in ('quality_checks', 'resource_metrics', 'compare_facts', 'redact_env'):
            for index, value in enumerate((None, 'field', {'field': True}, [1], [''], [' '],
                                           ['field', 'field'], [['field']])):
                m = copy.deepcopy(self.manifest)
                m[name] = value if name in ('compare_facts', 'redact_env') or not isinstance(value, list) else m[name] + value
                source = self.source / 'inventory-manifest.json'
                write_json(source, m)
                destination = self.root / ('inventory-%s-%d' % (name, index))
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        validate_manifest(self.manifest)  # Empty redact_env is allowed.

    def test_date_boundary_prevents_starting_the_mate_arm(self):
        from collector import execute as real_execute
        from common import timestamp
        m = copy.deepcopy(self.manifest)
        frozen_day = timestamp(m['dates'][0] + 'T23:59:59Z')
        clock = {'now': frozen_day}
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        calls = []
        def command(spec, context, directory, step, secrets):
            calls.append((context['arm'], step))
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'cleanup':
                clock['now'] = frozen_day + timedelta(days=1)
            return result
        with patch('collector.datetime') as date_clock, patch('collector.execute', side_effect=command):
            date_clock.strptime = datetime.strptime
            date_clock.now.side_effect = lambda tz: clock['now']
            self.assertTrue(collect(bundle, slot['block'], registration))
        self.assertEqual({arm for arm, _ in calls}, {slot['arms'][0]})
        mate = read_json(bundle / 'blocks' / slot['block'] / slot['arms'][1] / 'trial.json')
        self.assertFalse(mate['started'])
        self.assertEqual(mate['reason'], 'registered_date_expired')
        verify(bundle)
        result = analyze(bundle, self.root / 'date-boundary')
        self.assertIn('registered_date_expired', result['paired_missingness'][slot['block']])

    def test_date_boundary_during_prepare_cleans_up_without_running(self):
        from collector import execute as real_execute
        from common import timestamp
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        clock = {'now': timestamp(slot['date'] + 'T23:59:59Z')}
        calls = []
        def command(spec, context, directory, step, secrets):
            calls.append((context['arm'], step))
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'prepare':
                clock['now'] += timedelta(days=1)
            return result
        with patch('collector.datetime') as date_clock, patch('collector.execute', side_effect=command):
            date_clock.strptime = datetime.strptime
            date_clock.now.side_effect = lambda tz: clock['now']
            self.assertTrue(collect(bundle, slot['block'], registration))
        self.assertEqual(calls, [(slot['arms'][0], 'prepare'), (slot['arms'][0], 'cleanup')])
        verify(bundle)
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertFalse(row['started'])
            self.assertEqual(row['reason'], 'registered_date_expired')

    def test_later_hooks_cannot_reseal_changed_prior_evidence(self):
        from collector import execute as real_execute
        for index, mutation in enumerate(('overwrite', 'delete', 'frozen', 'added', 'inventory')):
            with self.subTest(mutation=mutation):
                source = self.source / 'protected-manifest.json'
                write_json(source, self.manifest)
                bundle = self.root / ('protected-%d' % index)
                freeze(source, bundle)
                registration = self.root / ('protected-registration-%d.json' % index)
                write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                         'order_sha256': digest(bundle / 'order.json'),
                                         'registered_at': '2020-01-01T00:00:00Z',
                                         'revision': 'constructed', 'public_url': 'https://example.invalid'})
                slots = order(self.manifest)
                collect(bundle, slots[0]['block'], registration)
                target = bundle / 'blocks' / slots[0]['block'] / 'baseline/evidence.log'
                calls = []
                def command(spec, context, directory, step, secrets):
                    calls.append((context['arm'], step))
                    result = real_execute(spec, context, directory, step, secrets)
                    if step == 'prepare':
                        if mutation == 'delete':
                            target.unlink()
                        elif mutation == 'frozen':
                            (bundle / 'pins/hook.py').write_text('replaced frozen evidence')
                        elif mutation == 'added':
                            (target.parent / 'replacement.log').write_text('invented prior evidence')
                        elif mutation == 'inventory':
                            write_json(bundle / 'SHA256SUMS', {})
                        else:
                            target.write_text('replacement observation')
                    return result
                with patch('collector.execute', side_effect=command), self.assertRaises(ValueError):
                    collect(bundle, slots[1]['block'], registration)
                with self.assertRaises(ValueError):
                    verify(bundle)
                self.assertNotIn((slots[1]['arms'][1], 'run'), calls)
                self.assertIn((slots[1]['arms'][0], 'cleanup'), calls)

    def test_nonobject_receipts_remain_sealed_failed_evidence(self):
        original_hook = self.hook.read_text()
        for index, value in enumerate(([], None, 'receipt', 1, True)):
            with self.subTest(value=value):
                self.hook.write_text(original_hook + '''
if step=='observe':
 (p/'receipt.json').write_text(os.environ['MALFORMED_RECEIPT'])
''')
                m = copy.deepcopy(self.manifest)
                checksum = digest(self.hook)
                m['pins'][0]['sha256'] = checksum
                m['execution_pins'][0]['sha256'] = checksum
                for arms in m['commands'].values():
                    for specs in arms.values():
                        specs['observe']['env']['MALFORMED_RECEIPT'] = json.dumps(value)
                source = self.source / 'receipt-manifest.json'
                write_json(source, m)
                bundle = self.root / ('receipts-%d' % index)
                freeze(source, bundle)
                registration = self.root / ('registration-%d.json' % index)
                write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                         'order_sha256': digest(bundle / 'order.json'),
                                         'registered_at': '2020-01-01T00:00:00Z',
                                         'revision': 'constructed', 'public_url': 'https://example.invalid'})
                self.assertTrue(collect(bundle, self.block(bundle), registration))
                verify(bundle)
                for arm in ('baseline', 'candidate'):
                    directory = bundle / 'blocks' / self.block(bundle) / arm
                    self.assertEqual(read_json(directory / 'receipt.json'), value)
                    row = read_json(directory / 'trial.json')
                    self.assertEqual(row['classification'], 'quality_failure')
                    self.assertTrue(row['cleanup_proven'])
                result = analyze(bundle, self.root / ('receipt-analysis-%d' % index))
                self.assertTrue(result['paired_missingness'][self.block(bundle)])
                # Bad first receipts must not prevent the next preassigned block.
                self.assertTrue(collect(bundle, order(m)[1]['block'], registration))
                verify(bundle)

    def test_expected_facts_distinguish_booleans_from_numbers_recursively(self):
        m = copy.deepcopy(self.manifest)
        workload, arm = 'example-fixed-replay', 'baseline'
        m['expected_facts'][workload][arm].update(
            passed=True, nested={'ok': False, 'probes': [True, 1]})
        directory = self.root / 'facts'
        directory.mkdir()
        (directory / 'proof.log').write_text('constructed evidence')
        receipt = {'checks': {key: True for key in QUALITY},
                   'facts': dict(m['expected_facts'][workload][arm], host='test'),
                   'metrics': dict.fromkeys(E1_RESOURCES, 1),
                   'environment': {'host': 'test'}, 'resource_method': 'constructed',
                   'evidence': {'proof.log': digest(directory / 'proof.log')}}
        receipt['metrics']['gate_seconds'] = 1
        row = {'workload': workload, 'arm': arm}
        receipt_valid(m, row, receipt, directory)
        mutations = [('flaky', 0), ('passed', 1),
                     ('nested', {'ok': 0, 'probes': [True, 1]}),
                     ('nested', {'ok': False, 'probes': [1, 1]}),
                     ('nested', {'ok': False, 'probes': [True, True]})]
        for key, value in mutations:
            bad = copy.deepcopy(receipt)
            bad['facts'][key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'facts differ'):
                receipt_valid(m, row, bad, directory)
        # JSON has a single number type; equal integer/float spellings remain valid.
        receipt['facts']['schedule_runs'] = 20.0
        receipt_valid(m, row, receipt, directory)

    def test_ci_pairing_facts_must_match_retained_provider_boundaries(self):
        m = self.e2_manifest()
        data, inventory = CITests().data()
        m['ci']['tasks'] = {arm: copy.deepcopy(inventory['tasks'])
                            for arm in ('baseline', 'candidate')}
        directory = self.root / 'provider'
        directory.mkdir()
        write_json(directory / 'ci.json', data)
        workload, arm = 'external-acceptance', 'baseline'
        receipt = {'checks': {key: True for key in QUALITY},
                   'facts': dict(m['expected_facts'][workload][arm], host='test',
                                 ci_created_at=data['run']['created_at'],
                                 ci_completed_at=data['run']['updated_at']),
                   'metrics': endpoint(data, inventory),
                   'environment': {'host': 'test'}, 'resource_method': 'constructed',
                   'ci_snapshot': 'ci.json', 'evidence': {'ci.json': digest(directory / 'ci.json')}}
        row = {'workload': workload, 'arm': arm}
        receipt_valid(m, row, receipt, directory)
        for key in ('ci_created_at', 'ci_completed_at'):
            for value in ('2026-10-13T00:00:30Z', None, 42):
                bad = copy.deepcopy(receipt)
                bad['facts'][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    receipt_valid(m, row, bad, directory)
            bad = copy.deepcopy(receipt)
            bad['facts'].pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                receipt_valid(m, row, bad, directory)
        # Equivalent UTC timestamp spellings match the provider instant.
        receipt['facts']['ci_created_at'] = '2026-10-13T00:00:00+00:00'
        receipt_valid(m, row, receipt, directory)
        # Use the latest started-job end when metadata precedes it.
        data['run']['updated_at'] = '2026-10-13T00:00:30Z'
        write_json(directory / 'ci.json', data)
        receipt['evidence']['ci.json'] = digest(directory / 'ci.json')
        receipt_valid(m, row, receipt, directory)

    def test_analyzer_rejects_forged_ci_pairing_facts(self):
        m = self.e2_manifest()
        _, inventory = CITests().data()
        m['ci']['tasks'] = {arm: copy.deepcopy(inventory['tasks'])
                            for arm in ('baseline', 'candidate')}
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        slot = order(m)[0]
        for index, arm in enumerate(slot['arms']):
            data, _ = CITests().data()
            if index:
                # The real second dispatch is 600 seconds after first completion.
                shift = timedelta(seconds=635)
                for key in ('created_at', 'updated_at'):
                    data['run'][key] = (datetime.fromisoformat(data['run'][key].replace('Z', '+00:00'))
                                        + shift).isoformat()
                data['run']['id'] = 2
                for job in data['pages'][0]['jobs']:
                    job['run_id'] = 2
                    for key in ('started_at', 'completed_at'):
                        job[key] = (datetime.fromisoformat(job[key].replace('Z', '+00:00')) + shift).isoformat()
                    for step in job['steps']:
                        step['completed_at'] = job['completed_at']
            directory = bundle / 'blocks' / slot['block'] / arm
            directory.mkdir(parents=True)
            write_json(directory / 'ci.json', data)
            facts = dict(m['expected_facts'][slot['workload']][arm], host='test',
                         ci_created_at='2026-10-13T00:00:35Z' if index else data['run']['created_at'],
                         ci_completed_at=data['run']['updated_at'])
            metrics = endpoint(data, inventory)
            receipt = {'checks': dict.fromkeys(QUALITY, True), 'facts': facts, 'metrics': metrics,
                       'environment': {'host': 'test'}, 'resource_method': 'constructed',
                       'ci_snapshot': 'ci.json', 'evidence': {'ci.json': digest(directory / 'ci.json')}}
            write_json(directory / 'receipt.json', receipt)
            for step in ('prepare', 'run', 'observe', 'check', 'cleanup'):
                write_json(directory / (step + '.command.json'),
                           {'exit': 0, 'timeout': False, 'error': None, 'seconds': 1,
                            'started_at': data['run']['created_at']})
            (directory / 'cleanup.log').write_text('constructed cleanup proof')
            write_json(directory / 'cleanup.json',
                       {'proven': True, 'evidence': {'cleanup.log': digest(directory / 'cleanup.log')}})
            write_json(directory / 'trial.json', dict(slot, arm=arm, attempt=1, started=True,
                       cleanup_proven=True, classification='complete', reason='', exit=0,
                       elapsed_seconds=1, facts=facts, metrics=metrics))
        seal(bundle)
        output = self.root / 'forged-analysis'
        result = analyze(bundle, output)
        self.assertIn('CI pairing timestamp disagrees with provider: ci_created_at',
                      result['paired_missingness'][slot['block']])
        rows = read_json(output / 'trials.json')
        self.assertEqual(len(rows), 12)
        self.assertEqual(next(r for r in rows if r['block'] == slot['block'] and
                              r['arm'] == slot['arms'][1])['classification'], 'quality_failure')

    def test_paired_facts_distinguish_booleans_from_numbers(self):
        bundle, registration = self.frozen()
        block = self.block(bundle)
        collect(bundle, block, registration)
        for arm, value in (('baseline', False), ('candidate', 0)):
            directory = bundle / 'blocks' / block / arm
            for name in ('receipt.json', 'trial.json'):
                record = read_json(directory / name)
                record['facts']['host'] = {'nested': [value]}
                write_json(directory / name, record)
        seal(bundle)
        result = analyze(bundle, self.root / 'paired-types')
        self.assertEqual(result['paired_missingness'][block], ['paired_facts_mismatch'])

    def test_freeze_rejects_malformed_e2_inventory_before_creating_bundle(self):
        valid = self.e2_manifest()
        malformed = [True, ['ci'], {}, {'repository': 'owner/repo'}]
        for key in ('repository', 'execution_sha', 'tasks'):
            ci = copy.deepcopy(valid['ci'])
            ci.pop(key)
            malformed.append(ci)
        for repository in (None, 1, 'repo', 'owner/ repo'):
            malformed.append(dict(valid['ci'], repository=repository))
        for revision in (None, 1, 'main', 'g' * 40):
            malformed.append(dict(valid['ci'], execution_sha=revision))
        for arm in ('baseline', 'candidate'):
            for bad_tasks in (None, [], {}, {task: {'job': 'a', 'step': 'b'}
                                            for task in TASKS if task != 'upload'}):
                ci = copy.deepcopy(valid['ci'])
                ci['tasks'][arm] = bad_tasks
                malformed.append(ci)
            for mapping in (None, [], {}, {'job': 'a'}, {'job': '', 'step': 'b'},
                            {'job': 'a', 'step': ' '}, {'job': 1, 'step': 'b'}):
                ci = copy.deepcopy(valid['ci'])
                ci['tasks'][arm]['upload'] = mapping
                malformed.append(ci)
        for ci in malformed:
            m = copy.deepcopy(valid)
            m['ci'] = ci
            with self.subTest(ci=ci), self.assertRaises(ValueError):
                self.frozen(m)
            self.assertFalse((self.root / 'bundle').exists())
        valid['ci']['tasks']['candidate']['extra'] = {'job': 'more', 'step': 'check'}
        self.frozen(valid)  # Additional required tasks remain supported.

    def test_malformed_ci_pairing_timestamps_retain_all_slots(self):
        m = self.e2_manifest()
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        slot = order(m)[0]
        state = {'key': 'ci_created_at', 'value': None}
        def constructed_row(root, manifest, assigned, arm):
            facts = {'schedule': 'sch_constructed', 'host': 'test',
                     'ci_created_at': '2026-10-13T00:00:00Z',
                     'ci_completed_at': '2026-10-13T00:00:00Z'}
            if ((state['key'] == 'ci_created_at' and arm == slot['arms'][1]) or
                    (state['key'] == 'ci_completed_at' and arm == slot['arms'][0])):
                facts[state['key']] = state['value']
            return dict(assigned, arm=arm, attempt=1, started=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics={'validation_seconds': 1, 'runner_minutes': 1,
                                 'peak_runner_concurrency': 1}, facts=facts)
        # Exercise pairing/accounting independently of receipt revalidation.
        for key in ('ci_created_at', 'ci_completed_at'):
            for index, value in enumerate((None, 42, [], {}, 'bad timestamp')):
                state.update(key=key, value=value)
                output = self.root / ('pair-%s-%d' % (key, index))
                with self.subTest(key=key, value=value), \
                        patch('analysis.trial', side_effect=constructed_row):
                    result = analyze(bundle, output)
                    self.assertEqual(result['paired_missingness'][slot['block']],
                                     ['pairing_window_missing'])
                    self.assertEqual(len(read_json(output / 'trials.json')), 12)
                    self.assertEqual(result['accounting']['external-acceptance']['baseline']['planned'], 6)

    def test_all_exits_retained_and_second_arm_runs_after_failure(self):
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for specs in arms.values():
                specs['run']['env'] = {'RUN_EXIT': '4'}
        bundle, registration = self.frozen(m)
        block = self.block(bundle)
        self.assertTrue(collect(bundle, block, registration))
        for arm in ('baseline', 'candidate'):
            row = read_json(bundle / 'blocks' / block / arm / 'trial.json')
            self.assertEqual(row['exit'], 4)
            self.assertEqual(row['classification'], 'operational_failure')
            self.assertTrue(row['cleanup_proven'])
            self.assertEqual(row['metrics']['cpu_seconds'], 2)
            self.assertGreater(row['elapsed_seconds'], 0)
        result = analyze(bundle, self.root / 'failed-report')
        report = (self.root / 'failed-report/report.md').read_text()
        workload = order(m)[0]['workload']
        self.assertIn('| %s | baseline | 8 | 1 | 0 | 1 | 0 | 7 |' % workload, report)
        self.assertIn('unexpected_exit', report)
        self.assertIn(block, report)
        self.assertEqual(result['accounting'][workload]['baseline']['operational_failure'], 1)

    def test_post_spawn_errors_preserve_started_accounting(self):
        from collector import execute as real_execute
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        processes = []
        def command(spec, context, directory, step, secrets):
            if step != 'run':
                return real_execute(spec, context, directory, step, secrets)
            process = Mock(pid=123)
            process.wait.side_effect = [OSError('constructed post-spawn error'), 4]
            processes.append(process)
            with patch('collector.subprocess.Popen', return_value=process), \
                    patch('collector.os.killpg') as kill, patch('collector.wait_group', return_value=True):
                result = real_execute(spec, context, directory, step, secrets)
                kill.assert_called_once_with(123, signal.SIGKILL)
                return result
        with patch('collector.execute', side_effect=command):
            self.assertTrue(collect(bundle, slot['block'], registration))
        self.assertEqual(len(processes), 2)
        verify(bundle)
        result = analyze(bundle, self.root / 'post-spawn-report')
        report = (self.root / 'post-spawn-report/report.md').read_text()
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            row = read_json(directory / 'trial.json')
            receipt = read_json(directory / 'run.command.json')
            self.assertIsNotNone(receipt['error'])
            self.assertTrue(row['started'])
            self.assertTrue(receipt['started'])
            self.assertEqual(row['reason'], 'execution_failed')
            self.assertEqual(row['classification'], 'operational_failure')
            self.assertTrue(row['cleanup_proven'])
            self.assertGreater(row['elapsed_seconds'], 0)
            self.assertEqual(result['accounting'][slot['workload']][arm]['started'], 1)
            self.assertIn('| %s | %s | 8 | 1 | 0 | 1 | 0 | 7 |' % (slot['workload'], arm), report)

    def test_spawn_failures_remain_unstarted_in_accounting(self):
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for specs in arms.values():
                specs['run']['argv'] = [str(self.root / 'absent')]
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertTrue(collect(bundle, slot['block'], registration))
        result = analyze(bundle, self.root / 'spawn-failure-report')
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertFalse(row['started'])
            self.assertEqual(row['reason'], 'spawn_failed')
            self.assertEqual(result['accounting'][slot['workload']][arm]['started'], 0)

    def test_timeout_boundary_error_preserves_started_receipt(self):
        spec = self.manifest['commands']['example-fixed-replay']['baseline']['run']
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired(spec['argv'], 600), 4]
        with patch('collector.subprocess.Popen', return_value=process), \
                patch('collector.os.killpg', side_effect=[ProcessLookupError('constructed exit race'), None]) as kill, \
                patch('collector.wait_group', return_value=True):
            result = execute(spec, {'trial_dir': self.root, 'block_id': 'test', 'redact_env': []},
                             self.root, 'run', [])
        self.assertTrue(result['started'])
        self.assertTrue(result['timeout'])
        self.assertIn('constructed exit race', result['error'])
        self.assertGreater(result['seconds'], 0)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(123, signal.SIGTERM), (123, signal.SIGKILL)])
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(read_json(self.root / 'run.command.json'), result)

    def test_unproven_cleanup_blocks_mate(self):
        m = copy.deepcopy(self.manifest)
        m['deviations'] = ['constructed cleanup|deviation']
        for arms in m['commands'].values():
            for specs in arms.values():
                specs['cleanup']['env'] = {'UNSAFE': '1'}
        bundle, registration = self.frozen(m)
        block = self.block(bundle)
        self.assertFalse(collect(bundle, block, registration))
        slot = read_json(bundle / 'order.json')[0]
        first, second = slot['arms']
        self.assertEqual(read_json(bundle / 'blocks' / block / first / 'trial.json')['reason'], 'cleanup_unproven')
        row = read_json(bundle / 'blocks' / block / second / 'trial.json')
        self.assertFalse(row['started'])
        self.assertIsNone(row['elapsed_seconds'])
        verify(bundle)
        analyze(bundle, self.root / 'stopped-report')
        report = (self.root / 'stopped-report/report.md').read_text()
        self.assertIn('Collection status: stopped', report)
        self.assertIn('cleanup_unproven', report)
        self.assertIn('constructed cleanup&#124;deviation', report)

    def test_redaction_before_hashing_and_retention(self):
        m = copy.deepcopy(self.manifest)
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        bundle, registration = self.frozen(m)
        secret = 'test-\n\"' + os.urandom(16).hex()
        with patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}):
            collect(bundle, self.block(bundle), registration)
        for path in bundle.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
                self.assertNotIn(json.dumps(secret)[1:-1].encode(), path.read_bytes())
        self.assertIn(b'[REDACTED]', (bundle / 'blocks' / self.block(bundle) /
                                    'baseline/run.stdout').read_bytes())
        command = read_json(bundle / 'blocks' / self.block(bundle) / 'baseline/run.command.json')
        self.assertEqual(command['redacted_env_names'], ['TEST_REDACTION_SECRET'])
        self.assertNotIn('TEST_REDACTION_SECRET', command['env'])

    def test_changed_live_checker_or_bundle_rejected(self):
        bundle, registration = self.frozen()
        self.hook.write_text('modified after freeze')
        with self.assertRaises(ValueError):
            collect(bundle, self.block(bundle), registration)
        (bundle / 'order.json').write_text('[]')
        with self.assertRaises(ValueError):
            analyze(bundle, self.root / 'bad')

    def test_missing_or_corrupt_evidence_cannot_complete(self):
        bundle, registration = self.frozen()
        collect(bundle, self.block(bundle), registration)
        directory = bundle / 'blocks' / self.block(bundle) / 'baseline'
        for name in ('receipt.json', 'evidence.log', 'cleanup.log'):
            path = directory / name
            original = path.read_bytes()
            path.write_text('{}')
            seal(bundle)  # Transport hash alone cannot manufacture valid evidence.
            result = analyze(bundle, self.root / name)
            self.assertTrue(result['paired_missingness'][self.block(bundle)])
            path.write_bytes(original)
            seal(bundle)

    def test_interrupted_block_stays_missing_and_cannot_restart(self):
        bundle, registration = self.frozen()
        block = self.block(bundle)
        (bundle / 'blocks' / block).mkdir(parents=True)
        seal(bundle)
        result = analyze(bundle, self.root / 'interrupted')
        self.assertIn('interrupted_collection', result['paired_missingness'][block])
        with self.assertRaises(FileExistsError):
            collect(bundle, block, registration)

    def test_timeout_and_spawn_error_observable(self):
        context = {'redact_env': []}
        spec = {'argv': [str(self.root / 'absent')], 'cwd': str(self.root), 'env': {}, 'timeout_seconds': .01}
        result = execute(spec, context, self.root, 'absent', [])
        self.assertIsNotNone(result['error'])
        self.assertIsNone(result['exit'])
        self.assertFalse(result['started'])
        spec['argv'] = [sys.executable, '-c', 'import threading; threading.Event().wait()']
        result = execute(spec, context, self.root, 'timeout', [])
        self.assertTrue(result['started'])
        self.assertTrue(result['timeout'])
        self.assertLess(result['exit'], 0)

    def test_interrupted_timeout_grace_kills_and_reaps_before_propagating(self):
        process = Mock(pid=123456)
        process.wait.side_effect = [subprocess.TimeoutExpired(['hook'], 1), KeyboardInterrupt(), -9]
        events = []
        def kill(pid, sig):
            events.append(('signal', pid, sig))
        def wait(*args, **kwargs):
            events.append(('wait', kwargs))
            value = next(responses)
            if isinstance(value, BaseException):
                raise value
            return value
        responses = iter(process.wait.side_effect)
        process.wait.side_effect = wait
        spec = {'argv': ['hook'], 'cwd': str(self.root), 'env': {},
                'timeout_seconds': 1, 'cancel_grace_seconds': 25}
        with patch('collector.subprocess.Popen', return_value=process), \
                patch('collector.os.killpg', side_effect=kill), self.assertRaises(KeyboardInterrupt):
            execute(spec, {'redact_env': []}, self.root, 'interrupt', [])
        self.assertEqual(events, [('wait', {'timeout': 1}), ('signal', process.pid, signal.SIGTERM),
                                  ('wait', {'timeout': 25}), ('signal', process.pid, signal.SIGKILL),
                                  ('wait', {})])

    def test_timeout_kills_descendants_after_leader_exits(self):
        import ctypes
        # Linux child-subreaper lets this test reap the orphaned grandchild.
        libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(old), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        spec = {'argv': [sys.executable, '-c', '''
import os, signal, sys, threading
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
read_end, write_end = os.pipe()
pid = os.fork()
if pid == 0:
 signal.signal(signal.SIGTERM, signal.SIG_IGN)
 os.close(read_end)
 os.write(write_end, b'ready')
 os.close(write_end)
 threading.Event().wait()
else:
 os.close(write_end)
 os.read(read_end, 5)
 os.close(read_end)
 Path('descendant.pid').write_text(str(pid))
 with Path('ready.fifo').open('wb', buffering=0) as stream:
  stream.write(b'ready')
 threading.Event().wait()
'''], 'cwd': str(self.root), 'env': {}, 'timeout_seconds': .02, 'cancel_grace_seconds': .05}
        descendant = None
        ready = self.root / 'ready.fifo'
        os.mkfifo(ready)
        original_popen = subprocess.Popen
        def ready_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            with ready.open('rb', buffering=0) as stream:
                self.assertEqual(stream.read(5), b'ready')
            return process
        try:
            with patch('collector.subprocess.Popen', side_effect=ready_process):
                result = execute(spec, {'redact_env': []}, self.root, 'descendant', [])
            descendant = int((self.root / 'descendant.pid').read_text())
            self.assertTrue(result['timeout'])
            pid, status = os.waitpid(descendant, os.WNOHANG)
            self.assertEqual(pid, descendant, 'descendant survived return from execute')
            self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
            descendant = None
        finally:
            if descendant is not None:
                os.kill(descendant, signal.SIGKILL)
                os.waitpid(descendant, 0)
            libc.prctl(36, old.value, 0, 0, 0)

    def test_missing_confirmation_cannot_claim_improvement(self):
        bundle, _ = self.frozen(self.make_manifest(phase='confirmation'))
        result = analyze(bundle, self.root / 'incomplete')
        self.assertEqual(result['decision']['status'], 'no_positive_claim')
        self.assertIsNone(result['estimates']['example-fixed-replay']['gate_seconds']['ratio'])
        report = (self.root / 'incomplete/report.md').read_text()
        self.assertIn('ratio <= 0.85', report)
        self.assertIn('95% bootstrap interval: N/A', report)
        self.assertIn('incomplete_or_invalid_planned_pairs', report)
        self.assertIn('Primary interval unavailable: `no_valid_pairs`', report)
        self.assertIn('| example-fixed-replay | baseline | 30 | 0 | 0 | 0 | 0 | 30 |', report)
        self.assertIn('slot_not_collected', report)
        self.assertIn('Planned pairs: 140; valid pairs: 0; excluded pairs: 140', report)

    def test_report_renders_calculated_uncertainty_for_pilot_metrics(self):
        m = self.e2_manifest()
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        def constructed_row(root, manifest, slot, arm):
            value = 2 if arm == 'baseline' else 1
            return dict(slot, arm=arm, attempt=1, started=True, cleanup_proven=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics={'validation_seconds': value, 'runner_minutes': value,
                                 'peak_runner_concurrency': value},
                        facts={'schedule': 'sch_constructed', 'host': 'test',
                               'ci_created_at': '2026-10-13T00:00:00Z',
                               'ci_completed_at': '2026-10-13T00:00:00Z'})
        with patch('analysis.trial', side_effect=constructed_row):
            result = analyze(bundle, self.root / 'pilot-intervals')
        self.assertEqual(result['decision']['status'], 'descriptive')
        report = (self.root / 'pilot-intervals/report.md').read_text()
        self.assertIn('Ratio 95% interval', report)
        self.assertIn('Difference 95% interval', report)
        for metric in ('validation_seconds', 'runner_minutes', 'peak_runner_concurrency'):
            self.assertEqual(result['estimates']['external-acceptance'][metric]['interval'],
                             {'ratio': [.5, .5], 'difference': [-1, -1], 'improvement': [.5, .5]})
            self.assertIn('| %s | 6 | 0.5 | [0.5, 0.5] | -1 | [-1, -1] |' % metric, report)
        self.assertNotIn('Confirmatory timing rule', report)


    def test_decision_requires_secondary_pairs_and_discloses_cost(self):
        m = self.make_manifest(phase='confirmation')
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        state = {'missing': False, 'cost': 1.0}
        def constructed_row(root, manifest, slot, arm):
            missing = state['missing'] and slot['workload'] == 'spring-vulnerable-replay'
            return dict(slot, arm=arm, attempt=1, started=True,
                        classification='not_started' if missing else 'complete',
                        reason='missing_secondary' if missing else '',
                        exit=0, elapsed_seconds=1,
                        metrics={name: 1 for name in m['resource_metrics']} | {'gate_seconds': 1},
                        facts={'schedule': 'sch_constructed', 'host': 'test'})
        def fixed_estimate(manifest, workload, metric, pairs, dates):
            # Isolate the decision gate; bootstrap arithmetic is independently tested above.
            ratio = .7 if metric == 'gate_seconds' else state['cost']
            return {'n': len(pairs), 'ratio': ratio, 'difference': -1,
                    'interval': {'ratio': [ratio, ratio]}}
        with patch('analysis.trial', side_effect=constructed_row), \
                patch('analysis.estimate', side_effect=fixed_estimate):
            result = analyze(bundle, self.root / 'good-decision')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertNotIn('faster with higher measured resource cost',
                             (self.root / 'good-decision/report.md').read_text())
            report = (self.root / 'good-decision/report.md').read_text()
            self.assertIn('95% bootstrap interval: [0.7, 0.7]', report)
            self.assertIn('ratio <= 0.85', report)
            state['missing'] = True
            result = analyze(bundle, self.root / 'missing-secondary')
            self.assertEqual(result['decision']['status'], 'no_positive_claim')
            self.assertIn('incomplete_or_invalid_planned_pairs', result['decision']['reasons'])
            state.update(missing=False, cost=1.2)
            result = analyze(bundle, self.root / 'higher-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(set(result['decision']['higher_resource_cost']),
                             {'cpu_seconds', 'peak_memory_bytes', 'disk_write_bytes'})
            report = (self.root / 'higher-cost/report.md').read_text()
            self.assertIn('faster with higher measured resource cost', report)
            for metric in result['decision']['higher_resource_cost']:
                self.assertIn('`' + metric + '`', report)
            state['cost'] = None
            result = analyze(bundle, self.root / 'undefined-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(set(result['decision']['undefined_resource_ratios']),
                             {'cpu_seconds', 'peak_memory_bytes', 'disk_write_bytes'})
            self.assertIn('no resource-efficiency claim follows',
                          (self.root / 'undefined-cost/report.md').read_text())

    def test_e2_report_qualifies_faster_feedback_with_higher_runner_cost(self):
        m = self.e2_manifest()
        m['phase'] = 'confirmation'
        m['strata'] = sampling('E2', 'confirmation')
        m['dates'].append('2099-01-02')
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        state = {'cost': 1.2, 'timing': .7}
        def constructed_row(root, manifest, slot, arm):
            return dict(slot, arm=arm, attempt=1, started=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics={'validation_seconds': 1, 'runner_minutes': 1,
                                 'peak_runner_concurrency': 1},
                        facts={'schedule': 'sch_constructed', 'host': 'test',
                               'ci_created_at': '2026-10-13T00:00:00Z',
                               'ci_completed_at': '2026-10-13T00:00:00Z'})
        def fixed_estimate(manifest, workload, metric, pairs, dates):
            ratio = state['timing'] if metric == 'validation_seconds' else state['cost']
            return {'n': len(pairs), 'ratio': ratio, 'difference': -1,
                    'interval': {'ratio': [ratio, ratio]}}
        with patch('analysis.trial', side_effect=constructed_row), \
                patch('analysis.estimate', side_effect=fixed_estimate):
            result = analyze(bundle, self.root / 'e2-higher-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(result['decision']['higher_resource_cost'], ['runner_minutes'])
            report = (self.root / 'e2-higher-cost/report.md').read_text()
            self.assertIn('faster feedback with higher runner cost', report)
            self.assertIn('`runner_minutes`', report)
            self.assertIn('ratio <= 0.8', report)
            self.assertIn('95% bootstrap interval: [0.7, 0.7]', report)
            state['cost'] = 1.10
            result = analyze(bundle, self.root / 'e2-boundary-cost')
            self.assertEqual(result['decision']['higher_resource_cost'], [])
            self.assertNotIn('faster feedback with higher runner cost',
                             (self.root / 'e2-boundary-cost/report.md').read_text())
            state.update(cost=1.2, timing=.95)
            result = analyze(bundle, self.root / 'e2-no-improvement')
            self.assertEqual(result['decision']['status'], 'no_positive_claim')
            report = (self.root / 'e2-no-improvement/report.md').read_text()
            self.assertIn('Higher measured resource cost', report)
            self.assertNotIn('faster feedback with higher runner cost', report)

    def test_wrong_expected_exit_and_repetition_counts_rejected(self):
        for field in ('expected_exit', 'expected_facts'):
            m = copy.deepcopy(self.manifest)
            if field == 'expected_exit':
                m[field]['example-vulnerable-replay']['baseline'] = [0]
            else:
                m[field]['example-fixed-replay']['baseline']['schedule_runs'] = 1
            with self.assertRaises(ValueError):
                validate_manifest(m)

    def test_assignment_sequence_date_registration_and_lock_are_enforced(self):
        bundle, registration = self.frozen()
        slots = read_json(bundle / 'order.json')
        with self.assertRaises(ValueError):
            collect(bundle, slots[1]['block'], registration)
        future = next(s for s in slots if s['date_slot'] == '02')
        with self.assertRaises(ValueError):
            collect(bundle, future['block'], registration)
        from collector import exclusive
        with exclusive(bundle), self.assertRaises(ValueError):
            collect(bundle, self.block(bundle), registration)
        reg = read_json(registration)
        reg['campaign_sha256'] = '0' * 64
        write_json(registration, reg)
        with self.assertRaises(ValueError):
            collect(bundle, self.block(bundle), registration)

    def test_cold_timer_and_wrapper_duration_stay_separate(self):
        m = self.make_manifest(phase='adoption-cold')
        for arms in m['commands'].values():
            for specs in arms.values():
                specs['run']['timeout_seconds'] = 1800
        directory = self.root / 'cold'
        directory.mkdir()
        (directory / 'timer.log').write_text('constructed nested monotonic timer')
        evidence = {'timer.log': digest(directory / 'timer.log')}
        receipt = {'checks': {key: True for key in QUALITY},
                   'facts': dict(m['expected_facts']['example-fixed-replay']['baseline'], host='test'),
                   'metrics': {key: 1 for key in E1_RESOURCES},
                   'environment': {'host': 'test'}, 'resource_method': 'constructed', 'evidence': evidence}
        receipt['metrics'].update(gate_seconds=3, adoption_seconds=12)
        row = {'workload': 'example-fixed-replay', 'arm': 'baseline'}
        with self.assertRaises(ValueError):
            receipt_valid(m, row, receipt, directory)
        receipt['gate_timer'] = {'source': 'monotonic', 'boundary': 'cli_spawn_to_exit',
                                 'seconds': 3, 'evidence': 'timer.log'}
        receipt_valid(m, row, receipt, directory)
        self.assertNotEqual(receipt['metrics']['gate_seconds'], receipt['metrics']['adoption_seconds'])


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.m = {'seed': SEED, 'experiment': 'E2', 'phase': 'confirmation'}

    def test_geometric_ratio_bootstrap_quantiles_and_dates(self):
        pairs = [('01', '001', 10, 5), ('01', '002', 100, 50),
                 ('02', '001', 20, 10), ('02', '002', 200, 100)]
        value = estimate(self.m, 'external-acceptance', 'validation_seconds', pairs, ['01', '02'])
        self.assertAlmostEqual(value['ratio'], .5)
        self.assertEqual(value['difference'], -41.25)
        self.assertAlmostEqual(value['interval']['ratio'][0], .5)
        self.assertAlmostEqual(value['interval']['ratio'][1], .5)
        self.assertEqual(value, estimate(self.m, 'external-acceptance', 'validation_seconds',
                                        list(reversed(pairs)), ['01', '02']))
        self.assertEqual(quantile([0, 10, 20, 30], .25), 7.5)

    def test_bootstrap_matches_independent_reference(self):
        pairs = [('01', '001', 10, 7), ('01', '002', 20, 18),
                 ('02', '001', 30, 20), ('02', '002', 40, 50)]
        result = estimate(self.m, 'external-acceptance', 'validation_seconds', pairs, ['01', '02'])
        import hashlib
        ratios, differences = [], []
        for r in range(10000):
            sampled = []
            for date in ('01', '02'):
                ps = [p for p in pairs if p[0] == date]
                for j in range(2):
                    key = f'{SEED}|E2|confirmation|external-acceptance|validation_seconds|{date}|{r:05}|{j:05}|bootstrap'
                    index = int.from_bytes(hashlib.sha256(key.encode()).digest(), 'big') * 2 // (2 ** 256)
                    sampled.append(ps[index])
            ratios.append(math.exp(sum(math.log(w / b) for _, _, b, w in sampled) / 4))
            differences.append(sum(w - b for _, _, b, w in sampled) / 4)
        for endpoint_index, probability in enumerate((.025, .975)):
            self.assertAlmostEqual(result['interval']['ratio'][endpoint_index], quantile(ratios, probability))
            self.assertEqual(result['interval']['difference'][endpoint_index], quantile(differences, probability))

    def test_missing_date_and_zero_counter_are_not_imputed(self):
        pairs = [('01', '001', 10, 5), ('01', '002', 10, 5)]
        self.assertIsNone(estimate(self.m, 'w', 'm', pairs, ['01', '02'])['interval'])
        pairs = [('01', '001', 0, 0), ('01', '002', 10, 5)]
        result = estimate(self.m, 'w', 'm', pairs, ['01'])
        self.assertIsNone(result['ratio'])
        self.assertEqual(result['difference'], -2.5)
        self.assertEqual(result['n'], 2)


class CITests(unittest.TestCase):
    def data(self):
        run = {'id': 1, 'status': 'completed', 'run_attempt': 1, 'head_sha': 'a' * 40,
               'created_at': '2026-10-13T00:00:00Z', 'updated_at': '2026-10-13T00:00:35Z'}
        def job(i, start, end, name, conclusion='success'):
            return {'id': i, 'run_id': 1, 'run_attempt': 1, 'head_sha': 'a' * 40,
                    'name': name, 'conclusion': conclusion, 'runner_id': i if conclusion != 'skipped' else 0,
                    'started_at': f'2026-10-13T00:00:{start:02}Z',
                    'completed_at': f'2026-10-13T00:00:{end:02}Z',
                    'steps': [{'name': 'check', 'conclusion': conclusion,
                               'completed_at': f'2026-10-13T00:00:{end:02}Z'}]}
        jobs = [job(1, 5, 15, 'one'), job(2, 5, 20, 'two'), job(3, 20, 30, 'three'),
                job(4, 5, 35, 'other', 'failure'), job(5, 0, 0, 'skip', 'skipped')]
        data = {'repository': 'owner/repo', 'run': run, 'pages': [{'total_count': 5, 'jobs': jobs}]}
        inventory = {'repository': 'owner/repo', 'execution_sha': 'a' * 40,
                     'tasks': {task: {'job': 'three', 'step': 'check'} for task in TASKS}}
        return data, inventory

    def test_half_open_intervals_include_failed_and_matrix_cost(self):
        data, inventory = self.data()
        metrics = endpoint(data, inventory)
        self.assertEqual(metrics['validation_seconds'], 25)
        self.assertEqual(metrics['queue_inclusive_seconds'], 30)
        self.assertEqual(metrics['initial_queue_seconds'], 5)
        self.assertEqual(metrics['runner_minutes'], 65 / 60)
        self.assertEqual(metrics['peak_runner_concurrency'], 3)

    def test_started_jobs_require_runner_identity(self):
        for index in (2, 3):  # Required successful job and unrelated failed job.
            for runner in (None, 0, -1, True, '3'):
                data, inventory = self.data()
                data['pages'][0]['jobs'][index]['runner_id'] = runner
                with self.subTest(index=index, runner=runner), self.assertRaises(ValueError):
                    endpoint(data, inventory)
            data, inventory = self.data()
            data['pages'][0]['jobs'][index].pop('runner_id')
            with self.subTest(index=index, missing=True), self.assertRaises(ValueError):
                endpoint(data, inventory)
        data, inventory = self.data()
        data['pages'][0]['jobs'][2]['started_at'] = None
        with self.assertRaises(ValueError):
            endpoint(data, inventory)

    def test_pages_attempt_sha_skipped_upload_and_missing_end_rejected(self):
        for failure in ('page', 'duplicate', 'attempt', 'sha', 'skipped', 'upload', 'end', 'task', 'running'):
            data, inventory = self.data()
            jobs = data['pages'][0]['jobs']
            if failure == 'page':
                data['pages'][0]['total_count'] += 1
            elif failure == 'duplicate':
                jobs[1]['id'] = jobs[0]['id']
            elif failure == 'attempt':
                data['run']['run_attempt'] = 2
            elif failure == 'sha':
                jobs[0]['head_sha'] = 'b' * 40
            elif failure == 'skipped':
                jobs[2]['conclusion'] = 'skipped'
            elif failure == 'upload':
                jobs[2]['steps'][0]['conclusion'] = 'failure'
            elif failure == 'end':
                jobs[3]['completed_at'] = None
            elif failure == 'running':
                data['run']['status'] = 'in_progress'
            else:
                inventory['tasks'].pop('death')
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                endpoint(data, inventory)

    def test_diagnostic_rerun_not_primary(self):
        with self.assertRaises(ValueError):
            snapshot('owner/repo', 1, 2, Path('/unused'))

    def test_outer_cap_includes_workflow_and_nonrequired_job_completion(self):
        for late in ('workflow', 'job'):
            data, inventory = self.data()
            data['run']['updated_at'] = '2026-10-13T01:30:00Z'
            data['pages'][0]['jobs'][3]['completed_at'] = '2026-10-13T01:30:00Z'
            metrics = endpoint(data, inventory)
            self.assertEqual(metrics['validation_seconds'], 25)
            self.assertEqual(metrics['queue_inclusive_seconds'], 30)
            if late == 'workflow':
                data['run']['updated_at'] = '2026-10-13T01:30:01Z'
            else:
                data['pages'][0]['jobs'][3]['completed_at'] = '2026-10-13T01:30:01Z'
            with self.subTest(late=late), self.assertRaisesRegex(ValueError, 'capped'):
                endpoint(data, inventory)

    def test_snapshot_provider_errors_use_controlled_cli_diagnostic(self):
        from tool import main
        for failing_call in (0, 1):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'ci.json'
                error = subprocess.CalledProcessError(1, ['gh', 'api'], stderr='provider error')
                responses = [subprocess.CompletedProcess(['gh'], 0, stdout='{}')] * failing_call + [error]
                stderr = io.StringIO()
                argv = ['tool.py', 'ci-snapshot', '--repository', 'owner/repo',
                        '--run-id', '1', '--output', str(output)]
                with self.subTest(failing_call=failing_call), patch.object(sys, 'argv', argv), \
                        patch('ci.subprocess.run', side_effect=responses), redirect_stderr(stderr):
                    self.assertEqual(main(), 1)
                    self.assertIn('campaign tooling: gh api failed', stderr.getvalue())
                    self.assertNotIn('Traceback', stderr.getvalue())
                    self.assertFalse(output.exists())


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print('CAMPAIGN_HARNESS_RESULT order=registered accounting=all_slots cleanup=quarantine analysis=deterministic ci=attempt_scoped')
    sys.exit(0 if result.wasSuccessful() else 1)
