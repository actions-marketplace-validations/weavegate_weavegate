#!/usr/bin/env python3
"""Constructed evidence tests; these are not E1/E2 measurements."""
import copy
import io
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from analysis import analyze, estimate, mean, quantile, trial as analyzed_trial
from ci import TASKS, endpoint, snapshot
from collector import collect, execute, freeze
from evidence import cleanup_inventory, receipt_valid
from execution import kill_group
from plan import validate_manifest
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
             'execution_pins': [{'path': str(self.hook), 'sha256': digest(self.hook), 'role': 'checker'},
                                {'path': sys.executable, 'sha256': digest(Path(sys.executable)),
                                 'role': 'Python interpreter'}],
             'protocols': {name: {'path': name, 'sha256': digest(self.source / name)} for name in PROTOCOLS}}
        for section in ('arms', 'inputs', 'runtime', 'environment', 'observation', 'sources'):
            m[section] = {'constructed': 'test only'}
        return m

    def frozen(self, m=None, name='bundle'):
        m = m or self.manifest
        manifest = self.source / 'manifest.json'
        write_json(manifest, m)
        bundle = self.root / name
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
                specs['run']['timeout_seconds'] = 5700
                m['expected_exit'][workload][arm] = [0]
        return m

    def block(self, bundle):
        return read_json(bundle / 'order.json')[0]['block']

    def test_signals_during_admission_preserve_stopped_bundle(self):
        import collector
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ('mkdir', 'registration', 'command', 'slot', 'seal', 'session'):
                bundle, registration = self.frozen(name='admit-%s-%s' % (signum, boundary))
                block = self.block(bundle)
                pair = bundle / 'blocks' / block
                sent = []
                def interrupt():
                    if not sent:
                        sent.append(signum)
                        os.kill(os.getpid(), signum)
                real_mkdir, real_write = Path.mkdir, collector.write_json
                real_seal, real_session = collector.seal, collector.Collection
                def mkdir(path, *args, **kwargs):
                    result = real_mkdir(path, *args, **kwargs)
                    if path == pair and boundary == 'mkdir':
                        interrupt()
                    return result
                def write(path, value):
                    result = real_write(path, value)
                    if path.name == {'registration': 'registration.json',
                                     'command': 'collection-command.json',
                                     'slot': 'slot.json'}.get(boundary):
                        interrupt()
                    return result
                def sealed(root):
                    result = real_seal(root)
                    if boundary == 'seal':
                        interrupt()
                    return result
                def session(*args):
                    result = real_session(*args)
                    if boundary == 'session':
                        interrupt()
                    return result
                with self.subTest(signum=signum, boundary=boundary):
                    with patch.object(Path, 'mkdir', mkdir), patch('collector.write_json', side_effect=write), \
                            patch('collector.seal', side_effect=sealed), \
                            patch('collector.Collection', side_effect=session), self.assertRaises(KeyboardInterrupt):
                        collect(bundle, block, registration)
                    self.assertEqual(sent, [signum])
                    self.assertTrue((bundle / 'stopped.json').exists())
                    verify(bundle)
                    result = analyze(bundle, self.root / ('admit-report-%s-%s' % (signum, boundary)))
                    self.assertTrue(all(arm['started'] == 0 for arms in result['accounting'].values()
                                        for arm in arms.values()))
                    with self.assertRaisesRegex(ValueError, 'campaign stopped'):
                        collect(bundle, block, registration)

    def test_signals_during_guard_mutation_preserve_stopped_bundle(self):
        import storage
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ('mkdir', 'attempt.json', 'redactions.json'):
                bundle, registration = self.frozen(name='guard-%s-%s' % (signum, boundary))
                slot = order(self.manifest)[0]
                directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                sent = []
                real_write, real_mkdir = storage.write_json, Path.mkdir
                def interrupt():
                    if not sent:
                        sent.append(signum)
                        os.kill(os.getpid(), signum)
                def write(path, value):
                    result = real_write(path, value)
                    if path == directory / boundary:
                        interrupt()
                    return result
                def mkdir(path, *args, **kwargs):
                    result = real_mkdir(path, *args, **kwargs)
                    if boundary == 'mkdir' and path == directory:
                        interrupt()
                    return result
                with self.subTest(signum=signum, boundary=boundary):
                    with patch('storage.write_json', side_effect=write), patch.object(Path, 'mkdir', mkdir), \
                            self.assertRaises(KeyboardInterrupt):
                        collect(bundle, slot['block'], registration)
                    self.assertEqual(sent, [signum])
                    verify(bundle)
                    self.assertTrue((bundle / 'stopped.json').exists())
                    self.assertFalse((directory.parent / 'integrity-error.json').exists())
                    if boundary == 'redactions.json':
                        self.assertTrue(read_json(directory / 'trial.json')['cleanup_proven'])
                    self.assertFalse(analyzed_trial(bundle, self.manifest, slot, slot['arms'][1])['started'])

    def test_lock_replacement_cannot_admit_another_collector(self):
        from collector import execute as real_execute
        from common import campaign_lock
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        seal_before = (bundle / 'SHA256SUMS').read_bytes()
        admitted = []
        def command(spec, context, directory, step, secrets):
            if step == 'prepare':
                seal_before_contents[0] = (bundle / 'SHA256SUMS').read_bytes()
                replacement = self.root / 'replacement.lock'
                replacement.write_bytes(b'')
                os.replace(replacement, bundle / 'collection.lock')
                try:
                    with campaign_lock(bundle):
                        admitted.append(True)
                except ValueError:
                    pass
            return real_execute(spec, context, directory, step, secrets)
        seal_before_contents = [seal_before]
        with patch('collector.execute', side_effect=command), self.assertRaisesRegex(ValueError, 'lock|protected'):
            collect(bundle, slot['block'], registration)
        self.assertEqual(admitted, [])
        self.assertEqual((bundle / 'SHA256SUMS').read_bytes(), seal_before_contents[0])
        directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
        self.assertTrue((directory / 'cleanup.command.json').exists())
        self.assertFalse((directory / 'run.command.json').exists())

    def test_cleanup_rejects_hard_link_to_product_original(self):
        from collector import execute as real_execute
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        def command(spec, context, directory, step, secrets):
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'cleanup':
                (directory / 'cleanup.log').unlink()
                os.link(directory / 'evidence.log', directory / 'cleanup.log')
                write_json(directory / 'cleanup.json', {'proven': True,
                           'evidence': {'cleanup.log': digest(directory / 'cleanup.log')}})
            return result
        with patch('collector.execute', side_effect=command):
            collect(bundle, slot['block'], registration)
        verify(bundle)
        self.assertTrue((bundle / 'stopped.json').exists())
        row = analyzed_trial(bundle, self.manifest, slot, slot['arms'][0])
        self.assertFalse(row['cleanup_proven'])
        self.assertEqual(row['reason'], 'cleanup_unproven')
        self.assertFalse(analyzed_trial(bundle, self.manifest, slot, slot['arms'][1])['started'])
        copied = self.root / 'copied-alias'
        shutil.copytree(bundle, copied, copy_function=shutil.copyfile)
        self.assertFalse(analyzed_trial(copied, self.manifest, slot, slot['arms'][0])['cleanup_proven'])

    def test_cleanup_rejects_hard_links_to_command_outputs(self):
        from collector import execute as real_execute
        for suffix in ('.stdout', '.stderr'):
            bundle, registration = self.frozen(name='output-alias-' + suffix[1:])
            slot = order(self.manifest)[0]
            def command(spec, context, directory, step, secrets):
                result = real_execute(spec, context, directory, step, secrets)
                if step == 'cleanup':
                    os.link(directory / ('run' + suffix), directory / 'teardown.log')
                    write_json(directory / 'cleanup.json', {'proven': True,
                               'evidence': {'teardown.log': digest(directory / 'teardown.log')}})
                return result
            with self.subTest(suffix=suffix), patch('collector.execute', side_effect=command):
                self.assertFalse(collect(bundle, slot['block'], registration))
                verify(bundle)
                self.assertTrue((bundle / 'stopped.json').exists())
                row = analyzed_trial(bundle, self.manifest, slot, slot['arms'][0])
                self.assertFalse(row['cleanup_proven'])
                self.assertEqual(row['reason'], 'cleanup_unproven')
                self.assertFalse(analyzed_trial(bundle, self.manifest, slot, slot['arms'][1])['started'])
                copied = self.root / ('copied-output-alias-' + suffix[1:])
                shutil.copytree(bundle, copied, copy_function=shutil.copyfile)
                verify(copied)
                self.assertFalse(analyzed_trial(copied, self.manifest, slot, slot['arms'][0])['cleanup_proven'])

    def test_cleanup_rejects_hard_links_to_control_receipts(self):
        original = self.hook.read_text()
        for name in ('attempt.json', 'redactions.json', 'cleanup-inputs.json'):
            self.hook.write_text(original + '''
if step=='cleanup':
 os.link(p/%r,p/'teardown.log')
 (p/'cleanup.json').write_text(json.dumps({'proven':True,
 'evidence':{'teardown.log':hashlib.sha256((p/'teardown.log').read_bytes()).hexdigest()}}))
''' % name)
            m = self.make_manifest()
            bundle, registration = self.frozen(m, 'control-alias-' + name)
            slot = order(m)[0]
            with self.subTest(name=name):
                self.assertFalse(collect(bundle, slot['block'], registration))
                verify(bundle)
                self.assertTrue((bundle / 'stopped.json').exists())
                directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                self.assertEqual(read_json(directory / 'cleanup.command.json')['exit'], 0)
                row = analyzed_trial(bundle, m, slot, slot['arms'][0])
                self.assertFalse(row['cleanup_proven'])
                self.assertEqual(row['reason'], 'cleanup_unproven')
                self.assertFalse(analyzed_trial(bundle, m, slot, slot['arms'][1])['started'])
                copied = self.root / ('copied-control-alias-' + name)
                shutil.copytree(bundle, copied, copy_function=shutil.copyfile)
                verify(copied)
                self.assertFalse(analyzed_trial(copied, m, slot, slot['arms'][0])['cleanup_proven'])

    def test_public_secrets_rejected_before_admission(self):
        cases = ('campaign-path', 'manifest-path', 'relocated-path', 'pin', 'json-pin',
                 'registration', 'expanded-argv', 'expanded-cwd', 'expanded-env')
        for kind in cases:
            secret = sha(kind.encode())
            m = copy.deepcopy(self.manifest)
            m['redact_env'] = ['TEST_REDACTION_SECRET']
            if kind.startswith('expanded-'):
                secret = 'candidate-' + secret
                spec = m['commands'][order(m)[0]['workload']]['candidate']['cleanup']
                value = '{arm}-' + secret.removeprefix('candidate-')
                if kind == 'expanded-argv':
                    spec['argv'].append(value)
                elif kind == 'expanded-cwd':
                    spec['cwd'] = '{trial_dir}/' + value
                else:
                    spec['env']['PUBLIC_VALUE'] = value
            if kind in ('pin', 'json-pin'):
                pin = self.source / ('private.json' if kind == 'json-pin' else 'private.log')
                # JSON escapes must be checked as decoded values too.
                data = ('{"value":"' + ''.join('\\u%04x' % ord(c) for c in secret) + '"}'
                        if kind == 'json-pin' else secret)
                pin.write_text(data)
                m['pins'].append({'path': pin.name, 'sha256': digest(pin), 'role': 'constructed'})
            source = self.source / (secret + '.json' if kind == 'manifest-path' else 'public.json')
            write_json(source, m)
            bundle = self.root / (secret if kind == 'campaign-path' else 'public-' + kind)
            freeze(source, bundle)
            if kind == 'relocated-path':
                moved = self.root / secret
                bundle.rename(moved)
                bundle = moved
            registration = self.root / 'public-registration.json'
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': secret if kind == 'registration' else 'constructed',
                                     'public_url': 'https://example.invalid/constructed'})
            before = (bundle / 'SHA256SUMS').read_bytes()
            with self.subTest(kind=kind), patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}), \
                    patch('collector.execute') as command:
                with self.assertRaisesRegex(ValueError, 'secret.*public') as error:
                    collect(bundle, self.block(bundle), registration)
                self.assertNotIn(secret, str(error.exception))
                command.assert_not_called()
                self.assertFalse((bundle / 'blocks').exists())
                self.assertFalse((bundle / 'registration.json').exists())
                self.assertEqual((bundle / 'SHA256SUMS').read_bytes(), before)
                verify(bundle)

    def workload_manifest(self, experiment, workload, counts):
        m = copy.deepcopy(self.manifest)
        m['experiment'] = experiment
        m['strata'] = {workload: counts}
        m['dates'] = [(datetime.now(timezone.utc) + timedelta(days=index)).strftime('%Y-%m-%d')
                      for index in range(len(counts))]
        for key in ('commands', 'expected_facts', 'expected_exit'):
            m[key] = {workload: m[key]['example-fixed-replay']}
        return m

    def test_block_component_length_checked_before_freeze(self):
        for experiment in ('E3', 'E4'):
            for length, counts in ((250, [1]), (249, [1]), (248, [1000]), (248, [1] * 100)):
                m = self.workload_manifest(experiment, 'w' * length, counts)
                destination = self.root / ('long-' + experiment + '-' + str(length) + '-' + str(len(counts)))
                with self.subTest(experiment=experiment, length=length, counts=counts):
                    with self.assertRaisesRegex(ValueError, 'block.*255'):
                        self.frozen(m, destination.name)
                    self.assertFalse(destination.exists())
            for length, counts in ((248, [1]), (247, [1000]), (247, [1] * 100)):
                validate_manifest(self.workload_manifest(experiment, 'w' * length, counts))
            m = self.workload_manifest(experiment, 'w' * 248, [1])
            bundle, registration = self.frozen(m, 'limit-' + experiment)
            self.assertEqual(len(self.block(bundle).encode()), 255)
            self.assertTrue(collect(bundle, self.block(bundle), registration))
            verify(bundle)

    def test_executable_permissions_checked_before_freeze_and_admission(self):
        binary = self.source / 'pinned-python'
        shutil.copyfile(sys.executable, binary)
        m = copy.deepcopy(self.manifest)
        m['execution_pins'].append({'path': str(binary), 'sha256': digest(binary), 'role': 'interpreter'})
        for arms in m['commands'].values():
            for commands in arms.values():
                for spec in commands.values():
                    spec['argv'][0] = str(binary)
        binary.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'executable'):
            self.frozen(m, 'nonexecutable')
        self.assertFalse((self.root / 'nonexecutable').exists())
        binary.chmod(0o700)
        bundle, registration = self.frozen(m)
        binary.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'executable'):
            collect(bundle, self.block(bundle), registration)
        self.assertFalse((bundle / 'blocks' / self.block(bundle)).exists())
        verify(bundle)

    def test_every_executable_requires_a_registered_pin(self):
        unpinned = self.source / 'unpinned-python'
        unpinned.symlink_to(sys.executable)
        for index, (executable, selected) in enumerate(((str(unpinned), None),
                (unpinned.name, None), (sys.executable, [str(self.hook)]))):
            m = copy.deepcopy(self.manifest)
            spec = m['commands']['example-fixed-replay']['baseline']['cleanup']
            spec['argv'][0] = executable
            spec['env']['PATH'] = str(self.source)
            if selected is not None:
                spec['execution_pins'] = selected
            source = self.source / 'unpinned.json'
            write_json(source, m)
            bundle = self.root / ('unpinned-%d' % index)
            with self.subTest(executable=executable, selected=selected):
                with self.assertRaisesRegex(ValueError, 'executable.*pin'):
                    freeze(source, bundle)
                self.assertFalse(bundle.exists())

    def test_frozen_executable_binding_rejects_drift_and_ignores_path_shadowing(self):
        binary = self.source / 'pinned-python'
        shutil.copyfile(sys.executable, binary)
        binary.chmod(0o700)
        original = binary.read_bytes()
        early = self.source / 'early'
        early.mkdir()
        for bare in (False, True):
            m = copy.deepcopy(self.manifest)
            m['execution_pins'].append({'path': str(binary), 'sha256': digest(binary), 'role': 'interpreter'})
            for arms in m['commands'].values():
                for commands in arms.values():
                    for spec in commands.values():
                        spec['argv'][0] = binary.name if bare else str(binary)
                        spec['env']['PATH'] = str(early) + os.pathsep + str(self.source)
            bundle, registration = self.frozen(m, 'binding-%s' % bare)
            frozen = read_json(bundle / 'campaign.json')
            slot = order(frozen)[0]
            binary.write_bytes(b'changed executable')
            with self.subTest(bare=bare), self.assertRaisesRegex(ValueError, 'live execution pin mismatch'):
                collect(bundle, slot['block'], registration)
            self.assertFalse((bundle / 'blocks').exists())
            binary.write_bytes(original)
            if bare:
                shadow = early / binary.name
                shadow.write_text('#!/bin/sh\nexit 99\n')
                shadow.chmod(0o700)
            self.assertTrue(collect(bundle, slot['block'], registration))
            verify(bundle)
            for arm in slot['arms']:
                directory = bundle / 'blocks' / slot['block'] / arm
                self.assertEqual(read_json(directory / 'run.command.json')['argv'][0], str(binary))
                self.assertEqual(read_json(directory / 'trial.json')['classification'], 'complete')

    def test_vanished_timeout_group_preserves_campaign_reuse(self):
        from collector import execute as real_execute
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        def command(spec, context, directory, step, secrets):
            if step != 'run':
                return real_execute(spec, context, directory, step, secrets)
            process = Mock(pid=123)
            process.wait.side_effect = [subprocess.TimeoutExpired(spec['argv'], 600), 0, 0]
            with patch('execution.subprocess.Popen', return_value=process), \
                    patch('execution.os.killpg', side_effect=ProcessLookupError), \
                    patch('execution.wait_group', return_value=True) as absent:
                result = real_execute(spec, context, directory, step, secrets)
                absent.assert_called_once()
                return result
        with patch('collector.execute', side_effect=command):
            self.assertTrue(collect(bundle, slot['block'], registration))
        verify(bundle)
        self.assertFalse((bundle / 'stopped.json').exists())
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            receipt = read_json(directory / 'run.command.json')
            self.assertTrue(receipt['timeout'])
            self.assertTrue(receipt['containment_proven'])
            self.assertIsNone(receipt['error'])
            row = read_json(directory / 'trial.json')
            self.assertTrue(row['started'])
            self.assertTrue(row['cleanup_proven'])
            self.assertEqual(row, analyzed_trial(bundle, self.manifest, slot, arm))

    def test_observation_must_exist_before_later_hooks(self):
        original = self.hook.read_text()
        for late in ('check', 'cleanup'):
            for missing in ('receipt', 'original'):
                script = original
                if missing == 'receipt':
                    script = script.replace("if step=='observe':", "if step==%r:" % late)
                else:
                    script += "\nif step=='observe': (p/'evidence.log').unlink()\n"
                    script += "if step==%r: (p/'evidence.log').write_bytes(%r)\n" % (
                        late, b'synthetic oracle and cleanup evidence\n')
                self.hook.write_text(script)
                m = self.make_manifest()
                bundle, registration = self.frozen(m, late + '-' + missing)
                slot = order(m)[0]
                with self.subTest(late=late, missing=missing):
                    self.assertTrue(collect(bundle, slot['block'], registration))
                    verify(bundle)
                    for arm in slot['arms']:
                        directory = bundle / 'blocks' / slot['block'] / arm
                        row = read_json(directory / 'trial.json')
                        self.assertEqual(row['classification'], 'quality_failure')
                        self.assertFalse((directory / 'check.command.json').exists())
                        self.assertTrue(row['cleanup_proven'])
                        self.assertEqual(row, analyzed_trial(bundle, m, slot, arm))

    def test_inventory_preserves_hardlinked_originals(self):
        self.hook.write_text(self.hook.read_text() + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 for name in ('trials.csv','commands.jsonl','events.jsonl','environments.jsonl'):
  target=p/('original-'+name)
  os.link(p.parents[2]/name,target)
  receipt['evidence'][target.name]=hashlib.sha256(target.read_bytes()).hexdigest()
 (p/'receipt.json').write_text(json.dumps(receipt))
''')
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        originals = {name: (bundle / name).read_bytes() for name in
                     ('trials.csv', 'commands.jsonl', 'events.jsonl', 'environments.jsonl')}
        self.assertTrue(collect(bundle, slot['block'], registration))
        verify(bundle)
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            for name, data in originals.items():
                with self.subTest(arm=arm, name=name):
                    self.assertEqual((directory / ('original-' + name)).read_bytes(), data)
                    self.assertFalse(os.path.samestat((directory / ('original-' + name)).stat(),
                                                     (bundle / name).stat()))
            row = analyzed_trial(bundle, m, slot, arm)
            self.assertEqual(row['classification'], 'complete')
            self.assertEqual(row, read_json(directory / 'trial.json'))

    def test_signals_during_final_seal_preserve_stopped_bundle(self):
        import collector
        for signum in (signal.SIGINT, signal.SIGTERM):
            for boundary in ('inventory', 'seal'):
                for when in ('before', 'after'):
                    bundle, registration = self.frozen(name='%s-%s-%s' % (signum, boundary, when))
                    slot = order(self.manifest)[0]
                    original = getattr(collector, boundary)
                    sent = []
                    def checkpoint(*args):
                        ready = not sent and all((bundle / 'blocks' / slot['block'] / arm /
                                                  'trial.json').exists() for arm in slot['arms'])
                        def interrupt():
                            sent.append(signum)
                            os.kill(os.getpid(), signum)
                        if ready and when == 'before':
                            interrupt()
                        result = original(*args)
                        if ready and when == 'after':
                            interrupt()
                        return result
                    with self.subTest(signum=signum, boundary=boundary, when=when):
                        with patch('collector.' + boundary, side_effect=checkpoint), \
                                self.assertRaises(KeyboardInterrupt):
                            collect(bundle, slot['block'], registration)
                        self.assertEqual(sent, [signum])
                        verify(bundle)
                        self.assertEqual(read_json(bundle / 'stopped.json')['reason'], 'interrupted_collection')
                        for arm in slot['arms']:
                            self.assertEqual(analyzed_trial(bundle, self.manifest, slot, arm)['classification'],
                                             'complete')
                        with self.assertRaisesRegex(ValueError, 'campaign stopped'):
                            collect(bundle, slot['block'], registration)

    def test_signals_during_cleanup_pins_still_run_trusted_teardown(self):
        from collector import verify_execution_pins as real_verify
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['cleanup']['execution_pins'] = [str(self.hook), sys.executable]
        for signum in (signal.SIGINT, signal.SIGTERM):
            bundle, registration = self.frozen(m, 'cleanup-pins-%s' % signum)
            slot = order(m)[0]
            sent = []
            def pins(manifest, selected=None):
                if selected is not None and not sent:
                    sent.append(signum)
                    os.kill(os.getpid(), signum)
                return real_verify(manifest, selected)
            with self.subTest(signum=signum):
                with patch('collector.verify_execution_pins', side_effect=pins), \
                        self.assertRaises(KeyboardInterrupt):
                    collect(bundle, slot['block'], registration)
                verify(bundle)
                directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                self.assertTrue((directory / 'cleanup.command.json').exists())
                self.assertTrue(read_json(directory / 'trial.json')['cleanup_proven'])
                self.assertTrue((bundle / 'stopped.json').exists())
                self.assertFalse(analyzed_trial(bundle, m, slot, slot['arms'][1])['started'])

    def test_executable_resolution_must_be_explicit_before_freeze(self):
        for index, executable in enumerate(('python3', './python3', 'bin/python3',
                                             '{trial_dir}/python3', '{campaign_dir}/bin/python3')):
            m = copy.deepcopy(self.manifest)
            spec = m['commands']['example-fixed-replay']['baseline']['run']
            spec['argv'][0] = executable
            source = self.source / 'resolution.json'
            write_json(source, m)
            bundle = self.root / ('resolution-%d' % index)
            with self.subTest(executable=executable), self.assertRaisesRegex(ValueError, 'executable'):
                freeze(source, bundle)
            self.assertFalse(bundle.exists())
        for executable in ('python3', sys.executable):
            m = copy.deepcopy(self.manifest)
            spec = m['commands']['example-fixed-replay']['baseline']['run']
            spec['argv'][0] = executable
            if executable == 'python3':
                spec['env']['PATH'] = '/usr/bin:/bin'
            validate_manifest(m)

    def test_signals_during_cleanup_spawn_wait_for_teardown_checkpoint(self):
        real_popen = subprocess.Popen
        for signum in (signal.SIGINT, signal.SIGTERM):
            bundle, registration = self.frozen(name='cleanup-spawn-%s' % signum)
            slot = order(self.manifest)[0]
            children = []
            def spawn(argv, **kwargs):
                process = real_popen(argv, **kwargs)
                if argv[2] == 'cleanup':
                    children.append(process)
                    os.kill(os.getpid(), signum)
                return process
            with self.subTest(signum=signum):
                with patch('execution.subprocess.Popen', side_effect=spawn), \
                        self.assertRaises(KeyboardInterrupt):
                    collect(bundle, slot['block'], registration)
                self.assertEqual([child.poll() for child in children], [0])
                verify(bundle)
                directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                self.assertTrue(read_json(directory / 'trial.json')['cleanup_proven'])
                self.assertFalse(read_json(directory / 'cleanup.command.json')['interrupted'])
                self.assertTrue((bundle / 'stopped.json').exists())

    def test_analysis_holds_campaign_lock_until_output_is_sealed(self):
        from common import campaign_lock
        from collector import exclusive
        from analysis import verify as real_verify, trial as real_trial
        bundle, _ = self.frozen()
        stages = set()
        def check_lock(stage):
            stages.add(stage)
            with self.assertRaises(ValueError):
                with exclusive(bundle):
                    pass
        def verified(root):
            check_lock('verify')
            return real_verify(root)
        def reduced(*args):
            check_lock('trial')
            return real_trial(*args)
        def sealed(root):
            check_lock('seal')
            return seal(root)
        with patch('analysis.verify', side_effect=verified), patch('analysis.trial', side_effect=reduced), \
                patch('common.seal', side_effect=sealed):
            result = analyze(bundle, self.root / 'locked-analysis')
        self.assertEqual(stages, {'verify', 'trial', 'seal'})
        self.assertEqual(result['input_sha256sums'], digest(bundle / 'SHA256SUMS'))
        with exclusive(bundle):
            with self.assertRaises(ValueError):
                analyze(bundle, self.root / 'blocked-analysis')
        self.assertFalse((self.root / 'blocked-analysis').exists())
        # Independent readers may coexist, including on read-only archives.
        (bundle / 'collection.lock').chmod(0o400)
        with campaign_lock(bundle, shared=True):
            analyze(bundle, self.root / 'shared-analysis')
        (bundle / 'order.json').write_text('[]')
        with self.assertRaises(ValueError):
            analyze(bundle, self.root / 'invalid-analysis')
        with exclusive(bundle):
            pass  # Failure releases the lock as well as successful publication.

    def test_collector_owned_paths_are_not_original_evidence(self):
        from evidence import hashes_valid
        from storage import COMMAND_FILES, CONTROL
        directory = self.root / 'originals'
        directory.mkdir()
        for name in sorted(COMMAND_FILES | CONTROL):
            path = directory / name
            path.write_text('constructed collector metadata')
            for reference in (name, './' + name):
                with self.subTest(name=reference), self.assertRaisesRegex(ValueError, 'collector-owned'):
                    hashes_valid(directory, {reference: digest(path)})
            nested = directory / 'originals' / name
            nested.parent.mkdir(exist_ok=True)
            nested.write_text('constructed product evidence')
            hashes_valid(directory, {str(nested.relative_to(directory)): digest(nested)})

    def test_observation_rejects_collector_receipts(self):
        self.hook.write_text(self.hook.read_text() + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 receipt['evidence']={'prepare.command.json':hashlib.sha256((p/'prepare.command.json').read_bytes()).hexdigest()}
 (p/'receipt.json').write_text(json.dumps(receipt))
 (p/'evidence.log').unlink()
''')
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertTrue(collect(bundle, slot['block'], registration))
        verify(bundle)
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertEqual(row['classification'], 'quality_failure')
            self.assertIn('collector-owned', row['reason'])
            self.assertEqual(row, analyzed_trial(bundle, m, slot, arm))

    def assert_cleanup_rejects_old_evidence(self, name):
        self.hook.write_text(self.hook.read_text() + '''
if step=='cleanup':
 name=%r
 (p/'cleanup.json').write_text(json.dumps({'proven':True,
 'evidence':{name:hashlib.sha256((p/name).read_bytes()).hexdigest()}}))
 (p/'cleanup.log').unlink()
''' % name)
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertFalse(collect(bundle, slot['block'], registration))
        verify(bundle)
        first, second = slot['arms']
        row = read_json(bundle / 'blocks' / slot['block'] / first / 'trial.json')
        self.assertFalse(row['cleanup_proven'])
        self.assertEqual(row['reason'], 'cleanup_unproven')
        self.assertEqual(row, analyzed_trial(bundle, m, slot, first))
        self.assertFalse(analyzed_trial(bundle, m, slot, second)['started'])

    def test_cleanup_rejects_collector_receipts(self):
        self.assert_cleanup_rejects_old_evidence('prepare.command.json')

    def test_cleanup_rejects_prior_evidence(self):
        self.assert_cleanup_rejects_old_evidence('./evidence.log')

    def test_cleanup_rejects_proof_retained_before_teardown(self):
        self.hook.write_text(self.hook.read_text().replace("if step=='cleanup':", "if step=='observe':"))
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertFalse(collect(bundle, slot['block'], registration))
        verify(bundle)
        for arm in slot['arms']:
            self.assertFalse(analyzed_trial(bundle, m, slot, arm)['cleanup_proven'])

    def test_redacting_hardlinks_preserves_external_originals(self):
        from redaction import sanitize
        directory = self.root / 'retained'
        directory.mkdir()
        for suffix, data in (('.log', b'constructed-secret'),
                             ('.json', b'{"value": "constructed-secret"}\n')):
            source = self.root / ('external' + suffix)
            source.write_bytes(data)
            paths = [directory / ('copy-%d' % index + suffix) for index in range(2)]
            for path in paths:
                os.link(source, path)
            with self.subTest(suffix=suffix):
                self.assertEqual(sanitize(directory, [b'constructed-secret']), 2)
                self.assertEqual(source.read_bytes(), data)
                for path in paths:
                    self.assertIn(b'[REDACTED]', path.read_bytes())
                    self.assertNotIn(b'constructed-secret', path.read_bytes())
                    self.assertFalse(os.path.samestat(source.stat(), path.stat()))

    def test_hooks_cannot_replace_current_command_outputs(self):
        from collector import execute as real_execute
        for suffix in ('.stdout', '.stderr'):
            for mutation in ('replace', 'remove', 'same_bytes'):
                m = self.make_manifest()
                slot = order(m)[0]
                script = ("import os; from pathlib import Path; "
                          "p=Path('{trial_dir}')/" + repr('run' + suffix) + "; "
                          "os.write(%d, b'original output'); p.unlink(); " %
                          (1 if suffix == '.stdout' else 2))
                if mutation != 'remove':
                    script += "p.write_bytes(%r)" % (b'original output' if mutation == 'same_bytes' else b'forged')
                m['commands'][slot['workload']][slot['arms'][0]]['run']['argv'] = [sys.executable, '-c', script]
                source = self.source / 'output-replacement.json'
                write_json(source, m)
                bundle = self.root / (suffix[1:] + '-' + mutation)
                freeze(source, bundle)
                registration = self.root / 'output-registration.json'
                write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                         'order_sha256': digest(bundle / 'order.json'),
                                         'registered_at': '2020-01-01T00:00:00Z',
                                         'revision': 'constructed', 'public_url': 'https://example.invalid'})
                original_seal = []
                def command(spec, context, directory, step, secrets):
                    if step == 'run':
                        original_seal.append((bundle / 'SHA256SUMS').read_bytes())
                    return real_execute(spec, context, directory, step, secrets)
                with self.subTest(suffix=suffix, mutation=mutation):
                    with patch('collector.execute', side_effect=command), \
                            self.assertRaisesRegex(ValueError, 'refusing to reseal'):
                        collect(bundle, slot['block'], registration)
                    directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                    self.assertTrue((directory / 'cleanup.command.json').exists())
                    self.assertTrue((directory.parent / 'integrity-error.json').exists())
                    self.assertEqual((bundle / 'SHA256SUMS').read_bytes(), original_seal[0])
                    with self.assertRaises(ValueError):
                        verify(bundle)

    def test_relative_working_directories_fail_before_freeze(self):
        for index, cwd in enumerate(('.', 'relative/path', '{arm}', '{block_id}/work',
                                     '{unknown}/work', '{trial_dir}suffix')):
            m = copy.deepcopy(self.manifest)
            m['commands']['example-fixed-replay']['baseline']['run']['cwd'] = cwd
            source = self.source / 'relative-cwd.json'
            write_json(source, m)
            destination = self.root / ('relative-cwd-%d' % index)
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, 'absolute'):
                freeze(source, destination)
            self.assertFalse(destination.exists())

    def test_working_directory_placeholders_expand_to_absolute_paths(self):
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for specs in arms.values():
                for step, spec in specs.items():
                    spec['cwd'] = '{campaign_dir}' if step == 'prepare' else '{trial_dir}'
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        collect(bundle, slot['block'], registration)
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            self.assertEqual(read_json(directory / 'prepare.command.json')['cwd'], str(bundle))
            self.assertEqual(read_json(directory / 'run.command.json')['cwd'], str(directory))
            self.assertEqual(read_json(directory / 'trial.json')['classification'], 'complete')

    def test_retained_registration_is_validated_without_commands(self):
        bundle, registration = self.frozen()
        analyze(bundle, self.root / 'unregistered-frozen')
        original = read_json(registration)
        for index, (key, value) in enumerate((('campaign_sha256', '0' * 64),
                                            ('order_sha256', '0' * 64),
                                            ('public_url', 'invalid'),
                                            ('registered_at', 'invalid'))):
            write_json(bundle / 'registration.json', dict(original, **{key: value}))
            seal(bundle)
            with self.subTest(key=key), self.assertRaises(ValueError):
                analyze(bundle, self.root / ('bad-registration-%d' % index))
        write_json(bundle / 'registration.json', original)
        seal(bundle)
        analyze(bundle, self.root / 'valid-registration')

    def test_interrupted_clean_arm_stops_campaign_and_retains_missing_mate(self):
        from collector import execute as real_execute
        for index, interruption in enumerate((KeyboardInterrupt, SystemExit)):
            source = self.source / 'interrupted.json'
            write_json(source, self.manifest)
            bundle = self.root / ('interrupted-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid'})
            slot, next_slot = order(self.manifest)[:2]
            def command(spec, context, directory, step, secrets):
                result = real_execute(spec, context, directory, step, secrets)
                if step == 'run':
                    raise interruption()
                return result
            with self.subTest(interruption=interruption), patch('collector.execute', side_effect=command):
                with self.assertRaises(interruption):
                    collect(bundle, slot['block'], registration)
            verify(bundle)
            self.assertEqual(read_json(bundle / 'stopped.json')['reason'], 'interrupted_collection')
            first = read_json(bundle / 'blocks' / slot['block'] / slot['arms'][0] / 'trial.json')
            self.assertTrue(first['cleanup_proven'])
            result = analyze(bundle, self.root / ('interrupted-analysis-%d' % index))
            mate = result['accounting'][slot['workload']][slot['arms'][1]]
            self.assertEqual(mate['started'], 0)
            self.assertEqual(mate['reasons']['interrupted_collection'], 1)
            self.assertIn('Collection status: stopped',
                          (self.root / ('interrupted-analysis-%d' % index) / 'report.md').read_text())
            with self.assertRaisesRegex(ValueError, 'campaign stopped'):
                collect(bundle, next_slot['block'], registration)

    def test_secret_bearing_paths_refuse_sealing_even_if_cleanup_removes_them(self):
        from collector import execute as real_execute
        for index, kind in enumerate(('file', 'directory', 'empty-directory')):
            secret = sha(b'synthetic-path-credential')
            m = copy.deepcopy(self.manifest)
            m['redact_env'] = ['TEST_REDACTION_SECRET']
            source = self.source / 'secret-path.json'
            write_json(source, m)
            bundle = self.root / ('secret-path-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('secret-registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid'})
            calls = []
            def command(spec, context, directory, step, secrets):
                calls.append(step)
                result = real_execute(spec, context, directory, step, secrets)
                path = directory / secret
                if step == 'prepare':
                    if kind == 'file':
                        path.write_text('constructed')
                    else:
                        path.mkdir()
                        if kind == 'directory':
                            (path / 'evidence.log').write_text('constructed')
                elif step == 'cleanup':
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                return result
            with self.subTest(kind=kind), patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}), \
                    patch('collector.execute', side_effect=command):
                with self.assertRaisesRegex(ValueError, 'refusing to reseal') as failure:
                    collect(bundle, self.block(bundle), registration)
            self.assertIn('cleanup', calls)
            self.assertNotIn('run', calls)
            self.assertNotIn(secret, str(failure.exception))
            self.assertNotIn(secret, (bundle / 'SHA256SUMS').read_text())
            self.assertNotIn(secret, (bundle / 'blocks' / self.block(bundle) / 'integrity-error.json').read_text())
            with self.assertRaises(ValueError):
                verify(bundle)

    def test_empty_directories_are_authenticated_and_guarded(self):
        from storage import BundleGuard
        bundle = self.root / 'directory-bundle'
        bundle.mkdir()
        (bundle / 'collection.lock').touch()
        (bundle / 'retained').mkdir()
        seal(bundle)
        verify(bundle)
        (bundle / 'retained').rmdir()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            verify(bundle)
        (bundle / 'retained').mkdir()
        verify(bundle)
        guard = BundleGuard(bundle)
        (bundle / 'unregistered').mkdir()
        with self.assertRaisesRegex(ValueError, 'unregistered evidence'):
            guard.check()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            verify(bundle)
        (bundle / 'unregistered').rmdir()
        with self.assertRaisesRegex(ValueError, 'unregistered evidence'):
            guard.check()
        (bundle / 'retained').rmdir()
        (bundle / 'retained').write_text('')
        with self.assertRaisesRegex(ValueError, 'inventory'):
            verify(bundle)

    def test_hook_cannot_add_empty_directories_outside_active_arm(self):
        from collector import execute as real_execute
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        calls = []
        def command(spec, context, directory, step, secrets):
            calls.append(step)
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'prepare':
                (bundle / 'blocks' / 'unregistered').mkdir()
            return result
        with patch('collector.execute', side_effect=command):
            with self.assertRaisesRegex(ValueError, 'refusing to reseal'):
                collect(bundle, slot['block'], registration)
        self.assertIn('cleanup', calls)
        self.assertNotIn('run', calls)
        with self.assertRaises(ValueError):
            verify(bundle)

    def test_plan_container_boundaries_fail_before_consuming_a_campaign(self):
        for key in ('strata', 'commands', 'expected_facts', 'expected_exit', 'protocols',
                    'dates', 'pins', 'execution_pins'):
            for index, value in enumerate((None, True, 'value', [], {})):
                m = copy.deepcopy(self.manifest)
                m[key] = value
                source = self.source / 'shape.json'
                write_json(source, m)
                destination = self.root / ('shape-%s-%d' % (key, index))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    freeze(source, destination)
                self.assertFalse(destination.exists())

    def test_observer_original_is_immutable_and_clock_is_joined_only_in_views(self):
        from collector import execute as real_execute
        originals = {}
        def command(spec, context, directory, step, secrets):
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'observe':
                originals[context['arm']] = (directory / 'receipt.json').read_bytes()
            return result
        bundle, registration = self.frozen()
        block = self.block(bundle)
        with patch('collector.execute', side_effect=command):
            self.assertTrue(collect(bundle, block, registration))
        for arm in ('baseline', 'candidate'):
            directory = bundle / 'blocks' / block / arm
            self.assertEqual((directory / 'receipt.json').read_bytes(), originals[arm])
            self.assertNotIn('gate_seconds', read_json(directory / 'receipt.json')['metrics'])
            row = read_json(directory / 'trial.json')
            self.assertEqual(row, analyzed_trial(bundle, self.manifest, order(self.manifest)[0], arm))
            self.assertEqual(row['metrics']['gate_seconds'],
                             read_json(directory / 'run.command.json')['seconds'])
            self.assertEqual(row['classification'], 'complete')

    def test_every_hook_boundary_protects_owned_receipts_and_originals(self):
        from collector import execute as real_execute
        cases = [('prepare', 'run.command.json'), ('prepare', 'trial.json'),
                 ('observe', 'run.command.json'), ('check', 'evidence.log'),
                 ('cleanup', 'receipt.json')]
        for index, (when, name) in enumerate(cases):
            source = self.source / 'boundary.json'
            write_json(source, self.manifest)
            bundle = self.root / ('boundary-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('boundary-registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid'})
            calls = []
            def command(spec, context, directory, step, secrets):
                calls.append((context['arm'], step))
                result = real_execute(spec, context, directory, step, secrets)
                if step == when:
                    (directory / name).write_text('hook replacement')
                return result
            with self.subTest(when=when, name=name), patch('collector.execute', side_effect=command):
                with self.assertRaisesRegex(ValueError, 'refusing to reseal'):
                    collect(bundle, self.block(bundle), registration)
            first, second = order(self.manifest)[0]['arms']
            self.assertIn((first, 'cleanup'), calls)
            self.assertNotIn((second, 'prepare'), calls)
            with self.assertRaises(ValueError):
                verify(bundle)

    def test_analysis_rebuilds_failed_accounting_without_cached_trial_rows(self):
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['run']['env']['RUN_EXIT'] = '4'
        bundle, registration = self.frozen(m)
        block = self.block(bundle)
        collect(bundle, block, registration)
        baseline = bundle / 'blocks' / block / 'baseline' / 'trial.json'
        candidate = bundle / 'blocks' / block / 'candidate' / 'trial.json'
        write_json(baseline, {'started': False, 'classification': 'complete',
                              'metrics': {'gate_seconds': 0.001}})
        candidate.unlink()
        seal(bundle)  # A transport checksum is not an outcome authority.
        result = analyze(bundle, self.root / 'reduced')
        for arm in ('baseline', 'candidate'):
            counts = result['accounting'][order(m)[0]['workload']][arm]
            self.assertEqual(counts['started'], 1)
            self.assertEqual(counts['operational_failure'], 1)
            self.assertEqual(counts.get('complete', 0), 0)

    def test_reanalysis_binds_commands_for_successes_and_failures(self):
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        collect(bundle, slot['block'], registration)
        for arm in slot['arms']:
            path = bundle / 'blocks' / slot['block'] / arm / 'run.command.json'
            receipt = read_json(path)
            receipt['argv'] = ['/invented/fast-success']
            if arm == slot['arms'][1]:
                receipt['exit'] = 4
            write_json(path, receipt)
        seal(bundle)
        result = analyze(bundle, self.root / 'invocation-bindings')
        for arm in slot['arms']:
            self.assertEqual(result['accounting'][slot['workload']][arm]['quality_failure'], 1)
        self.assertTrue(all('frozen invocation' in r for r in result['paired_missingness'][slot['block']]))

    def test_nested_original_names_never_inherit_control_redaction_exemptions(self):
        from redaction import sanitize
        secret = b'secret-payload-value'
        for name in ('trial.json', 'run.command.json', 'slot.json', 'redactions.json'):
            path = self.root / 'nested' / name
            path.parent.mkdir(exist_ok=True)
            write_json(path, {'argv': [secret.decode()], 'metrics': {secret.decode(): 1},
                              'collection_root': secret.decode(), 'replacements': secret.decode()})
        sanitize(self.root, [secret])
        for path in (self.root / 'nested').iterdir():
            self.assertNotIn(secret, path.read_bytes())
            self.assertIn(b'[REDACTED]', path.read_bytes())

    def test_sigterm_retains_started_receipt_cleanup_and_offline_accounting(self):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        address = str(self.root / 'ready.sock')
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(10)
        m = copy.deepcopy(self.manifest)
        script = ('import socket,threading,sys; s=socket.socket(socket.AF_UNIX); '
                  's.connect(sys.argv[1]); s.sendall(b"ready"); s.close(); threading.Event().wait()')
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['run']['argv'] = [sys.executable, '-c', script, address]
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        process = subprocess.Popen([sys.executable, '-B', str(HERE / 'tool.py'), 'collect',
                                    str(bundle), '--block', slot['block'], '--registration',
                                    str(registration)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        connection, _ = listener.accept()
        with connection:
            self.assertEqual(connection.recv(5), b'ready')
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, stderr.decode())
        verify(bundle)
        directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
        command = read_json(directory / 'run.command.json')
        self.assertIs(command['started'], True)
        self.assertIs(command['interrupted'], True)
        self.assertIs(command['finished'], True)
        self.assertEqual(command['exit'], -signal.SIGKILL)
        self.assertIs(command['containment_proven'], True)
        self.assertGreater(command['seconds'], 0)
        row = read_json(directory / 'trial.json')
        self.assertTrue(row['cleanup_proven'])
        self.assertEqual(row['classification'], 'operational_failure')
        result = analyze(bundle, self.root / 'signal-analysis')
        self.assertEqual(result['accounting'][slot['workload']][slot['arms'][0]]['started'], 1)
        self.assertEqual(result['accounting'][slot['workload']][slot['arms'][1]]['started'], 0)

    def test_future_protocols_reuse_accounting_without_inventing_estimands(self):
        m = copy.deepcopy(self.manifest)
        m.update(experiment='E3', phase='registered-future', seed='future')
        bundle, registration = self.frozen(m)
        collect(bundle, self.block(bundle), registration)
        result = analyze(bundle, self.root / 'future-accounting')
        self.assertEqual(result['decision']['status'], 'accounting_only')
        self.assertTrue(all(not values for values in result['estimates'].values()))
        for arm in ('baseline', 'candidate'):
            self.assertEqual(result['accounting'][order(m)[0]['workload']][arm]['complete'], 1)

    def test_reanalysis_rejects_invalid_command_dates_and_sequence(self):
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        collect(bundle, slot['block'], registration)
        directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
        path = directory / 'run.command.json'
        original = read_json(path)
        for index, (key, value, reason) in enumerate((
                ('started_at', '2010-01-01T00:00:00Z', 'predates registration'),
                ('started_at', '2099-01-01T00:00:00Z', 'command timing'),
                ('started_at', read_json(directory / 'prepare.command.json')['started_at'], 'chronology'))):
            write_json(path, dict(original, **{key: value}))
            seal(bundle)
            result = analyze(bundle, self.root / ('command-time-%d' % index))
            self.assertTrue(any(reason in r for r in result['paired_missingness'][slot['block']]))

    def test_uncontained_process_group_quarantines_despite_cleanup_receipt(self):
        from collector import execute as real_execute
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        calls = []
        def command(spec, context, directory, step, secrets):
            calls.append((context['arm'], step))
            if step != 'run':
                return real_execute(spec, context, directory, step, secrets)
            process = Mock(pid=123)
            process.wait.side_effect = [subprocess.TimeoutExpired('run', 600),
                                        subprocess.TimeoutExpired('term', 25),
                                        subprocess.TimeoutExpired('kill', 25)]
            with patch('execution.subprocess.Popen', return_value=process), \
                    patch('execution.os.killpg'), patch('execution.wait_group', return_value=False):
                return real_execute(spec, context, directory, step, secrets)
        with patch('collector.execute', side_effect=command):
            self.assertFalse(collect(bundle, slot['block'], registration))
        self.assertIn((slot['arms'][0], 'cleanup'), calls)
        self.assertNotIn((slot['arms'][1], 'prepare'), calls)
        verify(bundle)
        self.assertTrue((bundle / 'stopped.json').exists())
        result = analyze(bundle, self.root / 'uncontained')
        self.assertIn('cleanup_unproven', result['paired_missingness'][slot['block']])

    def test_interruption_during_started_checkpoint_is_not_hook_tampering(self):
        from common import write_json as real_write
        for committed in (False, True):
            directory = self.root / ('checkpoint-%s' % committed)
            directory.mkdir()
            spec = {'argv': [sys.executable, '-c', 'import threading; threading.Event().wait()'],
                    'cwd': str(directory), 'env': {}, 'timeout_seconds': 5,
                    'cancel_grace_seconds': 1}
            def interrupted_write(path, value):
                if value['started'] and not value['finished']:
                    if committed:
                        real_write(path, value)
                    raise KeyboardInterrupt()
                real_write(path, value)
            with self.subTest(committed=committed), patch('execution.write_json', side_effect=interrupted_write):
                with self.assertRaises(KeyboardInterrupt):
                    execute(spec, {'redact_env': []}, directory, 'run', [])
            command = read_json(directory / 'run.command.json')
            self.assertTrue(command['started'])
            self.assertTrue(command['finished'])
            self.assertTrue(command['interrupted'])
            self.assertTrue(command['containment_proven'])
            self.assertEqual(command['exit'], -signal.SIGKILL)

    def test_signal_during_spawn_transfers_process_ownership_before_interrupting(self):
        from collector import execute as real_execute
        real_popen = subprocess.Popen
        children = []
        def cleanup_children():
            for process in children:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        self.addCleanup(cleanup_children)
        def signalled_spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process
        def command(spec, context, directory, step, secrets):
            if step == 'run':
                with patch('execution.subprocess.Popen', side_effect=signalled_spawn):
                    return real_execute(spec, context, directory, step, secrets)
            return real_execute(spec, context, directory, step, secrets)
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['run']['argv'] = [sys.executable, '-c',
                                           'import threading; threading.Event().wait()']
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        with patch('collector.execute', side_effect=command), self.assertRaises(KeyboardInterrupt):
            collect(bundle, slot['block'], registration)
        verify(bundle)
        directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
        row = read_json(directory / 'trial.json')
        self.assertTrue(row['started'])
        self.assertTrue(row['cleanup_proven'])
        self.assertEqual(row['exit'], -signal.SIGKILL)
        self.assertTrue(all(p.poll() is not None for p in children))
        self.assertEqual(row, analyzed_trial(bundle, m, slot, slot['arms'][0]))

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
        # Invoke the archived entry point without -B too: imports must not write
        # bytecode into the immutable input. No checkout modules are on its path.
        subprocess.run([sys.executable, str(moved / 'analysis/source/tool.py'),
                        'analyze', str(moved), str(self.root / 'three')],
                       check=True, capture_output=True)
        verify(moved)
        self.assertEqual((self.root / 'one/results.json').read_bytes(),
                         (self.root / 'three/results.json').read_bytes())
        self.assertEqual((self.root / 'one/report.md').read_bytes(),
                         (self.root / 'three/report.md').read_bytes())
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

    def test_future_protocol_freeze_requires_string_phase(self):
        for experiment in ('E3', 'E4'):
            for index, phase in enumerate((1, True, None, [], {}, '', '   ')):
                m = copy.deepcopy(self.manifest)
                m.update(experiment=experiment, phase=phase)
                source = self.source / 'phase-manifest.json'
                write_json(source, m)
                destination = self.root / ('phase-%s-%d' % (experiment, index))
                with self.subTest(experiment=experiment, phase=phase):
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
            m['phase'] = 'custom phase'
            write_json(source, m)
            destination = self.root / ('valid-phase-' + experiment)
            freeze(source, destination)
            self.assertEqual(read_json(destination / 'order.json'), order(m))

    def test_freeze_rejects_unspawnable_command_strings(self):
        mutations = (('argv', ['', 'argument']), ('argv', ['bad\0executable']),
                     ('argv', [sys.executable, 'bad\0argument']), ('cwd', 'bad\0directory'))
        for step in ('prepare', 'run', 'observe', 'check', 'cleanup'):
            for index, (field, value) in enumerate(mutations):
                m = copy.deepcopy(self.manifest)
                spec = m['commands']['example-fixed-replay']['baseline'][step]
                spec[field] = value
                source = self.source / 'command-string-manifest.json'
                write_json(source, m)
                destination = self.root / ('command-string-%s-%d' % (step, index))
                with self.subTest(step=step, field=field, value=value):
                    with self.assertRaises((ValueError, OSError)):
                        subprocess.Popen(spec['argv'], cwd=spec['cwd'], env=spec['env'])
                    with self.assertRaises(ValueError):
                        freeze(source, destination)
                    self.assertFalse(destination.exists())
        # Empty non-executable arguments are legal and must not be rejected.
        self.manifest['commands']['example-fixed-replay']['baseline']['run']['argv'].append('')
        self.frozen()

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

    def test_receipt_requires_structured_environment_and_descriptive_method(self):
        directory = self.root / 'provenance'
        directory.mkdir()
        (directory / 'proof.log').write_text('constructed provenance evidence')
        row = {'workload': 'example-fixed-replay', 'arm': 'baseline'}
        receipt = {'checks': dict.fromkeys(QUALITY, True),
                   'facts': dict(self.manifest['expected_facts'][row['workload']][row['arm']], host='test'),
                   'metrics': dict(dict.fromkeys(E1_RESOURCES, 1), gate_seconds=1),
                   'environment': {'host': 'test', 'allocation': 'constructed'},
                   'resource_method': 'constructed scope, units and reset boundaries',
                   'evidence': {'proof.log': digest(directory / 'proof.log')}}
        receipt_valid(self.manifest, row, receipt, directory)
        for field, values in (('environment', (True, 1, 'host', ['host'], {}, None)),
                              ('resource_method', (True, 1, [], {'scope': 'test'}, '', '   ', None))):
            for value in values:
                bad = dict(receipt, **{field: value})
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'provenance'):
                    receipt_valid(self.manifest, row, bad, directory)

    def test_collection_rejects_scalar_provenance_and_preserves_cleanup(self):
        self.hook.write_text(self.hook.read_text() + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 receipt.update(environment=True,resource_method=True)
 (p/'receipt.json').write_text(json.dumps(receipt))
''')
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertTrue(collect(bundle, slot['block'], registration))
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertTrue(row['started'])
            self.assertTrue(row['cleanup_proven'])
            self.assertEqual(row['classification'], 'quality_failure')
            self.assertIn('provenance', row['reason'])
        verify(bundle)

    def test_analysis_rechecks_original_receipt_provenance(self):
        bundle, registration = self.frozen()
        slot = order(self.manifest)[0]
        collect(bundle, slot['block'], registration)
        directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
        receipt = read_json(directory / 'receipt.json')
        self.assertEqual(analyzed_trial(bundle, self.manifest, slot, slot['arms'][0])['classification'], 'complete')
        for index, (field, value) in enumerate((('environment', True), ('environment', 'host'),
                                               ('resource_method', True), ('resource_method', {'scope': 'test'}))):
            write_json(directory / 'receipt.json', dict(receipt, **{field: value}))
            seal(bundle)  # Construct a coherent bundle containing a malformed original.
            result = analyze(bundle, self.root / ('provenance-analysis-%d' % index))
            counts = result['accounting'][slot['workload']][slot['arms'][0]]
            self.assertEqual(counts['quality_failure'], 1)
            self.assertEqual(result['paired_missingness'][slot['block']],
                             ['observed environment and resource provenance required'])

    def test_e2_analysis_always_reports_provider_queue_metrics(self):
        m = self.e2_manifest()
        self.assertNotIn('analysis_metrics', m)
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        data, inventory = CITests().data()
        baseline = endpoint(data, inventory)
        candidate_data = copy.deepcopy(data)
        origin = datetime.fromisoformat(data['run']['created_at'].replace('Z', '+00:00'))
        def delayed_shorter(value):
            elapsed = (datetime.fromisoformat(value.replace('Z', '+00:00')) - origin).total_seconds()
            return (origin + timedelta(seconds=20 + (elapsed - 5) / 2)).isoformat()
        candidate_data['run']['updated_at'] = delayed_shorter(data['run']['updated_at'])
        for job in candidate_data['pages'][0]['jobs']:
            if job['conclusion'] == 'skipped':
                continue
            for field in ('started_at', 'completed_at'):
                job[field] = delayed_shorter(job[field])
            for step in job['steps']:
                for field in ('started_at', 'completed_at'):
                    if field in step:
                        step[field] = delayed_shorter(step[field])
        candidate = endpoint(candidate_data, inventory)
        def constructed_row(root, manifest, slot, arm):
            return dict(slot, arm=arm, attempt=1, started=True, cleanup_proven=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics=baseline if arm == 'baseline' else candidate,
                        facts={'schedule': 'sch_constructed', 'host': 'test',
                               'ci_created_at': '2026-10-13T00:00:00Z',
                               'ci_completed_at': '2026-10-13T00:00:00Z'})
        with patch('analysis.trial', side_effect=constructed_row):
            result = analyze(bundle, self.root / 'queue-analysis')
        estimates = result['estimates']['external-acceptance']
        self.assertAlmostEqual(estimates['validation_seconds']['ratio'], .5)
        report = (self.root / 'queue-analysis/report.md').read_text()
        for metric in ('queue_inclusive_seconds', 'initial_queue_seconds'):
            self.assertEqual(estimates[metric]['n'], 6)
            self.assertAlmostEqual(estimates[metric]['ratio'], candidate[metric] / baseline[metric])
            self.assertGreater(estimates[metric]['ratio'], 1)
            self.assertIsNotNone(estimates[metric]['interval'])
            self.assertIn('| %s | 6 |' % metric, report)
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
            from execution import expand_command
            context = dict(trial_dir=directory, campaign_dir=bundle, block_id=slot['block'],
                           arm=arm, redact_env=[])
            for step_index, step in enumerate(('prepare', 'run', 'observe', 'check', 'cleanup')):
                argv, cwd, env = expand_command(m['commands'][slot['workload']][arm][step], context)
                write_json(directory / (step + '.command.json'),
                           {'step': step, 'argv': argv, 'cwd': cwd, 'env': env,
                            'redacted_env_names': [], 'started': True, 'finished': True,
                            'interrupted': False, 'containment_proven': True,
                            'exit': 0, 'timeout': False, 'error': None, 'seconds': 1,
                            'started_at': slot['date'] + 'T00:00:%02dZ' % (step_index * 2),
                            'ended_at': slot['date'] + 'T00:00:%02dZ' % (step_index * 2 + 1)})
            write_json(directory / 'attempt.json', {'collection_root': str(bundle),
                       'state': 'finished', 'reason': '', 'cleanup_inputs_valid': True})
            write_json(directory / 'cleanup-inputs.json', cleanup_inventory(directory))
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
            with patch('execution.subprocess.Popen', return_value=process), \
                    patch('execution.os.killpg') as kill, patch('execution.wait_group', return_value=True):
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
        from collector import execute as real_execute
        m = copy.deepcopy(self.manifest)
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        def command(spec, context, directory, step, secrets):
            if step != 'run':
                return real_execute(spec, context, directory, step, secrets)
            with patch('execution.subprocess.Popen', side_effect=OSError('constructed spawn failure')):
                return real_execute(spec, context, directory, step, secrets)
        with patch('collector.execute', side_effect=command):
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
        process.wait.side_effect = [subprocess.TimeoutExpired(spec['argv'], 600), 4, 4]
        with patch('execution.subprocess.Popen', return_value=process), \
                patch('execution.os.killpg', side_effect=[ProcessLookupError('constructed exit race'), None]) as kill, \
                patch('execution.wait_group', return_value=True):
            result = execute(spec, {'trial_dir': self.root, 'block_id': 'test', 'redact_env': []},
                             self.root, 'run', [])
        self.assertTrue(result['started'])
        self.assertTrue(result['timeout'])
        self.assertIsNone(result['error'])
        self.assertTrue(result['containment_proven'])
        self.assertGreater(result['seconds'], 0)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(123, signal.SIGTERM)])
        self.assertEqual(process.wait.call_count, 3)
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

    def test_trusted_cleanup_runs_after_unrelated_execution_pin_drift(self):
        from collector import execute as real_execute
        other = self.source / 'checker-input.txt'
        other.write_text('registered input')
        m = copy.deepcopy(self.manifest)
        m['execution_pins'].append({'path': str(other), 'sha256': digest(other), 'role': 'checker input'})
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['cleanup']['execution_pins'] = [str(self.hook), sys.executable]
        for changed_step in ('prepare', 'run', 'observe', 'check'):
            other.write_text('registered input')
            source = self.source / 'pin-drift-manifest.json'
            write_json(source, m)
            bundle = self.root / ('pin-drift-' + changed_step)
            freeze(source, bundle)
            registration = self.root / ('pin-registration-' + changed_step + '.json')
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid'})
            slot = order(m)[0]
            calls = []
            def command(spec, context, directory, step, secrets):
                calls.append((context['arm'], step))
                result = real_execute(spec, context, directory, step, secrets)
                if step == changed_step:
                    other.write_text('changed input after allocation')
                return result
            with self.subTest(step=changed_step), patch('collector.execute', side_effect=command):
                self.assertFalse(collect(bundle, slot['block'], registration))
            directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
            self.assertIn((slot['arms'][0], 'cleanup'), calls)
            self.assertTrue(read_json(directory / 'cleanup.json')['proven'])
            self.assertEqual(read_json(directory / 'trial.json')['classification'], 'quality_failure')
            self.assertTrue((bundle / 'stopped.json').exists())
            self.assertNotIn((slot['arms'][1], 'prepare'), calls)
            verify(bundle)

    def test_cleanup_pin_selection_requires_registered_nonempty_inputs(self):
        for index, selected in enumerate(([], [str(self.hook), str(self.hook)],
                                          [str(self.source / 'unregistered.py')],
                                          [True], str(self.hook))):
            m = copy.deepcopy(self.manifest)
            m['commands']['example-fixed-replay']['baseline']['cleanup']['execution_pins'] = selected
            source = self.source / 'cleanup-selection.json'
            write_json(source, m)
            bundle = self.root / ('cleanup-selection-%d' % index)
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                freeze(source, bundle)
            self.assertFalse(bundle.exists())

    def test_changed_cleanup_execution_pin_prevents_untrusted_teardown(self):
        from collector import execute as real_execute
        m = copy.deepcopy(self.manifest)
        for arms in m['commands'].values():
            for commands in arms.values():
                commands['cleanup']['execution_pins'] = [str(self.hook), sys.executable]
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        calls = []
        def command(spec, context, directory, step, secrets):
            calls.append(step)
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'prepare':
                self.hook.write_text('untrusted replacement')
            return result
        with patch('collector.execute', side_effect=command):
            self.assertFalse(collect(bundle, slot['block'], registration))
        self.assertNotIn('cleanup', calls)
        self.assertTrue((bundle / 'stopped.json').exists())
        verify(bundle)

    def test_post_check_and_cleanup_redactions_are_counted(self):
        from collector import execute as real_execute
        secret = sha(b'constructed-secret-for-late-redaction')
        m = copy.deepcopy(self.manifest)
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        def command(spec, context, directory, step, secrets):
            result = real_execute(spec, context, directory, step, secrets)
            if step in ('check', 'cleanup'):
                (directory / (step + '-private.log')).write_text(secret + '\n')
            return result
        with patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}), \
                patch('collector.execute', side_effect=command):
            self.assertTrue(collect(bundle, slot['block'], registration))
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            self.assertEqual(read_json(directory / 'redactions.json')['replacements'], 4)
            for step in ('check', 'cleanup'):
                self.assertEqual((directory / (step + '-private.log')).read_text(), '[REDACTED]\n')
        verify(bundle)

    def test_missing_or_corrupt_evidence_cannot_complete(self):
        bundle, registration = self.frozen()
        collect(bundle, self.block(bundle), registration)
        directory = bundle / 'blocks' / self.block(bundle) / 'baseline'
        for name in ('receipt.json', 'evidence.log', 'cleanup.log', 'cleanup-inputs.json'):
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
        with patch('execution.subprocess.Popen', return_value=process), \
                patch('execution.os.killpg', side_effect=kill), \
                patch('execution.time.monotonic', return_value=10), \
                patch('execution.time.monotonic_ns', side_effect=[0, 0, 1000000000]), \
                patch('execution.wait_group', return_value=True), self.assertRaises(KeyboardInterrupt):
            execute(spec, {'redact_env': []}, self.root, 'interrupt', [])
        self.assertEqual(events, [('wait', {'timeout': 1}), ('signal', process.pid, signal.SIGTERM),
                                  ('wait', {'timeout': 25}), ('signal', process.pid, signal.SIGKILL),
                                  ('wait', {'timeout': 25})])

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
            with patch('execution.subprocess.Popen', side_effect=ready_process):
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
                             set(E1_RESOURCES))
            report = (self.root / 'higher-cost/report.md').read_text()
            self.assertIn('faster with higher measured resource cost', report)
            for metric in result['decision']['higher_resource_cost']:
                self.assertIn('`' + metric + '`', report)
            state['cost'] = None
            result = analyze(bundle, self.root / 'undefined-cost')
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(set(result['decision']['undefined_resource_ratios']),
                             set(E1_RESOURCES))
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

    def test_cold_timer_rejects_boolean_numeric_alias(self):
        m = self.make_manifest(phase='adoption-cold')
        directory = self.root / 'cold-timer-types'
        directory.mkdir()
        (directory / 'timer.log').write_text('constructed nested monotonic timer')
        receipt = {'checks': dict.fromkeys(QUALITY, True),
                   'facts': dict(m['expected_facts']['example-fixed-replay']['baseline'], host='test'),
                   'metrics': dict(dict.fromkeys(E1_RESOURCES, 1), gate_seconds=1),
                   'environment': {'host': 'test'}, 'resource_method': 'constructed',
                   'evidence': {'timer.log': digest(directory / 'timer.log')},
                   'gate_timer': {'source': 'monotonic', 'boundary': 'cli_spawn_to_exit',
                                  'seconds': 1.0, 'evidence': 'timer.log'}}
        row = {'workload': 'example-fixed-replay', 'arm': 'baseline'}
        receipt_valid(m, row, receipt, directory)
        receipt['gate_timer']['seconds'] = True
        with self.assertRaisesRegex(ValueError, 'separate nested CLI timer'):
            receipt_valid(m, row, receipt, directory)

    def test_overlapping_secrets_redact_longest_first_in_json(self):
        from redaction import redact_value, sanitize
        for index, secrets in enumerate(([b'prefix', b'prefix-SUFFIX'],
                                         [b'prefix-SUFFIX', b'prefix'])):
            directory = self.root / ('overlapping-secrets-%d' % index)
            directory.mkdir()
            value = {'prefix-SUFFIX': ['prefix-SUFFIX', 'prefix', 1, True]}
            expected = {'[REDACTED]': ['[REDACTED]', '[REDACTED]', 1, True]}
            write_json(directory / 'original.json', value)
            self.assertEqual(redact_value(value, secrets), expected)
            self.assertEqual(sanitize(directory, secrets), 3)
            self.assertEqual(read_json(directory / 'original.json'), expected)
            self.assertEqual(sanitize(directory, secrets), 0)

    def test_json_scalar_secrets_reject_sanitization_without_changing_types(self):
        from redaction import sanitize
        cases = [(b'123456789', b'{"metrics":{"artifact_bytes":123456789}}'),
                 (b'123456789', b'{"text":"123456789","nested":[123456789]}'),
                 (b'123456789', b'{"value":123456789.25}'),
                 (b'true', b'{"value":[true]}'),
                 (b'false', b'false'), (b'null', b'null'),
                 # Canonicalization may change the spelling without rewriting
                 # an unchanged original: both representations must be checked.
                 (b'123456789', b'1.23456789e8'),
                 (b'123456789e0', b'123456789e0')]
        for index, (secret, data) in enumerate(cases):
            directory = self.root / ('scalar-%d' % index)
            directory.mkdir()
            path = directory / 'original.json'
            path.write_bytes(data)
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, 'secret.*JSON') as error:
                    sanitize(directory, [secret])
                self.assertNotIn(secret.decode(), str(error.exception))
                self.assertEqual(path.read_bytes(), data)
        directory = self.root / 'owned-scalar'
        directory.mkdir()
        path = directory / 'trial.json'
        write_json(path, {'metrics': {'artifact_bytes': 123456789}})
        with self.assertRaisesRegex(ValueError, 'secret.*JSON'):
            sanitize(directory, [b'123456789'], owned={path})

    def test_json_scalar_secrets_prevent_sealing_after_trusted_cleanup(self):
        original = self.hook.read_text()
        for remove in (False, True):
            self.hook.write_text(original + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 receipt['metrics']['artifact_bytes']=int(os.environ['TEST_REDACTION_SECRET'])
 (p/'receipt.json').write_text(json.dumps(receipt))
if step=='cleanup' and %r:
 (p/'receipt.json').unlink()
''' % remove)
            m = self.make_manifest()
            m['redact_env'] = ['TEST_REDACTION_SECRET']
            bundle, registration = self.frozen(m, 'scalar-collection-' + str(remove))
            slot = order(m)[0]
            # Keep the decimal credential out of frozen source/public data.
            secret = str(int(sha(b'constructed-scalar-credential'), 16))
            with self.subTest(remove=remove), patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}):
                with self.assertRaisesRegex(ValueError, 'refusing to reseal') as error:
                    collect(bundle, slot['block'], registration)
                self.assertNotIn(secret, str(error.exception))
                directory = bundle / 'blocks' / slot['block'] / slot['arms'][0]
                self.assertEqual(read_json(directory / 'cleanup.command.json')['exit'], 0)
                self.assertFalse((directory / 'check.command.json').exists())
                self.assertFalse((directory.parent / slot['arms'][1]).exists())
                self.assertTrue((bundle / 'stopped.json').exists())
                diagnostic = read_json(directory.parent / 'integrity-error.json')['reason']
                self.assertIn('secret', diagnostic)
                self.assertIn('JSON', diagnostic)
                self.assertNotIn(secret, diagnostic)
                with self.assertRaisesRegex(ValueError, 'checksum inventory differs'):
                    verify(bundle)
                with self.assertRaisesRegex(ValueError, 'checksum inventory differs'):
                    analyze(bundle, self.root / ('scalar-analysis-' + str(remove)))

    def test_marker_overlapping_secrets_fail_before_entering_a_block(self):
        from redaction import redact_value, sanitize
        m = copy.deepcopy(self.manifest)
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        bundle, registration = self.frozen(m)
        for secret in ('REDACTED', '[REDACTED]', '[', 'D'):
            with self.subTest(secret=secret), patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}):
                with self.assertRaisesRegex(ValueError, 'redaction marker'):
                    collect(bundle, self.block(bundle), registration)
                self.assertFalse((bundle / 'blocks').exists())
                with self.assertRaisesRegex(ValueError, 'redaction marker'):
                    redact_value(secret, [secret.encode()])
                with self.assertRaisesRegex(ValueError, 'redaction marker'):
                    sanitize(bundle, [secret.encode()])
                verify(bundle)
        with patch.dict(os.environ, {'TEST_REDACTION_SECRET': sha(b'credential') + '-REDACTED'}):
            self.assertTrue(collect(bundle, self.block(bundle), registration))
        verify(bundle)

    def test_original_command_named_evidence_is_not_a_collector_receipt(self):
        from collector import execute as real_execute
        secret = sha(b'original-command-evidence-secret')
        m = copy.deepcopy(self.manifest)
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        bundle, registration = self.frozen(m)
        def command(spec, context, directory, step, secrets):
            result = real_execute(spec, context, directory, step, secrets)
            if step == 'observe':
                write_json(directory / 'original.command.json',
                           {'original': 'constructed product evidence', 'argv': [secret]})
            return result
        with patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}), \
                patch('collector.execute', side_effect=command):
            self.assertTrue(collect(bundle, self.block(bundle), registration))
        verify(bundle)
        result = analyze(bundle, self.root / 'original-command-evidence')
        workload = order(self.manifest)[0]['workload']
        for arm in ('baseline', 'candidate'):
            self.assertEqual(result['accounting'][workload][arm]['complete'], 1)
            original = read_json(bundle / 'blocks' / self.block(bundle) / arm / 'original.command.json')
            self.assertEqual(original['argv'], ['[REDACTED]'])

    def test_expanded_paths_preserve_literal_placeholder_segments(self):
        source = self.source / 'literal-path-manifest.json'
        write_json(source, self.manifest)
        bundle = self.root / '{arm}' / 'campaign'
        bundle.parent.mkdir()
        freeze(source, bundle)
        registration = self.root / 'literal-path-registration.json'
        write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                 'order_sha256': digest(bundle / 'order.json'),
                                 'registered_at': '2020-01-01T00:00:00Z',
                                 'revision': 'constructed', 'public_url': 'https://example.invalid'})
        self.assertTrue(collect(bundle, self.block(bundle), registration))
        slot = order(self.manifest)[0]
        for arm in slot['arms']:
            directory = bundle / 'blocks' / slot['block'] / arm
            command = read_json(directory / 'run.command.json')
            self.assertEqual(command['argv'][3], str(directory))
            self.assertEqual(read_json(directory / 'trial.json')['classification'], 'complete')
        verify(bundle)

    def test_unchanged_readonly_evidence_is_not_rewritten(self):
        from redaction import sanitize
        directory = self.root / 'readonly'
        directory.mkdir()
        path = directory / 'readonly.log'
        path.write_bytes(b'unchanged constructed evidence')
        path.chmod(0o444)
        original = path.stat().st_mtime_ns
        # Model a write denial even for privileged UIDs, which bypass mode bits.
        with patch('redaction.write_bytes', side_effect=PermissionError) as write:
            for secrets in ([], [b'absent-secret']):
                self.assertEqual(sanitize(directory, secrets), 0)
                self.assertEqual(path.stat().st_mtime_ns, original)
            write.assert_not_called()
            # A required redaction still fails rather than publishing raw bytes.
            with self.assertRaises(PermissionError):
                sanitize(directory, [b'constructed'])
            write.assert_called_once_with(path, b'unchanged [REDACTED] evidence')
        self.assertEqual(path.read_bytes(), b'unchanged constructed evidence')

    def test_nonregular_entries_cannot_be_sealed_or_verified(self):
        for kind in ('fifo', 'socket'):
            directory = self.root / kind
            directory.mkdir()
            (directory / 'evidence.log').write_text('constructed regular evidence')
            seal(directory)
            path = directory / 'untracked'
            with socket.socket(socket.AF_UNIX) as endpoint_socket:
                if kind == 'fifo':
                    os.mkfifo(path)
                else:
                    endpoint_socket.bind(str(path))
                with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'non-regular'):
                    seal(directory)
                with self.assertRaisesRegex(ValueError, 'non-regular'):
                    verify(directory)


    def test_all_e1_resources_receive_higher_cost_disclosure(self):
        m = self.make_manifest(phase='confirmation')
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        state = {'metric': 'containers_created'}
        def row(root, manifest, slot, arm):
            return dict(slot, arm=arm, attempt=1, started=True, cleanup_proven=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics=dict(dict.fromkeys(E1_RESOURCES, 1), gate_seconds=1),
                        facts={'schedule': 'constructed', 'host': 'test'})
        def fixed(manifest, workload, metric, pairs, dates):
            ratio = .7 if metric == 'gate_seconds' else 1.2 if metric == state['metric'] else 1
            return {'n': len(pairs), 'ratio': ratio, 'difference': ratio - 1,
                    'interval': {'ratio': [ratio, ratio]}}
        for index, metric in enumerate(E1_RESOURCES):
            state['metric'] = metric
            output = self.root / ('every-resource-%d' % index)
            with patch('analysis.trial', side_effect=row), patch('analysis.estimate', side_effect=fixed):
                result = analyze(bundle, output)
            self.assertEqual(result['decision']['status'], 'improvement_demonstrated')
            self.assertEqual(result['decision']['higher_resource_cost'], [metric])
            self.assertIn('faster with higher measured resource cost', (output / 'report.md').read_text())
            self.assertIn('`' + metric + '`', (output / 'report.md').read_text())

    def test_derived_ci_metrics_preserve_json_number_types(self):
        m = self.e2_manifest()
        data, inventory = CITests().data()
        m['ci']['tasks'] = {arm: copy.deepcopy(inventory['tasks']) for arm in ('baseline', 'candidate')}
        directory = self.root / 'typed-ci'
        directory.mkdir()
        for queue in (0, 1):
            data['pages'][0]['jobs'][0]['started_at'] = '2026-10-13T00:00:%02dZ' % queue
            write_json(directory / 'ci.json', data)
            row = {'workload': 'external-acceptance', 'arm': 'baseline'}
            receipt = {'checks': dict.fromkeys(QUALITY, True),
                       'facts': dict(m['expected_facts'][row['workload']][row['arm']], host='test',
                                     ci_created_at=data['run']['created_at'], ci_completed_at=data['run']['updated_at']),
                       'metrics': endpoint(data, inventory), 'environment': {'host': 'test'},
                       'resource_method': 'constructed', 'ci_snapshot': 'ci.json',
                       'evidence': {'ci.json': digest(directory / 'ci.json')}}
            receipt_valid(m, row, receipt, directory)
            receipt['metrics']['initial_queue_seconds'] = bool(queue)
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'provider timestamps'):
                receipt_valid(m, row, receipt, directory)

    def test_short_secrets_colliding_with_public_data_reject_admission(self):
        for index, secret in enumerate(('0', '1', 'a')):
            m = self.make_manifest()
            m['redact_env'] = ['TEST_REDACTION_SECRET']
            source = self.source / 'short-secret.json'
            write_json(source, m)
            bundle = self.root / ('short-secret-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('short-registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid/constructed'})
            with patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}):
                with self.assertRaisesRegex(ValueError, 'secret.*public'):
                    collect(bundle, self.block(bundle), registration)
            self.assertFalse((bundle / 'blocks').exists())
            self.assertFalse((bundle / 'registration.json').exists())
            verify(bundle)
            result = analyze(bundle, self.root / ('short-analysis-%d' % index))
            self.assertTrue(all(counts['started'] == 0 for arms in result['accounting'].values()
                                for counts in arms.values()))

    def test_wait_budget_includes_process_creation_delay(self):
        spec = self.manifest['commands']['example-fixed-replay']['baseline']['run']
        for index, setup in enumerate((30, 610)):
            process = Mock(pid=123)
            process.wait.return_value = 0
            with patch('execution.subprocess.Popen', return_value=process), \
                    patch('execution.time.monotonic_ns', side_effect=[0, int(setup * 1e9), int((setup + 1) * 1e9)]), \
                    patch('execution.os.killpg') as kill, patch('execution.wait_group', return_value=True):
                result = execute(spec, {'trial_dir': self.root, 'block_id': 'test', 'redact_env': []},
                                 self.root, 'spawn-delay-%d' % index, [])
            if setup == 30:
                process.wait.assert_called_once_with(timeout=570)
                self.assertFalse(result['timeout'])
            else:
                self.assertTrue(result['timeout'])
                kill.assert_called_once_with(123, signal.SIGTERM)
            self.assertTrue(result['started'])

    def test_hooks_cannot_write_collector_stopped_marker(self):
        m = self.make_manifest()
        slot = order(m)[0]
        m['commands'][slot['workload']][slot['arms'][0]]['prepare']['argv'] = [sys.executable, '-c',
            "from pathlib import Path; (Path('{campaign_dir}')/'stopped.json').write_text('{}')"]
        bundle, registration = self.frozen(m)
        with self.assertRaisesRegex(ValueError, 'refusing to reseal'):
            collect(bundle, slot['block'], registration)
        self.assertFalse((bundle / 'blocks' / slot['block'] / slot['arms'][0] / 'run.command.json').exists())
        self.assertTrue((bundle / 'blocks' / slot['block'] / 'integrity-error.json').exists())
        with self.assertRaises(ValueError):
            verify(bundle)

    def test_mate_hooks_cannot_replace_completed_arm_evidence(self):
        for index, mutation in enumerate(('overwrite', 'delete', 'add')):
            m = self.make_manifest()
            slot = order(m)[0]
            first, second = slot['arms']
            specs = m['commands'][slot['workload']][second]
            source = self.source / 'mate-mutation.json'
            script = ("from pathlib import Path; p=Path('{campaign_dir}')/'blocks'/'{block_id}'/" +
                      repr(first) + "; ")
            script += { 'overwrite': "(p/'trial.json').write_text('{}')",
                        'delete': "(p/'receipt.json').unlink()",
                        'add': "(p/'replacement.log').write_text('replacement')"}[mutation]
            specs['prepare']['argv'] = [sys.executable, '-c', script]
            write_json(source, m)
            bundle = self.root / ('mate-mutation-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('mate-registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid/constructed'})
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, 'refusing to reseal'):
                    collect(bundle, slot['block'], registration)
                self.assertTrue((bundle / 'blocks' / slot['block'] / 'integrity-error.json').exists())
                self.assertTrue((bundle / 'blocks' / slot['block'] / second / 'cleanup.stdout').exists())
                self.assertFalse((bundle / 'blocks' / slot['block'] / second / 'run.command.json').exists())
                with self.assertRaises(ValueError):
                    verify(bundle)

    def test_oversized_integer_metrics_are_quality_failures_not_tracebacks(self):
        self.hook.write_text(self.hook.read_text() + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 receipt['metrics']['cpu_seconds']=10**309
 (p/'receipt.json').write_text(json.dumps(receipt))
''')
        m = self.make_manifest()
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        self.assertTrue(collect(bundle, slot['block'], registration))
        verify(bundle)
        result = analyze(bundle, self.root / 'oversized-counter')
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertTrue(row['started'])
            self.assertTrue(row['cleanup_proven'])
            self.assertEqual(row['classification'], 'quality_failure')
            self.assertEqual(row['reason'], 'missing/invalid resource measurements')
            self.assertNotIn('cpu_seconds', row['metrics'])
            original = read_json(bundle / 'blocks' / slot['block'] / arm / 'receipt.json')
            self.assertEqual(original['metrics']['cpu_seconds'], 10**309)
            self.assertEqual(result['accounting'][slot['workload']][arm]['quality_failure'], 1)

    def test_e2_wrapper_watchdog_reserves_overhead_without_changing_provider_cap(self):
        m = self.e2_manifest()
        for arms in m['commands'].values():
            for spec in arms.values():
                spec['run']['timeout_seconds'] = 5700
        bundle, _ = self.frozen(m)
        spec = m['commands']['external-acceptance']['baseline']['run']
        process = Mock(pid=123)
        def wait(timeout):
            # 30 seconds before creation plus a valid 5,380-second workflow.
            if timeout < 5410:
                raise subprocess.TimeoutExpired('dispatch plus workflow', timeout)
            return 0
        process.wait.side_effect = wait
        with patch('execution.subprocess.Popen', return_value=process), \
                patch('execution.time.monotonic_ns', side_effect=[0, 0, 5410000000000]):
            result = execute(spec, {'trial_dir': self.root, 'block_id': 'test', 'redact_env': []},
                             self.root, 'run', [])
        self.assertFalse(result['timeout'])
        self.assertIsNone(result['error'])
        process.wait.assert_called_once_with(timeout=5700)
        data, inventory = CITests().data()
        origin = datetime.fromisoformat(data['run']['created_at'].replace('Z', '+00:00'))
        for duration in (5380, 5401):
            end = (origin + timedelta(seconds=duration)).isoformat()
            data['run']['updated_at'] = end
            for job in data['pages'][0]['jobs']:
                if job['conclusion'] == 'skipped':
                    continue
                job['completed_at'] = end
                for step in job['steps']:
                    step['completed_at'] = end
            if duration == 5380:
                self.assertEqual(endpoint(data, inventory)['queue_inclusive_seconds'], 5380)
            else:
                with self.assertRaisesRegex(ValueError, 'capped'):
                    endpoint(data, inventory)
        for bad in (5399, 5400):
            m['commands']['external-acceptance']['baseline']['run']['timeout_seconds'] = bad
            with self.subTest(watchdog=bad), self.assertRaises(ValueError):
                validate_manifest(m)
        verify(bundle)

    def test_freeze_rejects_unspawnable_redaction_names(self):
        for index, name in enumerate(('BAD=NAME', 'BAD\0NAME')):
            m = copy.deepcopy(self.manifest)
            m['redact_env'] = [name]
            source = self.source / 'redaction-name.json'
            write_json(source, m)
            destination = self.root / ('redaction-name-%d' % index)
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    subprocess.Popen([sys.executable, '-c', 'pass'], env={name: 'constructed'})
                with self.assertRaises(ValueError):
                    freeze(source, destination)
                self.assertFalse(destination.exists())
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        self.frozen(m)

    def test_future_protocol_names_fail_before_bundle_creation(self):
        for index, name in enumerate(('../future.md', '/tmp/future.md', '',
                                     'nested/../../future.md', 'bad\0name.md')):
            m = copy.deepcopy(self.manifest)
            m['experiment'] = 'E3'
            m['protocols'] = {name: self.manifest['protocols']['README.md']}
            source = self.source / 'protocol-name.json'
            write_json(source, m)
            destination = self.root / ('protocol-name-%d' % index)
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    freeze(source, destination)
                self.assertFalse(destination.exists())
        m['protocols'] = {'future/v1.md': self.manifest['protocols']['README.md']}
        for index, names in enumerate((('future', 'future/v1.md'), ('future/v1.md', 'future/./v1.md'))):
            bad = dict(m, protocols={name: self.manifest['protocols']['README.md'] for name in names})
            write_json(source, bad)
            destination = self.root / ('protocol-layout-%d' % index)
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, 'conflicting'):
                freeze(source, destination)
            self.assertFalse(destination.exists())
        bundle, _ = self.frozen(m)
        self.assertEqual((bundle / 'protocol/future/v1.md').read_bytes(),
                         (self.source / 'README.md').read_bytes())

    def test_cleanup_interruption_sanitizes_before_sealing(self):
        from collector import execute as real_execute
        secret = sha(b'cleanup-interruption-regression')
        m = copy.deepcopy(self.manifest)
        m['redact_env'] = ['TEST_REDACTION_SECRET']
        for index, interruption in enumerate((KeyboardInterrupt, SystemExit)):
            source = self.source / 'cleanup-interrupt.json'
            write_json(source, m)
            bundle = self.root / ('cleanup-interrupt-%d' % index)
            freeze(source, bundle)
            registration = self.root / ('cleanup-registration-%d.json' % index)
            write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                                     'order_sha256': digest(bundle / 'order.json'),
                                     'registered_at': '2020-01-01T00:00:00Z',
                                     'revision': 'constructed', 'public_url': 'https://example.invalid/constructed'})
            def command(spec, context, directory, step, secrets):
                if step != 'cleanup':
                    return real_execute(spec, context, directory, step, secrets)
                (directory / 'cleanup.stdout').write_text(secret + '\n' + json.dumps(secret))
                write_json(directory / 'cleanup-original.json', {'value': secret})
                raise interruption()
            with self.subTest(interruption=interruption), \
                    patch.dict(os.environ, {'TEST_REDACTION_SECRET': secret}), \
                    patch('collector.execute', side_effect=command):
                with self.assertRaises(interruption):
                    collect(bundle, self.block(bundle), registration)
            verify(bundle)
            self.assertTrue((bundle / 'stopped.json').exists())
            paths = [path for path in bundle.rglob('*') if path.is_file()]
            for path in paths:
                self.assertFalse(secret.encode() in path.read_bytes(), str(path.relative_to(bundle)))
            retained = b''.join(path.read_bytes() for path in paths)
            self.assertIn(b'[REDACTED]', retained)
            slot = order(m)[0]
            row = read_json(bundle / 'blocks' / slot['block'] / slot['arms'][0] / 'trial.json')
            self.assertTrue(row['started'])
            self.assertFalse(row['cleanup_proven'])
            self.assertEqual(row['reason'], 'cleanup_unproven')

    def test_cleanup_proof_interruption_quarantines_before_all_hashes_pass(self):
        from evidence import digest as real_digest
        bundle, registration = self.frozen()
        def digest_with_interruption(path):
            if Path(path).name == 'cleanup.log':
                raise KeyboardInterrupt()
            return real_digest(path)
        with patch('evidence.digest', side_effect=digest_with_interruption), self.assertRaises(KeyboardInterrupt):
            collect(bundle, self.block(bundle), registration)
        verify(bundle)
        self.assertTrue((bundle / 'stopped.json').exists())
        slot = order(self.manifest)[0]
        row = read_json(bundle / 'blocks' / slot['block'] / slot['arms'][0] / 'trial.json')
        self.assertTrue(row['started'])
        self.assertFalse(row['cleanup_proven'])
        self.assertEqual(row['reason'], 'cleanup_unproven')

    def test_nonfinite_json_exponents_fail_before_bundle_creation(self):
        for index, number in enumerate(('1e400', '-1e400', 'NaN', 'Infinity')):
            source = self.source / 'nonfinite.json'
            # Unknown inventory values also must obey the global JSON contract.
            write_json(source, self.manifest)
            source.write_text(source.read_text().replace('"test only"', number))
            destination = self.root / ('nonfinite-%d' % index)
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    read_json(source)
                with self.assertRaises(ValueError):
                    freeze(source, destination)
            self.assertFalse(destination.exists())
        source.write_text('{"value": 1e308}')
        self.assertEqual(read_json(source), {'value': 1e308})

    def test_sigkill_leader_reaping_uses_shared_deadline(self):
        process = Mock(pid=123)
        def wait(timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired('constructed leader', timeout)
            return 0  # An unbounded wait would not enforce the failure bound.
        process.wait.side_effect = wait
        with patch('execution.os.killpg') as kill, \
                patch('execution.time.monotonic', side_effect=[10, 10.01]), \
                patch('execution.wait_group', return_value=True) as group:
            with self.assertRaisesRegex(OSError, 'leader.*SIGKILL'):
                kill_group(process, 5)
        kill.assert_called_once_with(123, signal.SIGKILL)
        process.wait.assert_called_once()
        self.assertAlmostEqual(process.wait.call_args.kwargs['timeout'], 4.99)
        group.assert_not_called()
        process.wait.side_effect = None
        with patch('execution.os.killpg'), \
                patch('execution.time.monotonic', side_effect=[10, 10.01]), \
                patch('execution.wait_group', return_value=False) as group:
            with self.assertRaisesRegex(OSError, 'group.*SIGKILL'):
                kill_group(process, 5)
        group.assert_called_once_with(123, 15)

    def test_surviving_sigkill_leader_becomes_one_bounded_command_error(self):
        spec = self.manifest['commands']['example-fixed-replay']['baseline']['run']
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('run', 600),
                                    subprocess.TimeoutExpired('term', 25),
                                    subprocess.TimeoutExpired('kill', 25)]
        with patch('execution.subprocess.Popen', return_value=process), \
                patch('execution.os.killpg') as kill, \
                patch('execution.wait_group', return_value=False):
            result = execute(spec, {'trial_dir': self.root, 'block_id': 'test', 'redact_env': []},
                             self.root, 'run', [])
        self.assertTrue(result['started'])
        self.assertTrue(result['timeout'])
        self.assertIn('leader', result['error'])
        self.assertEqual(process.wait.call_count, 3)
        self.assertTrue(all('timeout' in call.kwargs for call in process.wait.call_args_list))
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(123, signal.SIGTERM), (123, signal.SIGKILL)])
        self.assertEqual(read_json(self.root / 'run.command.json'), result)

    def test_report_exposes_failed_attempt_elapsed_and_resource_cost(self):
        self.hook.write_text(self.hook.read_text() + '''
if step=='observe':
 receipt=json.loads((p/'receipt.json').read_text())
 receipt['metrics']['failed|counter']=0
 (p/'receipt.json').write_text(json.dumps(receipt))
''')
        m = self.make_manifest()
        m['resource_metrics'].append('failed|counter')
        m['analysis_metrics'] = ['unobserved']
        for arms in m['commands'].values():
            for specs in arms.values():
                specs['run']['env']['RUN_EXIT'] = '4'
        bundle, registration = self.frozen(m)
        slot = order(m)[0]
        collect(bundle, slot['block'], registration)
        result = analyze(bundle, self.root / 'failed-cost')
        report = (self.root / 'failed-cost/report.md').read_text()
        self.assertIn('Elapsed seconds (run/spawn)', report)
        for arm in slot['arms']:
            row = read_json(bundle / 'blocks' / slot['block'] / arm / 'trial.json')
            self.assertIn('| %s | %s | %s | operational_failure | unexpected_exit | true | 4 | %.9g |' %
                          (slot['block'], slot['workload'], arm, row['elapsed_seconds']), report)
            self.assertIn('| %s | %s | cpu_seconds | 2 |' % (slot['block'], arm), report)
            self.assertIn('| %s | %s | failed&#124;counter | 0 |' % (slot['block'], arm), report)
            self.assertIn('| %s | %s | unobserved | N/A |' % (slot['block'], arm), report)
        self.assertEqual(result['estimates'][slot['workload']]['cpu_seconds']['n'], 0)

    def test_analysis_retains_extreme_finite_metrics_without_aborting(self):
        m = self.e2_manifest()
        m['analysis_metrics'] = ['extreme_counter']
        bundle, registration = self.frozen(m)
        shutil.copyfile(registration, bundle / 'registration.json')
        seal(bundle)
        def constructed_row(root, manifest, slot, arm):
            value = 2 if arm == 'baseline' else 1
            return dict(slot, arm=arm, attempt=1, started=True, cleanup_proven=True,
                        classification='complete', reason='', exit=0, elapsed_seconds=1,
                        metrics={'validation_seconds': value, 'runner_minutes': value,
                                 'peak_runner_concurrency': value, 'queue_inclusive_seconds': value,
                                 'initial_queue_seconds': value,
                                 'extreme_counter': 1e-308 if arm == 'baseline' else 1e308},
                        facts={'schedule': 'sch_constructed', 'host': 'test',
                               'ci_created_at': '2026-10-13T00:00:00Z',
                               'ci_completed_at': '2026-10-13T00:00:00Z'})
        with patch('analysis.trial', side_effect=constructed_row):
            result = analyze(bundle, self.root / 'extreme-analysis')
        extreme = result['estimates']['external-acceptance']['extreme_counter']
        self.assertEqual(extreme['n'], 6)
        self.assertIsNone(extreme['ratio'])
        self.assertEqual(extreme['difference'], 1e308)
        self.assertEqual(result['estimates']['external-acceptance']['validation_seconds']['ratio'], .5)
        self.assertEqual(result['accounting']['external-acceptance']['baseline']['complete'], 6)
        self.assertIn('unrepresentable', (self.root / 'extreme-analysis/report.md').read_text())
        self.assertEqual(read_json(self.root / 'extreme-analysis/results.json'), result)


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


    def test_extreme_finite_ratios_do_not_abort_point_or_date_estimates(self):
        self.assertEqual(mean([sys.float_info.max] * 7), sys.float_info.max)
        for baseline, candidate in ((1e-308, 1e308), (1e308, 1e-308)):
            pairs = [('01', '001', baseline, candidate), ('01', '002', baseline, candidate)]
            result = estimate(self.m, 'w', 'm', pairs, ['01'])
            self.assertEqual(result['n'], 2)
            self.assertIsNone(result['ratio'])
            self.assertIsNone(result['improvement'])
            self.assertIsNone(result['by_date']['01']['ratio'])
            self.assertIsNone(result['interval']['ratio'])
            self.assertTrue(math.isfinite(result['difference']))
            self.assertEqual(result['ratio_reason'], 'unrepresentable_ratio')
            canonical(result)  # No infinity/NaN is emitted as JSON.

    def test_unrepresentable_bootstrap_ratios_keep_all_difference_replicates(self):
        pairs = [('01', '001', 1e-308, 1e308), ('01', '002', 1e308, 1e-308)]
        result = estimate(self.m, 'w', 'm', pairs, ['01'])
        self.assertEqual(result['ratio'], 1)
        self.assertEqual(result['difference'], 0)
        self.assertIsNone(result['interval']['ratio'])
        self.assertEqual(result['interval']['difference'], [-1e308, 1e308])
        self.assertEqual(result['ratio_interval_reason'], 'unrepresentable_bootstrap_ratio')
        self.assertEqual(quantile([-1e308, 1e308], .5), 0)
        canonical(result)


class CITests(unittest.TestCase):
    def test_pagination_totals_require_nonnegative_integers_on_every_page(self):
        for value in (True, 1.0, '1', None, -1):
            for page_index in (0, 1):
                data, inventory = self.data()
                job = data['pages'][0]['jobs'][2]
                data['pages'] = [{'total_count': 1, 'jobs': [job]}, {'total_count': 1, 'jobs': []}]
                data['pages'][page_index]['total_count'] = value
                with self.subTest(value=value, page=page_index), self.assertRaisesRegex(ValueError, 'total_count'):
                    endpoint(data, inventory)
        data, inventory = self.data()
        self.assertEqual(endpoint(data, inventory)['validation_seconds'], 25)

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

    def test_original_attempt_requires_integer_run_and_job_provenance(self):
        for target in ('run', 'job'):
            for attempt in (True, 1.0, '1', None):
                data, inventory = self.data()
                value = data['run'] if target == 'run' else data['pages'][0]['jobs'][0]
                value['run_attempt'] = attempt
                with self.subTest(target=target, attempt=attempt), self.assertRaises(ValueError):
                    endpoint(data, inventory)
        data, inventory = self.data()
        endpoint(data, inventory)
        for attempt in (True, 1.0):
            with patch('ci.subprocess.run') as api, self.assertRaises(ValueError):
                snapshot('owner/repo', 1, attempt, Path('/unused'))
            api.assert_not_called()

    def test_run_and_job_identifiers_require_positive_integers(self):
        for target, key in (('run', 'id'), ('job', 'run_id'), ('job', 'id')):
            for identifier in (True, 1.0, '1', None, 0, -1):
                data, inventory = self.data()
                value = data['run'] if target == 'run' else data['pages'][0]['jobs'][0]
                value[key] = identifier
                with self.subTest(target=target, key=key, identifier=identifier), self.assertRaises(ValueError):
                    endpoint(data, inventory)
        data, inventory = self.data()
        endpoint(data, inventory)

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
