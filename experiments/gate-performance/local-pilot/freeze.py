#!/usr/bin/env python3
"""Freeze the already-built local inputs before any gate is measured."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

from pilot import command, sha, utc, write


workspace = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
destination.mkdir()
source = Path(__file__).resolve().parent
runtime = workspace / 'runtime/bin'
runtime.mkdir(parents=True, exist_ok=True)
java = runtime / 'java'
if not java.exists():
    java.symlink_to(Path(shutil.which('java')).resolve())
(workspace / 'home').mkdir(exist_ok=True)
image = command(['docker', 'image', 'inspect', 'mysql:8.4', '--format', '{{index .RepoDigests 0}}'])
image = 'mysql:8.4@' + image.split('@', 1)[1]
seed = 'weavegate-e1-e2-v1-20261013'
plan = {'campaign_id': sys.argv[3] if len(sys.argv) > 3 else 'local-tuning-20261018',
        'phase': 'tuning', 'created_at': utc(),
        'not_registered_E1_pilot': True, 'pairs_per_workload': 8, 'seed': seed,
        'baseline_commit': 'ee10c7de2114086f14258037703dbd4b104e5908',
        'instrumentation_commit': '8d4bf3506d6af2ad432c5d31e4948f87709b22a0',
        'binaries': {'B': str(workspace / 'baseline/weavegate_0.2.0_linux_amd64/weavegate'),
                     'Bplus': str(workspace / 'weavegate-bplus')},
        'environment_allowlist': {'PATH': str(runtime) + ':/usr/bin:/bin', 'HOME': str(workspace / 'home')},
        'environment': {'platform': platform.platform(), 'cpu_count': os.cpu_count(),
                        'lscpu': command(['lscpu']),
                        'docker': command(['docker', 'version', '--format', '{{json .}}']),
                        'mysql_image': image, 'cpu_memory_cap': 'host defaults; no aggregate cgroup cap',
                        'cache': 'prepared dependencies; no OS/page-cache restore; pins hashed before every arm',
                        'background_load': 'shared WSL host; pre-existing containers retained'},
        'runtime': {'jdk': subprocess.run(['java', '-version'], capture_output=True, text=True, check=True).stderr,
                    'go': command(['go', 'version']), 'python': sys.version,
                    'sdk': 'Maven Central io.github.weavegate:weavegate-spring:0.2.0, identical in both arms',
                    'pool_maximum': 3, 'pool_minimum_idle': 0,
                    'transaction_isolation': 'database default; actual isolation and engine version in each manifest'},
        'unavailable': ['aggregate CPU seconds', 'aggregate peak memory', 'disk bytes written',
                        'JVM-internal Spring startup', 'full repeated isolated/paired acceptance campaign',
                        'independent allocations', 'memory/page-cache restoration'],
        'deviations': ['one UTC date instead of two', 'shared WSL host instead of dedicated Linux allocations',
                       'host resource limits instead of aggregate 4 vCPU/16 GiB cap',
                       'no identical cache snapshot restoration', 'resource telemetry unavailable'],
        'workloads': {}, 'execution_pins': [], 'order': [], 'protocol_sha256': {}}
for name in ('README.md', 'e1-gate.md', 'e2-ci.md'):
    plan['protocol_sha256'][name] = sha(source.parent / name)
for label, revision in [('fixed', 'f38b20b1634235a0f46c96a2238ba10e2143fa75'),
                        ('vulnerable', 'e4932192b160e60c967dc17c9d02ac478bd61968')]:
    app = workspace / label
    config = app / '.weavegate/config.yaml'
    original = subprocess.check_output(['git', '-C', str(workspace / 'example'), 'show', revision + ':.weavegate/config.yaml'])
    config.write_bytes(original.replace(b'db: mysql:8.4', ('db: ' + image).encode()))
    inputs = destination / label
    inputs.mkdir()
    for relative in ['pom.xml', '.mvn/wrapper/maven-wrapper.properties', '.weavegate/config.yaml',
                     '.weavegate/schedules/sch_6f1ffd61cc07.json', 'db/seed.sql', 'db/migration/001_schema.sql',
                     *[str(p.relative_to(app)) for p in sorted((app / 'src').rglob('*')) if p.is_file()]]:
        copy = inputs / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(app / relative, copy)
        plan['execution_pins'].append({'path': str(app / relative), 'sha256': sha(copy)})
    jar = app / 'target/booking-0.0.1-SNAPSHOT-weavegate.jar'
    plan['execution_pins'].append({'path': str(jar), 'sha256': sha(jar)})
    with zipfile.ZipFile(jar) as archive:
        write(inputs / 'jar-dependencies.json', {n: hashlib.sha256(archive.read(n)).hexdigest()
              for n in sorted(archive.namelist()) if n.startswith('BOOT-INF/lib/') and n.endswith('.jar')})
    shutil.copyfile(workspace / (label + '-build.log'), inputs / 'build.log')
    for operation in ('replay', 'explore'):
        workload = 'example-' + label + '-' + operation
        argv = ['run', '--config', '.weavegate/config.yaml', '--scenario', 'double-booking', '--repeat', '20']
        if operation == 'replay':
            argv += ['--replay', '.weavegate/schedules/sch_6f1ffd61cc07.json']
        vulnerable = label == 'vulnerable'
        explored = (1 if vulnerable else 18) if operation == 'explore' else 0
        plan['workloads'][workload] = {'cwd': str(app), 'argv': argv, 'source_commit': revision,
            'exit': 2 if vulnerable else 0, 'schedule_runs': (21 if vulnerable else 18) if operation == 'explore' else 20,
            'schedule_id': None if operation == 'explore' and not vulnerable else 'sch_6f1ffd61cc07',
            'observation': {'mode': operation, 'repeat': 20, 'flaky': False,
                'violation_runs': 20 if vulnerable else 0, 'schedules_explored': explored,
                'explore_passes': (1 if vulnerable else 3) if operation == 'explore' else 0}}
        ids = ['%03d' % i for i in range(1, 9)]
        def key(i, suffix):
            return hashlib.sha256('|'.join([seed, 'E1', 'tuning', workload, '01', i, suffix]).encode()).hexdigest()
        first = set(sorted(ids, key=lambda i: key(i, 'order'))[:4])
        plan['order'].extend({'id': workload + '-' + i, 'workload': workload,
                             'arms': ['B', 'Bplus'] if i in first else ['Bplus', 'B']}
                            for i in sorted(ids, key=lambda i: key(i, 'sequence')))
# Enumerate the existing two-worker/two-point ordered strategy before measurement.
candidates = []
for alice_positions in itertools.combinations(range(4), 2):
    counts = {'alice': 0, 'bob': 0}
    steps = []
    for i in range(4):
        worker = 'alice' if i in alice_positions else 'bob'
        point = ['after_seat_read', 'before_reservation_insert'][counts[worker]]
        counts[worker] += 1
        steps.append({'worker': worker, 'point': point})
    identity = hashlib.sha256(json.dumps(steps, separators=(',', ':')).encode()).hexdigest()[:12]
    candidates.append({'id': 'sch_' + identity, 'steps': steps})
write(destination / 'candidates.json', candidates)
for workload, spec in plan['workloads'].items():
    spec['schedule_ids'] = ([s['id'] for s in candidates] * 3 if workload == 'example-fixed-explore'
                            else [candidates[0]['id']] * spec['schedule_runs'])
for path in [*map(Path, plan['binaries'].values()), source / 'pilot.py', Path(sys.executable).resolve(),
             java]:
    public_path = '{repository}/' + str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)
    plan['execution_pins'].append({'path': public_path, 'sha256': sha(path)})
shutil.copyfile(workspace / 'checksums.txt', destination / 'release-checksums.txt')
write(destination / 'plan.json', plan)
print('Frozen', len(plan['order']), 'local tuning pairs; publish before collection.')
