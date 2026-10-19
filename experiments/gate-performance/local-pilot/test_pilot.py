"""Boundary tests using constructed intervals, never performance observations."""
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
import sys
from pathlib import Path

from pilot import (analyze, decomposition, inspect_trial, inventory, read,
                   run_command, sha, summary, verify, write)


def span(i, parent, name, start, duration):
    return dict(id=i, parent_id=parent, name=name, start_ns=start, duration_ns=duration)


def constructed_trial(path, mode='replay', vulnerable=False):
    """Synthetic complete evidence with two executions, never measured data."""
    run = path / 'output/runs/constructed'
    run.mkdir(parents=True)
    schedule_id = 'constructed-schedule'
    expected_schedule = schedule_id if mode == 'replay' or vulnerable else None
    observation = dict(mode=mode, repeat=2, diagnostics=[], assertion_violations=[])
    if vulnerable:
        observation['diagnostics'] = [dict(code='WG001', assertion='one-active-reservation-per-seat')]
    spec = dict(exit=2 if vulnerable else 0, observation=observation,
                schedule_id=expected_schedule, schedule_runs=2, schedule_ids=[schedule_id] * 2)
    write(path / 'command.json', dict(exit=spec['exit'], timed_out=False, interrupted=False,
                                    error=None, duration_ns=1100))
    write(path / 'cleanup.json', dict(no_new_containers=True, no_new_snapshot_jvms=True))
    write(run / 'observation.json', observation)
    write(run / 'scenario.json', {'schedule': {'id': expected_schedule}} if expected_schedule else {})
    write(run / 'trace.json', {})
    if expected_schedule:
        write(run / 'schedule.json', {'id': expected_schedule})
    (run / 'report.md').write_text('Constructed evidence\n')
    spans = [span(1, 0, 'command', 0, 1000), span(2, 1, 'preflight', 0, 5),
             span(3, 1, 'fixture_provision', 10, 5), span(4, 1, 'fixture_teardown', 900, 5)]
    for start in (100, 300):
        i = len(spans) + 1
        spans += [dict(span(i, 1, 'schedule_run', start, 180), schedule_id=schedule_id),
                  span(i+1, i, 'fixture_reset', start+1, 10),
                  span(i+2, i, 'adapter_create', start+11, 1),
                  span(i+3, i, 'adapter_start', start+12, 50),
                  span(i+4, i+3, 'jar_snapshot', start+12, 10),
                  span(i+5, i+3, 'process_start_to_ready', start+22, 40),
                  span(i+6, i, 'schedule_execute', start+62, 70),
                  span(i+7, i+6, 'block_recognition', start+63, 20),
                  span(i+8, i+6, 'step_wait', start+83, 10),
                  span(i+9, i, 'oracle_evaluate', start+132, 10),
                  span(i+10, i, 'adapter_stop', start+142, 20)]
    manifest = {k: 'constructed' for k in ('schema_version', 'seed_data', 'isolation_level',
                'engine', 'adapter', 'variant', 'image', 'sut_sha256')}
    manifest.update(cleanup_failed=False, timings=dict(scope='before_artifact_write', spans=spans))
    write(run / 'manifest.json', manifest)
    return spec, run


def constructed_campaign(root, mode='replay', vulnerable=False):
    order = []
    for block in ('first', 'second'):
        for arm in ('B', 'Bplus'):
            spec, _ = constructed_trial(root / (block + '-' + arm), mode, vulnerable)
        order.append(dict(id=block, workload='constructed', arms=['B', 'Bplus']))
    write(root / 'plan.json', dict(workloads={'constructed': spec}, order=order))
    write(root / 'registration.json', dict(plan_sha256=sha(root / 'plan.json')))
    write(root / 'status.json', dict(stopped=None))
    write(root / 'SHA256SUMS.json', inventory(root))


class AnalysisTest(unittest.TestCase):
    def reduce(self, root):
        write(root / 'SHA256SUMS.json', inventory(root))
        output = root.parent / (root.name + '-analysis.json')
        with redirect_stdout(StringIO()):
            analyze(root, output)
        return read(output)

    def test_missing_schedule_cannot_match_a_pair_that_expects_one(self):
        for mode, vulnerable in (('replay', False), ('replay', True), ('explore', True)):
            with self.subTest(mode=mode, vulnerable=vulnerable), tempfile.TemporaryDirectory() as name:
                root = Path(name) / 'campaign'
                constructed_campaign(root, mode, vulnerable)
                self.assertEqual(self.reduce(root)['estimates']['constructed']['artifact_matched_pairs'], 2)
                for arm in ('B', 'Bplus'):
                    (root / ('first-' + arm) / 'output/runs/constructed/schedule.json').unlink()
                result = self.reduce(root)
                self.assertEqual(result['estimates']['constructed']['artifact_matched_pairs'], 1)
                for row in result['trials'][:2]:
                    self.assertIn('missing_artifact', row['result']['problems'])

    def test_exhausted_fixed_exploration_allows_absent_schedule(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'campaign'
            constructed_campaign(root, 'explore', False)
            result = self.reduce(root)
            self.assertEqual(result['estimates']['constructed']['artifact_matched_pairs'], 2)
            self.assertTrue(all(row['result']['deterministic_sha256']['schedule.json'] is None
                                for row in result['trials']))

    def test_resealed_plan_must_match_registration(self):
        for field in ('order', 'workload', 'expectation'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                root = Path(name) / 'campaign'
                constructed_campaign(root)
                self.reduce(root)
                plan = read(root / 'plan.json')
                if field == 'order':
                    plan['order'].reverse()
                elif field == 'workload':
                    plan['order'].pop()
                else:
                    plan['workloads']['constructed']['observation']['repeat'] = 3
                write(root / 'plan.json', plan)
                with self.assertRaisesRegex(ValueError, 'plan.*registration'):
                    self.reduce(root)

    def test_missing_registration_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'campaign'
            constructed_campaign(root)
            write(root / 'registration.json', {})
            with self.assertRaisesRegex(ValueError, 'plan.*registration'):
                self.reduce(root)

    def test_missing_required_spans_exclude_trial_from_estimates(self):
        for stage in ('preflight', 'fixture_provision', 'fixture_teardown', 'fixture_reset',
                      'adapter_create', 'adapter_start', 'schedule_execute', 'oracle_evaluate',
                      'adapter_stop', 'jar_snapshot', 'process_start_to_ready'):
            for remove_all in (False, True):
                with self.subTest(stage=stage, remove_all=remove_all), tempfile.TemporaryDirectory() as name:
                    root = Path(name) / 'campaign'
                    constructed_campaign(root)
                    path = root / 'first-Bplus/output/runs/constructed/manifest.json'
                    manifest = read(path)
                    spans = manifest['timings']['spans']
                    removed = [s for s in spans if s['name'] == stage]
                    if not remove_all:
                        removed = removed[:1]
                    # Keep a valid tree so the failure specifically concerns missing observations.
                    parents = {s['id']: s['parent_id'] for s in removed}
                    for s in spans:
                        if s['parent_id'] in parents:
                            s['parent_id'] = parents[s['parent_id']]
                    manifest['timings']['spans'] = [s for s in spans if s not in removed]
                    write(path, manifest)
                    result = self.reduce(root)
                    first = next(r for r in result['trials'] if r['block'] == 'first' and r['arm'] == 'Bplus')
                    self.assertIn('timed_stage_count:' + stage, first['result']['problems'])
                    estimates = result['estimates']['constructed']
                    self.assertEqual(estimates['artifact_matched_pairs'], 1)
                    for summary in estimates['exclusive_stage_seconds'].values():
                        self.assertEqual(summary['n'], 1)
                    for summary in estimates['exclusive_stage_wall_fraction'].values():
                        self.assertEqual(summary['n'], 1)

    def test_duplicate_in_one_execution_cannot_replace_another(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            spec, run = constructed_trial(root)
            manifest = read(run / 'manifest.json')
            spans = manifest['timings']['spans']
            stops = [s for s in spans if s['name'] == 'adapter_stop']
            stops[1].update(parent_id=stops[0]['parent_id'], start_ns=stops[0]['start_ns'])
            write(run / 'manifest.json', manifest)
            self.assertIn('timed_stage_count:adapter_stop', inspect_trial(root, spec, 'Bplus')['problems'])

    def test_missing_variable_stage_is_not_imputed_as_zero(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'campaign'
            constructed_campaign(root)
            path = root / 'first-Bplus/output/runs/constructed/manifest.json'
            manifest = read(path)
            manifest['timings']['spans'] = [s for s in manifest['timings']['spans'] if s['name'] != 'step_wait']
            write(path, manifest)
            estimates = self.reduce(root)['estimates']['constructed']
            seconds = estimates['exclusive_stage_seconds']['step_wait']
            fraction = estimates['exclusive_stage_wall_fraction']['step_wait']
            self.assertEqual(seconds['n'], 1)
            self.assertEqual(seconds['min'], 20 / 1e9)
            self.assertEqual(fraction['n'], 1)
            self.assertEqual(fraction['min'], 20 / 1100)

    def test_recorded_zero_duration_is_an_observation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'campaign'
            constructed_campaign(root)
            for block in ('first', 'second'):
                path = root / (block + '-Bplus') / 'output/runs/constructed/manifest.json'
                manifest = read(path)
                for s in manifest['timings']['spans']:
                    if s['name'] == 'process_start_to_ready':
                        s['duration_ns'] = 0
                write(path, manifest)
            estimates = self.reduce(root)['estimates']['constructed']
            self.assertEqual(estimates['artifact_matched_pairs'], 2)
            seconds = estimates['exclusive_stage_seconds']['process_start_to_ready']
            self.assertEqual(seconds['n'], 2)
            self.assertEqual(seconds['max'], 0)


class TimingTest(unittest.TestCase):
    def test_nested_and_overlapping_intervals_partition_wall(self):
        data = [span(1, 0, 'command', 0, 100), span(2, 1, 'schedule_execute', 10, 80),
                span(3, 2, 'block_recognition', 20, 40), span(4, 2, 'block_recognition', 30, 40)]
        result = decomposition(data, 110)
        self.assertEqual(result['exclusive_ns'], dict(outside_timing=10, bookkeeping=20,
                         schedule_execute=30, block_recognition=50))
        self.assertEqual(result['cumulative_ns']['block_recognition'], 80)

    def test_database_children_remain_in_provision(self):
        data = [span(1, 0, 'command', 4, 100), span(2, 1, 'fixture_provision', 5, 80),
                span(3, 2, 'database_start', 6, 60)]
        self.assertEqual(decomposition(data, 120)['exclusive_ns'],
                         dict(outside_timing=20, bookkeeping=20, fixture_provision=80))

    def test_invalid_interval_and_parent_rejected(self):
        for invalid in [span(2, 1, 'preflight', 5, 101), span(2, 1, 'preflight', 0, -1)]:
            with self.assertRaises(ValueError):
                decomposition([span(1, 0, 'command', 0, 100), invalid], 120)
        with self.assertRaises(ValueError):
            decomposition([span(1, 0, 'command', 0, 100)], 99)

    def test_evidence_modification_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write(root / 'observation.json', {'flaky': False})
            write(root / 'SHA256SUMS.json', inventory(root))
            verify(root)
            write(root / 'observation.json', {'flaky': True})
            with self.assertRaises(ValueError):
                verify(root)

    def test_empty_and_single_samples_have_no_invented_variance(self):
        self.assertIsNone(summary([]))
        self.assertIsNone(summary([2])['sample_sd'])
        self.assertEqual(summary([1, 2, 3, 4])['iqr'], 1.5)

    def test_spawn_failure_retains_original_command(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            receipt = run_command(['/unavailable-local-pilot-command'], name, {}, root)
            self.assertFalse(receipt['started'])
            self.assertIsNone(receipt['exit'])
            self.assertIn('FileNotFoundError', receipt['error'])
            self.assertTrue((root / 'command.json').is_file())

    def test_expected_violation_exit_is_retained(self):
        with tempfile.TemporaryDirectory() as name:
            receipt = run_command([sys.executable, '-c', 'raise SystemExit(2)'], name, {}, Path(name))
            self.assertTrue(receipt['started'])
            self.assertEqual(receipt['exit'], 2)
            self.assertIsNone(receipt['error'])
            self.assertGreater(receipt['duration_ns'], 0)

    def test_artifact_checker_rejects_missing_timing_counts_and_cleanup(self):
        # Entirely constructed artifacts: this tests accounting, not the product.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            run = root / 'output/runs/constructed'
            run.mkdir(parents=True)
            write(root / 'command.json', dict(exit=0, timed_out=False, interrupted=False,
                                            error=None, duration_ns=200))
            write(root / 'cleanup.json', dict(no_new_containers=True, no_new_snapshot_jvms=True))
            manifest = {k: 'constructed' for k in ('schema_version', 'seed_data', 'isolation_level',
                        'engine', 'adapter', 'variant', 'image', 'sut_sha256')}
            manifest['cleanup_failed'] = False
            write(run / 'manifest.json', manifest)
            write(run / 'observation.json', dict(repeat=20, diagnostics=[], assertion_violations=[]))
            write(run / 'scenario.json', {})
            write(run / 'trace.json', {})
            (run / 'report.md').write_text('Constructed evidence\n')
            spec = dict(exit=0, observation={'repeat': 20}, schedule_id=None)
            self.assertEqual(inspect_trial(root, spec, 'B')['problems'], [])
            self.assertIn('missing_timing', inspect_trial(root, spec, 'Bplus')['problems'])
            spec['observation']['repeat'] = 21
            self.assertIn('observation:repeat', inspect_trial(root, spec, 'B')['problems'])
            write(root / 'cleanup.json', dict(no_new_containers=False, no_new_snapshot_jvms=True))
            self.assertIn('cleanup_not_observed', inspect_trial(root, spec, 'B')['problems'])
            (run / 'trace.json').unlink()
            self.assertIn('missing_artifact', inspect_trial(root, spec, 'B')['problems'])


if __name__ == '__main__':
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    print('LOCAL_PILOT_TEST_RESULT overlap=union evidence=checksummed missing=unknown')
