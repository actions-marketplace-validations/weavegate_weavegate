# E1: product gate time

Protocol revision: **1**, governed by the [shared contract](README.md).
This is a planned experiment, not a performance result. Its primary question
is whether the candidate finishes the adoption example's **fixed saved replay,
repeat 20**, faster than the published `v0.2.0` while preserving validation.

## Arms and frozen application inputs

| Arm | Definition | Use |
| --- | --- | --- |
| B | Published `v0.2.0` CLI release and Maven Central `weavegate-spring:0.2.0` | Contemporaneous baseline in every confirmation pair |
| B+ | B source plus reviewed timing instrumentation only, no optimization | Pilot decomposition and instrumentation overhead only |
| W | Candidate CLI and matching SDK, frozen after implementation and review | Confirmation treatment |

The baseline release resolves to weavegate commit
`ee10c7de2114086f14258037703dbd4b104e5908`. Verify downloaded release checksums
and record the actual CLI and SDK hashes. Do not rebuild B with instrumentation
and relabel it the published release. B+ must provide its full patch against
that commit; unrelated fixes or optimizations make it ineligible as B+.

Use the public
[adoption example](https://github.com/weavegate/spring-boot-adoption-example)
at fixed source `f38b20b1634235a0f46c96a2238ba10e2143fa75` and vulnerable source
`e4932192b160e60c967dc17c9d02ac478bd61968` (the intentionally failing pull
request #6). Freeze both trees, dependency resolutions and JARs in the campaign.
The application selects behavior through its source revision; passing
`--variant fixed` does not repair the vulnerable example. Within each workload,
B/B+/W use identical business source, schema, seed, hooks, workers, assertion,
schedule, Java/Boot/JDBC/pool versions and effective settings. Only the declared
CLI/SDK treatment and required packaging may differ. Publish that packaging
diff and account for any new cache/AOT generation.

Use `fixtures/spring-matching` from the baseline release commit for the second
application. Build it against each arm's SDK, preserving its business code and
configuration; it selects `fixed`/`vulnerable` through the existing CLI variant.
It is another small application of the same domain pattern, not an independent
sample of Spring application types. Go-native measurements, if later proposed,
are outside this sample and cannot be subtracted to estimate pure JVM cost.

## Workload inventory

Each row is a separate stratum and estimate. Expected counts below are
validation obligations from the existing workload shape, not new observations.

| ID | Application and operation | Expected gate result | Schedule runs per CLI |
| --- | --- | --- | --- |
| `example-fixed-replay` | Fixed example, saved `sch_6f1ffd61cc07`, repeat 20 | PASS, exit 0, not flaky | 20 |
| `example-vulnerable-replay` | Vulnerable example, same saved schedule, repeat 20 | WG001 for `one-active-reservation-per-seat`, exit 2, not flaky | 20 |
| `example-fixed-explore` | Fixed example, default ordered candidates and 3 passes | Exhausted without violation, exit 0 | 6 candidates × 3 = 18 |
| `example-vulnerable-explore` | Vulnerable example, default ordered candidates, repeat 20 | First-candidate discovery then stable confirmation, exit 2 | 1 discovery + 20 confirmation = 21 |
| `spring-fixed-replay` | Fixed spring-matching, saved `sch_7dcb74b1e506`, repeat 20 | PASS, exit 0, not flaky | 20 |
| `spring-vulnerable-replay` | Vulnerable spring-matching, same schedule, repeat 20 | WG001 for `active-assignment-is-unique`, exit 2, not flaky | 20 |

Archive the generated candidate inventory, order and content-derived IDs
before measurement. Exploration uses the existing exhaustive strategy with no
new pruning or budget cap. The vulnerable discovery must match the frozen
first candidate and the baseline's evidence; a different stopping point is a
quality failure, not fewer units of work. Do not turn exploration with early
termination into fixed-work throughput or pool it with replay.

For both applications preserve `arrive_timeout_ms=1000`, external startup
30,000 ms, cancellation 5,000 ms, stop 20,000 ms, capacity 2, two workers and
their identical configured arguments. Explicitly record effective repeat 20,
exploration passes 3, other defaults, pool size and isolation. Pin the same
MySQL 8.4 digest per campaign. Shorter waits, fewer repeats, passes, candidates
or assertions are not eligible optimizations.

## Sampling and phases

| Phase/stratum | Planned pairs | Arms | Purpose |
| --- | --- | --- | --- |
| Pilot, each of the four example workloads | 8 each (32 total) | B/B+ | Decomposition, variance and instrumentation overhead |
| Confirmation, each of the four example workloads | 30 each (120 total) | B/W | One primary workload and three descriptive secondary workloads |
| Confirmation, each spring-matching replay | 10 each (20 total) | B/W | Descriptive check on the second application |
| Empty-cache adoption, fixed example replay only | 8 | B/W | Descriptive setup/build/install/gate/upload cost |

The pilot deliberately does **not** require W: cost decomposition precedes
selecting an optimization. Compare B+ with B using whole-process time; report
instrumentation overhead and coverage gaps before interpreting stage shares.
Do not attribute B/B+ differences to an optimization. Further development
measurements are tuning data, recorded separately and excluded from the table's
confirmation counts.

Fix 30 independent allocations per example workload, spread as 10 on each of
three distinct UTC dates. Spring replay uses 4/3/3 allocations on those dates.
The empty-cache stratum uses 4/4 allocations on two dates. Pilot uses 4/4 on
two dates. Generate balanced arm order by the shared rule. A block contains
only one pair of one workload, not all workloads sharing one allocation.

These are fixed resource budgets for the study, not power guarantees. Thirty
blocks offer more stable descriptive uncertainty than a few repeated jobs;
the small supporting samples do not establish broad application or tail
behavior. Pilot variance diagnoses expected precision but **does not change
the registered counts or 15% threshold**. If precision is inadequate, complete
and report this sample or register a separate, explicitly motivated study.
Do not add observations until an interval passes the decision rule.

## Environment and cache control

Use fresh dedicated Linux x86-64 allocations with a total cap of **4 vCPUs and
16 GiB RAM**, covering CLI, child JVMs, MySQL and monitoring. The underlying
CPU model and VM image are pinned in the launch manifest and observed for
every allocation. No unrelated workloads share the allocation. W may change
internal parallelism only within the same aggregate cap; a larger resource
configuration requires a separate descriptive campaign.

In the primary and supporting replay/exploration strata, finish builds and
downloads before the timed gate. Restore an identical prepared disk/cache
snapshot for each arm, containing both arm binaries/JARs, the pinned MySQL
image and resolved dependencies. Each gate still creates a new CLI process
and database fixture; each schedule still has the existing isolation contract.
Restoration must remove prior containers, data volumes, run artifacts and
application caches created during a gate. Record cache inventory and restore
method, including VM memory/page-cache restoration so the second arm does not
inherit the first arm's OS cache. Do not reuse B's warmed database or W's
generated startup cache for its mate. Do not execute an unrecorded gate to
warm either arm.

If an optimization introduces a persistent startup cache, register its
construction, compatibility key, size and invalidation/rebuild command. The
warm stratum may prepare it, but must report that cost. A JVM-reuse treatment
requires a new ADR and isolation evidence before candidate freeze because
[ADR 0010](../../docs/adr/0010-external-sut-protocol.md) excludes reuse today.
This protocol does not choose an optimization strategy.

Empty-cache pairs restore clean environments with only the OS, Docker, JDK and
build tools installed. No repository checkout, Maven/Go dependency cache,
downloaded CLI, application JAR, MySQL image or generated startup cache is
present. Both arms have the same network policy. Time checkout, dependency
resolution, SDK/application build, CLI install, gate and evidence upload as
separate spans and end to end. Repeat after explicit startup-cache invalidation
if W introduces one, as a separate rebuild observation, not a ninth pair.

The outer gate collection budget is 600 seconds per invocation in warm
strata; the empty-cache end-to-end budget is 1,800 seconds. Expiry records an
incomplete attempt and initiates the existing cancellation/cleanup path;
record cleanup beyond the cap too. These collector limits do not replace or
relax any product timeout. Failure to prove cleanup ends use of that allocation.

## Measurements and quality gate

`T_gate` starts immediately before spawning the CLI and ends after waiting for
its exit. Measure it in the collector with a monotonic clock, retaining spawn
errors. It includes preflight, database preparation, schedule runs, Oracle,
artifact writing and cleanup. Installation/build/upload are outside `T_gate`
and inside the separately reported end-to-end adoption spans. CLI exit 2 is an
expected completed measurement only for the declared vulnerable workload.

Collect the following stage intervals when available in B+/W; unavailable
stages in released B are unknown, not fabricated from B+ or log subtraction:

- Preflight; database provision, migration and seed; each reset; JAR snapshot.
- Per-execution process start to ready, schedule/Oracle work, block recognition,
  stop/reap; final artifact writing and cleanup.
- JVM-internal Spring startup as its own locally measured duration, nested
  within process start to ready. Block recognition runs from wait start to
  classification; timeout-inferred blocking is not measured database lock time.

Record parent/child spans and overlap. Report exclusive wall coverage and an
unattributed remainder; never add nested spans or parallel worker times as if
they partitioned total wall time. Show cumulative work separately from critical
path. Timing fields must remain volatile metadata outside normalized evidence,
with any artifact change reviewed under
[ADR 0007](../../docs/adr/0007-artifact-version-policy.md).

For each whole gate, collect aggregate CPU seconds, peak concurrent memory,
container creations and peak simultaneous container count, disk bytes written,
and retained artifact/cache sizes. Include CLI descendants and MySQL; CLI RSS
alone is insufficient. Prefer isolated allocation/cgroup counters, with the
exact scope and collection overhead recorded. If memory is sampled, freeze a
100 ms cadence in both arms and label the peak as sampled, not a kernel high
water mark. Counter reset/restoration happens before timing, and teardown
resource use is included. Collection sampling is observation, not coordination.

Before confirmation, both frozen arms must pass the existing repeated Go/Java
isolated and Spring paired acceptance, including each `--require-complete`
check. Retain real logs and complete manifests; checked-in empty templates are
not evidence. Every measured pair must preserve expected verdicts, assertion
rows, schedule IDs, repeat/discovery counts, flaky status, normalized evidence,
reset/transaction isolation, JVM reaping, connection closure and cleanup.
Resource absence, lifecycle mismatch or incomplete evidence prevents the
quality gate from passing. The collector aggregates the Oracle's outcomes;
it never decides domain validity itself.

## Analysis and decision

Apply the [shared analysis](README.md#analysis-fixed-for-both-experiments) to
the 30 first-attempt fixed-example replay pairs. The primary performance rule
is **`R <= 0.85` and the 95% interval's upper endpoint `< 1`**, with all 140 warm
confirmation pairs and quality/resource obligations complete. Empty-cache
adoption is a separate descriptive phase; its failures must be reported but
do not redefine this warm-gate claim. This supports
the narrow phrase "measurable, practically useful gate-time improvement on
this workload and environment." Fifteen percent is a preselected practical
threshold, not a claim the true reduction exceeds 15%; the interval only has
to exclude no improvement.

Report paired CPU, memory and disk-write ratios and absolute container/storage
costs beside time. If any primary-workload resource ratio exceeds 1.10, label
the timing result "faster with higher measured resource cost" and name the
metric. Undefined ratios or unavailable counters preclude a resource-efficiency
claim. The 10% line is a disclosure rule, not a statistical non-inferiority
test; meeting it does not prove resource equivalence. New cache setup and
rebuild costs remain visible even if warm gate time improves.

Secondary workloads, stage shares and empty-cache results remain descriptive
regardless of their apparent effects. If the primary rule fails, publish the
estimate and uncertainty as no demonstrated practical improvement under this
protocol. A stopped/incomplete campaign is reported as such. Do not replace B
with B+, choose the fastest workload, or compare W against historical timings
to manufacture a headline improvement.
