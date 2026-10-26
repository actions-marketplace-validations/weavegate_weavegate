#!/usr/bin/env python3
"""Constructed boundary tests; these are not E5 performance observations."""
import copy
import fcntl
import json
import importlib.util
import py_compile
import runpy
import shutil
import signal
import socket
import subprocess
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support', 'analyze', 'tool')})
from support import *
from analyze import analyze, baseline, bound, membership, product, reduce_slot, stages, stats, validate_trace
from tool import collect, freeze, registered, validate, proof, hook_snapshot, unchanged


UNSUPPORTED = {'issue_url': 'https://example.com/issues/1', 'reason': 'Constructed unsupported arm'}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.protocol = HERE.parent / 'README.md'
        self.fixture_reference = {}
        self.reference_patch = patch('support.FIXTURE_INPUTS', self.fixture_reference)
        self.reference_patch.start()
        self.addCleanup(self.reference_patch.stop)
        self.library_reference = {'libraries': {}}
        self.library_patch = patch('support.REFERENCE_BUILD', self.library_reference, create=True)
        self.library_patch.start()
        self.addCleanup(self.library_patch.stop)

    def tearDown(self): self.temp.cleanup()

    def manifest(self):
        program = self.root / 'arm.py'
        program.write_text('raise SystemExit(4)\n')
        executable = str(Path(sys.executable).resolve())
        inputs, commands = {}, {}
        for w, spec in WORKLOADS.items():
            inputs[w], commands[w] = {}, {}
            for v in ('vulnerable', 'fixed'):
                inputs[w][v] = {'expected_domain_rows': [{'synthetic': '1'}], 'candidates': candidates(w), 'counts': {'B1': 2, 'B2': 2, 'T-R': 2},
                    'config': {'Run': {'Repeat': 20, 'ArriveTimeoutMS': 1000, 'ExplorePasses': 3},
                               'Target': {'DB': 'mysql:8.4@sha256:'+'a'*64, 'SUT': {'External': {'Capacity': 2, 'StartupTimeoutMS': 30000,
                                                              'CancelTimeoutMS': 5000, 'StopTimeoutMS': 20000}}},
                               'Oracle': {'Assertions': [{'ID': spec[2]}]}}}
                commands[w][v] = {a: {'argv': [executable, str(program)], 'cwd': str(self.root), 'env': {},
                                      'timeout_seconds': CAPS[a]} for a in ARMS}
        return {'schema': 1, 'protocol_revision': 1, 'campaign_id': 'constructed', 'phase': 'feasibility',
                'seed': SEED, 'inputs': inputs, 'commands': commands, 'hooks': {},
                'pins': {p: digest(p) for p in (executable, str(program), str(self.protocol))},
                'protocol': {'path': str(self.protocol), 'sha256': digest(self.protocol)}}

    def bundle(self):
        m = self.manifest()
        write_json(self.root / 'input.json', m)
        freeze(self.root / 'input.json', self.root / 'bundle')
        return self.root / 'bundle', m

    def raw(self, slot=None):
        slot = slot or dict(order()[0], workload='quota', variant='vulnerable', arm='B2')
        root = self.root / 'slot'; root.mkdir(); raw = root / 'raw'; raw.mkdir()
        run = {'arm': slot['arm'], 'variant': slot['variant'],
               'environment': {'image': 'mysql:8.4@sha256:'+'a'*64},
               'cleanup_proven': True, 'executions': 2, 'seed': {'synthetic': []}, 'process_elapsed_ns': 900000000,
               'timings': {'spans': [{'id': 1, 'parent_id': 0, 'name': 'command', 'start_ns': 0, 'duration_ns': 900000000}]}}
        write_json(raw / 'run.json', run)
        for i in (1, 2):
            write_json(raw / ('execution-%03d.json' % i), {'index': i, 'seed': run['seed'],
                'child_reaped': True, 'connections_closed': True, 'reset_verified': True, 'oracle_end_ns': i * 100000000,
                'oracle_ends_ns': {WORKLOADS[slot['workload']][2]: i * 100000000},
                'evaluation': {'fingerprint': 'a'*64, 'results': [{'oracle_id': WORKLOADS[slot['workload']][2],
                    'violations': [{'oracle_id': WORKLOADS[slot['workload']][2], 'rows': [{'synthetic': 1}]}]}]}})
        cmd = {'seconds': 1, 'started': True, 'timeout': False, 'exit': 0, 'error': None,
               'containment_proven': True, 'finished': True, 'interrupted': False}
        write_json(root / 'run.command.json', cmd)
        write_json(root / 'slot.json', {'cleanup_proven': True, 'slot': slot})
        seal(root)
        return root, slot, cmd

    def test_full_order_has_five_invocations_per_block(self):
        slots = order()
        self.assertEqual(len(slots), 600)
        self.assertEqual(len({s['id'] for s in slots}), 600)
        for w in WORKLOADS:
            for v in ('vulnerable', 'fixed'):
                for c in ('cold', 'warm'):
                    for a in ARMS:
                        self.assertEqual(sum((s['workload'],s['variant'],s['cache'],s['arm']) == (w,v,c,a) for s in slots), 10)
        for block in {tuple(s[k] for k in ('workload','variant','cache','block')) for s in slots}:
            pair = [s['arm'] for s in slots if tuple(s[k] for k in ('workload','variant','cache','block')) == block and s['arm'].startswith('T')]
            self.assertEqual(pair, ['T-R', 'T-E'] if int(block[-1]) % 2 else ['T-E', 'T-R'])

    def test_order_is_frozen_vector(self):
        self.assertEqual(order()[0]['id'], 'matching-fixed-cold-09-B2')
        # Independent vector generated at protocol implementation, not a random seed call.
        self.assertEqual([s['arm'] for s in order()[:5]], ['B2', 'T-R', 'T-E', 'B1', 'B0'])

    def test_candidate_counts_ids_and_preserved_points(self):
        for w, spec in WORKLOADS.items():
            values = candidates(w)
            self.assertEqual(len(values), spec[5])
            self.assertEqual(len({v['id'] for v in values}), spec[5])
            self.assertIn(spec[6], [v['id'] for v in values])

    def test_invalid_candidates_rejected(self):
        m = self.manifest(); m['inputs']['quota']['fixed']['candidates'].reverse()
        with self.assertRaisesRegex(ValueError, 'candidate'): validate(m)

    def test_campaign_id_rejects_report_markup_before_freeze(self):
        m = self.manifest()
        for value in ('campaign\n\n## Complete results', 'campaign\rreplacement',
                      '<b>campaign</b>', '[campaign](https://example.com)',
                      'campaign\x00', 'two words', '', None, 7):
            with self.subTest(value=value):
                if (self.root/'invalid-campaign').exists(): shutil.rmtree(self.root/'invalid-campaign')
                m['campaign_id'] = value
                write_json(self.root/'input.json', m)
                with self.assertRaisesRegex(ValueError, 'campaign ID'):
                    freeze(self.root/'input.json', self.root/'invalid-campaign')
                self.assertFalse((self.root/'invalid-campaign').exists())
        m['campaign_id'] = 'E5-2026.10_23'
        write_json(self.root/'input.json', m)
        freeze(self.root/'input.json', self.root/'valid-campaign')
        analyze(self.root/'valid-campaign', self.root/'valid-analysis')
        self.assertIn('Campaign: E5-2026.10_23\n', (self.root/'valid-analysis/report.md').read_text())

    def test_offline_analysis_rejects_campaign_markup(self):
        bundle, manifest = self.bundle()
        manifest = read_json(bundle/'frozen/manifest.json')
        manifest['campaign_id'] = 'campaign\n\n## Complete results'
        write_json(bundle/'frozen/manifest.json', manifest); seal(bundle/'frozen')
        with self.assertRaisesRegex(ValueError, 'campaign ID'):
            analyze(bundle, self.root/'analysis')
        self.assertFalse((self.root/'analysis').exists())

    def test_measurement_cannot_use_short_counts(self):
        m = self.manifest(); m['phase'] = 'measurement'
        with self.assertRaisesRegex(ValueError, 'counts'): validate(m)

    def test_changed_pin_rejected_before_freeze(self):
        m = self.manifest(); (self.root/'arm.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'pin changed'): validate(m)

    def test_command_deadlines_are_finite_positive_numbers(self):
        command = self.manifest()['commands']['matching']['fixed']['B0']
        command_valid(command)  # Omitted grace retains the executor's default.
        for field in ('timeout_seconds', 'cancel_grace_seconds'):
            for value in (None, True, False, '25', 0, -1, float('nan'),
                          float('inf'), -float('inf'), 10**400, [], {}):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        command_valid(dict(command, **{field: value}))
            for value in (.01, 25, 55.5):
                command_valid(dict(command, **{field: value}))

    def test_invalid_grace_cannot_freeze_commands_or_hooks(self):
        m = self.measurement_manifest()
        targets = [m['commands']['matching']['fixed'][a] for a in ARMS] + list(m['hooks'].values())
        for index, command in enumerate(targets):
            with self.subTest(command=index):
                command['cancel_grace_seconds'] = '25'
                write_json(self.root/'invalid-grace.json', m)
                with self.assertRaisesRegex(ValueError, 'cancel_grace_seconds'):
                    freeze(self.root/'invalid-grace.json', self.root/'invalid-bundle')
                self.assertFalse((self.root/'invalid-bundle').exists())
                del command['cancel_grace_seconds']

    def test_measurement_output_is_one_active_slot_destination(self):
        m = self.measurement_manifest()
        for arm in ARMS:
            argv = m['commands']['matching']['fixed'][arm]['argv']
            index = argv.index('--out')
            prefix, suffix = argv[:index], argv[index+2:]
            for output in ([], ['--out'], ['--out', ''],
                           ['--out', '{campaign_dir}/frozen/injected'],
                           ['--out', '{campaign_dir}/slots/future/raw'],
                           ['--out', '{trial_dir}/../other/raw'],
                           ['--out', str(self.root/'outside')],
                           ['--out', '{trial_dir}/raw', '--out={trial_dir}/raw'],
                           ['--out={trial_dir}/raw', '-out={campaign_dir}/frozen/injected'],
                           ['--', '--out', '{trial_dir}/raw']):
                with self.subTest(arm=arm, output=output):
                    changed = copy.deepcopy(m)
                    changed['commands']['matching']['fixed'][arm]['argv'] = prefix + output + suffix
                    with self.assertRaisesRegex(ValueError, 'output'):
                        validate(changed)
            for output in (['--out', '{trial_dir}/raw'], ['--out={trial_dir}/raw'],
                           ['-out', '{trial_dir}/raw'], ['-out={trial_dir}/raw']):
                with self.subTest(arm=arm, accepted=output):
                    changed = copy.deepcopy(m)
                    changed['commands']['matching']['fixed'][arm]['argv'] = prefix + output + suffix
                    validate(changed)

    def test_measurement_t_commands_require_candidate_executable(self):
        m = self.measurement_manifest()
        validate(m)
        other = self.root / 'other-cli'
        other.write_text('different pinned executable\n')
        m['pins'][str(other)] = digest(other)
        for workload in WORKLOADS:
            for variant in ('vulnerable', 'fixed'):
                for arm in ('T-R', 'T-E'):
                    with self.subTest(workload=workload, variant=variant, arm=arm):
                        changed = copy.deepcopy(m)
                        changed['commands'][workload][variant][arm]['argv'][0] = str(other)
                        with self.assertRaisesRegex(ValueError, 'registered CLI'):
                            validate(changed)

    def test_cleanup_script_must_be_in_trusted_subset(self):
        m = self.measurement_manifest()
        hook = m['hooks']['cleanup']
        script = hook['argv'][1]
        for globally_pinned in (False, True):
            with self.subTest(globally_pinned=globally_pinned):
                changed = copy.deepcopy(m)
                if not globally_pinned: del changed['pins'][script]
                changed['hooks']['cleanup']['execution_pins'] = [hook['argv'][0]]
                with self.assertRaisesRegex(ValueError, 'cleanup.*dependenc'):
                    validate(changed)

    def test_cleanup_file_arguments_must_be_in_trusted_subset(self):
        m = self.measurement_manifest()
        dependency = self.root / 'cleanup-config.json'
        dependency.write_text('{}')
        m['pins'][str(dependency)] = digest(dependency)
        for argument in (str(dependency), dependency.name, '--config=' + str(dependency)):
            with self.subTest(argument=argument):
                changed = copy.deepcopy(m)
                changed['hooks']['cleanup']['argv'].append(argument)
                with self.assertRaisesRegex(ValueError, 'cleanup.*dependenc'):
                    validate(changed)
                changed['hooks']['cleanup']['execution_pins'].append(str(dependency))
                validate(changed)

    def test_frozen_inputs_tamper_rejected_offline(self):
        bundle, _ = self.bundle()
        (bundle/'frozen/order.json').write_text('[]')
        with self.assertRaises(ValueError): analyze(bundle, self.root/'analysis')

    def test_cleanup_environment_file_must_be_in_trusted_subset(self):
        m = self.measurement_manifest()
        dependency = self.root / 'cleanup-config.json'
        dependency.write_text('{}')
        m['pins'][str(dependency)] = digest(dependency)
        m['hooks']['cleanup']['env']['CLEANUP_CONFIG'] = str(dependency)
        with self.assertRaisesRegex(ValueError, 'cleanup.*dependenc'): validate(m)
        m['hooks']['cleanup']['execution_pins'].append(str(dependency))
        validate(m)

    def test_changed_cleanup_dependency_is_never_executed(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        calls = []
        def drift(spec, context, directory, label, secrets):
            calls.append(label)
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'prepare':
                (self.root/'hook.py').write_text('raise SystemExit(0)\n')
            return result
        with patch('tool.execute', side_effect=drift): collect(bundle, registration)
        self.assertEqual(calls, ['prepare'])
        state = read_json(bundle/'slots'/order()[0]['id']/'slot.json')
        self.assertFalse(state['cleanup_proven'])
        self.assertIn('live pin changed', state['cleanup_error'])

    def test_failed_process_quarantines_and_preserves_all_slots(self):
        bundle, _ = self.bundle()
        collect(bundle, max_slots=2)
        self.assertTrue((bundle/'stopped.json').exists())
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(result['slots'][0]['status'], 'operational_failed')
        self.assertEqual(sum(s['status'] == 'not_started' for s in result['slots']), 599)
        with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, max_slots=1)

    def test_deleted_stop_marker_cannot_resume_unproven_cleanup(self):
        bundle, _ = self.bundle()
        collect(bundle, max_slots=1)
        before = analyze(bundle, self.root/'before')
        (bundle/'stopped.json').unlink()
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'stopped|cleanup unproven'):
                collect(bundle, max_slots=1)
            execute.assert_not_called()
        after = analyze(bundle, self.root/'after')
        self.assertEqual(before, after)

    def test_deleted_stop_marker_cannot_resume_interruption_after_cleanup(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        def interrupt_observer(spec, context, directory, label, secrets):
            if label == 'observe': raise KeyboardInterrupt('constructed interruption')
            return real_execute(spec, context, directory, label, secrets)
        with patch('tool.execute', side_effect=interrupt_observer):
            collect(bundle, registration)
        self.assertTrue(read_json(bundle/'slots'/order()[0]['id']/'slot.json')['cleanup_proven'])
        (bundle/'stopped.json').unlink()
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'stopped'):
                collect(bundle, registration)
            execute.assert_not_called()

    def test_offline_analysis_rejects_missing_consumed_slot(self):
        bundle, registration = self.measurement_bundle()
        collect(bundle, registration)
        for index in (0, 1):
            with self.subTest(index=index):
                slot = bundle/'slots'/order()[index]['id']
                retained = self.root/'retained-slot'
                slot.rename(retained)
                with self.assertRaisesRegex(ValueError, 'prefix|checkpoint'):
                    analyze(bundle, self.root/'analysis')
                retained.rename(slot)

    def test_offline_analysis_rejects_mismatched_stop_marker(self):
        bundle, _ = self.bundle()
        collect(bundle, max_slots=1)
        stopped = read_json(bundle/'stopped.json')
        stopped['slot'] = order()[1]['id']
        write_json(bundle/'stopped.json', stopped)
        with self.assertRaisesRegex(ValueError, 'stop.*checkpoint'):
            analyze(bundle, self.root/'analysis')

    def test_partial_checkpoint_continues_only_the_next_slot(self):
        bundle, _ = self.bundle()
        def clean_evidence(*args): return {'cleanup_proven': True}
        with patch('analyze.baseline', side_effect=clean_evidence), patch('analyze.product', side_effect=clean_evidence):
            collect(bundle, max_slots=1)
            collect(bundle, max_slots=1)
        self.assertEqual(set(p.name for p in (bundle/'slots').iterdir()), {s['id'] for s in order()[:2]})
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(sum(s['status'] == 'not_started' for s in result['slots']), 598)

    def test_missing_or_corrupt_progress_cannot_be_recreated(self):
        bundle, _ = self.bundle()
        path = bundle/'progress/checkpoint.json'
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'): analyze(bundle, self.root/'analysis')
        path.unlink()
        with self.assertRaises((ValueError, OSError)): collect(bundle, max_slots=1)

    def test_removed_active_slot_is_never_retried(self):
        bundle, _ = self.bundle()
        save_progress(bundle, {}, active=order()[0]['id'])
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'unfinished checkpoint'):
                collect(bundle, max_slots=1)
            execute.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'unfinished checkpoint'):
            analyze(bundle, self.root/'analysis')

    def test_complete_checkpoint_requires_every_consumed_slot(self):
        m = self.manifest()
        m['unsupported'] = {w + '/' + a: UNSUPPORTED for w in WORKLOADS for a in ARMS}
        write_json(self.root/'input.json', m)
        bundle = self.root/'bundle'
        freeze(self.root/'input.json', bundle)
        consumed = {}
        for slot in order():
            directory = bundle/'slots'/slot['id']; directory.mkdir()
            write_json(directory/'slot.json', {'slot': slot, 'unsupported': UNSUPPORTED})
            seal(directory)
            consumed[slot['id']] = digest(directory/'SHA256SUMS')
        save_progress(bundle, consumed)
        self.assertTrue(retained_progress(bundle, m, order())['complete'])
        analyze(bundle, self.root/'complete')
        shutil.rmtree(bundle/'slots'/order()[-1]['id'])
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            analyze(bundle, self.root/'analysis')

    def test_offline_regeneration_is_byte_identical(self):
        bundle, _ = self.bundle()
        analyze(bundle, self.root/'first')
        analyze(bundle, self.root/'second')
        for name in ('results.json','report.md'):
            self.assertEqual((self.root/'first'/name).read_bytes(), (self.root/'second'/name).read_bytes())

    def test_unsealed_crash_is_not_retried(self):
        bundle, _ = self.bundle()
        (bundle/'slots'/order()[0]['id']).mkdir()
        with self.assertRaises(OSError): collect(bundle, max_slots=1)

    def test_detection_does_not_erase_later_evaluations(self):
        root, slot, cmd = self.raw()
        r = baseline(root, slot, cmd, self.manifest()['inputs']['quota']['vulnerable'])
        self.assertEqual((r['attempted'],r['valid_evaluations'],r['domain_violations']), (2,2,2))
        self.assertEqual(r['x_over_m'], 1)
        self.assertAlmostEqual(r['first_event_seconds'][1], .2)

    def test_timeout_retains_valid_partial_stress_exposure(self):
        root, slot, cmd = self.raw()
        (root/'raw/execution-002.json').unlink()
        run = read_json(root/'raw/run.json'); run['executions'] = 1
        write_json(root/'raw/run.json',run)
        cmd['timeout'] = True
        cmd['exit'] = 1
        run.update(error='context canceled', cancellation_only=True)
        write_json(root/'raw/run.json',run)
        manifest = self.manifest()
        r = baseline(root, slot, cmd, manifest['inputs']['quota']['vulnerable'])
        self.assertEqual((r['valid_evaluations'],r['unobserved'],r['x_over_planned']), (1,1,.5))
        self.assertFalse(r['operational_reasons'])
        write_json(root/'run.command.json',cmd); seal(root)
        self.assertEqual(reduce_slot(root,slot,manifest)['status'], 'budget_exhausted')

    def test_cleanup_failure_does_not_hide_first_detection(self):
        root, slot, _ = self.raw()
        run=read_json(root/'raw/run.json');run['cleanup_proven']=False;write_json(root/'raw/run.json',run);seal(root)
        r=reduce_slot(root,slot,self.manifest())
        self.assertEqual(r['status'],'operational_failed')
        self.assertIsNotNone(r['first_event_seconds'])

    def test_missing_evaluation_is_never_pass(self):
        root, slot, cmd = self.raw()
        p=root/'raw/execution-001.json';v=read_json(p);v['evaluation']=None;write_json(p,v)
        r=baseline(root,slot,cmd,self.manifest()['inputs']['quota']['vulnerable'])
        self.assertEqual(r['valid_evaluations'],1)
        self.assertTrue(r['operational_reasons'])

    def test_receipt_assertion_is_not_domain_detection(self):
        root, slot, cmd = self.raw()
        m=self.manifest();expected=m['inputs']['quota']['vulnerable']
        expected['config']['Oracle']['Assertions'].append({'ID':'receipt'})
        for p in (root/'raw').glob('execution-*.json'):
            v=read_json(p);v['evaluation']['results'][0]['violations']=[]
            v['evaluation']['results'].append({'oracle_id':'receipt','violations':[{'oracle_id':'receipt','rows':[{}]}]})
            v['oracle_ends_ns']['receipt'] = v['oracle_end_ns']
            write_json(p,v)
        r=baseline(root,slot,cmd,expected)
        self.assertEqual(r['domain_violations'],0)
        self.assertTrue(r['quality_reasons'])

    def test_fixed_violation_is_quality_failure(self):
        root, slot, _=self.raw(dict(order()[0], workload='quota', variant='fixed', arm='B2'))
        r=reduce_slot(root,slot,self.manifest())
        self.assertEqual(r['status'],'quality_failed')

    def test_baseline_domain_rows_must_match_frozen_expectation(self):
        root, slot, cmd = self.raw()
        m = self.manifest()
        expected = m['inputs']['quota']['vulnerable']
        expected['expected_domain_rows'] = [{'synthetic': '1'}]
        self.assertFalse(baseline(root, slot, cmd, expected)['quality_reasons'])
        expected['expected_domain_rows'] = [{'synthetic': '2'}]
        for arm in ('B1', 'B2'):
            with self.subTest(arm=arm):
                current = dict(slot, arm=arm)
                run = read_json(root/'raw/run.json'); run['arm'] = arm
                write_json(root/'raw/run.json', run)
                write_json(root/'slot.json', {'cleanup_proven': True, 'slot': current})
                seal(root)
                result = reduce_slot(root, current, m)
                self.assertEqual(result['status'], 'quality_failed')
                self.assertIn('domain assertion rows differ', result['reasons'])
                self.assertEqual(result['attempted'], 2)

    def test_corrupt_raw_cannot_be_reclassified_by_cached_status(self):
        root, slot, _=self.raw()
        (root/'raw/run.json').write_text('{}')
        with self.assertRaises(ValueError):reduce_slot(root,slot,self.manifest())

    def test_event_bounds_preserve_unknown_budget_membership(self):
        self.assertEqual(bound(5,8,10),[5,7])
        self.assertEqual(membership([5,7],6),'budget_membership_unknown')
        self.assertEqual(membership([7,8],6),'late')
        self.assertIsNone(bound(5,11,10))

    def test_quantiles_and_missing_values(self):
        self.assertEqual(stats([])['median'],None)
        self.assertEqual(stats([7])['p90'],7)
        self.assertAlmostEqual(stats(list(range(1,11)))['p90'],9.1)

    def test_nested_stage_coverage_uses_union(self):
        spans=[{'id':1,'parent_id':0,'name':'command','start_ns':0,'duration_ns':10_000_000_000},
               {'id':2,'parent_id':1,'name':'adapter_start','start_ns':1_000_000_000,'duration_ns':5_000_000_000},
               {'id':3,'parent_id':2,'name':'process_start_to_ready','start_ns':2_000_000_000,'duration_ns':3_000_000_000}]
        r=stages(spans,12)
        self.assertEqual(r['exclusive_covered_seconds'],5)
        self.assertEqual(r['remainder_seconds'],7)
        spans[2]['duration_ns']=6_000_000_000
        with self.assertRaises(ValueError):stages(spans,12)

    def test_missing_final_receipt_preserves_published_evaluations(self):
        root, slot, cmd = self.raw()
        (root/'raw/run.json').unlink()
        r = baseline(root,slot,cmd,self.manifest()['inputs']['quota']['vulnerable'])
        self.assertEqual(r['valid_evaluations'],2)
        self.assertEqual(r['domain_violations'],2)
        self.assertTrue(r['operational_reasons'])
        self.assertIsNone(r['first_event_seconds'])

    def test_cap_killed_baselines_preserve_exposure_as_budget_exhausted(self):
        manifest = self.manifest()
        for arm in ('B0', 'B1', 'B2'):
            with self.subTest(arm=arm):
                if (self.root/'slot').exists(): shutil.rmtree(self.root/'slot')
                slot = dict(order()[0], workload='quota', variant='vulnerable', arm=arm)
                root, slot, command = self.raw(slot)
                (root/'raw/run.json').unlink()
                if arm == 'B0':
                    for path in (root/'raw').glob('execution-*.json'): path.unlink()
                command.update(timeout=True, exit=-signal.SIGKILL, seconds=slot['cap_seconds']+1)
                write_json(root/'run.command.json', command); seal(root)
                result = reduce_slot(root, slot, manifest)
                self.assertEqual(result['status'], 'budget_exhausted', result['reasons'])
                self.assertEqual(result['evidence_absence'], 'collection_cap_before_publication')
                self.assertFalse(result['cleanup_proven'])
                self.assertTrue(result['allocation_cleanup_proven'])
                self.assertIsNone(result['first_event_seconds'])
                self.assertIsNone(result['usable_evidence_seconds'])
                self.assertEqual(result['valid_evaluations'], 0 if arm == 'B0' else 2)
                self.assertEqual(result['domain_violations'], 0 if arm == 'B0' else 2)
                for code in (-signal.SIGTERM, 128 + signal.SIGTERM, 128 + signal.SIGKILL):
                    write_json(root/'run.command.json', dict(command, exit=code)); seal(root)
                    self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'budget_exhausted')
                write_json(root/'run.command.json', command)
                if arm != 'B0':
                    path = root/'raw/execution-002.json'; execution = read_json(path); path.unlink()
                    seal(root); result = reduce_slot(root, slot, manifest)
                    self.assertEqual(result['status'], 'budget_exhausted')
                    self.assertEqual((result['valid_evaluations'], result['unobserved']), (1, 1))
                    self.assertEqual(result['unobserved_slots'], [{'index':2, 'reason':'collection_cap'}])
                    write_json(path, execution)
                for changes in ({'timeout':False}, {'exit':1}, {'exit':0}, {'started':False},
                                {'finished':False}, {'interrupted':True},
                                {'error':'wait failed'}, {'containment_proven':False}):
                    with self.subTest(changes=changes):
                        write_json(root/'run.command.json', dict(command, **changes)); seal(root)
                        self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')
                write_json(root/'run.command.json', command)
                write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':False}); seal(root)
                self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')
                write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':True})
                (root/'raw/run.json').write_text('{partial'); seal(root)
                self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')
                (root/'raw/run.json').unlink()
                if arm != 'B0':
                    path = root/'raw/execution-002.json'
                    execution = read_json(path); execution['error'] = 'database failed'
                    write_json(path, execution); seal(root)
                    result = reduce_slot(root, slot, manifest)
                    self.assertEqual(result['status'], 'operational_failed')
                    self.assertEqual(result['valid_evaluations'], 1)
                shutil.rmtree(root)

    def test_matching_fixed_skip_requires_prior_terminal(self):
        saved={'id':'saved','steps':[{'worker':'w1','point':'before_insert_assignment'},
                                   {'worker':'w2','point':'before_insert_assignment'}]}
        slot={'workload':'matching','variant':'fixed'}
        trace={'schedule_ref':'saved','terminals':[{'worker':w,'state':'done','failure_class':'none'} for w in ('w1','w2')],
               'events':[{'seq':1,'kind':'point_released','worker':'w1','point':'before_insert_assignment','step':0},
                         {'seq':2,'kind':'worker_done','worker':'w2'},
                         {'seq':3,'kind':'step_terminal_skipped','worker':'w2','point':'before_insert_assignment','step':1}]}
        self.assertEqual(validate_trace(trace,saved,slot)['terminal_skips'],1)
        with self.assertRaises(ValueError):validate_trace(trace,saved,dict(slot,variant='vulnerable'))
        trace['events'][1]['worker']='w1'
        with self.assertRaises(ValueError):validate_trace(trace,saved,slot)


    def test_boolean_schema_is_not_integer_one(self):
        m = self.manifest(); m['schema'] = True
        with self.assertRaises(ValueError): validate(m)

    def test_proof_rejects_numeric_true_and_reused_cleanup(self):
        (self.root/'proof').mkdir()
        evidence=self.root/'proof/closed.json';write_json(evidence, {'observed':True})
        write_json(self.root/'cleanup.json', {'proven':1, 'evidence':{'proof/closed.json':digest(evidence)}})
        with self.assertRaises(ValueError): proof(self.root,'cleanup.json',{'proven':True})
        write_json(self.root/'cleanup.json', {'proven':True, 'evidence':{'proof/closed.json':digest(evidence)}})
        with self.assertRaises(ValueError): proof(self.root,'cleanup.json',{'proven':True}, previous=hook_snapshot(self.root))
        self.assertTrue(proof(self.root,'cleanup.json',{'proven':True},previous={})['proven'])

    def test_out_of_order_consumed_slot_is_rejected(self):
        bundle,m=self.bundle()
        wrong=bundle/'slots'/order()[2]['id'];wrong.mkdir()
        write_json(wrong/'slot.json', {'slot':order()[2],'unsupported':'constructed'})
        seal(wrong)
        with self.assertRaisesRegex(ValueError,'ordered prefix'):collect(bundle,max_slots=1)

    def product_raw(self, arm, explored_count=None, exhausted=False, saved_index=None, repeat=2, passes=None):
        """Construct a valid vulnerable replay/discovery, including stage/trace evidence."""
        root = self.root / arm; root.mkdir()
        directory = root/'raw/runs/constructed'; directory.mkdir(parents=True)
        slot = dict(order()[0], arm=arm, variant='vulnerable')
        expected = self.manifest()['inputs']['matching']['vulnerable']
        expected['counts']['T-R'] = repeat
        expected['jar_sha256'] = 'b'*64
        expected['config']['Oracle']['Assertions'][0].update(SQL='constructed', ExpectRows=0)
        saved = next(c for c in expected['candidates'] if c['id'] == WORKLOADS['matching'][6])
        if saved_index is not None: saved = expected['candidates'][saved_index]
        spans = [{'id': 1, 'parent_id': 0, 'name': 'command', 'start_ns': 0, 'duration_ns': 900000000}]
        def span(name, parent, **extra):
            spans.append(dict(id=len(spans)+1, parent_id=parent, name=name, start_ns=0, duration_ns=0, **extra))
            return len(spans)
        def execution(parent, schedule):
            child = span('schedule_run', parent, schedule_id=schedule['id'])
            for name in ('fixture_reset','adapter_create','adapter_start','schedule_execute','oracle_evaluate','adapter_stop'):
                span(name, child)
        explored = 0
        passes = (3 if exhausted else 1) if passes is None else passes
        if arm == 'T-E':
            for number in range(passes):
                parent = span('explore_pass', 1)
                prefix = (expected['candidates'] if number < passes - 1 else
                          expected['candidates'][:explored_count] if explored_count is not None
                          else expected['candidates'] if exhausted
                          else expected['candidates'][:expected['candidates'].index(saved)+1])
                for candidate in prefix:
                    execution(parent, candidate); explored += 1
        if not exhausted:
            parent = span('replay', 1)
            for _ in range(repeat): execution(parent, saved)
        manifest = {'variant': 'vulnerable', 'adapter': 'external', 'sut_sha256': 'sha256:'+'b'*64,
                    'image': expected['config']['Target']['DB'],
                    'isolation_level': 'REPEATABLE-READ', 'cleanup_failed': False, 'timings': {'spans': spans}}
        observation = {'mode': 'explore' if arm == 'T-E' else 'replay',
            'oracles': [{'id': WORKLOADS['matching'][2], 'sql': 'constructed', 'expect_rows': 0}],
            'violation_runs': repeat, 'fingerprints': {'a'*64: repeat}, 'schedules_explored': explored,
            'discovery_fingerprint': 'a'*64 if explored else None, 'flaky': False, 'repeat': repeat,
            'explore_passes': passes if arm == 'T-E' else 0,
            'assertion_violations': [{'oracle_id': WORKLOADS['matching'][2], 'rows': [{'synthetic': 1}]}]}
        if exhausted:
            observation.update(violation_runs=0, fingerprints={}, discovery_fingerprint=None,
                               explore_passes=3, assertion_violations=[])
        scenario = {} if exhausted else {'schedule': saved}
        trace = {'schedule_ref': saved['id'],
                 'terminals': [{'worker':w, 'state':'done', 'failure_class':'none'} for w in ('w1','w2')],
                 'events': [dict(s, seq=i+1, kind='point_released', step=i) for i,s in enumerate(saved['steps'])]}
        for name, value in (('manifest',manifest),('observation',observation),('scenario',scenario),('trace',trace),
                            ('report',{'manifest':manifest,'observation':observation,'scenario':scenario})):
            write_json(directory/(name+'.json'), value)
        (directory/'report.md').write_text('Constructed product evidence\n')
        command = {'seconds': 1, 'started': True, 'timeout': False, 'exit': 0 if exhausted else 2, 'error': None,
                   'containment_proven': True, 'finished': True, 'interrupted': False}
        write_json(root/'run.command.json', command)
        write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':True})
        seal(root)
        return root, slot, command, expected

    def test_discovery_must_fit_registered_exploration_pass_limit(self):
        for count in (0, 1, 2, 3, 4):
            with self.subTest(passes=count):
                root, slot, command, expected = self.product_raw('T-E', passes=count)
                manifest = self.manifest(); manifest['inputs']['matching']['vulnerable'] = expected
                state = read_json(root/'slot.json'); state['evidence_received_seconds'] = [2, 2]
                write_json(root/'slot.json', state); seal(root)
                result = reduce_slot(root, slot, manifest)
                shutil.rmtree(root)
                if 1 <= count <= 3:
                    self.assertEqual(result['status'], 'complete', result['reasons'])
                    self.assertTrue(result['stable_confirmation'])
                else:
                    self.assertEqual(result['status'], 'quality_failed')
                    self.assertIn('discovery exceeds registered exploration pass limit', result['reasons'])
                    self.assertFalse(result['stable_confirmation'])
                    self.assertIsNone(result['usable_evidence_seconds'])

    def test_product_artifact_mode_must_match_requested_arm(self):
        for arm, other in (('T-R','T-E'), ('T-E','T-R')):
            with self.subTest(arm=arm):
                root, slot, command, expected = self.product_raw(arm)
                valid = product(root, slot, command, expected)
                self.assertFalse(valid['quality_reasons'] + valid['operational_reasons'])
                with self.assertRaisesRegex(ValueError, 'mode'):
                    product(root, dict(slot, arm=other), command, expected)

    def test_product_terminal_workers_must_match_schedule(self):
        for arm in ('T-R', 'T-E'):
            root, slot, command, expected = self.product_raw(arm)
            path = root/'raw/runs/constructed/trace.json'
            original = read_json(path)
            for workers in (['w1','w1'], ['w2','w2'], [None,None], ['w1',None], ['w1','other']):
                with self.subTest(arm=arm, workers=workers):
                    trace = copy.deepcopy(original)
                    for terminal, worker in zip(trace['terminals'], workers):
                        if worker is None: terminal.pop('worker')
                        else: terminal['worker'] = worker
                    write_json(path, trace)
                    evidence = product(root, slot, command, expected)
                    self.assertIn('terminal workers differ from schedule', evidence['quality_reasons'])
            original['terminals'].reverse()
            write_json(path, original)
            self.assertFalse(product(root, slot, command, expected)['quality_reasons'])

    def test_nonzero_baseline_exits_cannot_complete(self):
        root, slot, command = self.raw()
        manifest = self.manifest()
        original = read_json(root/'raw/run.json')
        for arm in ('B0','B1','B2'):
            current = dict(slot, arm=arm)
            run = dict(original, arm=arm, executions=0 if arm == 'B0' else 2)
            write_json(root/'raw/run.json', run)
            write_json(root/'slot.json', {'slot':current, 'cleanup_proven':True})
            for code in (1, 2, 137, -9, None):
                with self.subTest(arm=arm, exit=code):
                    write_json(root/'run.command.json', dict(command, exit=code))
                    seal(root)
                    result = reduce_slot(root, current, manifest)
                    self.assertEqual(result['status'], 'operational_failed')
                    self.assertIn('baseline operational exit', result['reasons'])
                    if arm != 'B0':
                        self.assertEqual(result['valid_evaluations'], 2)
                        self.assertEqual(result['domain_violations'], 2)
                        self.assertIsNotNone(result['first_event_seconds'])
            write_json(root/'run.command.json', command); seal(root)
            self.assertEqual(reduce_slot(root, current, manifest)['status'], 'complete')

    def test_measured_command_must_finish_without_interruption(self):
        baseline_root, baseline_slot, baseline_command = self.raw()
        manifest = self.manifest()
        trials = [(baseline_root, baseline_slot, baseline_command)]
        for arm in ('T-R','T-E'):
            root, slot, command, expected = self.product_raw(arm)
            manifest['inputs'][slot['workload']][slot['variant']] = expected
            trials.append((root,slot,command))
        for root, slot, command in trials:
            for change in ({'finished':False}, {'interrupted':True}):
                with self.subTest(arm=slot['arm'], change=change):
                    write_json(root/'run.command.json', dict(command, **change)); seal(root)
                    result = reduce_slot(root, slot, manifest)
                    self.assertEqual(result['status'], 'operational_failed')
                    self.assertIsNotNone(result['first_event_seconds'])

    def test_baseline_runtime_overrides_are_rejected(self):
        m = self.measurement_manifest()
        other = self.root/'another-java'; other.write_bytes(b'another runtime')
        m['pins'][str(other)] = digest(other)
        for arm in ('B0','B1','B2'):
            for option in ('java','jar','image'):
                for prefix, equals in (('--',False), ('-',False), ('--',True), ('-',True)):
                    with self.subTest(arm=arm, option=option, prefix=prefix, equals=equals):
                        changed = copy.deepcopy(m)
                        flag = prefix+option
                        changed['commands']['matching']['fixed'][arm]['argv'] += (
                            [flag+'='+str(other)] if equals else [flag,str(other)])
                        with self.assertRaisesRegex(ValueError, 'baseline.*override'):
                            validate(changed)
                        with self.assertRaisesRegex(ValueError, 'baseline.*override'):
                            live_pins(changed)

    def test_baseline_libraries_must_match_reference_build(self):
        m = self.measurement_manifest()
        spec = m['commands']['matching']['fixed']['B1']
        original = spec['argv'][-1]
        library = self.root/'spring.jar'
        library_bytes = library.read_bytes()
        for kind in ('substitute','extra','missing','duplicate','loose-class','resource'):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(m)
                argv = changed['commands']['matching']['fixed']['B1']['argv']
                if kind == 'substitute':
                    with zipfile.ZipFile(library,'w') as archive:
                        archive.writestr('org/springframework/Constructed.class', b'substituted')
                    changed['pins'][str(library)] = digest(library)
                elif kind == 'missing':
                    argv[-1] = os.pathsep.join(p for p in original.split(os.pathsep) if p != str(library))
                elif kind == 'duplicate':
                    argv[-1] += os.pathsep+str(library)
                else:
                    extra = self.root/('extra-'+kind)
                    if kind == 'extra':
                        with zipfile.ZipFile(extra,'w') as archive:
                            archive.writestr('org/springframework/Constructed.class', b'shadow')
                        changed['pins'][str(extra)] = digest(extra)
                    else:
                        extra.mkdir()
                        name = ('org/springframework/Constructed.class' if kind == 'loose-class'
                                else 'META-INF/services/constructed')
                        path = extra/name; path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b'shadow'); changed['pins'][str(path)] = digest(path)
                    argv[-1] = str(extra)+os.pathsep+original
                with self.assertRaisesRegex(ValueError, 'classpath.*(librar|reference|entry)'):
                    validate(changed)
            library.write_bytes(library_bytes)

    def test_product_libraries_must_match_reference_build(self):
        m = self.measurement_manifest()
        jar = self.root/'fixture.jar'
        original = jar.read_bytes()
        for kind in ('substitute','extra','missing'):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(m)
                with zipfile.ZipFile(jar) as archive:
                    contents = {name:archive.read(name) for name in archive.namelist()}
                key = 'BOOT-INF/lib/spring.jar'
                if kind == 'substitute': contents[key] = b'changed library'
                elif kind == 'extra': contents['BOOT-INF/lib/extra.jar'] = b'extra library'
                else: del contents[key]
                with zipfile.ZipFile(jar, 'w') as archive:
                    for name, data in contents.items(): archive.writestr(name,data)
                changed['pins'][str(jar)] = digest(jar)
                for variants in changed['inputs'].values():
                    for inp in variants.values(): inp['jar_sha256'] = digest(jar)
                with self.assertRaisesRegex(ValueError, 'fixture.*librar'):
                    validate(changed)
            jar.write_bytes(original)

    def test_product_classpath_shadows_are_rejected(self):
        m = self.measurement_manifest()
        jar = self.root/'fixture.jar'
        original = jar.read_bytes()
        for name in ('org/springframework/Constructed.class', 'META-INF/services/constructed'):
            with self.subTest(entry=name):
                changed = copy.deepcopy(m)
                with zipfile.ZipFile(jar,'a') as archive:
                    archive.writestr('BOOT-INF/classes/'+name, b'shadow')
                changed['pins'][str(jar)] = digest(jar)
                for variants in changed['inputs'].values():
                    for inp in variants.values(): inp['jar_sha256'] = digest(jar)
                with self.assertRaisesRegex(ValueError, 'fixture classpath'):
                    validate(changed)
            jar.write_bytes(original)

    def test_sdk_identity_comes_from_registration_not_reference_compiler(self):
        m = self.measurement_manifest()
        sdk = self.root/'weavegate-spring-constructed.jar'
        with zipfile.ZipFile(sdk,'w') as archive:
            archive.writestr('io/github/weavegate/sdk/Weavegate.class', b'registered candidate SDK')
        jar = self.root/'fixture.jar'
        with zipfile.ZipFile(jar) as archive:
            contents = {name:archive.read(name) for name in archive.namelist()}
        contents['BOOT-INF/lib/'+sdk.name] = sdk.read_bytes()
        with zipfile.ZipFile(jar,'w') as archive:
            for name,data in contents.items(): archive.writestr(name,data)
        for path in (jar,sdk): m['pins'][str(path)] = digest(path)
        for variants in m['inputs'].values():
            for inp in variants.values():
                inp.update(sdk_sha256=digest(sdk), jar_sha256=digest(jar))
        m['candidate']['sdk_sha256'] = m['e1_registration']['sdk_sha256'] = digest(sdk)
        self.write_prerequisites(m)
        self.assertNotEqual(self.library_reference['libraries']['matching'][sdk.name], digest(sdk))
        validate(m)

    def test_baseline_receipt_identity_must_match_requested_slot(self):
        root, slot, command = self.raw()
        expected = self.manifest()['inputs']['quota']['vulnerable']
        original = read_json(root/'raw/run.json')
        # A clean fixed non-detection must not count as vulnerable exposure.
        for path in (root/'raw').glob('execution-*.json'):
            value = read_json(path); value['evaluation']['results'][0]['violations'] = []
            write_json(path, value)
        for arm in ('B0','B1','B2'):
            for key, value in (('arm','T-R'), ('variant','fixed'), ('arm',None), ('variant',None)):
                with self.subTest(arm=arm, key=key, value=value):
                    run = dict(original, arm=arm)
                    if arm == 'B0': run['executions'] = 0
                    run[key] = value
                    write_json(root/'raw/run.json', run)
                    with self.assertRaisesRegex(ValueError, 'baseline identity'):
                        baseline(root, dict(slot, arm=arm), command, expected)

    def test_registration_cannot_be_replaced_after_partial_collection(self):
        m = self.measurement_manifest()
        m['unsupported'] = {'matching/B2': UNSUPPORTED}
        bundle, registration = self.measurement_bundle(m)
        registered(bundle, m, registration)
        slot = order()[0]
        directory = bundle/'slots'/slot['id']; directory.mkdir()
        write_json(directory/'slot.json', {'slot':slot, 'unsupported':UNSUPPORTED})
        seal(directory)
        save_progress(bundle, {slot['id']:digest(directory/'SHA256SUMS')})
        replacement = read_json(registration)
        replacement.update(public_url='https://example.com/replaced', revision='replacement', registered_at='2021-01-01T00:00:00Z')
        write_json(registration, replacement)
        for missing in (True, False):
            with self.subTest(missing=missing):
                trial = self.root/('missing-registration' if missing else 'replaced-registration')
                shutil.copytree(bundle, trial)
                retained = trial/'registration.json'
                if missing: retained.unlink()
                else: write_json(retained, replacement)
                with patch('tool.execute') as execute:
                    with self.assertRaisesRegex(ValueError, 'registration'):
                        collect(trial, registration)
                    execute.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'registration'):
                    analyze(trial, self.root/'analysis')

    def test_registration_is_preserved_across_checkpoint_updates(self):
        m = self.measurement_manifest()
        bundle, registration = self.measurement_bundle(m)
        registered(bundle, m, registration)
        original = read_json(registration)
        save_progress(bundle, {}, active=order()[0]['id'])
        self.assertEqual(read_json(bundle/'progress/checkpoint.json')['registration'], original)
        save_progress(bundle, {})
        registered(bundle, m, registration)
        self.assertEqual(retained_progress(bundle, m, order())['registration'], original)

    def test_unregistered_consumed_checkpoint_cannot_register_late(self):
        m = self.measurement_manifest()
        m['unsupported'] = {'matching/B2':UNSUPPORTED}
        bundle, registration = self.measurement_bundle(m)
        slot = order()[0]
        directory = bundle/'slots'/slot['id']; directory.mkdir()
        write_json(directory/'slot.json', {'slot':slot, 'unsupported':UNSUPPORTED})
        seal(directory)
        save_progress(bundle, {slot['id']:digest(directory/'SHA256SUMS')})
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'registration'): collect(bundle, registration)
            execute.assert_not_called()

    def test_measurement_literal_and_config_dependencies_must_be_pinned(self):
        m = self.measurement_manifest()
        inputs = self.root/'command-inputs'; inputs.mkdir()
        classes = inputs/'classes'; classes.mkdir()
        launcher = classes/'io/github/weavegate/experiment/BaselineMain.class'
        launcher.parent.mkdir(parents=True); launcher.write_bytes(b'constructed launcher')
        libraries = inputs/'lib'; libraries.mkdir()
        shutil.copyfile(self.root/'spring.jar', libraries/'spring.jar')
        migrations = inputs/'migrations'; migrations.mkdir()
        (migrations/'001.sql').write_text('constructed migration')
        for name in ('config.yaml','seed.sql','schedule.json'):
            (inputs/name).write_text('constructed seed' if name == 'seed.sql' else 'constructed '+name)
        for p in files(inputs): m['pins'][str(p)] = digest(p)
        inp = m['inputs']['matching']['vulnerable']
        inp['config']['Target']['Schema'] = {'Migrations':str(migrations), 'Seed':str(inputs/'seed.sql')}
        document = read_json(self.root/'matching-vulnerable.json')
        document['target']['schema'] = {'migrations':str(migrations), 'seed':str(inputs/'seed.sql')}
        write_json(inputs/'config.yaml', document)
        m['pins'][str(inputs/'config.yaml')] = digest(inputs/'config.yaml')
        for arm in ARMS:
            spec = m['commands']['matching']['vulnerable'][arm]
            index = spec['argv'].index('--config')
            spec['argv'][index:index+2] = ['--config='+str(inputs/'config.yaml')]
            spec['argv'] += ['--replay', str(inputs/'schedule.json')]
            if arm.startswith('B'):
                index = spec['argv'].index('--classpath')+1
                paths = [p for p in spec['argv'][index].split(os.pathsep) if p != str(self.root/'spring.jar')]
                spec['argv'][index] = os.pathsep.join([*paths, str(classes), str(libraries/'*')])
        validate(m)
        dependencies = files(inputs) + [Path(inp['config']['Target']['SUT']['External']['JAR']),
                                        self.root/'weavegate-spring-constructed.jar'] + files(self.root/'fixture-classes')
        for p in dependencies:
            with self.subTest(missing=p.name):
                changed = copy.deepcopy(m); del changed['pins'][str(p)]
                with self.assertRaisesRegex(ValueError, 'dependenc'):
                    validate(changed)

    def test_new_classpath_file_after_freeze_prevents_execution(self):
        m = self.measurement_manifest()
        classes = self.root/'classes'; classes.mkdir()
        original = classes/'io/github/weavegate/experiment/BaselineMain.class'
        original.parent.mkdir(parents=True); original.write_bytes(b'original')
        m['pins'][str(original)] = digest(original)
        m['commands']['matching']['fixed']['B2']['argv'][-1] += os.pathsep + str(classes)
        bundle, registration = self.measurement_bundle(m)
        (classes/'Added.class').write_bytes(b'unpinned addition')
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'dependenc'):
                collect(bundle, registration)
            execute.assert_not_called()

    def warm_collection(self, receipt_change, cache_change=None):
        m = self.measurement_manifest()
        warm = next(s for s in order() if s['cache'] == 'warm')
        # Retain the preceding consumed prefix without spending runtime on it.
        m['unsupported'] = {w+'/'+a:UNSUPPORTED for w in WORKLOADS for a in ARMS}
        del m['unsupported'][warm['workload']+'/'+warm['arm']]
        bundle, registration = self.measurement_bundle(m)
        registered(bundle, m, registration)
        consumed = {}
        for slot in order()[:warm['ordinal']-1]:
            directory = bundle/'slots'/slot['id']; directory.mkdir()
            write_json(directory/'slot.json', {'slot':slot, 'unsupported':UNSUPPORTED})
            seal(directory); consumed[slot['id']] = digest(directory/'SHA256SUMS')
        save_progress(bundle, consumed)
        from tool import execute as real_execute
        calls = []
        def execute(spec, context, directory, label, secrets):
            calls.append((label,directory.name))
            if directory.name == 'warmup':
                value = dict({'seconds':1, 'exit':2, 'timeout':False, 'error':None, 'started':True, 'finished':True,
                              'interrupted':False, 'containment_proven':True}, **receipt_change)
                write_json(directory/'run.command.json',value)
                return value
            if label == 'cleanup': raise KeyboardInterrupt('end constructed test')
            value = real_execute(spec, context, directory, label, secrets)
            if label == 'warm-cleanup' and cache_change:
                receipt = read_json(directory/'warm-cleanup.json')
                cache_change(receipt)
                write_json(directory/'warm-cleanup.json',receipt)
            return value
        with patch('tool.execute', side_effect=execute), patch('analyze.product', return_value={
                'cleanup_proven':True, 'quality_reasons':[], 'operational_reasons':[]}):
            collect(bundle, registration)
        state = read_json(bundle/'slots'/warm['id']/'slot.json')
        return state, calls

    def test_operationally_failed_warmup_prevents_measured_command(self):
        changes = ({'error':'descendants remained'}, {'started':False}, {'containment_proven':False},
                   {'finished':False}, {'interrupted':True})
        for change in changes:
            with self.subTest(change=change):
                state, calls = self.warm_collection(change)
                self.assertIn('warm-up failed', state['error'])
                self.assertNotIn('warmup', state)
                self.assertEqual([label for label,_ in calls], ['prepare','run','cleanup'])
            shutil.rmtree(self.root/'measurement')

    def test_clean_warmup_accepts_pass_and_domain_violation_exits(self):
        for code in (0, 2):
            with self.subTest(exit=code):
                state, calls = self.warm_collection({'exit':code})
                self.assertEqual(state['warmup']['exit'], code)
                self.assertEqual(state['warmup']['startup_caches'], [])
                self.assertIn(('warm-cleanup',state['slot']['id']), calls)
            shutil.rmtree(self.root/'measurement')

    def test_warm_cache_inventory_is_bound_to_this_warmup(self):
        for change in ('missing', 'wrong-warmup', 'metadata', 'proof'):
            with self.subTest(change=change):
                def alter(receipt):
                    if change == 'missing': receipt.pop('startup_caches')
                    elif change == 'wrong-warmup': receipt['warmup_command_sha256'] = 'a'*64
                    else:
                        cache = {'path':'/guest/generated.cache', 'key':'candidate-key', 'sha256':'b'*64,
                                 'size_bytes':12, 'construction_seconds':.5, 'invalidation':'candidate digest changes',
                                 'proof':'proof/warm_cleanup.json'}
                        if change == 'metadata': cache.pop('key')
                        if change == 'proof': cache['proof'] = 'proof/absent.json'
                        receipt['startup_caches'] = [cache]
                state, calls = self.warm_collection({}, alter)
                self.assertIn('error',state)
                self.assertIn('warm',state['error'])
                self.assertNotIn(('run',state['slot']['id']), calls)
                self.assertNotIn('startup_caches',state['warmup'])
            shutil.rmtree(self.root/'measurement')

    def test_generated_warm_cache_metadata_is_retained(self):
        cache = {'path':'/guest/generated.cache', 'key':'candidate-key', 'sha256':'b'*64,
                 'size_bytes':12, 'construction_seconds':.5, 'invalidation':'candidate digest changes',
                 'proof':'proof/warm_cleanup.json'}
        state, calls = self.warm_collection({}, lambda r:r.update(startup_caches=[cache]))
        self.assertEqual(state['warmup']['startup_caches'],[cache])
        self.assertIn(('run',state['slot']['id']),calls)

    def test_prerequisites_require_candidate_bound_successful_completion(self):
        m = self.measurement_manifest()
        for kind in PREREQUISITES:
            for change in ('arbitrary', 'candidate', 'failed', 'marker'):
                with self.subTest(kind=kind, change=change):
                    changed = copy.deepcopy(m)
                    path = Path(m['prerequisites'][kind])
                    record = read_json(path)
                    if change == 'arbitrary':
                        changed['prerequisites'][kind] = str(self.root/'arm.py')
                    elif change == 'candidate':
                        record['candidate']['cli_sha256'] = 'f'*64
                        write_json(path,record); changed['pins'][str(path)] = digest(path)
                    elif change == 'failed':
                        command = read_json(record['command']); command['exit'] = 1
                        write_json(record['command'],command)
                        changed['pins'][record['command']] = digest(record['command'])
                    else:
                        Path(record['stdout']).write_text('incomplete\n')
                        changed['pins'][record['stdout']] = digest(record['stdout'])
                    with self.assertRaises((ValueError, KeyError)):
                        validate(changed)
                self.write_prerequisites(m)

    def test_acceptance_prerequisites_require_complete_checker_and_repetitions(self):
        m = self.measurement_manifest()
        for change in ('checker', 'result', 'repetitions', 'revision', 'artifact'):
            with self.subTest(change=change):
                record = read_json(m['prerequisites']['go_acceptance'])
                path = record['command'] if change == 'checker' else record['results']
                value = read_json(path)
                if change == 'checker': value['argv'].remove('--require-complete')
                elif change == 'result': value['results']['constructed']['status'] = 'incomplete'
                elif change == 'repetitions': value['run']['repetitions'] = 1
                elif change == 'revision': value['run']['revision'] = 'b'*40
                else: value['run']['artifacts']['stdout']['sha256'] = 'b'*64
                write_json(path,value); m['pins'][path] = digest(path)
                with self.assertRaisesRegex(ValueError,'prerequisite'): validate(m)
            self.write_prerequisites(m)

    def test_prerequisite_completion_is_checked_from_retained_bytes_offline(self):
        m = self.measurement_manifest()
        bundle, _ = self.measurement_bundle(m)
        shutil.rmtree(self.root/'prerequisites')
        analyze(bundle,self.root/'analysis')
        record = read_json(bundle/'frozen/inputs'/m['pins'][m['prerequisites']['go_acceptance']])
        path = bundle/'frozen/inputs'/m['pins'][record['stdout']]
        path.write_text('replaced completion')
        with self.assertRaises(ValueError): analyze(bundle,self.root/'bad-analysis')

    def test_report_shows_baseline_exposure_beside_planned_detections(self):
        bundle, _ = self.bundle()
        for valid in (0,2):
            def reduce(root,slot,manifest):
                selected = (slot['workload'],slot['variant'],slot['cache'],slot['arm']) == ('matching','fixed','cold','B2')
                return dict(slot, started=selected, reasons=[], invocation_seconds=1 if selected else None,
                            status=('complete' if valid else 'operational_failed') if selected else 'not_started',
                            valid_evaluations=valid if selected else 0)
            output = self.root/('exposure-'+str(valid))
            with patch('analyze.reduce_slot',side_effect=reduce): analyze(bundle,output)
            text = (output/'report.md').read_text()
            self.assertIn('Baseline exposure / 10',text)
            row = next(line for line in text.splitlines() if line.startswith('| matching | fixed | cold | B2 |'))
            self.assertTrue(row.endswith('| 0 / 10 | %d / 10 |' % (10 if valid else 0)))

    def test_report_shows_controlled_search_counts_and_confirmation(self):
        bundle, _ = self.bundle()
        for outcome in ('confirmed', 'zero', 'partial', 'exhausted'):
            with self.subTest(outcome=outcome):
                root, slot, command, expected = self.product_raw(
                    'T-E', exhausted=outcome == 'exhausted', repeat=20)
                if outcome in ('zero', 'partial'):
                    directory = root/'raw/runs/constructed'
                    observation = read_json(directory/'observation.json')
                    count = 0 if outcome == 'zero' else 7
                    observation.update(violation_runs=count, flaky=True,
                                       fingerprints={'a'*64: count, 'b'*64: 20-count} if count else {'b'*64: 20})
                    write_json(directory/'observation.json', observation)
                    merged = read_json(directory/'report.json'); merged['observation'] = observation
                    write_json(directory/'report.json', merged)
                    command['exit'] = 3
                evidence = product(root, slot, command, expected)
                def reduce(root, current, manifest):
                    result = dict(current, status='not_started', started=False, reasons=[])
                    if current['ordinal'] == 3:
                        result.update(evidence, status='quality_failed' if evidence['quality_reasons'] else 'complete',
                                      started=True, invocation_seconds=1,
                                      first_event_membership=membership(evidence['first_event_seconds'], 900))
                    return result
                output = self.root/('search-report-'+outcome)
                with patch('analyze.reduce_slot', side_effect=reduce): analyze(bundle, output)
                text = (output/'report.md').read_text()
                self.assertIn('Explored candidates', text)
                self.assertIn('Violating confirmations / planned', text)
                row = next(line for line in text.splitlines() if line.startswith('| '+order()[2]['id']+' |'))
                self.assertIn('| %d |' % evidence['exploration_executions'], row)
                if outcome == 'exhausted':
                    self.assertIn('not run (no discovery)', row)
                else:
                    self.assertIn('| %d / 20 |' % evidence['violation_runs'], row)
                    self.assertIn(evidence['selected_schedule'], row)
                    self.assertIn('a'*64 if outcome != 'zero' else 'b'*64, row)
                shutil.rmtree(root)

    def test_observation_hook_cannot_reuse_preparation_proof(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        def reuse(spec, context, directory, label, secrets):
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'observe':
                value = read_json(directory/'observation.json')
                value['evidence'] = read_json(directory/'preparation.json')['evidence']
                write_json(directory/'observation.json', value)
            return result
        with patch('tool.execute', side_effect=reuse): collect(bundle, registration)
        state = read_json(bundle/'slots'/order()[0]['id']/'slot.json')
        self.assertIn('newly retained', state.get('error',''))
        self.assertNotIn('resources', state)
        self.assertTrue(state['cleanup_proven'])

    def test_frozen_shared_modules_precede_ambient_pythonpath(self):
        bundle, _ = self.bundle()
        ambient = self.root/'ambient'; ambient.mkdir()
        for name in ('common','plan','execution','redaction'):
            (ambient/(name+'.py')).write_text("raise RuntimeError('ambient module executed')\n")
        program = "import support,sys; from pathlib import Path; assert all(Path(sys.modules[n].__file__).resolve() == support.SHARED / (n+'.py') for n in ('common','plan','execution','redaction')); import tool; assert Path(tool.__file__).parent == support.HERE"
        for source in (HERE, bundle/'frozen/source'):
            with self.subTest(source=source):
                result = subprocess.run([sys.executable,'-B','-c',program], cwd=source,
                    env=dict(os.environ, PYTHONPATH=str(ambient)), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_preloaded_ambient_shared_module_is_rejected(self):
        program = "import sys,types; m=types.ModuleType('common'); m.__file__='/ambient/common.py'; sys.modules['common']=m; import support"
        result = subprocess.run([sys.executable,'-B','-c',program], cwd=HERE, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shared module location differs', result.stderr)

    def test_frozen_replay_rejects_unregistered_bytecode_before_import(self):
        bundle, _ = self.bundle()
        source = bundle/'frozen/source'
        marker = self.root/'bytecode-executed'
        payload = self.root/'payload.py'
        payload.write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\nraise SystemExit(42)\n')
        for name in ('support', 'analyze', 'fixture_contract', 'shared/common', 'shared/execution'):
            with self.subTest(module=name):
                original = source/(name+'.py')
                cache = Path(importlib.util.cache_from_source(str(original)))
                cache.parent.mkdir(exist_ok=True)
                py_compile.compile(str(payload), cfile=str(cache), dfile=str(original), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
                data = cache.read_bytes()
                cache.write_bytes(data[:8] + struct.pack('<II', int(original.stat().st_mtime), original.stat().st_size) + data[16:])
                seal(bundle/'frozen')  # Local resealing does not register bytecode.
                result = subprocess.run([sys.executable, '-B', str(source/'tool.py'), 'analyze',
                                         str(bundle), str(self.root/'analysis')], text=True, capture_output=True)
                executed = marker.exists()
                if executed: marker.unlink()
                shutil.rmtree(cache.parent)
                self.assertFalse(executed, name+' bytecode ran before validation')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('source inventory', result.stderr)
                self.assertFalse((self.root/'analysis').exists())

    def test_frozen_source_inventory_rejects_non_source_entries(self):
        bundle, _ = self.bundle()
        for name in ('source/__pycache__', 'source/shared/__pycache__', 'source/support.pyc',
                     'source/shared/common.so', 'source/unregistered.txt'):
            with self.subTest(name=name):
                path = bundle/'frozen'/name
                if name.endswith('__pycache__'): path.mkdir()
                else: path.write_bytes(b'unregistered')
                seal(bundle/'frozen')
                try:
                    with self.assertRaisesRegex(ValueError, 'source inventory'):
                        analyze(bundle, self.root/'analysis')
                finally:
                    if path.is_dir(): path.rmdir()
                    else: path.unlink()

    def test_frozen_imports_ignore_external_bytecode_cache(self):
        bundle, _ = self.bundle()
        source = bundle/'frozen/source'
        prefix = self.root/'external-cache'
        marker = self.root/'bytecode-executed'
        payload = self.root/'payload.py'
        payload.write_text('from pathlib import Path\nPath('+repr(str(marker))+').touch()\nraise SystemExit(42)\n')
        for name in ('support', 'analyze', 'fixture_contract', 'shared/common', 'shared/plan', 'shared/execution'):
            original = source/(name+'.py')
            with patch.object(sys, 'pycache_prefix', str(prefix)):
                cache = Path(importlib.util.cache_from_source(str(original)))
            cache.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(str(payload), cfile=str(cache), dfile=str(original), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
            data = cache.read_bytes()
            cache.write_bytes(data[:8] + struct.pack('<II', int(original.stat().st_mtime), original.stat().st_size) + data[16:])
        result = subprocess.run([sys.executable, '-B', str(source/'tool.py'), 'analyze',
                                 str(bundle), str(self.root/'analysis')],
            env=dict(os.environ, PYTHONPYCACHEPREFIX=str(prefix)), text=True, capture_output=True)
        self.assertFalse(marker.exists(), result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root/'analysis/results.json').is_file())

    def test_preloaded_local_modules_cannot_replace_frozen_tools(self):
        bundle, _ = self.bundle()
        ambient = self.root/'ambient'; ambient.mkdir()
        for source in (HERE, bundle/'frozen/source'):
            for name in ('support', 'analyze', 'stream', 'fixture_contract'):
                with self.subTest(source=source, module=name):
                    # A fully functional module in sys.modules used to bypass
                    # source checks. Alter its origin, not its public interface.
                    (ambient/'sitecustomize.py').write_text(
                        'import importlib, sys\n'
                        'sys.path.insert(0, '+repr(str(source))+')\n'
                        'module = importlib.import_module('+repr(name)+')\n'
                        'module.__file__ = '+repr(str(ambient/(name+'.py')))+'\n'
                        'sys.path.pop(0)\n')
                    output = self.root/('ambient-analysis-'+source.name+'-'+name)
                    result = subprocess.run([sys.executable, '-B', str(source/'tool.py'),
                                             'analyze', str(bundle), str(output)],
                        env=dict(os.environ, PYTHONPATH=str(ambient)), text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0, 'ambient module was accepted: '+name)
                    self.assertIn('runner module location differs: '+name, result.stderr)
                    self.assertFalse(output.exists())

    def test_fully_pinned_classpath_substitutions_are_rejected(self):
        for kind in ('business', 'declared_business', 'sdk', 'shadow', 'missing', 'extra'):
            with self.subTest(kind=kind):
                m = self.measurement_manifest()
                validate(m)
                business = next((self.root/'fixture-classes').rglob('*.class'))
                sdk = self.root/'weavegate-spring-constructed.jar'
                if kind in ('business', 'declared_business'):
                    business.write_bytes(b'changed business class'); m['pins'][str(business)] = digest(business)
                    if kind == 'declared_business':
                        for variants in m['inputs'].values():
                            for inp in variants.values():
                                inp['business_classes'] = {n:digest(business) for n in inp['business_classes']}
                elif kind == 'sdk':
                    with zipfile.ZipFile(sdk, 'w') as archive:
                        archive.writestr('io/github/weavegate/sdk/Weavegate.class', b'changed SDK')
                    m['pins'][str(sdk)] = digest(sdk)
                elif kind == 'shadow':
                    shadow = self.root/'shadow.jar'
                    with zipfile.ZipFile(shadow, 'w') as archive:
                        archive.writestr('io/github/weavegate/fixture/constructed/Business.class', b'shadow')
                    m['pins'][str(shadow)] = digest(shadow)
                    m['commands']['matching']['fixed']['B1']['argv'][-1] = str(shadow)+os.pathsep+m['commands']['matching']['fixed']['B1']['argv'][-1]
                elif kind == 'missing':
                    m['commands']['quota']['fixed']['B0']['argv'][-1] = str(sdk)
                else:
                    extra = business.with_name('Added.class'); extra.write_bytes(b'fully pinned extra class')
                    m['pins'][str(extra)] = digest(extra)
                with self.assertRaisesRegex(ValueError, 'business|SDK|classpath'):
                    validate(m)
                shutil.rmtree(self.root/'fixture-classes')

    def test_measurement_configs_must_use_registered_image(self):
        m = self.measurement_manifest()
        validate(m)
        for workload in WORKLOADS:
            for variant in ('vulnerable','fixed'):
                for image in ('mysql:8.4', 'mysql:8.4@sha256:'+'b'*64, None):
                    with self.subTest(workload=workload, variant=variant, image=image):
                        changed = copy.deepcopy(m)
                        changed['inputs'][workload][variant]['config']['Target']['DB'] = image
                        with self.assertRaisesRegex(ValueError, 'image'):
                            validate(changed)

    def test_retained_receipts_must_use_expected_image(self):
        root, slot, command = self.raw()
        expected = self.manifest()['inputs']['quota']['vulnerable']
        for arm in ('B0','B1','B2'):
            for image in (None, 'mysql:8.4', 'mysql:8.4@sha256:'+'b'*64):
                with self.subTest(arm=arm, image=image):
                    run = read_json(root/'raw/run.json'); run['arm'] = arm
                    run['environment']['image'] = image
                    if arm == 'B0': run['executions'] = 0
                    write_json(root/'raw/run.json', run)
                    with self.assertRaisesRegex(ValueError, 'image'):
                        baseline(root, dict(slot, arm=arm), command, expected)
        for arm in ('T-R','T-E'):
            root, slot, command, expected = self.product_raw(arm)
            directory = root/'raw/runs/constructed'
            for image in (None, 'mysql:8.4', 'mysql:8.4@sha256:'+'b'*64):
                with self.subTest(arm=arm, image=image):
                    merged = read_json(directory/'report.json'); merged['manifest']['image'] = image
                    write_json(directory/'manifest.json', merged['manifest']); write_json(directory/'report.json', merged)
                    with self.assertRaisesRegex(ValueError, 'image'):
                        product(root, slot, command, expected)

    def test_final_integrity_failure_fails_active_slot_and_stops(self):
        for boundary in ('observe','cleanup','protect'):
            with self.subTest(boundary=boundary):
                for path in (self.root/'measurement', self.root/'slot'):
                    if path.exists(): shutil.rmtree(path)
                m = self.measurement_manifest()
                bundle, registration = self.measurement_bundle(m)
                raw, slot, command = self.raw(order()[0])
                receipt = read_json(raw/'raw/run.json'); receipt['executions'] = 100
                write_json(raw/'raw/run.json', receipt)
                execution = read_json(raw/'raw/execution-001.json')
                execution['evaluation']['results'][0]['violations'] = []
                for i in range(1,101): write_json(raw/'raw'/('execution-%03d.json'%i), dict(execution,index=i))
                from tool import execute as real_execute, protect as real_protect
                cleaned = False
                def execute(spec, context, directory, label, secrets):
                    nonlocal cleaned
                    if label == 'run':
                        shutil.copytree(raw/'raw', directory/'raw')
                        write_json(directory/'run.command.json', command)
                        return command
                    value = real_execute(spec, context, directory, label, secrets)
                    if label == boundary:
                        (self.root/'arm.py').write_text('changed after execution\n')
                    if label == 'cleanup': cleaned = True
                    return value
                def protect(bundle, directory):
                    real_protect(bundle, directory)
                    if boundary == 'protect' and cleaned: raise ValueError('constructed final protection failure')
                with patch('tool.execute',side_effect=execute), patch('tool.protect',side_effect=protect):
                    collect(bundle, registration)
                result = analyze(bundle, self.root/('analysis-'+boundary))
                self.assertEqual(result['slots'][0]['status'], 'operational_failed')
                self.assertTrue(any('pin changed' in r or 'protection failure' in r for r in result['slots'][0]['reasons']))
                self.assertTrue(read_json(bundle/'slots'/slot['id']/'slot.json')['cleanup_proven'])
                self.assertEqual(sum(s['status']=='not_started' for s in result['slots']),599)
                with self.assertRaisesRegex(ValueError,'stopped'): collect(bundle, registration)
                shutil.rmtree(bundle); shutil.rmtree(raw)

    def test_discovery_must_be_final_explored_execution(self):
        root, slot, command, expected = self.product_raw('T-E', saved_index=2)
        self.assertTrue(product(root,slot,command,expected)['stable_confirmation'])
        for count in (0, 1, 2, 4):
            with self.subTest(explored_count=count):
                shutil.rmtree(root)
                root, slot, command, expected = self.product_raw('T-E', explored_count=count, saved_index=2)
                evidence = product(root, slot, command, expected)
                self.assertIn('discovery schedule differs from final explored execution', evidence['quality_reasons'])
                self.assertFalse(evidence['stable_confirmation'])
                self.assertIsNone(evidence['first_event_seconds'])

    def test_exhausted_search_requires_clean_outcome(self):
        root, slot, command, expected = self.product_raw('T-E', exhausted=True)
        directory = root/'raw/runs/constructed'
        original = read_json(directory/'observation.json')
        self.assertFalse(product(root,slot,command,expected)['quality_reasons'])
        for changes, code in (({'violation_runs':2,'assertion_violations':[{'oracle_id':WORKLOADS['matching'][2], 'rows':[{'synthetic':1}]}]},2),
                              ({'violation_runs':1},0), ({'assertion_violations':[{'oracle_id':WORKLOADS['matching'][2],'rows':[{'synthetic':1}]}]},2),
                              ({},2), ({'flaky':True},3)):
            with self.subTest(changes=changes, exit=code):
                observation = dict(original, **changes)
                merged = read_json(directory/'report.json'); merged['observation'] = observation
                write_json(directory/'report.json',merged); write_json(directory/'observation.json',observation)
                evidence = product(root,slot,dict(command,exit=code),expected)
                self.assertIn('exhausted search has non-clean outcome', evidence['quality_reasons'])
                self.assertFalse(evidence['unexpected_miss'])

    def test_analysis_excludes_collector_for_entire_reduction(self):
        bundle, _ = self.bundle()
        with (bundle/'.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): analyze(bundle, self.root/'blocked')
        from analyze import reduce_slot as real_reduce
        checked = []
        def reduce(root, slot, manifest):
            if slot['ordinal'] in (1,600):
                with self.assertRaises(BlockingIOError): collect(bundle,max_slots=1)
                with (bundle/'.lock').open('r') as reader:
                    fcntl.flock(reader, fcntl.LOCK_SH | fcntl.LOCK_NB)
                checked.append(slot['ordinal'])
            return real_reduce(root,slot,manifest)
        with patch('analyze.reduce_slot', side_effect=reduce): analyze(bundle,self.root/'analysis')
        self.assertEqual(checked,[1,600])
        with (bundle/'.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse((self.root/'blocked').exists())

    def test_command_config_must_match_effective_snapshot(self):
        m = self.measurement_manifest()
        for arm in ARMS:
            for field in ('worker', 'sync_points', 'timeout'):
                with self.subTest(arm=arm, field=field):
                    changed = copy.deepcopy(m)
                    config = self.root/'substituted.json'
                    document = read_json(self.root/'matching-vulnerable.json')
                    if field == 'worker':
                        document['scenarios']['concurrent-assign']['workers'][0]['args']['ID'] = '99'
                    elif field == 'sync_points':
                        document['scenarios']['concurrent-assign']['sync_points'] = ['different']
                    else:
                        document['run']['arrive_timeout_ms'] = 5000
                    write_json(config, document)
                    changed['pins'][str(config)] = digest(config)
                    argv = changed['commands']['matching']['vulnerable'][arm]['argv']
                    argv[argv.index('--config')+1] = str(config)
                    with self.assertRaisesRegex(ValueError, 'config'):
                        validate(changed)

    def test_command_config_paths_are_unambiguous_and_checked_live(self):
        m = self.measurement_manifest()
        command = m['commands']['matching']['fixed']['T-R']
        index = command['argv'].index('--config')
        for flag in ('--config', '-config', '--config=', '-config='):
            changed = copy.deepcopy(m)
            argv = changed['commands']['matching']['fixed']['T-R']['argv']
            path = Path(argv[index+1]).name
            argv[index:index+2] = [flag+path] if flag.endswith('=') else [flag,path]
            validate(changed)
        for options in ([], ['--config'], ['--config', ''], ['--config', '{trial_dir}/config'],
                        ['--config', command['argv'][index+1]] * 2):
            changed = copy.deepcopy(m)
            changed['commands']['matching']['fixed']['T-R']['argv'][index:index+2] = options
            with self.assertRaisesRegex(ValueError, 'config'): validate(changed)
        # A replacement can be fully pinned and still disagree with the effective
        # snapshot. Freeze and the pre-execution live check must both reject it.
        changed = copy.deepcopy(m)
        path = self.root/'replacement.json'
        document = read_json(command['argv'][index+1])
        document['target']['sut']['external']['startup_timeout_ms'] = 1
        write_json(path, document)
        changed['pins'][str(path)] = digest(path)
        changed['commands']['matching']['fixed']['T-R']['argv'][index+1] = str(path)
        write_json(self.root/'bad-manifest.json', changed)
        with self.assertRaisesRegex(ValueError, 'config'):
            freeze(self.root/'bad-manifest.json', self.root/'bad-bundle')
        with self.assertRaisesRegex(ValueError, 'config'): live_pins(changed)
        self.assertFalse((self.root/'bad-bundle').exists())

    def test_replaced_lock_cannot_admit_collector_or_analyzer(self):
        bundle, _ = self.bundle()
        from analyze import reduce_slot as real_reduce
        checked = []
        def reduce(root, slot, manifest):
            if slot['ordinal'] == 1:
                (bundle/'.lock').unlink()
                (bundle/'.lock').touch()
                with self.assertRaises((BlockingIOError, ValueError)):
                    collect(bundle, max_slots=1)
                checked.append(True)
            return real_reduce(root, slot, manifest)
        with patch('analyze.reduce_slot', side_effect=reduce):
            with self.assertRaisesRegex(ValueError, 'lock'):
                analyze(bundle, self.root/'analysis')
        self.assertEqual(checked, [True])
        self.assertFalse((self.root/'analysis').exists())
        self.assertEqual(list((bundle/'slots').iterdir()), [])

    def test_replaced_lock_during_collection_seals_stop(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        checked = []
        def execute(spec, context, directory, label, secrets):
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'prepare':
                (bundle/'.lock').unlink()
                (bundle/'.lock').touch()
                for operation in (lambda: collect(bundle, registration),
                                  lambda: analyze(bundle, self.root/'concurrent')):
                    with self.assertRaises((BlockingIOError, ValueError)):
                        operation()
                checked.append(True)
            return result
        with patch('tool.execute', side_effect=execute):
            with self.assertRaisesRegex(ValueError, 'lock'):
                collect(bundle, registration)
        self.assertEqual(checked, [True])
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(result['slots'][0]['status'], 'preparation_failed')
        self.assertFalse(any(s['started'] for s in result['slots']))
        self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
        self.assertIn('lock', result['stopped']['reason'])

    def test_analysis_output_cannot_modify_campaign(self):
        bundle, _ = self.bundle()
        collect(bundle, max_slots=1)
        alias = self.root/'alias'; alias.symlink_to(bundle, target_is_directory=True)
        before = inventory(bundle)
        for output in (bundle, bundle/'new', bundle/'frozen/new', bundle/'progress/new',
                       bundle/'slots'/order()[0]['id']/'new', alias/'frozen/new'):
            with self.subTest(output=str(output)):
                with self.assertRaisesRegex(ValueError, 'output.*campaign'):
                    analyze(bundle, output)
                self.assertEqual(inventory(bundle), before)
        first = analyze(bundle, self.root/'analysis')
        self.assertEqual(first, analyze(bundle, self.root/'reanalysis'))

    def test_cleanup_signals_preserve_sealed_stop_checkpoint(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ('cleanup', 'slot-seal', 'progress'):
                self.check_checkpoint_signal(signum, boundary)

    def test_registration_signals_leave_resumable_checkpoint(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ('registration.json', 'checkpoint.json', 'seal'):
                with self.subTest(signal=signum, boundary=boundary):
                    bundle, registration = self.measurement_bundle()
                    from tool import write_json as real_write, seal as real_seal
                    signaled = False
                    def interrupt():
                        nonlocal signaled
                        if not signaled:
                            signaled = True
                            os.kill(os.getpid(), signum)
                    def write(path, value):
                        real_write(path, value)
                        if path.name == boundary: interrupt()
                    def sealing(path):
                        if boundary == 'seal' and path == bundle/'progress': interrupt()
                        real_seal(path)
                    previous = {s:signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
                    with patch('tool.write_json', side_effect=write), patch('tool.seal', side_effect=sealing), \
                            patch('tool.execute') as execute:
                        with self.assertRaises(KeyboardInterrupt): collect(bundle, registration)
                        execute.assert_not_called()
                    self.assertTrue(signaled)
                    self.assertEqual(previous, {s:signal.getsignal(s) for s in previous})
                    verify(bundle/'progress')
                    self.assertEqual(list((bundle/'slots').iterdir()), [])
                    m = read_json(bundle/'frozen/manifest.json')
                    progress = retained_progress(bundle, m, order())
                    self.assertEqual(progress['registration'], read_json(registration))
                    self.assertIsNone(progress['active'])
                    self.assertIsNone(progress['stopped'])
                    result = analyze(bundle, self.root/('registration-output-'+str(signum)+'-'+boundary))
                    self.assertTrue(all(s['status'] == 'not_started' for s in result['slots']))
                    collect(bundle, registration)
                    self.assertEqual(len(list((bundle/'slots').iterdir())), 2)
                    shutil.rmtree(bundle)

    @staticmethod
    def resource_observations():
        return {
            'cpu_seconds': {'value': 0, 'scope': 'whole invocation: Go, Java, MySQL and observers'},
            'memory_bytes': {'value': 1024, 'scope': 'whole invocation: Go, Java, MySQL and observers',
                             'method': 'sampled', 'cadence_ms': 100,
                             'limitation': 'peaks between samples may be missed'},
            'container_creations': {'value': 1, 'scope': 'invocation allocation'},
            'evidence_bytes': {'value': 2048, 'scope': 'retained invocation evidence'},
            'cache_bytes': {'value': None, 'reason': 'aggregate cache size unavailable'},
        }

    def test_resource_observations_revalidated_offline(self):
        directory, slot, _ = self.raw()
        m = self.manifest(); m['phase'] = 'measurement'
        valid = self.resource_observations()
        changed = []
        for counter in valid:
            for change in ('missing', 'null-reason', 'scope', 'boolean', 'negative', 'string'):
                value = copy.deepcopy(valid)
                if change == 'missing': del value[counter]
                elif change == 'null-reason': value[counter] = {'value': None, 'reason': ' '}
                else:
                    value[counter] = {'value': 1, 'scope': 'invocation'}
                    if change == 'scope': value[counter]['scope'] = ''
                    else: value[counter]['value'] = {'boolean': True, 'negative': -1, 'string': '0'}[change]
                changed.append((counter+'-'+change, value))
        for field, value in (('method', 'unknown'), ('cadence_ms', 99), ('cadence_ms', True), ('limitation', '')):
            resource = copy.deepcopy(valid); resource['memory_bytes'][field] = value
            changed.append(('memory-'+field, resource))
        for counter in ('memory_bytes', 'container_creations', 'evidence_bytes', 'cache_bytes'):
            resource = copy.deepcopy(valid)
            resource[counter] = dict(resource[counter], value=1.5, scope='invocation')
            changed.append((counter+'-fractional', resource))
        changed.extend([('missing-all', None), ('old-incomplete', {'cpu_seconds': 0}),
                        ('wrong-type', []), ('unknown-counter', dict(valid, other={'value': 1}))])
        for name, resources in changed:
            with self.subTest(case=name):
                state = {'slot': slot, 'cleanup_proven': True}
                if resources is not None: state['resources'] = resources
                write_json(directory/'slot.json', state); seal(directory)
                result = reduce_slot(directory, slot, m)
                self.assertEqual(result['status'], 'operational_failed')
                self.assertTrue(any('resource' in r for r in result['reasons']), result['reasons'])
                self.assertAlmostEqual(result['first_event_seconds'][0], 0.1)
                self.assertAlmostEqual(result['first_event_seconds'][1], 0.2)
                self.assertIsNone(result['usable_evidence_seconds'])
                # Invalid values remain in sealed originals, never in derived counters.
                self.assertTrue(all(c['value'] is None and c['reason'] for c in result['resources'].values()))
        high_water = copy.deepcopy(valid)
        high_water['memory_bytes'] = {'value': 1024, 'scope': 'whole invocation allocation',
                                      'method': 'high_water_mark'}
        for values in (valid, high_water, {k:{'value': None, 'reason': 'counter unavailable'} for k in valid}):
            write_json(directory/'slot.json', {'slot': slot, 'cleanup_proven': True, 'resources': values})
            seal(directory)
            result = reduce_slot(directory, slot, m)
            self.assertEqual(result['status'], 'complete', result['reasons'])
            self.assertEqual(result['resources'], values)

    def test_collector_rejects_invalid_resource_receipt(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        def execute(spec, context, directory, label, secrets):
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'observe':
                receipt = read_json(directory/'observation.json')
                receipt['resources'] = {'cpu_seconds': 0}
                write_json(directory/'observation.json', receipt)
            return result
        with patch('tool.execute', side_effect=execute): collect(bundle, registration)
        directory = bundle/'slots'/order()[0]['id']
        state = read_json(directory/'slot.json')
        self.assertNotIn('resources', state)
        self.assertIn('resource', state['error'])
        self.assertTrue(state['cleanup_proven'])
        self.assertEqual(read_json(directory/'observation.json')['resources'], {'cpu_seconds': 0})
        result = analyze(bundle, self.root/'resource-output')
        self.assertEqual(result['slots'][0]['status'], 'operational_failed')
        self.assertTrue(any('resource' in r for r in result['slots'][0]['reasons']))

    def test_preparation_failure_retains_cleanup_and_integrity_reasons(self):
        bundle, registration = self.measurement_bundle()
        from tool import execute as real_execute
        def execute(spec, context, directory, label, secrets):
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'prepare': result['exit'] = 1
            if label == 'cleanup':
                raise ValueError('constructed allocation cleanup failure')
            return result
        with patch('tool.execute', side_effect=execute), \
                patch('tool.protect', side_effect=[None, ValueError('constructed integrity failure')]):
            collect(bundle, registration)
        result = analyze(bundle, self.root/'preparation-output')
        row = result['slots'][0]
        self.assertEqual(row['status'], 'preparation_failed')
        self.assertFalse(row['started'])
        self.assertFalse(row['cleanup_proven'])
        for reason in ('restore failed', 'constructed allocation cleanup failure',
                       'constructed integrity failure', 'allocation cleanup unproven'):
            self.assertIn(reason, row['reasons'])
        self.assertEqual(result['stopped']['slot'], row['id'])
        with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, registration)
        self.assertEqual(result, analyze(bundle, self.root/'preparation-reanalysis'))

    def test_cleanup_failure_reason_survives_measured_run(self):
        directory, slot, _ = self.raw()
        state = read_json(directory/'slot.json')
        state.update(cleanup_proven=False, cleanup_error='constructed cleanup proof failure')
        write_json(directory/'slot.json', state); seal(directory)
        m = self.manifest()
        result = reduce_slot(directory, slot, m)
        self.assertEqual(result['status'], 'operational_failed')
        self.assertIn('constructed cleanup proof failure', result['reasons'])

    def check_checkpoint_signal(self, signum, boundary):
        with self.subTest(signal=signum, boundary=boundary):
            bundle, registration = self.measurement_bundle()
            from tool import execute as real_execute, seal as real_seal, save_progress as real_progress
            cleanup_calls = []
            signaled = False
            def interrupt():
                nonlocal signaled
                if not signaled:
                    os.kill(os.getpid(), signum)
                    signaled = True
            def execute(spec, context, directory, label, secrets):
                if label == 'cleanup':
                    if boundary == label: interrupt()
                    cleanup_calls.append(label)
                return real_execute(spec, context, directory, label, secrets)
            def seal(path):
                if boundary == 'slot-seal' and path.resolve().parent == bundle/'slots': interrupt()
                return real_seal(path)
            def progress(root, consumed, **kwargs):
                if boundary == 'progress' and consumed: interrupt()
                return real_progress(root, consumed, **kwargs)
            with patch('tool.execute', side_effect=execute), patch('tool.seal', side_effect=seal), \
                    patch('tool.save_progress', side_effect=progress):
                try: collect(bundle, registration)
                except KeyboardInterrupt: pass
            self.assertEqual(cleanup_calls, ['cleanup'])
            self.assertTrue(signaled)
            result = analyze(bundle, self.root/('analysis-'+str(signum)+'-'+boundary))
            self.assertIn('interrupted', result['stopped']['reason'])
            self.assertIn('collector interrupted', result['slots'][0]['reasons'])
            self.assertTrue(read_json(bundle/'slots'/order()[0]['id']/'slot.json')['cleanup_proven'])
            self.assertEqual(sum(s['started'] for s in result['slots']), 1)
            (bundle/'stopped.json').unlink()
            with self.assertRaisesRegex(ValueError, 'stopped'):
                collect(bundle, registration)
            shutil.rmtree(bundle)

    def test_rehashed_fixture_substitutions_are_rejected(self):
        for change in ('worker', 'oracle', 'migration', 'seed', 'business'):
            with self.subTest(change=change):
                m = self.measurement_manifest()
                inp = m['inputs']['matching']['fixed']
                if change == 'worker':
                    inp['config']['Scenarios'][WORKLOADS['matching'][1]]['Workers'][0]['Args']['ID'] = '99'
                elif change == 'oracle':
                    inp['config']['Oracle']['Assertions'][0]['SQL'] = 'SELECT 1'
                elif change in ('migration', 'seed'):
                    path = self.root/('migrations/001.sql' if change == 'migration' else 'seed.sql')
                    path.write_text('changed '+change); m['pins'][str(path)] = digest(path)
                else:
                    classes = self.root/'fixture-classes'
                    business = next(classes.rglob('*.class'))
                    business.write_bytes(b'changed, consistently declared business class')
                    jar = self.root/'fixture.jar'; sdk = self.root/'weavegate-spring-constructed.jar'
                    with zipfile.ZipFile(jar, 'w') as archive:
                        archive.write(business, 'BOOT-INF/classes/'+str(business.relative_to(classes)))
                        archive.write(sdk, 'BOOT-INF/lib/'+sdk.name)
                        archive.write(self.root/'spring.jar', 'BOOT-INF/lib/spring.jar')
                    for path in (business, jar): m['pins'][str(path)] = digest(path)
                    for variants in m['inputs'].values():
                        for value in variants.values():
                            value['business_classes'] = {str(business.relative_to(classes)):digest(business)}
                            value['jar_sha256'] = digest(jar)
                for workload, variants in m['inputs'].items():
                    for variant, value in variants.items():
                        path = self.root/(workload+'-'+variant+'.json')
                        write_json(path, config_document(value['config'])); m['pins'][str(path)] = digest(path)
                with self.assertRaisesRegex(ValueError, 'reviewed revision'):
                    validate(m)

    def test_measurement_requires_protocol_domain_rows(self):
        m = self.measurement_manifest()
        for workload in WORKLOADS:
            for variant in ('vulnerable', 'fixed'):
                for rows in (None, [], [{'unexpected': '1'}]):
                    with self.subTest(workload=workload, variant=variant, rows=rows):
                        changed = copy.deepcopy(m)
                        inp = changed['inputs'][workload][variant]
                        if rows is None: inp.pop('expected_domain_rows', None)
                        else: inp['expected_domain_rows'] = rows
                        with self.assertRaisesRegex(ValueError, 'domain.*expectation'):
                            validate(changed)

    def test_saved_clean_exploration_is_not_complete(self):
        root, slot, command, expected = self.product_raw('T-E')
        directory = root/'raw/runs/constructed'
        slot['variant'] = 'fixed'
        manifest = read_json(directory/'manifest.json'); manifest['variant'] = 'fixed'
        observation = read_json(directory/'observation.json')
        observation.update(violation_runs=0, assertion_violations=[])
        merged = read_json(directory/'report.json')
        merged.update(manifest=manifest, observation=observation)
        write_json(directory/'manifest.json', manifest)
        write_json(directory/'observation.json', observation)
        write_json(directory/'report.json', merged)
        result = product(root, slot, dict(command, exit=0), expected)
        self.assertIn('saved exploration schedule lacks discovery evidence', result['quality_reasons'])
        self.assertFalse(result['stable_confirmation'])
        self.assertIsNone(result['first_event_seconds'])

    def test_unconfirmed_discovery_preserves_first_event(self):
        root, slot, command, expected = self.product_raw('T-E')
        directory = root/'raw/runs/constructed'
        confirmed = product(root, slot, command, expected)
        observation = read_json(directory/'observation.json')
        observation.update(violation_runs=0, fingerprints={'b'*64: 2}, flaky=True)
        merged = read_json(directory/'report.json'); merged['observation'] = observation
        write_json(directory/'observation.json', observation)
        write_json(directory/'report.json', merged)
        result = product(root, slot, dict(command, exit=3), expected)
        self.assertIsNotNone(result['first_event_seconds'])
        self.assertEqual(result['first_event_seconds'], confirmed['first_event_seconds'])
        self.assertFalse(result['stable_confirmation'])
        self.assertIn('flaky', result['quality_reasons'])
        observation['violation_runs'] = 1
        write_json(directory/'observation.json', observation)
        write_json(directory/'report.json', dict(merged, observation=observation))
        self.assertIsNone(product(root, slot, dict(command, exit=3), expected)['first_event_seconds'])
        observation['violation_runs'] = 0
        observation['assertion_violations'] = []
        write_json(directory/'observation.json', observation)
        write_json(directory/'report.json', dict(merged, observation=observation))
        self.assertIsNone(product(root, slot, dict(command, exit=3), expected)['first_event_seconds'])

    def test_analysis_requires_executing_frozen_source(self):
        bundle, _ = self.bundle()
        for folder, name, target in (('source', 'analyze.py', 'support.HERE'),
                                     ('source/shared', 'execution.py', 'support.SHARED')):
            with self.subTest(module=name):
                changed = self.root/name
                shutil.copytree(bundle/'frozen'/folder, changed)
                (changed/name).write_text((changed/name).read_text()+'\n# changed analyzer\n')
                # Also patch the old call-site globals to exercise reviewed code.
                with patch(target, changed), patch('analyze.'+target.split('.')[-1], changed):
                    with self.assertRaisesRegex(ValueError, 'source changed'):
                        analyze(bundle, self.root/('output-'+name))
                self.assertFalse((self.root/('output-'+name)).exists())
        command = [sys.executable, '-B', str(bundle/'frozen/source/tool.py'), 'analyze', str(bundle)]
        subprocess.run(command+[str(self.root/'frozen-output')], check=True, capture_output=True)
        analyze(bundle, self.root/'checkout-output')
        self.assertEqual(inventory(self.root/'frozen-output'), inventory(self.root/'checkout-output'))

    def test_activation_signals_leave_analyzable_stop(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            for unsupported in (False, True):
                for boundary in ('active-progress', 'initial-state', 'unsupported-seal'):
                    if boundary == 'unsupported-seal' and not unsupported: continue
                    with self.subTest(signal=signum, unsupported=unsupported, boundary=boundary):
                        m = self.manifest()
                        if unsupported: m['unsupported'] = {'matching/B2': UNSUPPORTED}
                        write_json(self.root/'input.json', m)
                        bundle = self.root/'activation'
                        freeze(self.root/'input.json', bundle)
                        from tool import save_progress as real_progress, write_json as real_write, seal as real_seal
                        signaled, calls = False, []
                        def interrupt():
                            nonlocal signaled
                            if not signaled:
                                signaled = True
                                os.kill(os.getpid(), signum)
                        def progress(root, consumed, **kwargs):
                            real_progress(root, consumed, **kwargs)
                            if boundary == 'active-progress' and kwargs.get('active'): interrupt()
                        def write(path, value):
                            real_write(path, value)
                            if boundary == 'initial-state' and path.name == 'slot.json': interrupt()
                        def sealing(path):
                            if boundary == 'unsupported-seal' and path.resolve().parent == bundle/'slots': interrupt()
                            real_seal(path)
                        previous = signal.getsignal(signal.SIGTERM)
                        with patch('tool.save_progress', side_effect=progress), patch('tool.write_json', side_effect=write), \
                                patch('tool.seal', side_effect=sealing), patch('tool.execute', side_effect=lambda *a: calls.append(a)):
                            try: collect(bundle, max_slots=1)
                            except KeyboardInterrupt: pass
                        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
                        self.assertTrue(signaled)
                        self.assertFalse(calls)
                        result = analyze(bundle, self.root/('activation-output-'+str(signum)+'-'+str(unsupported)+'-'+boundary))
                        self.assertIn('interrupted', result['stopped']['reason'])
                        self.assertIn('collector interrupted', result['slots'][0]['reasons'])
                        self.assertIsNone(read_json(bundle/'progress/checkpoint.json')['active'])
                        self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                        (bundle/'stopped.json').unlink()
                        with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, max_slots=1)
                        shutil.rmtree(bundle)

    def test_sigterm_stops_running_process_and_seals_cleanup(self):
        from execution import group_running
        for label in ('run', 'prepare'):
            with self.subTest(label=label):
                m = self.measurement_manifest()
                program = self.root/'interrupt-child.py'
                address = str(self.root/'ready.sock')
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(address); listener.listen(1); listener.settimeout(30)
                program.write_text("import os,signal,socket\n"
                                   + "s=socket.socket(socket.AF_UNIX);s.connect("+repr(address)+")\n"
                                   + "s.sendall(str(os.getpgrp()).encode());s.close()\nsignal.pause()\n")
                m['pins'][str(program)] = digest(program)
                if label == 'run':
                    m['commands']['matching']['fixed']['B2']['argv'][1] = str(program)
                else:
                    m['hooks']['prepare']['argv'][1] = str(program)
                bundle, registration = self.measurement_bundle(m)
                write_json(self.root/'reference.json', {'fixtures':self.fixture_reference, 'build':self.library_reference})
                probe = ('import support,sys; from tool import collect; '
                         'reference=support.read_json(sys.argv[1]); support.FIXTURE_INPUTS=reference["fixtures"]; '
                         'support.REFERENCE_BUILD=reference["build"]; collect(sys.argv[2],sys.argv[3])')
                child = subprocess.Popen([sys.executable, '-B', '-c', probe, str(self.root/'reference.json'),
                                          str(bundle), str(registration)], cwd=HERE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                group = None
                try:
                    # Signal the collector after the measured child explicitly
                    # announces readiness; a mount supervisor can be its parent.
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(30)
                        group = int(connection.recv(100))
                    child.send_signal(signal.SIGTERM)
                    _, stderr = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, 0, stderr.decode())
                    self.assertFalse(group_running(group))
                    directory = bundle/'slots'/order()[0]['id']
                    self.assertTrue(read_json(directory/(label+'.command.json'))['interrupted'])
                    self.assertTrue(read_json(directory/'slot.json')['cleanup_proven'])
                    result = analyze(bundle, self.root/('sigterm-output-'+label))
                    self.assertIn('interrupted', result['stopped']['reason'])
                    self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                    with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, registration)
                finally:
                    if group is not None:
                        try: os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError: pass
                    if child.poll() is None: child.kill()
                    child.communicate(timeout=30)
                    listener.close(); Path(address).unlink()
                    shutil.rmtree(bundle)

    def write_measurement_configs(self, m):
        # Constructed wire documents use Go's YAML field names, while preserving
        # user-owned scenario and argument keys.
        fields = {'Run':'run', 'Repeat':'repeat', 'ArriveTimeoutMS':'arrive_timeout_ms',
                  'ExplorePasses':'explore_passes', 'Target':'target', 'DB':'db', 'SUT':'sut',
                  'External':'external', 'Capacity':'capacity', 'StartupTimeoutMS':'startup_timeout_ms',
                  'CancelTimeoutMS':'cancel_timeout_ms', 'StopTimeoutMS':'stop_timeout_ms',
                  'Java':'java', 'JAR':'jar', 'Schema':'schema', 'Migrations':'migrations',
                  'Seed':'seed', 'Oracle':'oracle', 'Assertions':'assertions', 'ID':'id',
                  'Workers':'workers', 'Command':'command', 'SyncPoints':'sync_points', 'Variant':'variant'}
        def wire(value):
            if isinstance(value, list): return [wire(v) for v in value]
            if not isinstance(value, dict): return value
            return {('scenarios' if k == 'Scenarios' else 'args' if k == 'Args' else fields[k]):
                    ({name:wire(v) for name,v in item.items()} if k == 'Scenarios'
                     else item if k == 'Args' else wire(item)) for k,item in value.items()}
        for workload, variants in m['inputs'].items():
            for variant, inp in variants.items():
                path = self.root/(workload+'-'+variant+'.json')
                write_json(path, wire(inp['config']))
                m['pins'][str(path)] = digest(path)
                for command in m['commands'][workload][variant].values():
                    command['argv'] += ['--config', str(path)]

    def measurement_manifest(self):
        m=self.manifest()
        m['phase']='measurement'
        m['fixture_revision']='e1bd8ba3fc07d024b8cb4037aa2263d24044dbb0'
        identity=digest(sys.executable)
        classes = self.root/'fixture-classes'; classes.mkdir(exist_ok=True)
        business = classes/'io/github/weavegate/fixture/constructed/Business.class'
        business.parent.mkdir(parents=True, exist_ok=True); business.write_bytes(b'constructed business class')
        sdk = self.root/'weavegate-spring-constructed.jar'
        with zipfile.ZipFile(sdk, 'w') as archive:
            archive.writestr('io/github/weavegate/sdk/Weavegate.class', b'constructed SDK class')
        library = self.root/'spring.jar'
        with zipfile.ZipFile(library, 'w') as archive:
            archive.writestr('org/springframework/Constructed.class', b'constructed library class')
        jar = self.root/'fixture.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.write(business, 'BOOT-INF/classes/'+str(business.relative_to(classes)))
            archive.write(sdk, 'BOOT-INF/lib/'+sdk.name)
            archive.write(library, 'BOOT-INF/lib/'+library.name)
        for p in (business,sdk,library,jar): m['pins'][str(p)] = digest(p)
        m['candidate']={'cli_sha256':identity,'sdk_sha256':digest(sdk),'source_revision':'a'*40}
        m['e1_registration']=dict(m['candidate'],url='https://example.com/constructed',revision='constructed')
        m['environment']={'host_id':'constructed','vm_image':'constructed','cpu_model':'constructed',
            'kernel':'constructed','docker':'constructed','background_policy':'constructed',
            'mysql_image':'mysql:8.4@sha256:'+'a'*64,'vcpus':4,'memory_bytes':16*1024**3}
        self.write_prerequisites(m)
        migrations = self.root/'migrations'; migrations.mkdir(exist_ok=True)
        (migrations/'001.sql').write_text('constructed migration')
        seed = self.root/'seed.sql'; seed.write_text('constructed seed')
        for p in [*files(migrations), seed]: m['pins'][str(p)] = digest(p)
        for values in m['inputs'].values():
            for inp in values.values():
                inp['counts']={'B1':20,'B2':100,'T-R':20};inp['sdk_sha256']=digest(sdk)
                inp['business_classes']={str(business.relative_to(classes)):digest(business)}
                inp['jar_sha256']=digest(jar)
                inp['config']['Target']['Schema'] = {'Migrations':str(migrations), 'Seed':str(seed)}
                inp['config']['Target']['SUT']['External'].update(Java=str(Path(sys.executable).resolve()), JAR=str(jar))
        for workload, variants in m['inputs'].items():
            for variant, inp in variants.items():
                inp['expected_domain_rows'] = EXPECTED_DOMAIN_ROWS[workload]
                inp['config']['Target']['SUT']['Variant'] = variant
                inp['config']['Scenarios'] = {WORKLOADS[workload][1]: {
                    'Workers':[{'ID':'a', 'Command':'constructed', 'Args':{'ID':'42'}}],
                    'SyncPoints':['read','write']}}
        self.write_measurement_configs(m)
        # An independent constructed authority keeps lifecycle tests small;
        # mutation tests below must not update this reference with the manifest.
        for workload, variants in m['inputs'].items():
            inp = variants['vulnerable']
            self.fixture_reference[workload] = {
                'config': normalized_fixture_config(inp['config']),
                'migrations': inventory(migrations), 'seed_sha256': digest(seed),
                'business_classes': copy.deepcopy(inp['business_classes'])}
            self.library_reference['libraries'][workload] = {p.name:digest(p) for p in (library,sdk)}
        for variants in m['commands'].values():
            for commands in variants.values():
                for command in commands.values():
                    command['argv'] += ['--out', '{trial_dir}/raw']
                for arm in ('B0','B1','B2'):
                    commands[arm]['argv'] += ['--classpath', os.pathsep.join(map(str,[classes,sdk,library]))]
        hook=self.root/'hook.py'
        hook.write_text("""import sys,json,hashlib
from pathlib import Path
stage,directory,arm=sys.argv[1:]
p=Path(directory);(p/'proof').mkdir(exist_ok=True)
if stage=='cleanup' and arm=='T-R': raise SystemExit(1)
f=p/'proof'/(stage+'.json');f.write_text(json.dumps({'constructed':True,'stage':stage}))
v={'evidence':{str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest()}}
if stage=='prepare':
 v.update(host_id='constructed',disk_image='constructed',fresh_memory=True,clean_data=True,vcpus=4,memory_bytes=16*1024**3)
if stage=='observe':
 v.update(host_id='constructed',resources={k:{'value':None,'reason':'constructed counter unavailable'}
          for k in ('cpu_seconds','memory_bytes','container_creations','evidence_bytes','cache_bytes')})
if stage=='cleanup':v.update(proven=True)
if stage=='warm_cleanup':
 v.update(outputs_removed=True,data_removed=True,cache_preserved=True,startup_caches=[],
          warmup_command_sha256=hashlib.sha256((p/'warmup/run.command.json').read_bytes()).hexdigest())
name={'prepare':'preparation','observe':'observation','cleanup':'cleanup','warm_cleanup':'warm-cleanup'}[stage]
(p/(name+'.json')).write_text(json.dumps(v))
""")
        executable=str(Path(sys.executable).resolve())
        m['pins'][str(hook)]=digest(hook)
        m['hooks']={k:{'argv':[executable,str(hook),k,'{trial_dir}','{arm}'],
            'cwd':str(self.root),'env':{},'timeout_seconds':10} for k in ('prepare','observe','cleanup','warm_cleanup')}
        m['hooks']['cleanup']['execution_pins']=[executable,str(hook)]
        return m

    def write_prerequisites(self,m):
        directory = self.root/'prerequisites'; directory.mkdir(exist_ok=True)
        m['prerequisites'] = {}
        for kind in PREREQUISITES:
            base = directory/kind
            command, stdout, results, record = [str(base)+suffix for suffix in ('.command.json','.stdout','.results.json','.json')]
            value = {'schema':1, 'kind':kind, 'candidate':m['candidate'],
                     'fixture_revision':m['fixture_revision'], 'command':command, 'stdout':stdout}
            argv = ['constructed-command']
            markers = prerequisite_markers(kind)
            if kind in ('go_acceptance','java_acceptance','paired_acceptance'):
                target = kind.split('_')[0]
                argv = ['python3','check-external-sut-acceptance.py','--results',results,'--require-complete']
                markers = ['EXTERNAL_SUT_ACCEPTANCE_RESULT target='+target+' manifest=valid acceptance=complete pass=1 fail=0 incomplete=0']
                value['results'] = results
            Path(stdout).write_text('\n'.join(markers)+'\n')
            write_json(command,{'argv':argv,'started':True,'finished':True,'interrupted':False,
                                'exit':0,'error':None,'timeout':False,'containment_proven':True})
            if 'results' in value:
                write_json(results,{'target':target,
                    'run':{'revision':m['candidate']['source_revision'],'repetitions':20,
                           'artifacts':{'stdout':{'path':Path(stdout).name,'sha256':digest(stdout)}}},
                    'results':{'constructed':{'status':'pass','checks':{'check':{'status':'pass'}}}}})
            write_json(record,value)
            m['prerequisites'][kind] = record
        for path in files(directory): m['pins'][str(path)] = digest(path)

    def measurement_bundle(self, m=None):
        m = m or self.measurement_manifest()
        write_json(self.root/'measurement.json',m)
        bundle=self.root/'measurement';freeze(self.root/'measurement.json',bundle)
        registration={'manifest_sha256':digest(bundle/'frozen/manifest.json'),
            'order_sha256':digest(bundle/'frozen/order.json'),'registered_at':'2020-01-01T00:00:00Z',
            'public_url':'https://example.com/constructed','revision':'constructed'}
        write_json(self.root/'registration.json',registration)
        return bundle, self.root/'registration.json'

    def test_measurement_cleanup_allows_next_slot_then_quarantines(self):
        bundle, registration = self.measurement_bundle()
        collect(bundle, registration)
        self.assertEqual(len(list((bundle/'slots').iterdir())),2)
        self.assertTrue((bundle/'stopped.json').exists())
        self.assertTrue(read_json(bundle/'slots'/order()[0]['id']/'slot.json')['cleanup_proven'])
        self.assertFalse(read_json(bundle/'slots'/order()[1]['id']/'slot.json')['cleanup_proven'])

    def test_cleanup_proof_cannot_override_uncontained_process(self):
        from tool import execute as real_execute
        parent = self.root
        for failed_step in ('prepare', 'run', 'observe', 'cleanup'):
            with self.subTest(step=failed_step):
                self.root = parent/failed_step; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                calls = []
                def uncontained(spec, context, directory, label, secrets):
                    calls.append(label)
                    result = real_execute(spec, context, directory, label, secrets)
                    if label == failed_step:
                        # The actual child is already reaped. Model an owned
                        # process whose TERM/KILL containment could not be proved.
                        result.update(containment_proven=False, error='constructed containment failure')
                        write_json(directory/(label+'.command.json'), result)
                    return result
                with patch('tool.execute', side_effect=uncontained): collect(bundle, registration)
                self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                if failed_step in ('prepare', 'run'): self.assertNotIn('observe', calls)
                if failed_step == 'prepare': self.assertNotIn('run', calls)
                result = analyze(bundle, self.root/('uncontained-'+failed_step))
                self.assertIn('containment unproven', result['stopped']['reason'])
                self.assertIn(failed_step, result['stopped']['reason'])
                self.assertTrue(any('containment unproven' in r for r in result['slots'][0]['reasons']))
                with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, registration)
                shutil.rmtree(bundle)

    def test_malformed_hook_command_cannot_bypass_containment_checkpoint(self):
        from tool import execute as real_execute
        bundle, registration = self.measurement_bundle()
        def execute(spec, context, directory, label, secrets):
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'observe':
                receipt = dict(result); del receipt['started']
                write_json(directory/(label+'.command.json'), receipt)
            return result
        with patch('tool.execute', side_effect=execute): collect(bundle, registration)
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
        self.assertIn('containment unproven: observe', result['stopped']['reason'])
        self.assertTrue(result['slots'][0]['allocation_cleanup_proven'])

    def test_stable_confirmation_requires_all_violating_replays(self):
        parent = self.root
        for defect in ('violation_count', 'replay_count', 'trace'):
            with self.subTest(defect=defect):
                self.root = parent/defect; self.root.mkdir()
                root, slot, command, expected = self.product_raw('T-E', repeat=20)
                directory = root/'raw/runs/constructed'
                observation = read_json(directory/'observation.json')
                manifest = read_json(directory/'manifest.json')
                if defect == 'violation_count':
                    observation['violation_runs'] = 7
                elif defect == 'replay_count':
                    # Drop the last confirmation with its six child stages.
                    manifest['timings']['spans'] = manifest['timings']['spans'][:-7]
                else:
                    trace = read_json(directory/'trace.json')
                    trace['terminals'][1]['worker'] = 'w1'
                    write_json(directory/'trace.json', trace)
                write_json(directory/'observation.json', observation)
                write_json(directory/'manifest.json', manifest)
                merged = read_json(directory/'report.json')
                merged.update(observation=observation, manifest=manifest)
                write_json(directory/'report.json', merged)
                result = product(root, slot, command, expected)
                self.assertTrue(result['quality_reasons'])
                self.assertFalse(result['stable_confirmation'])
                shutil.rmtree(root)

    def test_malformed_raw_evidence_preserves_all_slot_accounting(self):
        parent = self.root
        for defect in ('baseline_object', 'fingerprints', 'timings'):
            with self.subTest(defect=defect):
                self.root = parent/defect; self.root.mkdir()
                m = self.manifest()
                if defect == 'baseline_object':
                    slot = order()[0]
                    root, _, _ = self.raw(slot)
                    write_json(root/'raw/run.json', [])
                else:
                    root, _, _, expected = self.product_raw('T-E')
                    slot = order()[2]
                    m['inputs'][slot['workload']][slot['variant']] = expected
                    directory = root/'raw/runs/constructed'
                    manifest = read_json(directory/'manifest.json'); manifest['variant'] = slot['variant']
                    observation = read_json(directory/'observation.json')
                    if defect == 'fingerprints': observation['fingerprints'] = None
                    else: manifest['timings'] = None
                    merged = read_json(directory/'report.json')
                    merged.update(manifest=manifest, observation=observation)
                    for name, value in (('manifest',manifest), ('observation',observation), ('report',merged)):
                        write_json(directory/(name+'.json'), value)
                m['unsupported'] = {s['workload']+'/'+s['arm']: UNSUPPORTED
                                    for s in order()[:slot['ordinal']-1]}
                write_json(self.root/'input.json', m)
                bundle = self.root/'bundle'; freeze(self.root/'input.json', bundle)
                consumed = {}
                for prior in order()[:slot['ordinal']-1]:
                    path = bundle/'slots'/prior['id']; path.mkdir()
                    write_json(path/'slot.json', {'slot':prior, 'unsupported':UNSUPPORTED})
                    seal(path); consumed[prior['id']] = digest(path/'SHA256SUMS')
                write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':True}); seal(root)
                retained = bundle/'slots'/slot['id']; root.rename(retained)
                consumed[slot['id']] = digest(retained/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/'analysis')
                self.assertEqual(len(result['slots']), 600)
                row = result['slots'][slot['ordinal']-1]
                self.assertEqual(row['status'], 'operational_failed')
                self.assertTrue(any('missing/invalid evidence' in reason for reason in row['reasons']))

    def test_analysis_output_is_sealed_and_tamper_evident(self):
        bundle, _ = self.bundle()
        output = self.root/'analysis'; analyze(bundle, output)
        verify(output)
        self.assertEqual(set(read_json(output/'SHA256SUMS')), {'results.json', 'report.md'})
        again = self.root/'again'; analyze(bundle, again)
        self.assertEqual(inventory(output), inventory(again))
        for name in ('results.json', 'report.md'):
            path = output/name; original = path.read_bytes()
            path.write_bytes(original+b'\n')
            with self.assertRaisesRegex(ValueError, 'checksum'): verify(output)
            path.write_bytes(original)

    def test_new_directory_replacement_before_open_never_adopts_existing_tree(self):
        parent = self.root
        for operation in ('collect', 'analysis', 'analysis-parent'):
            with self.subTest(operation=operation):
                self.root = parent/operation; self.root.mkdir()
                bundle, _ = self.bundle()
                output = self.root/'analysis'
                target = bundle/'slots'/order()[0]['id'] if operation == 'collect' else output
                if operation == 'analysis-parent': output = target/'nested'
                victim = self.root/'victim'; victim.mkdir()
                for name in ('slot.json', 'results.json', 'report.md', 'SHA256SUMS'):
                    (victim/name).write_text('must remain unchanged\n')
                before = inventory(victim)
                real_open = os.open
                real_publish = publish_directory
                swapped = False
                def swap():
                    nonlocal swapped
                    swapped = True
                    target.rename(self.root/'displaced')
                    victim.rename(target)
                def opened(path, flags, *args, **kwargs):
                    if not swapped and Path(path).name == target.name and target.is_dir():
                        swap()
                    return real_open(path, flags, *args, **kwargs)
                def published(parent_fd, source, destination):
                    real_publish(parent_fd, source, destination)
                    if not swapped and destination == target.name: swap()
                with patch('os.open', side_effect=opened), patch('support.publish_directory', side_effect=published):
                    if operation == 'collect':
                        with self.assertRaises(ValueError): collect(bundle, max_slots=1)
                    else:
                        with self.assertRaises(ValueError): analyze(bundle, output)
                self.assertTrue(swapped)
                self.assertEqual(inventory(target), before)
                if operation == 'collect':
                    self.assertEqual(read_json(bundle/'progress/checkpoint.json')['active'], order()[0]['id'])

    def retained_product_bundle(self, arm='T-E', variant='vulnerable'):
        root, _, _, expected = self.product_raw(arm)
        slot = next(s for s in order() if s['arm'] == arm and s['variant'] == variant
                    and s['workload'] == 'matching')
        directory = root/'raw/runs/constructed'
        product_manifest = read_json(directory/'manifest.json'); product_manifest['variant'] = variant
        merged = read_json(directory/'report.json'); merged['manifest'] = product_manifest
        write_json(directory/'manifest.json', product_manifest); write_json(directory/'report.json', merged)
        m = self.manifest()
        m['inputs']['matching'][variant] = expected
        write_json(self.root/'input.json', m)
        bundle = self.root/'bundle'; freeze(self.root/'input.json', bundle)
        consumed = {}
        for prior in order()[:slot['ordinal']-1]:
            path = bundle/'slots'/prior['id']; path.mkdir()
            write_json(path/'slot.json', {'slot':prior, 'cleanup_proven':True, 'error':'constructed preparation failure'})
            seal(path); consumed[prior['id']] = digest(path/'SHA256SUMS')
        write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':True}); seal(root)
        retained = bundle/'slots'/slot['id']; root.rename(retained)
        consumed[slot['id']] = digest(retained/'SHA256SUMS'); save_progress(bundle, consumed)
        return bundle, retained, slot, consumed

    def test_fixed_product_detection_survives_expected_outcome_failure(self):
        parent = self.root
        for arm in ('T-R', 'T-E'):
            with self.subTest(arm=arm):
                self.root = parent/arm; self.root.mkdir()
                bundle, root, slot, consumed = self.retained_product_bundle(arm, 'fixed')
                state = read_json(root/'slot.json'); state['evidence_received_seconds'] = [2, 2]
                write_json(root/'slot.json', state); seal(root)
                consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/'analysis')
                row = result['slots'][slot['ordinal']-1]
                self.assertEqual(row['status'], 'quality_failed')
                self.assertIn('fixed domain violation', row['reasons'])
                self.assertEqual(row['usable_evidence_seconds'], [2, 2])
                cell = next(c for c in result['cells'] if all(c[k] == slot[k]
                            for k in ('workload','variant','cache','arm')))
                self.assertEqual(cell['observed_detections'], 1)
                self.assertEqual(cell['statuses']['complete'], 0)
                directory = root/'raw/runs/constructed'
                originals = {name:read_json(directory/(name+'.json'))
                             for name in ('manifest', 'observation', 'trace', 'report')}
                for defect in ('rows', 'trace', 'stage', 'count', 'receipt'):
                    with self.subTest(defect=defect):
                        documents = copy.deepcopy(originals)
                        current_state = dict(state)
                        if defect == 'rows':
                            documents['observation']['assertion_violations'][0]['rows'] = [{'wrong':1}]
                        elif defect == 'trace': documents['trace']['events'] = []
                        elif defect == 'stage':
                            next(s for s in documents['manifest']['timings']['spans']
                                 if s['name'] == 'fixture_reset')['name'] = 'invalid_stage'
                        elif defect == 'count': documents['observation']['violation_runs'] = 1
                        else: current_state['evidence_received_seconds'] = [0, 0]
                        documents['report'].update(manifest=documents['manifest'], observation=documents['observation'])
                        for name, value in documents.items(): write_json(directory/(name+'.json'), value)
                        write_json(root/'slot.json', current_state); seal(root)
                        row = reduce_slot(root, slot, read_json(bundle/'frozen/manifest.json'))
                        self.assertIsNone(row['usable_evidence_seconds'])
                        self.assertNotEqual(row['status'], 'complete')

    def test_exhausted_search_rejects_phantom_fingerprints(self):
        root, slot, command, expected = self.product_raw('T-E', exhausted=True)
        directory = root/'raw/runs/constructed'
        manifest = self.manifest(); manifest['inputs']['matching']['vulnerable'] = expected
        self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'complete')
        original = read_json(directory/'observation.json')
        for change in ({'fingerprints':{'a'*64:2}}, {'discovery_fingerprint':'b'*64},
                       {'discovery_fingerprint':''}, {'discovery_fingerprint':False}):
            with self.subTest(change=change):
                observation = dict(original, **change)
                merged = read_json(directory/'report.json'); merged['observation'] = observation
                write_json(directory/'report.json', merged); write_json(directory/'observation.json', observation)
                seal(root); result = reduce_slot(root, slot, manifest)
                self.assertEqual(result['status'], 'quality_failed')
                self.assertIn('exhausted search contains confirmation/discovery fingerprints', result['reasons'])
                self.assertFalse(result['unexpected_miss'])
                self.assertFalse(result['stable_confirmation'])

    def test_confirmation_aggregate_survives_later_operational_failures(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        for failure in ('observation failed', 'cleanup failed'):
            with self.subTest(failure=failure):
                state = read_json(root/'slot.json'); state['error'] = failure
                write_json(root/'slot.json', state); seal(root)
                consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/failure)
                row = result['slots'][slot['ordinal']-1]
                self.assertEqual(row['status'], 'operational_failed')
                self.assertTrue(row['stable_confirmation'])
                cell = next(c for c in result['cells'] if all(c[k] == slot[k]
                            for k in ('workload','variant','cache','arm')))
                self.assertEqual(cell['stable_confirmations'], 1)
                self.assertEqual(cell['statuses']['complete'], 0)

    def test_timely_product_detection_survives_later_hook_failures(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        for failure in ('observation failed', 'cleanup failed'):
            with self.subTest(failure=failure):
                state = read_json(root/'slot.json')
                state.update(error=failure, evidence_received_seconds=[2, 2])
                write_json(root/'slot.json', state); seal(root)
                consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/failure)
                row = result['slots'][slot['ordinal']-1]
                self.assertEqual(row['status'], 'operational_failed')
                self.assertEqual(row['usable_evidence_seconds'], [2, 2])
                cell = next(c for c in result['cells'] if all(c[k] == slot[k]
                            for k in ('workload','variant','cache','arm')))
                self.assertEqual(cell['observed_detections'], 1)
                self.assertEqual(cell['statuses']['complete'], 0)

    def test_exploration_cost_uses_actual_search_and_confirmation_count(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        result = analyze(bundle, self.root/'cost')
        row = result['slots'][slot['ordinal']-1]
        cell = next(c for c in result['cells'] if all(c[k] == slot[k]
                    for k in ('workload','variant','cache','arm')))
        count = row['exploration_executions'] + 2
        self.assertEqual(cell['amortized_execution_seconds'], stats([1/count]))
        self.assertEqual(row['evaluated_executions'], count)
        self.assertIn('Descriptive cost per evaluated execution', (self.root/'cost/report.md').read_text())

    def test_clean_product_timeout_without_published_run_is_budget_exhausted(self):
        root, slot, command, expected = self.product_raw('T-E')
        manifest = self.manifest(); manifest['inputs']['matching']['vulnerable'] = expected
        shutil.rmtree(root/'raw')
        command.update(timeout=True, exit=130, seconds=slot['cap_seconds']+1)
        write_json(root/'run.command.json', command); seal(root)
        result = reduce_slot(root, slot, manifest)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertIsNone(result['first_event_seconds'])
        state = read_json(root/'slot.json'); state['cleanup_proven'] = False
        write_json(root/'slot.json', state); seal(root)
        self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')
        state['cleanup_proven'] = True; write_json(root/'slot.json', state)
        (root/'run.stderr').write_text('weavegate: warning: cleanup failed: constructed\n')
        seal(root)
        self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')
        (root/'run.stderr').unlink()
        # A partial published run and unrelated operational exits still fail.
        for partial, code in ((False, 5), (True, 130)):
            with self.subTest(partial=partial, code=code):
                if partial: (root/'raw/runs/incomplete').mkdir(parents=True)
                command['exit'] = code; write_json(root/'run.command.json', command); seal(root)
                self.assertEqual(reduce_slot(root, slot, manifest)['status'], 'operational_failed')

    def test_replay_rejects_consistent_exploration_evidence(self):
        root, slot, command, expected = self.product_raw('T-E')
        directory = root/'raw/runs/constructed'
        slot['arm'] = 'T-R'
        manifest = self.manifest(); manifest['inputs']['matching']['vulnerable'] = expected
        original = read_json(directory/'observation.json')
        for counts in ({}, {'schedules_explored':0}, {'explore_passes':0}):
            with self.subTest(counts=counts):
                observation = dict(original, mode='replay', **counts)
                merged = read_json(directory/'report.json'); merged['observation'] = observation
                write_json(directory/'report.json', merged)
                write_json(directory/'observation.json', observation)
                write_json(root/'slot.json', {'slot':slot, 'cleanup_proven':True}); seal(root)
                result = reduce_slot(root, slot, manifest)
                self.assertEqual(result['status'], 'operational_failed')
                self.assertIn('replay contains exploration', ' '.join(result['reasons']))
                self.assertIsNone(result['usable_evidence_seconds'])

    def test_baseline_domain_endpoint_precedes_secondary_assertion(self):
        root, slot, command = self.raw()
        expected = self.manifest()['inputs']['quota']['vulnerable']
        assertion = WORKLOADS['quota'][2]
        expected['config']['Oracle']['Assertions'].append({'ID':'receipt'})
        for path in (root/'raw').glob('execution-*.json'):
            value = read_json(path)
            value['oracle_end_ns'] = 800_000_000
            value['oracle_ends_ns'] = {assertion: 100_000_000, 'receipt': 800_000_000}
            value['evaluation']['results'].append({'oracle_id':'receipt', 'violations':[]})
            write_json(path, value)
        result = baseline(root, slot, command, expected)
        self.assertEqual(result['first_event_seconds'], bound(.1, .9, 1))
        path = root/'raw/execution-001.json'
        value = read_json(path)
        for invalid in ({assertion: 900_000_000, 'receipt': 800_000_000},
                        {assertion: 800_000_000, 'receipt': 100_000_000},
                        {assertion: True, 'receipt': 800_000_000}, {assertion: 100_000_000}):
            with self.subTest(invalid=invalid):
                value['oracle_ends_ns'] = invalid; write_json(path, value)
                with self.assertRaisesRegex(ValueError, 'per-assertion completion'):
                    baseline(root, slot, command, expected)

    def test_analysis_publishes_registered_deviations(self):
        m = self.manifest()
        m['deviations'] = ['No VM restoration.', 'Cache state <unestablished>.']
        write_json(self.root/'input.json', m)
        bundle = self.root/'bundle'; freeze(self.root/'input.json', bundle)
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(result['deviations'], m['deviations'])
        report = (self.root/'analysis/report.md').read_text()
        self.assertIn('No VM restoration.', report)
        self.assertIn('Cache state &lt;unestablished&gt;.', report)

    def test_launch_uses_validated_bytes_after_live_path_replacement(self):
        runtime = self.root/'runtime'; runtime.mkdir()
        executable = runtime/'python'; shutil.copyfile(sys.executable, executable); executable.chmod(0o700)
        program = runtime/'arm.py'
        program.write_text('from pathlib import Path\nprint(Path(__file__).with_name("input.txt").read_text())\n')
        dependency = runtime/'input.txt'; dependency.write_text('registered')
        pins = {str(p):digest(p) for p in (executable, program, dependency)}
        sandbox = str(Path(shutil.which('bwrap')).resolve())
        context = {'redact_env': [], 'execution_pins': pins,
                   'execution_runtime': {'path': sandbox, 'sha256': digest(sandbox)}}
        spec = {'argv': [str(executable), '-B', str(program)], 'cwd': str(runtime),
                'env': {}, 'timeout_seconds': 5}
        real_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            # Replace the entire ancestor after validation, then restore it
            # before the next boundary check. No scheduling delay is needed.
            runtime.rename(self.root/'original-runtime'); runtime.mkdir()
            shutil.copyfile('/bin/false', executable); executable.chmod(0o700)
            program.write_text('print("unregistered")\n'); dependency.write_text('unregistered')
            try:
                process = real_spawn(*args, **kwargs)
                process.wait(timeout=5)
                return process
            finally:
                shutil.rmtree(runtime); (self.root/'original-runtime').rename(runtime)
        directory = self.root/'command'; directory.mkdir()
        with patch('execution.subprocess.Popen', side_effect=spawn):
            result = execute(spec, context, directory, 'run', [])
        live_pins({'pins':pins})
        self.assertTrue(succeeded(result), result)
        self.assertEqual((directory/'run.stdout').read_text(), 'registered\n')

    def test_frozen_launch_timeout_preserves_child_cleanup_exit(self):
        from snapshot import MountedProcess
        program = self.root/'cancel.py'
        address = str(self.root/'ready.sock')
        program.write_text('import signal,socket,sys\n'
                           'signal.signal(signal.SIGTERM, lambda *args: sys.exit(130))\n'
                           's=socket.socket(socket.AF_UNIX);s.connect('+repr(address)+')\n'
                           's.sendall(b"ready");s.close()\nsignal.pause()\n')
        executable = str(Path(sys.executable).resolve())
        spec = {'argv':[executable, str(program)], 'cwd':str(self.root), 'env':{},
                'timeout_seconds':30, 'cancel_grace_seconds':5}
        context = {'redact_env':[], 'execution_runtime':runtime_identity(),
                   'execution_pins':{p:digest(p) for p in spec['argv']}}
        directory = self.root/'run';directory.mkdir()
        real_wait = MountedProcess.wait
        waits = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(address);listener.listen(1);listener.settimeout(30)
            def wait(process, timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(30)
                        self.assertEqual(connection.recv(100), b'ready')
                    raise subprocess.TimeoutExpired(spec['argv'], 30)
                return real_wait(process, timeout)
            with patch.object(MountedProcess, 'wait', wait):
                result = execute(spec, context, directory, 'run', [])
        self.assertTrue(result['timeout'])
        self.assertEqual(result['exit'], 130)
        self.assertTrue(result['containment_proven'])
        self.assertIsNone(result['error'])

    def test_frozen_dependencies_are_readonly_and_directory_inventory_is_closed(self):
        program = self.root/'read.py'
        inputs = self.root/'inputs';inputs.mkdir()
        dependency = inputs/'registered.txt';dependency.write_text('registered')
        program.write_text('from pathlib import Path\n'
                           'p=Path('+repr(str(dependency))+')\n'
                           'try: p.write_text("changed")\n'
                           'except OSError: print("readonly")\n'
                           'print(",".join(sorted(x.name for x in p.parent.iterdir())))\n')
        executable = str(Path(sys.executable).resolve())
        spec = {'argv':[executable,str(program)], 'cwd':str(self.root), 'env':{}, 'timeout_seconds':5}
        context = {'redact_env':[], 'execution_runtime':runtime_identity(), 'execution_directories':[str(inputs)],
                   'execution_pins':{str(p):digest(p) for p in (executable, program, dependency)}}
        real_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            dependency.write_text('unregistered')
            (inputs/'shadow.py').write_text('unregistered')
            return real_spawn(*args, **kwargs)
        directory = self.root/'run';directory.mkdir()
        with patch('execution.subprocess.Popen', side_effect=spawn):
            result = execute(spec, context, directory, 'run', [])
        self.assertTrue(succeeded(result), result)
        self.assertEqual((directory/'run.stdout').read_text(), 'readonly\nregistered.txt\n')

    def test_namespace_startup_failure_retains_spawn_and_unproven_containment(self):
        executable = str(Path(sys.executable).resolve())
        spec = {'argv':[executable,'-c','pass'], 'cwd':str(self.root), 'env':{}, 'timeout_seconds':5}
        context = {'redact_env':[], 'execution_runtime':runtime_identity(),
                   'execution_pins':{executable:digest(executable)}}
        directory = self.root/'run';directory.mkdir()
        with patch('snapshot.json.loads', side_effect=ValueError('constructed namespace receipt failure')):
            result = execute(spec, context, directory, 'run', [])
        self.assertTrue(result['started'])
        self.assertFalse(result['containment_proven'])
        self.assertIn('namespace ownership unproven', result['error'])
        self.assertEqual(read_json(directory/'run.command.json'), result)

    def test_malformed_command_receipts_preserve_all_slot_accounting(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        original = read_json(root/'run.command.json')
        invalid = [[], None, {}, 'bad']
        invalid += [{k:v for k,v in original.items() if k != field} for field in original]
        invalid += [dict(original, **{field:value}) for field, value in
                    (('seconds', True), ('seconds', -1), ('seconds', '1'), ('seconds', 10**400),
                     ('started', 1), ('timeout', 'false'), ('exit', False), ('exit', '0'),
                     ('error', []), ('containment_proven', 1), ('finished', 1), ('interrupted', 0))]
        for index, value in enumerate(invalid):
            with self.subTest(receipt=value):
                write_json(root/'run.command.json', value); seal(root)
                consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/('receipt-%d' % index))
                self.assertEqual(len(result['slots']), 600)
                row = result['slots'][slot['ordinal']-1]
                self.assertEqual(row['status'], 'operational_failed')
                self.assertTrue(any('command receipt' in r for r in row['reasons']))

    def test_publication_never_replaces_existing_empty_directory(self):
        bundle, _ = self.bundle()
        output = self.root/'analysis'
        real_publish = publish_directory
        occupied = []
        def publish(parent, source, destination):
            if destination == output.name:
                output.mkdir()
                occupied.append(output.stat())
            real_publish(parent, source, destination)
        with patch('support.publish_directory', side_effect=publish):
            with self.assertRaises(FileExistsError): analyze(bundle, output)
        self.assertTrue(os.path.samestat(occupied[0], output.stat()))
        self.assertEqual(list(output.iterdir()), [])

    def test_staging_swap_never_writes_to_frozen_inputs(self):
        parent = self.root
        for operation in ('collect', 'analyze'):
            for boundary in ('created', 'opened', 'published'):
                with self.subTest(operation=operation, boundary=boundary):
                    self.root = parent/(operation+'-'+boundary); self.root.mkdir()
                    bundle, _ = self.bundle()
                    victim = bundle/'frozen'; before = inventory(victim)
                    real_mkdir, real_open, real_publish = os.mkdir, os.open, publish_directory
                    targets = []
                    def swap(path):
                        path.rename(self.root/'displaced')
                        victim.rename(path)
                        targets.append(path)
                    def mkdir(path, *args, **kwargs):
                        real_mkdir(path, *args, **kwargs)
                        if not targets and boundary == 'created' and Path(path).name.startswith('.e5-new-'):
                            swap(Path(path))
                    def opened(path, flags, *args, **kwargs):
                        if not targets and boundary == 'opened' and Path(path).name.startswith('.e5-new-'):
                            swap(Path('/proc/self/fd')/str(kwargs['dir_fd'])/path)
                        return real_open(path, flags, *args, **kwargs)
                    def publish(parent, source, destination):
                        if not targets and boundary == 'published':
                            swap(Path('/proc/self/fd')/str(parent)/source)
                        real_publish(parent, source, destination)
                        if targets and boundary == 'published': targets[-1] = targets[-1].with_name(destination)
                    with patch('os.mkdir', side_effect=mkdir), patch('os.open', side_effect=opened), \
                         patch('support.publish_directory', side_effect=publish):
                        with self.assertRaises(ValueError):
                            if operation == 'collect': collect(bundle, max_slots=1)
                            else: analyze(bundle, self.root/'analysis')
                    self.assertEqual(len(targets), 1)
                    # Resolve the moved tree after retained parent FDs have closed.
                    moved = next(p.parent for p in self.root.rglob('protocol.md') if p != self.protocol)
                    self.assertEqual(inventory(moved), before)

    def test_lifecycle_entry_cannot_reacquire_substituted_slot(self):
        from tool import execute as real_execute, Monitor as RealMonitor
        parent = self.root
        for phase in ('prepare', 'run', 'observe', 'cleanup', 'monitor'):
            with self.subTest(phase=phase):
                self.root = parent/phase; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                frozen = bundle/'frozen'; before = inventory(frozen)
                targets = []
                def swap(directory):
                    directory.rename(self.root/'displaced-slot')
                    frozen.rename(directory)
                    targets.append(directory)
                def execute(spec, context, directory, step, secrets):
                    if step == phase: swap(directory)
                    return real_execute(spec, context, directory, step, secrets)
                def monitor(directory, *args, **kwargs):
                    if phase == 'monitor': swap(directory)
                    return RealMonitor(directory, *args, **kwargs)
                with patch('tool.execute', side_effect=execute), patch('tool.Monitor', side_effect=monitor):
                    with self.assertRaises((ValueError, FileNotFoundError)): collect(bundle, registration)
                self.assertEqual(len(targets), 1)
                self.assertEqual(inventory(targets[0]), before)
                self.assertEqual(read_json(bundle/'progress/checkpoint.json')['active'], order()[0]['id'])

    def test_warmup_publication_does_not_adopt_frozen_directory(self):
        real_mkdir, real_publish = Path.mkdir, publish_directory
        originals, targets = [], []
        def swap(directory):
            frozen = self.root/'measurement/frozen'
            originals.append(inventory(frozen))
            directory.rename(self.root/'displaced-warmup')
            frozen.rename(directory)
            targets.append(directory)
        def mkdir(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            if path.name == 'warmup': swap(path)
        def publish(parent, source, destination):
            real_publish(parent, source, destination)
            if destination == 'warmup': swap((Path('/proc/self/fd')/str(parent)/destination).resolve())
        with patch.object(Path, 'mkdir', mkdir), patch('support.publish_directory', side_effect=publish):
            with self.assertRaises((ValueError, FileNotFoundError)): self.warm_collection({})
        self.assertEqual(len(targets), 1)
        self.assertEqual(inventory(targets[0]), originals[0])

    def test_invalid_product_counts_cannot_abort_analysis_rendering(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        directory = root/'raw/runs/constructed'
        original = read_json(directory/'observation.json')
        changes = [{'fingerprints': {'a'*64: 'two'}}, {'fingerprints': {'a'*64: True}},
                   {'repeat': True}, {'schedules_explored': -1}, {'violation_runs': '2'},
                   {'flaky': 0}, {'explore_passes': 1.0}]
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                observation = dict(original, **change)
                merged = read_json(directory/'report.json'); merged['observation'] = observation
                write_json(directory/'observation.json', observation); write_json(directory/'report.json', merged)
                seal(root); consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
                result = analyze(bundle, self.root/('counts-%d' % index))
                self.assertEqual(len(result['slots']), 600)
                self.assertEqual(result['slots'][slot['ordinal']-1]['status'], 'operational_failed')

    def test_quality_failed_confirmations_are_not_aggregated(self):
        bundle, root, slot, consumed = self.retained_product_bundle()
        directory = root/'raw/runs/constructed'
        observation = read_json(directory/'observation.json')
        observation['assertion_violations'][0]['rows'] = [{'unexpected': 1}]
        merged = read_json(directory/'report.json'); merged['observation'] = observation
        write_json(directory/'observation.json', observation); write_json(directory/'report.json', merged)
        seal(root); consumed[slot['id']] = digest(root/'SHA256SUMS'); save_progress(bundle, consumed)
        result = analyze(bundle, self.root/'analysis')
        row = result['slots'][slot['ordinal']-1]
        self.assertEqual(row['status'], 'quality_failed')
        self.assertFalse(row['stable_confirmation'])
        cell = next(c for c in result['cells'] if all(c[k] == slot[k] for k in ('workload','variant','cache','arm')))
        self.assertEqual(cell['stable_confirmations'], 0)

    def test_boundaries_authenticate_prior_slots_against_checkpoint(self):
        from tool import execute as real_execute
        parent = self.root
        for defect in ('delete', 'reseal', 'insert', 'reseal_checkpoint'):
            with self.subTest(defect=defect):
                self.root = parent/defect; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                calls = []
                def mutate(spec, context, directory, label, secrets):
                    calls.append((context['id'], label))
                    result = real_execute(spec, context, directory, label, secrets)
                    if context['id'] == order()[1]['id'] and label == 'prepare':
                        prior = bundle/'slots'/order()[0]['id']
                        if defect == 'delete': shutil.rmtree(prior)
                        elif defect == 'insert':
                            prior = bundle/'slots'/'unregistered'; prior.mkdir(); seal(prior)
                        else:
                            (prior/'injected').write_text('replaced evidence'); seal(prior)
                            if defect == 'reseal_checkpoint':
                                checkpoint = read_json(bundle/'progress/checkpoint.json')
                                checkpoint['slots'][prior.name] = digest(prior/'SHA256SUMS')
                                write_json(bundle/'progress/checkpoint.json', checkpoint); seal(bundle/'progress')
                    return result
                with patch('tool.execute', side_effect=mutate): collect(bundle, registration)
                self.assertNotIn((order()[1]['id'], 'run'), calls)
                self.assertFalse(any(identity == order()[2]['id'] for identity, _ in calls))
                state = read_json(bundle/'slots'/order()[1]['id']/'slot.json')
                self.assertTrue(any('slot' in reason for reason in state['integrity_errors']))

    def test_missing_literal_hook_dependencies_fail_before_freeze(self):
        m = self.measurement_manifest()
        for hook in m['hooks']:
            for path in (str(self.root/'generated.py'), './generated.py', '../generated.py',
                         '--settings='+str(self.root/'missing.json')):
                with self.subTest(hook=hook, path=path):
                    changed = copy.deepcopy(m)
                    changed['hooks'][hook]['argv'].append(path)
                    with self.assertRaisesRegex(ValueError, 'dependency missing'):
                        validate(changed)

    def test_hook_dependencies_are_rechecked_before_warmup(self):
        from tool import proof as real_proof
        def mutate(directory, filename, *args, **kwargs):
            receipt = real_proof(directory, filename, *args, **kwargs)
            if filename == 'preparation.json':
                script = self.root/'hook.py'
                script.write_text(script.read_text()+'\n# changed after preparation\n')
            return receipt
        with patch('tool.proof', side_effect=mutate):
            state, calls = self.warm_collection({})
        self.assertIn('live pin changed', state['error'])
        self.assertFalse(any(label == 'run' for label, _ in calls))

    def test_required_command_timing_cannot_be_missing_or_duplicated(self):
        parent = self.root
        for defect in ('missing', 'duplicate', 'unrooted'):
            with self.subTest(defect=defect):
                self.root = parent/defect; self.root.mkdir()
                root, slot, _, expected = self.product_raw('T-E')
                directory = root/'raw/runs/constructed'
                manifest = read_json(directory/'manifest.json')
                spans = manifest['timings']['spans']
                if defect == 'missing': spans[0]['name'] = 'unidentified_root'
                elif defect == 'duplicate':
                    spans.append(dict(spans[0], id=len(spans)+1))
                else: spans[-1]['parent_id'] = 0
                write_json(directory/'manifest.json', manifest)
                merged = read_json(directory/'report.json'); merged['manifest'] = manifest
                write_json(directory/'report.json', merged); seal(root)
                m = self.manifest(); m['inputs'][slot['workload']][slot['variant']] = expected
                result = reduce_slot(root, slot, m)
                self.assertEqual(result['status'], 'operational_failed')
                self.assertTrue(any('timing' in reason for reason in result['reasons']))

    def test_offline_analysis_binds_frozen_bytes_to_registration(self):
        parent = self.root
        for name in ('manifest.json', 'order.json'):
            with self.subTest(file=name):
                self.root = parent/name; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                registered(bundle, read_json(bundle/'frozen/manifest.json'), registration)
                path = bundle/'frozen'/name
                if name == 'manifest.json':
                    value = read_json(path); value['campaign_id'] = 'substituted'; write_json(path, value)
                else: path.write_bytes(path.read_bytes()+b'\n')  # Same order, different registered bytes.
                seal(bundle/'frozen')
                with self.assertRaisesRegex(ValueError, 'registration.*hash'):
                    analyze(bundle, self.root/'analysis')
                self.assertFalse((self.root/'analysis').exists())

    def test_registered_frozen_bytes_are_checked_at_hook_boundaries(self):
        from tool import execute as real_execute
        bundle, registration = self.measurement_bundle()
        calls = []
        def mutate(spec, context, directory, label, secrets):
            calls.append(label)
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'prepare':
                path = bundle/'frozen/manifest.json'
                value = read_json(path); value['campaign_id'] = 'substituted'; write_json(path, value)
                seal(bundle/'frozen')
            return result
        with patch('tool.execute', side_effect=mutate): collect(bundle, registration)
        self.assertEqual(calls, ['prepare', 'cleanup'])
        state = read_json(bundle/'slots'/order()[0]['id']/'slot.json')
        self.assertIn('registration', state['stop']['reason'])


    def test_frozen_source_inventory_cannot_shrink_or_expand(self):
        bundle, _ = self.bundle()
        frozen = bundle/'frozen'
        originals = {p: p.read_bytes() for p in (frozen/'source').rglob('*.py')}
        for path, data in originals.items():
            with self.subTest(missing=str(path.relative_to(frozen))):
                path.unlink(); seal(frozen)
                with self.assertRaisesRegex(ValueError, 'source inventory'):
                    analyze(bundle, self.root/('analysis-'+path.name))
                self.assertFalse((self.root/('analysis-'+path.name)).exists())
            path.write_bytes(data); seal(frozen)
        added = frozen/'source/injected.py'; added.write_text('# unregistered module\n'); seal(frozen)
        with self.assertRaisesRegex(ValueError, 'source inventory'):
            collect(bundle, max_slots=1)

    def test_hook_source_reseal_stops_before_measurement(self):
        from tool import execute as real_execute
        bundle, registration = self.measurement_bundle()
        calls = []
        def mutate(spec, context, directory, label, secrets):
            calls.append(label)
            result = real_execute(spec, context, directory, label, secrets)
            if label == 'prepare':
                path = bundle/'frozen/source/analyze.py'
                path.write_bytes(path.read_bytes()+b'\n# altered frozen analyzer\n')
                seal(bundle/'frozen')
            return result
        with patch('tool.execute', side_effect=mutate): collect(bundle, registration)
        self.assertEqual(calls, ['prepare', 'cleanup'])
        state = read_json(bundle/'slots'/order()[0]['id']/'slot.json')
        self.assertIn('source changed', state['stop']['reason'])

    def test_active_slot_replacement_never_writes_or_seals_target(self):
        from tool import execute as real_execute
        parent = self.root
        for phase in ('prepare', 'run', 'observe', 'cleanup'):
            for replacement in ('symlink', 'directory'):
                with self.subTest(phase=phase, replacement=replacement):
                    self.root = parent/(phase+'-'+replacement); self.root.mkdir()
                    bundle, registration = self.measurement_bundle()
                    frozen_before = inventory(bundle/'frozen')
                    target = bundle/'frozen'
                    moved = self.root/'original-slot'
                    calls = []
                    def mutate(spec, context, directory, label, secrets):
                        calls.append(label)
                        result = real_execute(spec, context, directory, label, secrets)
                        if label == phase:
                            directory.rename(moved)
                            if replacement == 'symlink': directory.symlink_to(target, target_is_directory=True)
                            else: directory.mkdir()
                        return result
                    with patch('tool.execute', side_effect=mutate):
                        with self.assertRaisesRegex(ValueError, 'directory.*replaced'):
                            collect(bundle, registration)
                    self.assertEqual(inventory(target), frozen_before)
                    checkpoint = read_json(bundle/'progress/checkpoint.json')
                    self.assertEqual(checkpoint['active'], order()[0]['id'])
                    self.assertEqual(checkpoint['slots'], {})
                    self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                    if phase != 'cleanup': self.assertNotIn('cleanup', calls)
                    if replacement == 'directory': self.assertEqual(list((bundle/'slots'/order()[0]['id']).iterdir()), [])

    def test_command_directory_swap_cannot_redirect_final_receipt(self):
        from tool import execute
        root = self.root/'command'; root.mkdir()
        target = self.root/'target'; target.mkdir()
        # The hook preserves receipt bytes at the target, defeating a digest-only check.
        script = self.root/'swap.py'
        script.write_text("""import pathlib, shutil, sys
p, target = map(pathlib.Path, sys.argv[1:])
shutil.copytree(p, target, dirs_exist_ok=True)
p.rename(p.with_name('moved'))
p.symlink_to(target, target_is_directory=True)
(target/'snapshot').write_bytes((target/'run.command.json').read_bytes())
""")
        spec = {'argv':[sys.executable, str(script), str(root), str(target)],
                'cwd':str(self.root), 'env':{}, 'timeout_seconds':5}
        with self.assertRaises(ValueError):
            execute(spec, {'redact_env':[]}, root, 'run', [])
        self.assertEqual((target/'run.command.json').read_bytes(), (target/'snapshot').read_bytes())

    def test_stream_directory_swap_cannot_write_into_target(self):
        from stream import Monitor
        root = self.root/'stream'; root.mkdir()
        target = self.root/'target'; target.mkdir()
        with self.assertRaisesRegex(ValueError, 'directory.*replaced'):
            with Monitor(root):
                root.rename(self.root/'moved')
                root.symlink_to(target, target_is_directory=True)
        self.assertEqual(list(target.iterdir()), [])

    def test_command_timing_must_fit_invocation_and_have_integer_clock(self):
        valid = {'id':1, 'parent_id':0, 'name':'command', 'start_ns':0, 'duration_ns':1000000000}
        self.assertEqual(stages([valid], 1)['remainder_seconds'], 1)
        for seconds, change in ((1, {'duration_ns':2000000000}),
                                (float('inf'), {}), (True, {}),
                                (1, {'start_ns':-1}), (1, {'duration_ns':True}),
                                (1, {'id':True}), (1, {'parent_id':False})):
            with self.subTest(seconds=seconds, change=change):
                with self.assertRaises(ValueError): stages([dict(valid, **change)], seconds)
        root, slot, _, expected = self.product_raw('T-R')
        directory = root/'raw/runs/constructed'
        manifest = read_json(directory/'manifest.json')
        manifest['timings']['spans'][0]['duration_ns'] = 2000000000
        write_json(directory/'manifest.json', manifest)
        merged = read_json(directory/'report.json'); merged['manifest'] = manifest
        write_json(directory/'report.json', merged); seal(root)
        m = self.manifest(); m['inputs'][slot['workload']][slot['variant']] = expected
        result = reduce_slot(root, slot, m)
        self.assertEqual(result['status'], 'operational_failed')
        self.assertTrue(any('timing' in reason for reason in result['reasons']))

    def test_campaign_directory_replacement_cannot_redirect_recovery(self):
        from tool import execute as real_execute
        parent = self.root
        for name in ('slots', 'progress'):
            with self.subTest(directory=name):
                self.root = parent/name; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                frozen_before = inventory(bundle/'frozen')
                def mutate(spec, context, directory, label, secrets):
                    result = real_execute(spec, context, directory, label, secrets)
                    if label == 'prepare':
                        path = bundle/name
                        path.rename(self.root/'original')
                        path.symlink_to(bundle/'frozen', target_is_directory=True)
                    return result
                with patch('tool.execute', side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, 'directory.*replaced'):
                        collect(bundle, registration)
                self.assertEqual(inventory(bundle/'frozen'), frozen_before)

    def test_offline_analysis_rejects_symlinked_consumed_slot(self):
        bundle, m = self.bundle()
        collect(bundle, max_slots=1)
        path = bundle/'slots'/order()[0]['id']
        moved = self.root/'original-slot'; path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'directory.*symlink'):
            analyze(bundle, self.root/'analysis')

    def test_frozen_tool_checks_sources_against_registered_manifest(self):
        bundle, registration = self.measurement_bundle()
        registered(bundle, read_json(bundle/'frozen/manifest.json'), registration)
        path = bundle/'frozen/source/analyze.py'
        path.write_bytes(path.read_bytes()+b'\n# changed source\n'); seal(bundle/'frozen')
        with patch('support.HERE', bundle/'frozen/source'), patch('support.SHARED', bundle/'frozen/source/shared'):
            with self.assertRaisesRegex(ValueError, 'registered source changed'):
                frozen_sources(bundle)
        command = [sys.executable, '-B', str(bundle/'frozen/source/tool.py'), 'analyze', str(bundle), str(self.root/'output')]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('registered source changed', result.stderr)
        self.assertFalse((self.root/'output').exists())

    def test_unsupported_declarations_require_registered_keys_and_issue_reasons(self):
        valid = {'issue_url': 'https://github.com/weavegate/weavegate/issues/190',
                 'reason': 'Constructed launcher boundary requires an engine change'}
        m = self.manifest()
        validate(dict(m, unsupported={'matching/B2': valid}))
        bad = [None, True, [], 'matching/B2', {'missing/B2': valid}, {'matching/B3': valid},
               {'matching/fixed/B2': valid}]
        bad += [{'matching/B2': value} for value in
                (True, False, None, '', 'unsupported', {}, dict(valid, reason=' '),
                 dict(valid, issue_url=''), dict(valid, issue_url='http://example.com/issues/1'),
                 dict(valid, issue_url='https://'), dict(valid, issue_url='https://host/issue with space'),
                 dict(valid, issue_url='https://user:password@host/issues/1'),
                 dict(valid, typo='extra'))]
        for index, value in enumerate(bad):
            with self.subTest(value=value):
                write_json(self.root/'unsupported.json', dict(m, unsupported=value))
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    freeze(self.root/'unsupported.json', self.root/('invalid-'+str(index)))
                self.assertFalse((self.root/('invalid-'+str(index))).exists())

    def test_offline_and_collection_reject_malformed_unsupported_inventory(self):
        bundle, _ = self.bundle()
        path = bundle/'frozen/manifest.json'
        manifest = read_json(path); manifest['unsupported'] = {'matching/B2': True}
        write_json(path, manifest); seal(bundle/'frozen')
        with patch('tool.execute') as execute:
            with self.assertRaisesRegex(ValueError, 'unsupported'): collect(bundle, max_slots=1)
            execute.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'unsupported'): analyze(bundle, self.root/'analysis')
        self.assertFalse((self.root/'analysis').exists())

    def test_collector_evidence_uses_exact_invocation_origin(self):
        from types import SimpleNamespace
        parent = self.root
        for index in (0, 1):  # Baseline and product paths use the same collector clock.
            with self.subTest(arm=order()[index]['arm']):
                self.root = parent/str(index); self.root.mkdir()
                m = self.manifest()
                if index:
                    m['unsupported'] = {'matching/B2': {'issue_url': 'https://example.com/issues/1',
                                                       'reason': 'Constructed prefix'}}
                write_json(self.root/'input.json', m)
                bundle = self.root/'bundle'; freeze(self.root/'input.json', bundle)
                slot = order()[index]
                origin = 11_000_000_000
                endpoint = slot['cap_seconds'] + 1
                command = {'seconds': 1, 'collector_start_ns': origin, 'started': True,
                           'timeout': False, 'exit': 0, 'error': None, 'containment_proven': True,
                           'finished': True, 'interrupted': False}
                def execute(spec, context, directory, label, secrets):
                    write_json(directory/(label+'.command.json'), command)
                    return command
                evidence = {'cleanup_proven': True, 'quality_reasons': [], 'operational_reasons': [],
                            'first_event_seconds': None}
                clock = SimpleNamespace(monotonic_ns=lambda: origin + endpoint * 10**9,
                                        monotonic=iter((10, 11 + endpoint)).__next__)
                with patch('tool.execute', side_effect=execute), patch('tool.time', clock), \
                     patch('analyze.baseline', return_value=evidence), patch('analyze.product', return_value=evidence):
                    collect(bundle, max_slots=index+1)
                    root = bundle/'slots'/slot['id']
                    state = read_json(root/'slot.json')
                    self.assertEqual(state['evidence_received_seconds'], [endpoint, endpoint])
                    result = reduce_slot(root, slot, m)
                self.assertEqual(result['usable_evidence_membership'], 'late')

    def test_stream_receipts_use_exact_collector_origin_and_valid_bounds(self):
        root, slot, command = self.raw()
        command['collector_start_ns'] = 20_000_000_000
        expected = self.manifest()['inputs']['quota']['vulnerable']
        stream = {'clock': 'collector-monotonic', 'start_monotonic_ns': 19_000_000_000,
                  'envelope_ns': 5_000_000_000, 'cadence_ms': 100, 'error': None,
                  'receipts': {'raw/execution-001.json': {'received_ns': 3_000_000_000,
                                                       'sha256': digest(root/'raw/execution-001.json')}}}
        write_json(root/'stream.json', stream)
        result = baseline(root, slot, command, expected)
        self.assertEqual(result['usable_first_event_seconds'], [2, 2])
        for value in (-1, True, 6_000_000_000, 500_000_000):
            with self.subTest(received_ns=value):
                changed = copy.deepcopy(stream)
                changed['receipts']['raw/execution-001.json']['received_ns'] = value
                write_json(root/'stream.json', changed)
                with self.assertRaisesRegex(ValueError, 'stream'):
                    baseline(root, slot, command, expected)

    def test_analysis_includes_remainder_and_uses_each_arms_count(self):
        bundle, manifest = self.bundle()
        manifest = read_json(bundle/'frozen/manifest.json')
        manifest['inputs']['matching']['fixed']['counts'].update(B1=2, **{'T-R': 4})
        write_json(bundle/'frozen/manifest.json', manifest); seal(bundle/'frozen')
        def reduced(root, slot, unused):
            result = dict(slot, status='not_started', reasons=[], started=False)
            if (slot['workload'], slot['variant'], slot['cache'], slot['arm'], slot['block']) == \
                    ('matching', 'fixed', 'cold', 'B1', '09'):
                spans = [{'id': 1, 'parent_id': 0, 'name': 'command', 'start_ns': 0, 'duration_ns': 3*10**9},
                         {'id': 2, 'parent_id': 1, 'name': 'work', 'start_ns': 0, 'duration_ns': 2*10**9}]
                result.update(status='complete', started=True, invocation_seconds=4, stages=stages(spans, 4))
            return result
        with patch('analyze.reduce_slot', side_effect=reduced):
            result = analyze(bundle, self.root/'analysis')
        cell = next(c for c in result['cells'] if (c['workload'], c['variant'], c['cache'], c['arm']) ==
                    ('matching', 'fixed', 'cold', 'B1'))
        self.assertEqual(cell['stage_seconds'].get('remainder'), {'n': 1, 'median': 2, 'p90': 2, 'min': 2, 'max': 2})
        self.assertEqual(cell['amortized_execution_seconds']['median'], 2)
        self.assertIn('| matching | fixed | cold | B1 | remainder | 2.000000 (1) |',
                      (self.root/'analysis/report.md').read_text())

    def test_analysis_output_swap_does_not_modify_campaign(self):
        from analyze import write_json as real_write
        from analyze import seal as real_seal
        from analyze import write_bytes as real_bytes
        parent = self.root
        for boundary in ('results', 'report', 'seal', 'after-seal'):
            for kind in ('symlink', 'directory'):
                with self.subTest(boundary=boundary, kind=kind):
                    self.root = parent/(boundary+'-'+kind); self.root.mkdir()
                    bundle, _ = self.bundle()
                    target = bundle/'frozen'; before = inventory(target)
                    output = self.root/'analysis'
                    def swap():
                        output.rename(self.root/'original-analysis')
                        if kind == 'symlink': output.symlink_to(target, target_is_directory=True)
                        else: output.mkdir()
                    def write(path, value):
                        if boundary == 'results': swap()
                        real_write(path, value)
                    def seal_output(path):
                        if boundary == 'seal': swap()
                        real_seal(path)
                        if boundary == 'after-seal': swap()
                    def write_report(path, data):
                        if boundary == 'report': swap()
                        real_bytes(path, data)
                    with patch('analyze.write_json', side_effect=write), patch('analyze.seal', side_effect=seal_output), \
                         patch('analyze.write_bytes', side_effect=write_report):
                        with self.assertRaisesRegex(ValueError, 'directory.*replaced'):
                            analyze(bundle, output)
                    self.assertEqual(inventory(target), before)
                    if kind == 'directory': self.assertEqual(list(output.iterdir()), [])

    def test_analysis_report_symlink_cannot_overwrite_originals(self):
        from analyze import write_json as real_write
        bundle, _ = self.bundle()
        output = self.root/'analysis'
        target = bundle/'frozen/protocol.md'; before = target.read_bytes()
        def write(path, value):
            real_write(path, value)
            (path.parent/'report.md').symlink_to(target)
        with patch('analyze.write_json', side_effect=write): analyze(bundle, output)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((output/'report.md').is_symlink())
        verify(output)

    def test_invalid_usable_receipt_times_fail_without_losing_accounting(self):
        root, slot, _, expected = self.product_raw('T-R')
        manifest = self.manifest(); manifest['inputs']['matching']['vulnerable'] = expected
        for value in ([0, 0], [-1, -1], [1, 2], [True, True], [1], 'invalid', None):
            with self.subTest(value=value):
                state = read_json(root/'slot.json'); state['evidence_received_seconds'] = value
                write_json(root/'slot.json', state); seal(root)
                result = reduce_slot(root, slot, manifest)
                self.assertEqual(result['status'], 'operational_failed')
                self.assertIsNone(result['usable_evidence_seconds'])
                self.assertTrue(any('receipt time' in reason for reason in result['reasons']))

    def test_command_receipt_clock_contains_real_spawn(self):
        import time
        root = self.root/'command'; root.mkdir()
        spec = {'argv': [sys.executable, '-B', '-c', 'print("constructed")'],
                'cwd': str(self.root), 'env': {}, 'timeout_seconds': 5}
        real_spawn = subprocess.Popen
        spawned = []
        def spawn(*args, **kwargs):
            spawned.append(time.monotonic_ns())
            return real_spawn(*args, **kwargs)
        with patch('execution.subprocess.Popen', side_effect=spawn):
            command = execute(spec, {'redact_env': []}, root, 'run', [])
        self.assertTrue(succeeded(command))
        self.assertEqual(read_json(root/'run.command.json'), command)
        self.assertLessEqual(command['collector_start_ns'], spawned[0])
        self.assertLessEqual((spawned[0] - command['collector_start_ns']) / 1e9, command['seconds'])

    def test_analysis_parent_swap_before_creation_preserves_campaign(self):
        bundle, _ = self.bundle()
        parent = self.root/'publication'; parent.mkdir()
        output = parent/'analysis'
        before = inventory(bundle/'frozen')
        real_mkdir = os.mkdir
        real_publish = publish_directory
        swapped = False
        def mkdir(path, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path).name == output.name:
                swapped = True
                parent.rename(self.root/'original-publication')
                parent.symlink_to(bundle/'frozen', target_is_directory=True)
            return real_mkdir(path, *args, **kwargs)
        def published(parent_fd, source, destination):
            nonlocal swapped
            if not swapped and destination == output.name:
                swapped = True
                parent.rename(self.root/'original-publication')
                parent.symlink_to(bundle/'frozen', target_is_directory=True)
            real_publish(parent_fd, source, destination)
        with patch('os.mkdir', side_effect=mkdir), patch('support.publish_directory', side_effect=published):
            with self.assertRaisesRegex(ValueError, 'directory.*replaced'):
                analyze(bundle, output)
        self.assertTrue(swapped)
        self.assertEqual(inventory(bundle/'frozen'), before)

    def test_analysis_detection_requires_timely_usable_evidence(self):
        bundle, _ = self.bundle()
        for arm in ('B1', 'B2', 'T-R', 'T-E'):
            for receipt in ('in_budget', 'late', 'unknown'):
                with self.subTest(arm=arm, receipt=receipt):
                    def reduced(root, slot, unused):
                        row = dict(slot, status='not_started', started=False, reasons=[])
                        if slot['arm'] == arm and slot['block'] == '09':
                            row.update(status='complete', started=True, invocation_seconds=1,
                                       first_event_seconds=[.1, .2], first_event_membership='in_budget',
                                       usable_evidence_membership=receipt)
                            if arm.startswith('B'):
                                value = 1 if receipt == 'in_budget' else slot['cap_seconds'] + 1
                                row['usable_first_event_seconds'] = None if receipt == 'unknown' else [value, value]
                        return row
                    with patch('analyze.reduce_slot', side_effect=reduced):
                        result = analyze(bundle, self.root/('detection-'+arm+'-'+receipt))
                    cell = next(c for c in result['cells'] if (c['workload'], c['variant'], c['cache'], c['arm']) ==
                                ('matching', 'fixed', 'cold', arm))
                    self.assertEqual(cell['observed_detections'], int(receipt == 'in_budget'))

    def test_hooks_reject_hardlink_alias_of_prior_proof(self):
        from tool import execute as real_execute
        parent = self.root
        for phase in ('observe', 'cleanup'):
            with self.subTest(phase=phase):
                self.root = parent/phase; self.root.mkdir()
                bundle, registration = self.measurement_bundle()
                def alias(spec, context, directory, label, secrets):
                    result = real_execute(spec, context, directory, label, secrets)
                    if label == phase:
                        original = directory/'proof/prepare.json'
                        path = directory/'proof/reused.json'
                        os.link(original, path)
                        receipt = directory/('observation.json' if phase == 'observe' else 'cleanup.json')
                        value = read_json(receipt)
                        value['evidence'] = {'proof/reused.json': digest(original)}
                        write_json(receipt, value)
                    return result
                with patch('tool.execute', side_effect=alias): collect(bundle, registration)
                state = read_json(bundle/'slots'/order()[0]['id']/'slot.json')
                self.assertIn('newly retained', state.get('error' if phase == 'observe' else 'cleanup_error', ''))
                if phase == 'observe':
                    self.assertNotIn('resources', state)
                else:
                    self.assertFalse(state['cleanup_proven'])
                    self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                    with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, registration)

    def test_sealed_analysis_identifies_registered_sources_and_replay(self):
        bundle, registration = self.measurement_bundle()
        manifest = read_json(bundle/'frozen/manifest.json')
        registered(bundle, manifest, registration)
        output = self.root/'analysis'
        result = analyze(bundle, output)
        source = result['source']
        for key, path in (('manifest_sha256', 'frozen/manifest.json'),
                          ('order_sha256', 'frozen/order.json'),
                          ('frozen_sha256', 'frozen/SHA256SUMS'),
                          ('checkpoint_sha256', 'progress/checkpoint.json')):
            self.assertEqual(source[key], digest(bundle/path))
            self.assertIn(source[key], (output/'report.md').read_text())
        self.assertEqual(source['registration'], read_json(registration))
        self.assertEqual(source['candidate'], manifest['candidate'])
        self.assertEqual(source['e1_registration'], manifest['e1_registration'])
        argv = source['replay']['argv']
        self.assertEqual(argv[1:5], ['-B', str(bundle/'frozen/source/tool.py'), 'analyze', str(bundle)])
        self.assertIn(source['replay']['command'], (output/'report.md').read_text())
        verify(output)
        # The retained tool works independently and emits the same sealed bytes.
        result = subprocess.run(argv, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(inventory(output), inventory(Path(argv[-1])))

    def test_analysis_source_distinguishes_same_id_campaigns_and_checkpoints(self):
        parent = self.root
        sources = []
        for index in (0, 1):
            self.root = parent/str(index); self.root.mkdir()
            m = self.measurement_manifest()
            m['candidate']['source_revision'] = str(index)*40
            m['e1_registration']['source_revision'] = m['candidate']['source_revision']
            self.write_prerequisites(m)
            bundle, registration = self.measurement_bundle(m)
            registered(bundle, m, registration)
            result = analyze(bundle, self.root/'analysis')
            self.assertEqual(result['campaign_id'], 'constructed')
            sources.append(result['source'])
        self.assertNotEqual(sources[0]['candidate'], sources[1]['candidate'])
        for field in ('manifest_sha256', 'frozen_sha256', 'checkpoint_sha256'):
            self.assertNotEqual(sources[0][field], sources[1][field])
        collect(bundle, registration)
        changed = analyze(bundle, self.root/'after-collection')['source']
        self.assertEqual(changed['manifest_sha256'], sources[1]['manifest_sha256'])
        self.assertNotEqual(changed['checkpoint_sha256'], sources[1]['checkpoint_sha256'])

    def test_proof_freshness_rejects_renamed_and_external_inodes(self):
        originals = self.root/'proof'; originals.mkdir()
        earlier = originals/'earlier.json'; earlier.write_text('constructed evidence')
        before = hook_snapshot(self.root)
        moved = originals/'moved.json'; earlier.rename(moved)
        write_json(self.root/'cleanup.json', {'proven': True, 'evidence': {'proof/moved.json': digest(moved)}})
        with self.assertRaisesRegex(ValueError, 'newly retained'):
            proof(self.root, 'cleanup.json', {'proven': True}, previous=before)
        # No pre-hook pathname for an external artifact: link count still rejects it.
        external = self.root/'outside.json'; external.write_text('constructed external')
        linked = originals/'linked.json'; os.link(external, linked)
        write_json(self.root/'preparation.json', {'evidence': {'proof/linked.json': digest(linked)}})
        with self.assertRaisesRegex(ValueError, 'newly retained'):
            proof(self.root, 'preparation.json', {}, previous={})

    def test_hook_cannot_replace_original_with_identical_bytes(self):
        path = self.root/'original.json'; path.write_text('constructed')
        before = hook_snapshot(self.root)
        path.rename(self.root/'old.json'); path.write_text('constructed')
        with self.assertRaisesRegex(ValueError, 'hook changed original'):
            unchanged(self.root, before)

    def test_baseline_first_domain_time_comes_from_valid_execution(self):
        root, slot, command = self.raw()
        path = root/'raw/execution-001.json'
        value = read_json(path); value['child_reaped'] = False; write_json(path, value)
        result = baseline(root, slot, command, self.manifest()['inputs']['quota']['vulnerable'])
        self.assertEqual(result['first_event_seconds'], result['executions'][1]['event_seconds'])
        self.assertEqual(result['valid_evaluations'], 1)

    def test_analysis_creates_missing_parents_and_refuses_existing_output(self):
        bundle, _ = self.bundle()
        output = self.root/'new'/'nested'/'analysis'
        first = analyze(bundle, output); before = inventory(output)
        verify(output)
        self.assertEqual(first, analyze(bundle, self.root/'other'))
        with self.assertRaises(FileExistsError): analyze(bundle, output)
        self.assertEqual(inventory(output), before)
        with self.assertRaises(FileExistsError):
            with created_directory(Path('/')):
                self.fail('filesystem root accepted as new output')

    def test_declared_unsupported_slot_cannot_be_collected_as_supported(self):
        m = self.manifest(); m['unsupported'] = {'matching/B2': UNSUPPORTED}
        write_json(self.root/'input.json', m)
        bundle = self.root/'bundle'; freeze(self.root/'input.json', bundle)
        slot = order()[0]; directory = bundle/'slots'/slot['id']; directory.mkdir()
        write_json(directory/'slot.json', {'slot': slot, 'cleanup_proven': True})
        seal(directory); save_progress(bundle, {slot['id']: digest(directory/'SHA256SUMS')})
        with self.assertRaisesRegex(ValueError, 'unsupported slot declaration'):
            analyze(bundle, self.root/'analysis')

if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests))
    if not result.wasSuccessful(): raise SystemExit(1)
    print('E5_RUNNER_RESULT order=600 accounting=all_slots cleanup=quarantine analysis=deterministic')
