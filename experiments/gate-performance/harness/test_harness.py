#!/usr/bin/env python3
"""Constructed evidence tests; these are not E1/E2 measurements."""
import copy
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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

    def test_unproven_cleanup_blocks_mate(self):
        m = copy.deepcopy(self.manifest)
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
        spec['argv'] = [sys.executable, '-c', 'import threading; threading.Event().wait()']
        result = execute(spec, context, self.root, 'timeout', [])
        self.assertTrue(result['timeout'])
        self.assertLess(result['exit'], 0)

    def test_missing_confirmation_cannot_claim_improvement(self):
        bundle, _ = self.frozen(self.make_manifest(phase='confirmation'))
        result = analyze(bundle, self.root / 'incomplete')
        self.assertEqual(result['decision']['status'], 'no_positive_claim')
        self.assertIsNone(result['estimates']['example-fixed-replay']['gate_seconds']['ratio'])


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
            state['missing'] = True
            result = analyze(bundle, self.root / 'missing-secondary')
            self.assertEqual(result['decision']['status'], 'no_positive_claim')
            self.assertIn('incomplete_or_invalid_planned_pairs', result['decision']['reasons'])
            state.update(missing=False, cost=1.2)
            result = analyze(bundle, self.root / 'higher-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(set(result['decision']['higher_resource_cost']),
                             {'cpu_seconds', 'peak_memory_bytes', 'disk_write_bytes'})
            state['cost'] = None
            result = analyze(bundle, self.root / 'undefined-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(set(result['decision']['undefined_resource_ratios']),
                             {'cpu_seconds', 'peak_memory_bytes', 'disk_write_bytes'})

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
               'created_at': '2026-10-13T00:00:00Z'}
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


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print('CAMPAIGN_HARNESS_RESULT order=registered accounting=all_slots cleanup=quarantine analysis=deterministic ci=attempt_scoped')
    sys.exit(0 if result.wasSuccessful() else 1)
