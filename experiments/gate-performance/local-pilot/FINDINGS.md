# Local stage-cost findings

The second local tuning campaign completed **32/32 pairs, 64/64 gate
invocations**, on 2026-10-18 UTC. Every workload completed its eight planned
B/B+ pairs. All 32 pairs had byte-identical deterministic artifacts, matching
recorded schema/seed/isolation/image/JAR facts, expected outcomes and repetition
counts, and no new container or snapshot JVM remaining after cleanup observation.
The [full accounting and dispersion tables](results/local-r2.md) are generated
from [checksummed original evidence](results/local-r2.tar).

The first campaign remains stopped: two preflight failures, zero completed
gates, and 62 unstarted arm slots. Its [original evidence](results/stopped-r1.tar.gz)
and [accounting](results/stopped-r1.json) are retained separately. The
[amendment](AMENDMENT.md) records the corrected image-reference syntax before
the second registration. No observation from the first campaign enters the
second campaign's estimates.

## What the stage observations show

The largest measured B+ stage was `process_start_to_ready` in every workload.
The following values are medians over eight whole gates per workload; wall
fractions are computed separately for each gate before taking their median.

| Workload | B+ whole-gate seconds | Start-to-ready seconds / wall % | Block-recognition seconds / wall % |
| --- | ---: | ---: | ---: |
| Fixed replay, repeat 20 | 110.10 | 64.69 / 58.75% | 21.32 / 19.37% |
| Fixed exploration, 18 executions | 90.54 | 51.90 / 57.54% | 19.12 / 21.11% |
| Vulnerable replay, repeat 20 | 115.28 | 88.45 / 77.02% | 2.91 / 2.47% |
| Vulnerable exploration, 1 discovery + 20 confirmations | 116.87 | 91.62 / 77.53% | 3.05 / 2.61% |

This supports investigating the process-start-to-ready boundary as a major
cost on this host. It includes JVM/application startup, protocol exchange,
and local supervision; it does not isolate JVM or Spring-internal startup.
Fixed workloads also spend substantial recorded time classifying initial
worker arrivals. That duration includes timeout inference and must not be
described as measured database lock time. These are inputs to the later
optimization decision, not a selection of an optimization or permission to
shorten existing timeouts.

Other stage costs remain visible. Across the four workload medians, database
provisioning accounted for 8.33–9.91% of whole-gate time, adapter stop for
6.36–6.96%, fixture reset for 0.99–1.84%, and fixture teardown for 1.42–2.21%.
The full reduction preserves stage IQR and sample standard deviation, cumulative
inclusive work, exclusive coverage, and the unattributed remainder. Nested
spans are never added together as a wall-time partition.

## Variability and interpretation

B+ whole-gate sample standard deviation ranged from 7.25 to 14.03 seconds
across the four workloads. The vulnerable-exploration baseline ranged from
87.52 to 152.19 seconds. All observations remain in the data; none were
trimmed or replaced. The paired geometric B+/B ratios were 0.9648 for fixed
replay, 0.9918 for fixed exploration, 1.0386 for vulnerable replay and 1.0415
for vulnerable exploration. They mix instrumentation, build differences,
cache effects and shared-host variation. They do not identify pure
instrumentation overhead or a performance improvement.

Workloads ran in successive groups on one shared WSL host. Differences
between their wall times therefore do not establish a causal fixed-versus-
vulnerable cost difference. Neither internal repetitions nor repeated gates
on this host supply independent allocation/day samples. No confidence
interval, significance, p99, resource-efficiency, or general Spring-service
claim is made.

This completes the registered **local tuning** collection. E1 protocol
revision 1's independent-allocation/two-date pilot is still unfulfilled.
The local campaign lacks the registered CPU/memory cap, cache restoration,
aggregate CPU/memory/disk counters, JVM-internal timing, and complete acceptance
manifests. The raw executable/JAR bytes are not in this repository bundle;
their source identities, build commands, dependency hashes and artifact hashes
are retained. Those limits remain relevant when auditing or reproducing inputs.
The public headline results and the fixed E1 confirmation plan are unchanged.

## Reproduce the numbers

Run from the repository root, using Python 3.9+:

```bash
python3 -B experiments/gate-performance/local-pilot/verify_results.py
```

That command extracts both original bundles into a temporary directory,
verifies every file checksum and the plan's registration hash, reruns the
analysis with the [review validation corrections](AMENDMENT.md#post-collection-analysis-validation),
and compares the generated JSON and Markdown with the committed result files.
The source numbers for the rounded table above are in
`results/local-r2.json` under `estimates`. Collection commands, environment,
input identities and deviations are in the [campaign README](README.md)
and both retained registrations.
