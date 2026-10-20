#!/usr/bin/env python3
"""Constructed boundary tests; these are not E5 performance observations."""
import copy
import fcntl
import json
import shutil
import signal
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from support import *
from analyze import analyze, baseline, bound, membership, product, reduce_slot, stages, stats, validate_trace
from tool import collect, freeze, registered, validate, proof


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

    def test_measurement_cannot_use_short_counts(self):
        m = self.manifest(); m['phase'] = 'measurement'
        with self.assertRaisesRegex(ValueError, 'counts'): validate(m)

    def test_changed_pin_rejected_before_freeze(self):
        m = self.manifest(); (self.root/'arm.py').write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'pin changed'): validate(m)

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
        m['unsupported'] = {w + '/' + a: 'constructed unsupported' for w in WORKLOADS for a in ARMS}
        write_json(self.root/'input.json', m)
        bundle = self.root/'bundle'
        freeze(self.root/'input.json', bundle)
        consumed = {}
        for slot in order():
            directory = bundle/'slots'/slot['id']; directory.mkdir()
            write_json(directory/'slot.json', {'slot': slot, 'unsupported': 'constructed unsupported'})
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
        with self.assertRaises(ValueError): proof(self.root,'cleanup.json',{'proven':True}, previous={'proof/closed.json':digest(evidence)})
        self.assertTrue(proof(self.root,'cleanup.json',{'proven':True},previous={})['proven'])

    def test_out_of_order_consumed_slot_is_rejected(self):
        bundle,m=self.bundle()
        wrong=bundle/'slots'/order()[2]['id'];wrong.mkdir()
        write_json(wrong/'slot.json', {'slot':order()[2],'unsupported':'constructed'})
        seal(wrong)
        with self.assertRaisesRegex(ValueError,'ordered prefix'):collect(bundle,max_slots=1)

    def product_raw(self, arm, explored_count=None, exhausted=False, saved_index=None):
        """Construct a valid vulnerable replay/discovery, including stage/trace evidence."""
        root = self.root / arm; root.mkdir()
        directory = root/'raw/runs/constructed'; directory.mkdir(parents=True)
        slot = dict(order()[0], arm=arm, variant='vulnerable')
        expected = self.manifest()['inputs']['matching']['vulnerable']
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
        if arm == 'T-E':
            for _ in range(3 if exhausted else 1):
                parent = span('explore_pass', 1)
                prefix = (expected['candidates'][:explored_count] if explored_count is not None
                          else expected['candidates'] if exhausted
                          else expected['candidates'][:expected['candidates'].index(saved)+1])
                for candidate in prefix:
                    execution(parent, candidate); explored += 1
        if not exhausted:
            parent = span('replay', 1)
            for _ in range(2): execution(parent, saved)
        manifest = {'variant': 'vulnerable', 'adapter': 'external', 'sut_sha256': 'sha256:'+'b'*64,
                    'image': expected['config']['Target']['DB'],
                    'isolation_level': 'REPEATABLE-READ', 'cleanup_failed': False, 'timings': {'spans': spans}}
        observation = {'mode': 'explore' if arm == 'T-E' else 'replay',
            'oracles': [{'id': WORKLOADS['matching'][2], 'sql': 'constructed', 'expect_rows': 0}],
            'violation_runs': 2, 'fingerprints': {'a'*64: 2}, 'schedules_explored': explored,
            'discovery_fingerprint': 'a'*64 if explored else None, 'flaky': False, 'repeat': 2,
            'explore_passes': int(bool(explored)),
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
        m['unsupported'] = {'matching/B2': 'constructed unsupported'}
        bundle, registration = self.measurement_bundle(m)
        registered(bundle, m, registration)
        slot = order()[0]
        directory = bundle/'slots'/slot['id']; directory.mkdir()
        write_json(directory/'slot.json', {'slot':slot, 'unsupported':'constructed unsupported'})
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
        m['unsupported'] = {'matching/B2':'constructed unsupported'}
        bundle, registration = self.measurement_bundle(m)
        slot = order()[0]
        directory = bundle/'slots'/slot['id']; directory.mkdir()
        write_json(directory/'slot.json', {'slot':slot, 'unsupported':'constructed unsupported'})
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
        m['unsupported'] = {w+'/'+a:'constructed unsupported' for w in WORKLOADS for a in ARMS}
        del m['unsupported'][warm['workload']+'/'+warm['arm']]
        bundle, registration = self.measurement_bundle(m)
        registered(bundle, m, registration)
        consumed = {}
        for slot in order()[:warm['ordinal']-1]:
            directory = bundle/'slots'/slot['id']; directory.mkdir()
            write_json(directory/'slot.json', {'slot':slot, 'unsupported':'constructed unsupported'})
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
                if boundary == 'slot-seal' and path.parent == bundle/'slots': interrupt()
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
                        if unsupported: m['unsupported'] = {'matching/B2': 'constructed unsupported'}
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
                            if boundary == 'unsupported-seal' and path.parent == bundle/'slots': interrupt()
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
                pidfile = self.root/'child.pid'
                program.write_text("import os,signal\nfrom pathlib import Path\n"
                                   + "Path("+repr(str(pidfile))+").write_text(str(os.getpid()))\n"
                                   + "os.kill(os.getppid(), signal.SIGTERM)\nsignal.pause()\n")
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
                child = subprocess.run([sys.executable, '-B', '-c', probe, str(self.root/'reference.json'),
                                        str(bundle), str(registration)], cwd=HERE, capture_output=True, timeout=30)
                try:
                    self.assertEqual(child.returncode, 0, child.stderr.decode())
                    pid = int(pidfile.read_text())
                    self.assertFalse(group_running(pid))
                    directory = bundle/'slots'/order()[0]['id']
                    self.assertTrue(read_json(directory/(label+'.command.json'))['interrupted'])
                    self.assertTrue(read_json(directory/'slot.json')['cleanup_proven'])
                    result = analyze(bundle, self.root/('sigterm-output-'+label))
                    self.assertIn('interrupted', result['stopped']['reason'])
                    self.assertEqual(len(list((bundle/'slots').iterdir())), 1)
                    with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, registration)
                finally:
                    if pidfile.exists():
                        try: os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                        except ProcessLookupError: pass
                        pidfile.unlink()
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
if stage=='observe':v.update(host_id='constructed',resources={'cpu_seconds':None,'reason':'constructed'})
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


if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests))
    if not result.wasSuccessful(): raise SystemExit(1)
    print('E5_RUNNER_RESULT order=600 accounting=all_slots cleanup=quarantine analysis=deterministic')
