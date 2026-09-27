#!/usr/bin/env python3
"""Constructed accounting tests, not external adapter conformance evidence."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

M = runpy.run_path(str(Path(__file__).with_name('check-external-sut-acceptance.py')))


class AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan, cls.data = M['load_plan']()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.result = M['template'](self.plan, self.data, 'go')

    def validate(self, result=None):
        return M['validate_result'](self.result if result is None else result,
                                    self.plan, self.data, self.directory)

    def observed(self, target='go'):
        # These deliberately constructed records test accounting, not the SUT.
        result = M['template'](self.plan, self.data, target)
        artifact = self.directory / 'constructed.log'
        artifact.write_text('constructed accounting unit-test evidence\n')
        result['run'] = {
            'revision': 'a' * 40, 'command': 'go test ./example -count=20' if target == 'go' else './example-test --repeat 20',
            'repetitions': 20, 'repetition_method': 'constructed test input, not executed',
            'versions': {k: 'constructed' for k in ('go', 'java', 'spring', 'transaction_manager', 'jdbc_driver', 'pool', 'build_tool', 'mysql')},
            'artifacts': {'log': {'path': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}},
        }
        for name, checks in M['inventory'](self.plan, self.data, target).items():
            result['results'][name] = {
                'status': 'pass', 'reason': '',
                'checks': {check: {'status': 'pass', 'handler': 'constructed:test_handler', 'evidence': ['log']}
                           for check in checks},
            }
        return result

    def test_checked_in_manifests_match_templates(self):
        for target in M['TARGETS']:
            path = M['PLAN'].with_name('external-sut-results-' + target + '.json')
            result = M['decode'](path.read_bytes())
            self.assertEqual(result, M['template'](self.plan, self.data, target))
            counts = self.validate(result)
            self.assertEqual(counts['pass'], 0)
            self.assertGreater(counts['incomplete'], 0)

    def test_pin_matches_git_revision(self):
        pin = self.plan['vector']
        # No network: full checkout history must retain the pinned commit.
        subprocess.run(['git', 'merge-base', '--is-ancestor', pin['revision'], 'HEAD'],
                       cwd=M['ROOT'], check=True)
        raw = subprocess.check_output(['git', 'show', pin['revision'] + ':' + pin['path']], cwd=M['ROOT'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), pin['sha256'])

    def test_roles_and_occurrences(self):
        go = M['inventory'](self.plan, self.data, 'go')
        java = M['inventory'](self.plan, self.data, 'java')
        self.assertIn('case/readiness_mismatch', go)
        self.assertNotIn('case/readiness_mismatch', java)
        self.assertIn('step/0/output/start', go['case/success'])
        self.assertNotIn('step/0/expect/0/initialize_application', go['case/success'])
        self.assertIn('step/0/expect/0/initialize_application', java['case/success'])
        self.assertIn('control/fresh_decoder_accepts', java['framing/invalid_utf8'])
        self.assertIn('input/read_chunk_sizes', go['framing/fragmented_valid_frame'])
        for checks in list(go.values()) + list(java.values()):
            self.assertEqual(len(checks), len(set(checks)))
        repeated = [c for c in go['case/duplicate_stop_call'] if c.endswith('/call_pending')]
        self.assertGreater(len(repeated), 1)

    def test_missing_extra_and_nonapplicable_cases(self):
        for mutation in ('missing', 'unknown', 'wrong_target'):
            with self.subTest(mutation=mutation):
                result = copy.deepcopy(self.result)
                if mutation == 'missing':
                    result['results'].pop('case/success')
                else:
                    result['results']['case/' + ('future_release' if mutation == 'wrong_target' else 'typo')] = result['results']['case/success']
                with self.assertRaisesRegex(ValueError, 'case inventory'):
                    self.validate(result)

    def test_no_silent_pass(self):
        self.result['results']['case/success'].update(status='pass', reason='')
        with self.assertRaisesRegex(ValueError, 'unhandled checks'):
            self.validate()

    def test_friendly_missing_success(self):
        data = copy.deepcopy(self.data)
        data['cases'] = [c for c in data['cases'] if c['id'] != 'success']
        with self.assertRaisesRegex(ValueError, 'missing required success case'):
            M['require_success'](data)

    def test_constructed_complete_records(self):
        for target in M['TARGETS']:
            with self.subTest(target=target):
                counts = self.validate(self.observed(target))
                self.assertEqual(counts['incomplete'], 0)
                self.assertEqual(counts['fail'], 0)
                self.assertGreater(counts['pass'], 0)

    def test_missing_handler_assertion_artifact_and_unknown_check(self):
        for mutation in ('missing_check', 'missing_assertion', 'unknown_check', 'handler', 'artifact', 'failing_check'):
            with self.subTest(mutation=mutation):
                result = self.observed()
                checks = result['results']['case/success']['checks']
                key = next(iter(checks))
                if mutation == 'missing_check':
                    checks.pop(key)
                elif mutation == 'missing_assertion':
                    checks.pop(next(k for k in checks if '/expect/' in k))
                elif mutation == 'unknown_check':
                    checks['expect/typo'] = checks.pop(key)
                elif mutation == 'handler':
                    checks[key]['handler'] = ''
                elif mutation == 'artifact':
                    checks[key]['evidence'] = ['missing']
                else:
                    checks[key]['status'] = 'fail'
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_status_and_failure_accounting(self):
        for status in ('skip', 'not_applicable', 'PASS', None):
            with self.subTest(status=status):
                self.result['results']['case/success']['status'] = status
                with self.assertRaises(ValueError):
                    self.validate()
        result = self.observed()
        row = result['results']['case/success']
        row.update(status='fail', reason='constructed failure')
        with self.assertRaisesRegex(ValueError, 'failure observation'):
            self.validate(result)
        next(iter(row['checks'].values()))['status'] = 'fail'
        self.assertEqual(self.validate(result)['fail'], 1)

    def test_run_evidence(self):
        mutations = {
            'pin': lambda r: r['vector'].update(sha256='0' * 64),
            'command': lambda r: r['run'].update(command=''),
            'count': lambda r: r['run'].update(command='go test ./example -count=1'),
            'repetitions': lambda r: r['run'].update(repetitions=1),
            'boolean_count': lambda r: r['run'].update(repetitions=True),
            'versions': lambda r: r['run'].update(versions={}),
            'revision': lambda r: r['run'].update(revision='main'),
            'digest': lambda r: r['run']['artifacts']['log'].update(sha256='0' * 64),
            'missing_file': lambda r: r['run']['artifacts']['log'].update(path='missing.log'),
            'escape': lambda r: r['run']['artifacts']['log'].update(path='../outside.log'),
            'absolute': lambda r: r['run']['artifacts']['log'].update(path='/tmp/outside.log'),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                result = copy.deepcopy(self.observed())
                mutate(result)
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_duplicate_keys_and_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            M['decode'](b'{"case": 1, "case": 2}')
        self.result['ignored_field'] = True
        with self.assertRaisesRegex(ValueError, 'fields'):
            self.validate()

    def test_artifact_symlink_escape(self):
        result = self.observed()
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / 'log'
            path.write_text('external artifact')
            (self.directory / 'link.log').symlink_to(path)
            result['run']['artifacts']['log'].update(
                path='link.log', sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, 'escapes manifest directory'):
                self.validate(result)

    def test_cli_constructed_complete_and_failure(self):
        script = str(Path(__file__).with_name('check-external-sut-acceptance.py'))
        result = self.observed()
        path = self.directory / 'constructed.json'
        for failing, code in ((False, 0), (True, 1)):
            if failing:
                row = result['results']['case/success']
                row.update(status='fail', reason='constructed failure')
                next(iter(row['checks'].values()))['status'] = 'fail'
            path.write_text(json.dumps(result))
            process = subprocess.run([sys.executable, script, '--results', str(path), '--require-complete'],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, code, process.stderr)
            self.assertIn('acceptance=' + ('incomplete' if failing else 'complete'), process.stdout)

    def test_cli_incomplete_is_not_acceptance(self):
        script = str(Path(__file__).with_name('check-external-sut-acceptance.py'))
        path = str(M['PLAN'].with_name('external-sut-results-go.json'))
        for strict, code in (([], 0), (['--require-complete'], 1)):
            process = subprocess.run([sys.executable, script, '--results', path] + strict, capture_output=True, text=True)
            self.assertEqual(process.returncode, code, process.stderr)
            self.assertIn('manifest=valid acceptance=incomplete pass=0', process.stdout)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print('EXTERNAL_SUT_ACCOUNTING_TEST_RESULT omissions=rejected evidence=required runtime=not_executed')
    sys.exit(0 if result.wasSuccessful() else 1)
