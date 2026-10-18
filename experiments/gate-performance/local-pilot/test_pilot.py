"""Boundary tests using constructed intervals, never performance observations."""
import tempfile
import unittest
import sys
from pathlib import Path

from pilot import decomposition, inspect_trial, inventory, run_command, summary, verify, write


def span(i, parent, name, start, duration):
    return dict(id=i, parent_id=parent, name=name, start_ns=start, duration_ns=duration)


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
