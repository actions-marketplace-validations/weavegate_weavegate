#!/usr/bin/env python3
"""Real Docker/JDK feasibility checks, explicitly outside any E5 measurement sample."""
import argparse
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().with_name('imports.py')),
              init_globals={'dependencies': ('support', 'prepare', 'tool', 'analyze', 'stream')})
from support import *
from prepare import prepare
from tool import freeze, collect
from analyze import baseline, analyze
from stream import Monitor


def smoke(destination, count):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    m = prepare(HERE.parents[2], destination / 'prepared', count)
    for workload in WORKLOADS:
        for variant in ('vulnerable', 'fixed'):
            directory = destination / (workload + '-' + variant); directory.mkdir()
            slot = dict(order()[0], workload=workload, variant=variant, arm='B1', cap_seconds=600)
            context = dict(slot, trial_dir=str(directory), campaign_dir=str(destination), redact_env=[],
                           execution_pins=m['pins'], execution_runtime=runtime_identity(),
                           execution_directories=execution_directories(m))
            with Monitor(directory):
                command = execute(m['commands'][workload][variant]['B1'], context, directory, 'run', [])
            result = baseline(directory, slot, command, m['inputs'][workload][variant])
            write_json(directory / 'validation.json', result)
            seal(directory)
            require(succeeded(command) and result['cleanup_proven'] and not result['quality_reasons']
                    and not result['operational_reasons'] and result['valid_evaluations'] == count,
                    'baseline feasibility failed: ' + str(directory))
            print('E5_BASELINE_RESULT workload=%s variant=%s executions=%d proxy=original hooks=inactive oracle=existing resets=checked cleanup=proved' % (workload, variant, count), flush=True)
    # All five actual arms in exactly the first registered block order. Other
    # slots remain explicitly unstarted. No VM restore claim is made here.
    bundle = destination / 'campaign'
    freeze(destination / 'prepared/manifest.json', bundle)
    collect(bundle, max_slots=5)
    result = analyze(bundle, destination / 'analysis')
    require([s['status'] for s in result['slots'][:5]] == ['complete'] * 5,
            'five-arm feasibility failed; inspect retained results.json')
    analyze(bundle, destination / 'reanalysis')
    for name in ('results.json', 'report.md'):
        require((destination/'analysis'/name).read_bytes() == (destination/'reanalysis'/name).read_bytes(), 'offline analysis differs')
    print('E5_SMOKE_RESULT arms=B0,B1,B2,T-R,T-E sample=feasibility raw=json analysis=identical', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--count', type=int, default=20)
    args = parser.parse_args()
    smoke(args.destination, args.count)
