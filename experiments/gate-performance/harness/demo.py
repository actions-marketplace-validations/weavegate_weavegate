#!/usr/bin/env python3
"""Run a constructed harness example. Outputs are NOT performance observations."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis import analyze
from collector import collect, freeze
from common import E1_RESOURCES, PROTOCOLS, QUALITY, SEED, digest, read_json, sampling, write_json

HOOK = '''import hashlib,json,sys,os
from pathlib import Path
step=sys.argv[1]; root=Path(sys.argv[2])
if step=='run': sys.exit(int(os.environ['RUN_EXIT']))
if step in ('observe','cleanup'):
 data=b'CONSTRUCTED demo evidence; no product was executed\\n'
 (root/(step+'.log')).write_bytes(data)
 evidence={step+'.log':hashlib.sha256(data).hexdigest()}
 if step=='cleanup':
  value={'proven':True,'evidence':evidence}
 else:
  value={'checks':{k:True for k in json.loads(sys.argv[3])},
         'metrics':{k:1 for k in json.loads(sys.argv[4])},
         'facts':dict(json.loads(sys.argv[5]),environment='constructed'),
         'environment':{'identity':'constructed'},
         'resource_method':'constructed demo values, not telemetry','evidence':evidence}
 (root/('cleanup.json' if step=='cleanup' else 'receipt.json')).write_text(json.dumps(value))
print('CONSTRUCTED_DEMO step='+step)
'''


def demo(destination):
    root = Path(destination).resolve()
    root.mkdir(mode=0o700)
    source = root / 'input'
    source.mkdir()
    hook = source / 'hook.py'
    hook.write_text(HOOK)
    for name in PROTOCOLS:
        (source / name).write_bytes((Path(__file__).resolve().parents[1] / name).read_bytes())
    strata = sampling('E1', 'pilot')
    def facts(w):
        return {'schedule': 'constructed', 'flaky': False,
                'schedule_runs': 21 if w == 'example-vulnerable-explore' else (
                    18 if w == 'example-fixed-explore' else 20)}
    def command(step, w):
        return {'argv': [sys.executable, str(hook), step, '{trial_dir}',
                         json.dumps(QUALITY), json.dumps(E1_RESOURCES), json.dumps(facts(w))],
                'cwd': str(source), 'env': {'RUN_EXIT': str(2 if 'vulnerable' in w else 0)}, 'timeout_seconds': 600 if step == 'run' else 10}
    m = {'schema': 1, 'campaign_id': 'constructed-demo', 'experiment': 'E1', 'phase': 'pilot',
         'protocol_revision': 1, 'seed': SEED, 'strata': strata,
         'dates': [(datetime.now(timezone.utc) + timedelta(days=i)).strftime('%Y-%m-%d')
                   for i in range(2)],
         'quality_checks': list(QUALITY), 'resource_metrics': list(E1_RESOURCES),
         'compare_facts': ['schedule', 'environment'], 'redact_env': [],
         'deviations': ['constructed demo: no product, resource monitor or real public registration'],
         'commands': {w: {arm: {step: command(step, w) for step in
                               ('prepare', 'run', 'observe', 'check', 'cleanup')}
                         for arm in ('baseline', 'candidate')} for w in strata},
         'expected_exit': {w: {a: [2 if 'vulnerable' in w else 0] for a in ('baseline', 'candidate')} for w in strata},
         'expected_facts': {w: {a: facts(w) for a in
                                ('baseline', 'candidate')} for w in strata},
         'pins': [{'path': 'hook.py', 'sha256': digest(hook), 'role': 'constructed demo hook'}],
         'execution_pins': [{'path': str(hook), 'sha256': digest(hook), 'role': 'constructed demo hook'},
                            {'path': sys.executable, 'sha256': digest(Path(sys.executable)),
                             'role': 'Python interpreter'}],
         'protocols': {name: {'path': name, 'sha256': digest(source / name)} for name in PROTOCOLS}}
    for inventory in ('arms', 'inputs', 'runtime', 'environment', 'observation', 'sources'):
        m[inventory] = {'description': 'constructed demo; not a registered measurement campaign'}
    write_json(source / 'manifest.json', m)
    bundle = root / 'campaign'
    freeze(source / 'manifest.json', bundle)
    registration = root / 'constructed-registration.json'
    write_json(registration, {'campaign_sha256': digest(bundle / 'campaign.json'),
                             'order_sha256': digest(bundle / 'order.json'),
                             'registered_at': '2020-01-01T00:00:00Z',
                             'revision': 'constructed', 'public_url': 'https://example.invalid/demo'})
    block = read_json(bundle / 'order.json')[0]['block']
    collect(bundle, block, registration)
    analyze(bundle, root / 'analysis')
    print('CAMPAIGN_DEMO_RESULT evidence=constructed measured_product=false')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: python3 demo.py NEW_DIRECTORY')
    demo(sys.argv[1])
