#!/usr/bin/env python3
"""Prepare local feasibility inputs; never assert E1 candidate registration."""
import argparse
import os
import shutil
import subprocess
import zipfile
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support',)})
from support import *


def prepare(root, destination, count=20):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    require(count > 0, 'positive feasibility count required')
    destination.mkdir(parents=True, exist_ok=False)
    java, javac, go = [str(Path(shutil.which(x)).resolve()) for x in ('java', 'javac', 'go')]
    driver, cli = destination / 'baseline', destination / 'weavegate'
    subprocess.run([go, 'build', '-o', str(driver), './experiments/control-comparison/driver'], cwd=root, check=True)
    subprocess.run([go, 'build', '-o', str(cli), './cmd/weavegate'], cwd=root, check=True)
    image = subprocess.check_output(['docker', 'image', 'inspect', 'mysql:8.4', '--format', '{{index .RepoDigests 0}}'], text=True).strip()
    require('@sha256:' in image, 'pre-pulled MySQL 8.4 digest required')
    image = image.replace('mysql@', 'mysql:8.4@')
    protocol = root / 'experiments/control-comparison/README.md'
    m = {'schema': 1, 'protocol_revision': 1, 'campaign_id': destination.name,
         'phase': 'feasibility', 'seed': SEED, 'pins': {}, 'commands': {}, 'inputs': {}, 'hooks': {},
         'protocol': {'path': str(protocol), 'sha256': digest(protocol)},
         'fixture_revision': FIXTURE_REVISION,
         'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
         'environment': {'mysql_image': image}, 'deviations': [
             'Local feasibility only: no disk-only VM restore or 4-vCPU/16-GiB allocation proof.',
             'cold/warm are planned labels only; local feasibility does not establish those cache states.',
             'No E1 confirmation-candidate identity or measurement prerequisites are asserted.']}
    env = {'PATH': os.environ['PATH'], 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}
    def command(argv, arm):
        return {'argv': [str(a) for a in argv], 'cwd': str(root), 'env': env,
                'timeout_seconds': CAPS[arm], 'cancel_grace_seconds': 55}
    for workload, (fixture, scenario, assertion, saved, app, candidates, schedule_id) in WORKLOADS.items():
        base = root / 'fixtures' / fixture
        jar = base / 'app/target' / (fixture + '.jar')
        require(jar.is_file(), 'build fixture JAR first: ' + str(jar))
        extracted = destination / (workload + '-classpath'); extracted.mkdir()
        with zipfile.ZipFile(jar) as archive:
            for name in archive.namelist():
                if name.startswith(('BOOT-INF/classes/', 'BOOT-INF/lib/')) and not name.endswith('/'):
                    target = child(extracted, name); target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
        libraries = sorted((extracted / 'BOOT-INF/lib').glob('*.jar'))
        classes = extracted / 'BOOT-INF/classes'
        cp = os.pathsep.join(map(str, [classes, *libraries]))
        subprocess.run([javac, '-cp', cp, '-d', str(classes), str(root / 'experiments/control-comparison/launcher/BaselineMain.java')], check=True)
        m['commands'][workload], m['inputs'][workload] = {}, {}
        for variant in ('vulnerable', 'fixed'):
            inputs = destination / (workload + '-' + variant)
            subprocess.run([str(driver), '--arm', 'inputs', '--config', str(base / '.weavegate/config.yaml'),
                            '--scenario', scenario, '--variant', variant, '--image', image, '--java', java,
                            '--jar', str(jar), '--out', str(inputs)], check=True)
            value = read_json(inputs / 'inputs.json')
            # JSON is valid YAML and lets the standard-library runner compare
            # every effective field without introducing another YAML decoder.
            write_json(inputs / 'config.yaml', config_document(value['config']))
            value['counts'] = {'B1': count, 'B2': count if count != 20 else 100, 'T-R': count}
            value['jar_sha256'] = digest(jar)
            value['expected_domain_rows'] = EXPECTED_DOMAIN_ROWS[workload]
            sdk = [p for p in libraries if p.name.startswith('weavegate-spring-')]
            require(len(sdk) == 1, 'SDK identity ambiguous')
            value['sdk_sha256'] = digest(sdk[0])
            value['business_classes'] = {str(p.relative_to(classes)): digest(p) for p in classes.rglob('*.class')
                                          if '/fixture/' in str(p)}
            m['inputs'][workload][variant] = value
            commands = {}
            for arm in ARMS:
                if arm.startswith('B'):
                    argv = [driver, '--arm', arm, '--config', inputs / 'config.yaml', '--scenario', scenario,
                            '--variant', variant, '--classpath', cp, '--application',
                            'io.github.weavegate.fixture.' + fixture.replace('-', '') + '.' + app,
                            '--out', '{trial_dir}/raw']
                    if arm != 'B0': argv += ['--executions', str(value['counts'][arm])]
                else:
                    argv = [cli, 'run', '--config', inputs / 'config.yaml', '--scenario', scenario,
                            '--variant', variant, '--repeat', str(count), '--out', '{trial_dir}/raw']
                    if arm == 'T-R': argv += ['--replay', base / 'schedules' / (saved + '.json')]
                commands[arm] = command(argv, arm)
            m['commands'][workload][variant] = commands
        for path in files(base):
            if '/target/' not in str(path): m['pins'][str(path)] = digest(path)
        m['pins'][str(jar)] = digest(jar)
    for path in files(destination): m['pins'][str(path)] = digest(path)
    for folder in ('experiments/control-comparison', 'experiments/gate-performance/harness'):
        for path in files(root / folder): m['pins'][str(path)] = digest(path)
    for path in (Path(java), Path(javac), protocol, root / 'go.mod', root / 'go.sum'):
        m['pins'][str(path)] = digest(path)
    m['candidate'] = {'cli_sha256': digest(cli), 'sdk_sha256': m['inputs']['matching']['fixed']['sdk_sha256']}
    write_json(destination / 'manifest.json', m)
    print('E5_PREPARE_RESULT phase=feasibility business_classes=unchanged launcher=separate')
    return m


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination'); parser.add_argument('--root', default=str(HERE.parents[2]))
    parser.add_argument('--count', type=int, default=20, help='feasibility only; default B1=20, B2=100, T-R=20')
    args = parser.parse_args()
    prepare(args.root, args.destination, args.count)
