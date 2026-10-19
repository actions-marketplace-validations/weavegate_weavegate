#!/usr/bin/env python3
"""Constructed boundary tests; these are not E5 performance observations."""
import copy
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from support import *
from analyze import analyze, baseline, bound, membership, reduce_slot, stages, stats, validate_trace
from tool import collect, freeze, validate, proof


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.protocol = HERE.parent / 'README.md'

    def tearDown(self): self.temp.cleanup()

    def manifest(self):
        program = self.root / 'arm.py'
        program.write_text('raise SystemExit(4)\n')
        executable = str(Path(sys.executable).resolve())
        inputs, commands = {}, {}
        for w, spec in WORKLOADS.items():
            inputs[w], commands[w] = {}, {}
            for v in ('vulnerable', 'fixed'):
                inputs[w][v] = {'candidates': candidates(w), 'counts': {'B1': 2, 'B2': 2, 'T-R': 2},
                    'config': {'Run': {'Repeat': 20, 'ArriveTimeoutMS': 1000, 'ExplorePasses': 3},
                               'Target': {'SUT': {'External': {'Capacity': 2, 'StartupTimeoutMS': 30000,
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
        run = {'cleanup_proven': True, 'executions': 2, 'seed': {'synthetic': []}, 'process_elapsed_ns': 900000000,
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

    def test_frozen_inputs_tamper_rejected_offline(self):
        bundle, _ = self.bundle()
        (bundle/'frozen/order.json').write_text('[]')
        with self.assertRaises(ValueError): analyze(bundle, self.root/'analysis')

    def test_failed_process_quarantines_and_preserves_all_slots(self):
        bundle, _ = self.bundle()
        collect(bundle, max_slots=2)
        self.assertTrue((bundle/'stopped.json').exists())
        result = analyze(bundle, self.root/'analysis')
        self.assertEqual(result['slots'][0]['status'], 'operational_failed')
        self.assertEqual(sum(s['status'] == 'not_started' for s in result['slots']), 599)
        with self.assertRaisesRegex(ValueError, 'stopped'): collect(bundle, max_slots=1)

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
        r = baseline(root, slot, cmd, self.manifest()['inputs']['quota']['vulnerable'])
        self.assertEqual((r['valid_evaluations'],r['unobserved'],r['x_over_planned']), (1,1,.5))
        self.assertFalse(r['operational_reasons'])

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
        saved={'id':'saved','steps':[{'worker':'w2','point':'before_insert_assignment'}]}
        slot={'workload':'matching','variant':'fixed'}
        trace={'schedule_ref':'saved','terminals':[{'state':'done','failure_class':'none'}]*2,
               'events':[{'seq':1,'kind':'worker_done','worker':'w2'},
                         {'seq':2,'kind':'step_terminal_skipped','worker':'w2','point':'before_insert_assignment','step':0}]}
        self.assertEqual(validate_trace(trace,saved,slot)['terminal_skips'],1)
        with self.assertRaises(ValueError):validate_trace(trace,saved,dict(slot,variant='vulnerable'))
        trace['events'][0]['worker']='w1'
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

    def test_measurement_cleanup_allows_next_slot_then_quarantines(self):
        m=self.manifest()
        m['phase']='measurement'
        m['fixture_revision']='e1bd8ba3fc07d024b8cb4037aa2263d24044dbb0'
        identity=digest(sys.executable)
        m['candidate']={'cli_sha256':identity,'sdk_sha256':identity}
        m['e1_registration']=dict(m['candidate'],url='https://example.com/constructed',revision='constructed')
        m['environment']={'host_id':'constructed','vm_image':'constructed','cpu_model':'constructed',
            'kernel':'constructed','docker':'constructed','background_policy':'constructed',
            'mysql_image':'mysql:8.4@sha256:'+'a'*64,'vcpus':4,'memory_bytes':16*1024**3}
        m['prerequisites']={k:str(self.root/'arm.py') for k in ('go_acceptance','java_acceptance','paired_acceptance','corpus_acceptance','baseline_feasibility')}
        for values in m['inputs'].values():
            for inp in values.values():
                inp['counts']={'B1':20,'B2':100,'T-R':20};inp['sdk_sha256']=identity
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
name={'prepare':'preparation','observe':'observation','cleanup':'cleanup','warm_cleanup':'warm-cleanup'}[stage]
(p/(name+'.json')).write_text(json.dumps(v))
""")
        executable=str(Path(sys.executable).resolve())
        m['pins'][str(hook)]=digest(hook)
        m['hooks']={k:{'argv':[executable,str(hook),k,'{trial_dir}','{arm}'],
            'cwd':str(self.root),'env':{},'timeout_seconds':10} for k in ('prepare','observe','cleanup','warm_cleanup')}
        m['hooks']['cleanup']['execution_pins']=[executable,str(hook)]
        write_json(self.root/'measurement.json',m)
        bundle=self.root/'measurement';freeze(self.root/'measurement.json',bundle)
        registration={'manifest_sha256':digest(bundle/'frozen/manifest.json'),
            'order_sha256':digest(bundle/'frozen/order.json'),'registered_at':'2020-01-01T00:00:00Z',
            'public_url':'https://example.com/constructed','revision':'constructed'}
        write_json(self.root/'registration.json',registration)
        collect(bundle,self.root/'registration.json')
        self.assertEqual(len(list((bundle/'slots').iterdir())),2)
        self.assertTrue((bundle/'stopped.json').exists())
        self.assertTrue(read_json(bundle/'slots'/order()[0]['id']/'slot.json')['cleanup_proven'])
        self.assertFalse(read_json(bundle/'slots'/order()[1]['id']/'slot.json')['cleanup_proven'])


if __name__ == '__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests))
    if not result.wasSuccessful(): raise SystemExit(1)
    print('E5_RUNNER_RESULT order=600 accounting=all_slots cleanup=quarantine analysis=deterministic')
