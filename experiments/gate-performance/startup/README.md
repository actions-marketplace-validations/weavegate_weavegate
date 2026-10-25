# Fresh-JVM startup tuning

This is A3 engineering evidence for [#172](https://github.com/weavegate/weavegate/issues/172),
separate from the registered E1 pilot and confirmation. The candidate adds
`-XX:TieredStopAtLevel=1` to each owned JVM launch. `jvm_tuning: none` keeps the
original launch. Both use a fresh process and the same self-contained JAR,
Spring context, registration validation, database probe, transaction handling,
timeouts and cleanup checks.

HotSpot's [compilation levels](https://github.com/openjdk/jdk21u/blob/jdk-21.0.8%2B9/src/hotspot/share/compiler/compilerDefinitions.hpp)
define level 1 as C1 without the profiling tiers used on the way to C2.
The [Spring startup guidance](https://spring.io/blog/2018/11/08/spring-boot-in-a-container/)
also identifies this option and its later-throughput tradeoff. This candidate
does not disable class verification or defer application initialization. It
requires a VM accepting this option; there is no automatic fallback or second
startup after rejection. The explicit `none` mode supports retaining the
original compiler policy. No cache, SDK change or packaging change is required.

## Startup attribution

`instrument.py` makes a disposable copy of the fixture JAR with numeric
`System.nanoTime()` measurements around `Peer.startup`'s initialize,
registration and database-probe calls. It recompiles only `Peer` and its
nested classes against the original nested dependencies. It writes one JSON
line to `WEAVEGATE_STARTUP_PROBE` per successful startup. That experiment-only
path must be set; a failed write fails the probe run. It never records database
credentials, SQL, application logs, session IDs or parameters. The production
SDK, wire and timing artifacts are unchanged.

Build the SDK and fixture as documented in `CONTRIBUTING.md`, then:

```bash
python3 -B experiments/gate-performance/startup/instrument.py \
  --jar fixtures/spring-matching/app/target/spring-matching.jar \
  --output /tmp/spring-matching-probe.jar
```

Create a copy of the fixture configuration with absolute migration/seed paths
and `jar: /tmp/spring-matching-probe.jar`. Run both binaries with that same
configuration, `--scenario concurrent-assign --variant fixed`, the committed
replay schedule and `--repeat 3`, setting `WEAVEGATE_STARTUP_PROBE` to a separate
new JSONL file for each arm. The three startups are attribution probes, not
acceptance repetitions or E1 observations. Use the **unmodified JAR** for the
whole-gate comparison below. JVM bootstrap before `Peer.startup` and control
transport are outside these three Java intervals; they remain inside the Go
`process_start_to_ready` span. No cross-process clock subtraction is used.

## Whole-gate comparison

Build the parent CLI from `658406065cdd1a1ff3e880d467284503df08e848` and the
candidate CLI from this branch. For example, from a clean source checkout:

```bash
mkdir -p /tmp/startup-baseline-src
git archive 658406065cdd1a1ff3e880d467284503df08e848 | tar -x -C /tmp/startup-baseline-src
(cd /tmp/startup-baseline-src && go build -o /tmp/startup-baseline ./cmd/weavegate)
go build -o /tmp/startup-candidate ./cmd/weavegate
python3 -B experiments/gate-performance/startup/compare.py \
  --baseline /tmp/startup-baseline --candidate /tmp/startup-candidate \
  --config fixtures/spring-matching/.weavegate/config.yaml \
  --schedule fixtures/spring-matching/schedules/concurrent-assign.json \
  --scenario concurrent-assign --variant fixed --pairs 1 \
  --out /tmp/startup-fixed
python3 -B experiments/gate-performance/startup/compare.py \
  --baseline /tmp/startup-baseline --candidate /tmp/startup-candidate \
  --config fixtures/spring-matching/.weavegate/config.yaml \
  --schedule fixtures/spring-matching/schedules/concurrent-assign.json \
  --scenario concurrent-assign --variant vulnerable --pairs 1 \
  --out /tmp/startup-vulnerable
```

Each command runs replay and exploration, retaining all original artifacts,
CLI output, command vectors, monotonic whole-process duration and input hashes.
It uses the existing repeat count of 20 and the config's exploration passes
and timeouts. Each pair requires byte-identical deterministic artifacts and
the same JAR digest, expected exit status, non-flaky results and clean teardown.
The observer checks for new MySQL containers, snapshot JVMs and remaining
snapshot directories after each gate. It does not replace paired acceptance's
connection/transaction observers. Errors retain partial accounting and stop
collection; they are not replaced by a new observation.

One pair per workload is sufficient to inspect this prototype, not to estimate
variance or support an E1 improvement claim. The order is baseline then
candidate in the first pair, so warm-up and order effects are confounded.
Additional pairs alternate arm order. These runs use one shared WSL host,
prepared dependencies and ordinary host resource limits; they do not restore
caches or measure aggregate resource consumption. The parent CLI is a
development control, not E1's published `v0.2.0` baseline. The reviewed
candidate still needs E1's independent allocations, resource accounting and
published-baseline comparison. Public headline results remain unchanged.

## Retained local result (2026-10-25 UTC)

All eight gates completed on one shared WSL host with Corretto 21.0.8,
Spring Boot 4.0.8 and MySQL 8.4. The same original application JAR was used in
each pair. All deterministic files matched byte for byte; cleanup observations
found no new MySQL container, snapshot JVM or leftover JAR snapshot. The
parent is `6584060`; candidate production sources match `d929c11`. The binary
was built before that commit; its original build metadata and SHA-256 remain
in the bundle. No binary or application JAR bytes are bundled.

| Workload | Parent seconds | Candidate seconds | Change | Executions per arm |
| --- | ---: | ---: | ---: | ---: |
| fixed replay | 125.573 | 116.888 | -6.9% | 20 |
| fixed explore | 114.396 | 106.605 | -6.8% | 18 |
| vulnerable replay | 109.505 | 99.189 | -9.4% | 20 |
| vulnerable explore | 115.402 | 104.564 | -9.4% | 21 |

| Java stage (median of 3 startups) | Parent ms | Candidate ms |
| --- | ---: | ---: |
| initialize | 2291.338 | 1775.072 |
| registration | 24.350 | 48.884 |
| probe | 680.143 | 583.440 |

These are descriptive paired differences, with one pair per workload. The
primary fixed replay was 6.9% shorter here, below E1's 15% point-estimate
criterion; there is no confidence interval or confirmation claim. Both
Spring initialization and the first DB probe were shorter in the separate
three-startup attribution sample, while registration was slower. That small
sample is not a decomposition of the later whole-gate differences. The latter
include variable database provisioning and teardown as well as JVM startup.
The probe does not distinguish JDBC class loading from connection establishment.

The [original evidence bundle](results.tar.gz) contains both attribution
runs, all whole-gate artifacts and command vectors, source/build identities,
collector sources, the implementation patch and a complete SHA-256 inventory.
Reproduce the tables and validate counts, budgets, manifest facts and artifact
equality without Docker or a JVM:

```bash
mkdir -p /tmp/startup-evidence
tar -xzf experiments/gate-performance/startup/results.tar.gz -C /tmp/startup-evidence
python3 -B experiments/gate-performance/startup/summarize.py /tmp/startup-evidence/evidence
```

## Review boundary

The production patch changes compilation policy only. C1 can reduce throughput
in long or compute-heavy commands; retain `none` when the complete gate benefits
from higher tiers. Application scanning, eager initialization, proxy inspection,
the lease-returning DB probe, single-use adapters, process reaping and fixture
quarantine remain in their original paths. Unit tests exercise both argv forms;
the mini JVM integration observes the actual argument in two fresh sessions
and the selected manifest policy. Repeated Go acceptance and the Spring paired
suite cover lifecycle failures and deterministic outcomes. Complete
Go/Java/paired manifests are retained below; independent A4 review and
candidate freeze still precede E1 confirmation.

## Repeated acceptance and release compatibility

The separate [validation bundle](validation.tar.gz) retains logs and complete
Go/Java/paired manifests for production implementation `d929c11`, plus the
Docker-backed Go package checks and adoption-example artifacts. The later
commits only add evidence and documentation. Reproduce manifest accounting
from the repository root after extracting the bundle:

```bash
mkdir -p /tmp/startup-validation
tar -xzf experiments/gate-performance/startup/validation.tar.gz -C /tmp/startup-validation
(cd /tmp/startup-validation/validation && sha256sum -c SHA256SUMS)
for result in go/go.json java/java.json paired.json; do
  python3 scripts/check-external-sut-acceptance.py \
    --results "/tmp/startup-validation/validation/acceptance/$result" --require-complete
done
```

The Go adapter ran 20 repetitions and 20 race-enabled repetitions. Java ran
2,124 tests without failures, errors or skips, with 20 repetitions for its
acceptance witnesses. Its Surefire VM used `-XX:TieredStopAtLevel=1`; existing
child-process test launchers retain their own argv. The paired suite exercises
the product launcher and actual Spring/JDBC path, with 20 repetitions each
for vulnerable and fixed replay, rollback, process death and cancellation,
plus exploration with 20 confirmation replays. It checks process reaping,
connection cleanup and fixture state before reuse. Go, Java and paired
manifests pass `--require-complete` with 72, 60 and 2 passing rows respectively.

The first paired attempt was interrupted by session shutdown during rollback.
Its unmodified `paired-interrupted.log` is retained separately and does not
satisfy any acceptance row; the completed rerun supplies `paired.log`.

The retained adoption-example sources from the earlier local pilot were built
with the published SDK **0.2.0**. Both that release's checksum-verified CLI
and the candidate replayed each variant 20 times: vulnerable produced 20/20
violations, fixed produced 20/20 PASS, each retained one fingerprint and
`sch_6f1ffd61cc07`, and all five deterministic files matched byte for byte.
The same application JAR was used for each comparison; its dependency
inventory and SHA-256 are retained. Build/launch commands and original
artifacts are in `adoption/`, `run-adoption-checks.sh` and
`run-release-comparison.sh`. These are compatibility checks, not additional
performance observations. The workflows ran concurrently with other tests,
so their durations do not enter the tuning table or E1.

A4 still owns independent review and candidate freeze; the registered E1
confirmation and its public performance claims remain separate work.
