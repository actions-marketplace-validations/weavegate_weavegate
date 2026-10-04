#!/usr/bin/env python3
"""Test paired evidence accounting with explicitly constructed logs and manifests."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECORDER = runpy.run_path(str(ROOT / 'scripts/record-external-sut-paired-results.py'))
ACCOUNTING = runpy.run_path(str(ROOT / 'scripts/check-external-sut-acceptance.py'))
REVISION = 'a' * 40
COMMAND = ("WEAVEGATE_SPRING_PAIRED=1 go test ./cmd/weavegate "
           "-run '^TestSpringMatchingPairedReplay$' -v -count=1")
ENVIRONMENT = ('SPRING_ENVIRONMENT go=go1.25.0 java=21.0.8+9-LTS mysql=8.4.6 spring_boot=4.0.8 '
               'spring=7.0.9 transaction_manager=spring-jdbc-7.0.9 jdbc_driver=9.7.0 pool=7.0.2 '
               'weavegate_spring=0.0.0-SNAPSHOT')


class PairedRecordingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.log = self.root / 'paired.log'
        self.plan, self.data = ACCOUNTING['load_plan']()
        self.go = self.isolated('go')
        self.java = self.isolated('java')

    def isolated(self, target, revision=REVISION, complete=True):
        directory = self.root / target
        directory.mkdir(exist_ok=True)
        log = directory / (target + '.log')
        log.write_text('constructed ' + target + ' evidence\n')
        manifest = ACCOUNTING['template'](self.plan, self.data, target)
        for name, checks in ACCOUNTING['inventory'](self.plan, self.data, target).items():
            if not complete and name.startswith('requirement/'):
                continue
            manifest['results'][name] = {'status': 'pass', 'reason': '', 'checks': {
                check: {'status': 'pass', 'handler': 'constructed', 'evidence': ['log']} for check in checks}}
        versions = {'go': 'go1.25.0'} if target == 'go' else dict.fromkeys(
            ('java', 'spring', 'transaction_manager', 'jdbc_driver', 'pool', 'build_tool'), 'constructed')
        manifest['run'] = {
            'revision': revision, 'repetitions': 20, 'repetition_method': 'constructed',
            'command': 'go test ./internal/sut/external -v -count=20' if target == 'go' else 'constructed',
            'versions': versions,
            'artifacts': {'log': {'path': log.name, 'sha256': hashlib.sha256(log.read_bytes()).hexdigest()}},
        }
        path = directory / (target + '.json')
        path.write_text(json.dumps(manifest))
        return path

    def write(self, lines=None, drop=None, repeat=None):
        body = ['=== RUN   TestSpringMatchingPairedReplay\n']
        for marker in list(RECORDER['markers'](20)) + [ENVIRONMENT]:
            if marker != drop:
                body.append('    spring_integration_test.go:1: ' + marker + '\n')
        if repeat:
            body.append('    spring_integration_test.go:1: ' + repeat + '\n')
        body.extend(lines if lines is not None else [
            '--- PASS: TestSpringMatchingPairedReplay (300.00s)\n', 'PASS\n',
            'ok  \tgithub.com/weavegate/weavegate/cmd/weavegate\t300.0s\n'])
        self.log.write_text(''.join(body))

    def record(self, **overrides):
        args = dict(log=self.log, go_manifest=self.go, java_manifest=self.java, revision=REVISION,
                    command=COMMAND, repetitions=20, build_tool='Apache Maven 3.9.16')
        args.update(overrides)
        return RECORDER['record'](**args)

    def test_complete_evidence_passes_strict_gate(self):
        self.write()
        result = self.record()
        counts = ACCOUNTING['validate_result'](result, self.plan, self.data, self.root)
        self.assertEqual(counts, {'pass': 2, 'fail': 0, 'incomplete': 0})
        self.assertEqual(result['run']['versions']['mysql'], '8.4.6')
        self.assertEqual(set(result['run']['artifacts']), {'paired-log', 'go-manifest', 'java-manifest'})

    def test_every_marker_is_required_once(self):
        for marker in RECORDER['markers'](20):
            for mode in ('drop', 'repeat'):
                with self.subTest(marker=marker[:40], mode=mode):
                    self.write(**{mode: marker})
                    with self.assertRaises(ValueError):
                        self.record()

    def test_marker_repetitions_must_match(self):
        self.write()
        with self.assertRaises(ValueError):
            self.record(repetitions=21)
        with self.assertRaises(ValueError):
            self.record(repetitions=19)

    def test_failed_skipped_or_truncated_logs(self):
        for lines in (['--- FAIL: TestSpringMatchingPairedReplay (1.00s)\n', 'FAIL\n'],
                      ['--- SKIP: TestSpringMatchingPairedReplay (0.00s)\n', 'PASS\n',
                       'ok  \tgithub.com/weavegate/weavegate/cmd/weavegate\t0.1s\n'],
                      ['--- PASS: TestSpringMatchingPairedReplay (300.00s)\n'], []):
            with self.subTest(lines=lines):
                self.write(lines)
                with self.assertRaises(ValueError):
                    self.record()

    def test_environment_is_required_and_pinned(self):
        self.write(drop=ENVIRONMENT)
        with self.assertRaises(ValueError):
            self.record()
        self.write(drop=ENVIRONMENT, repeat=ENVIRONMENT.replace('mysql=8.4.6', 'mysql=8.0.40'))
        with self.assertRaises(ValueError):
            self.record()

    def test_isolated_manifests_must_be_complete_same_revision(self):
        self.write()
        for target, kwargs in (('go', {'complete': False}), ('java', {'revision': 'b' * 40})):
            with self.subTest(target=target):
                broken = self.isolated(target, **kwargs)
                with self.assertRaises(ValueError):
                    self.record(**{target + '_manifest': broken})
                self.isolated(target)
        with self.assertRaises(ValueError):
            self.record(go_manifest=self.java)

    def test_command_and_revision(self):
        self.write()
        for overrides in ({'command': 'go test ./cmd/weavegate -run TestSpringMatchingPairedReplay'},
                          {'revision': 'HEAD'}, {'build_tool': ' '}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.record(**overrides)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PairedRecordingTests)
    result = unittest.TextTestRunner().run(suite)
    if result.wasSuccessful():
        print('EXTERNAL_SUT_PAIRED_RECORD_TEST_RESULT markers=required isolated=complete_same_revision failures=rejected')
    raise SystemExit(not result.wasSuccessful())
