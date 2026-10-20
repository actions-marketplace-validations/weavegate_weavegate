#!/usr/bin/env python3
"""Render descriptive tables from pilot.py's reproducible reduction."""
import json
from pathlib import Path
import sys


result = json.loads(Path(sys.argv[1]).read_text())
lines = ['# Local timing observations', '',
         'Descriptive local tuning only; no E1 quality pass or improvement claim.', '',
         'The unit is a whole CLI invocation paired with its mate on the same shared WSL host. '
         'Internal schedule runs are not independent samples. All intervals below use seconds; '
         'SD is sample standard deviation and IQR uses linear-interpolated quartiles.', '',
         '| Workload | Artifact-matched / planned pairs | B median (IQR; SD) | B+ median (IQR; SD) | B+/B geometric mean | SD of log ratio |',
         '| --- | ---: | ---: | ---: | ---: | ---: |']


def number(value, scale=1):
    return 'N/A' if value is None else f'{value * scale:.4f}'


def time_cell(value):
    return 'N/A' if value is None else f"{number(value['median'])} ({number(value['iqr'])}; {number(value['sample_sd'])})"


for name, value in result['estimates'].items():
    lines.append(f"| {name} | {value['artifact_matched_pairs']} / {value['planned_pairs']} | "
                 f"{time_cell(value['B_seconds'])} | {time_cell(value['Bplus_seconds'])} | "
                 f"{number(value['Bplus_over_B_geometric_mean'])} | {number(value['log_ratio_sample_sd'])} |")
lines += ['', 'B is the published release. B+ adds timing only. Their ratio includes instrumentation, '
          'build differences, cache effects, and shared-host variation; it is not an optimization effect.', '',
          '## Exclusive stage coverage', '',
          'Every B+ gate is partitioned without summing nested or overlapping intervals. '
          'Percentages are the median of per-gate stage/wall fractions, so medians need not sum to 100%. '
          'The raw reduction retains cumulative inclusive work separately.', '']
for name, value in result['estimates'].items():
    if lines[-1] != '':
        lines.append('')
    lines += ['### ' + name, '',
              '| Stage | Gates | Median seconds | IQR seconds | SD seconds | Median wall % |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for stage, seconds in value['exclusive_stage_seconds'].items():
        fraction = value['exclusive_stage_wall_fraction'][stage]
        lines.append(f"| {stage} | {seconds['n']} | {number(seconds['median'])} | "
                     f"{number(seconds['iqr'])} | {number(seconds['sample_sd'])} | {number(fraction['median'], 100)} |")
lines += ['', '## Accounting', '',
          '| Workload | Arm | Planned | Completed receipts | Artifact checks passed |',
          '| --- | --- | ---: | ---: | ---: |']
for workload in result['estimates']:
    for arm in ('B', 'Bplus'):
        rows = [r for r in result['trials'] if r['workload'] == workload and r['arm'] == arm]
        observed = [r for r in rows if r['result'] is not None]
        good = [r for r in observed if not r['result']['problems']]
        lines.append(f'| {workload} | {arm} | {len(rows)} | {len(observed)} | {len(good)} |')
lines += ['', 'Artifact checks passed means the local checks described in the registration. '
          'It does not include the missing resource, dedicated-host, cache restoration, or full '
          'acceptance obligations of E1. The initial stopped campaign remains separate.', '',
          'Collection stop reason: ' + (result['status']['stopped'] or 'none; assigned collection finished') + '.', '']
Path(sys.argv[2]).write_text('\n'.join(lines))
