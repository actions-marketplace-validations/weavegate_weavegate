# E5: same-host control experiment

Protocol revision: **1**, published for
[#188](https://github.com/weavegate/weavegate/issues/188). This is a prospective
measurement contract, not an experiment result. The baseline drivers, campaign
registration, one-command runner, optional CI job and measurements are **planned**.
The existing [E1/E2 harness](../gate-performance/harness/README.md) does not yet
implement E5. All counts and budgets below are design choices.

The question is what a controlled gate costs beside environment preparation and
uncontrolled execution, and what discovery and repeatable evidence it provides
on the same host. This is descriptive, with no significance test, confidence
interval, improvement threshold or population detection-rate claim. E5 does not
modify the [E1 protocol](../gate-performance/e1-gate.md), contribute observations
to its confirmation sample, or update existing gate-time figures.

## Freeze and prerequisites

This file fixes the design before collection. A later public, machine-readable
campaign manifest fixes the concrete artifacts and infrastructure before any
campaign warm-up or measured attempt. Record this file's Git revision and
SHA-256; publish the manifest, expanded order and named input files in a commit
or immutable archive timestamped before collection. Filling artifact identities,
host identity and execution dates cannot change the rules below.

T must use the **same CLI and SDK binaries as E1's reviewed, frozen confirmation
candidate**, identified by source commit and executable/JAR digests and linked
to that E1 registration. The candidate is not known at protocol publication;
collection waits for that freeze. Do not substitute the published `v0.2.0`, a
development build or an instrumentation-only build. E5 may build its own fixture
JARs against that SDK; E1 and E5 application JARs need not be identical.

Freeze the collector, baseline launcher and analyzer source and dependency
identities too. Implement them under `experiments/`, outside the product CLI.
Feasibility/debug runs use separate campaign IDs and retained logs, never the
measurement sample. Finish feasibility before registration. Changing a rule,
workload, candidate or exclusion after observing results requires a new protocol
revision and campaign, with the reason, prior hashes and observations already
seen disclosed. Preserve stopped campaigns. An analysis bug fix retains both
analyzer versions and outputs against unchanged raw data.

Before measurement, retain successful existing repeated Go/Java external-peer
acceptance and paired Spring acceptance, including all `--require-complete`
manifest checks, and the
[Spring corpus acceptance](../../docs/reference/spring-regression-corpus.md#reproduce-and-retain-the-acceptance-evidence)
for the frozen candidate and fixture inputs. The latter includes all eight
selected schedule/variant combinations, each repeated 20 times. Baseline
feasibility must separately prove proxy invocation, no-op hooks, commit before
evaluation, reset and cleanup. These are prerequisites, not E5 measurements.

## Workloads and immutable inputs

Use fixture business source, schema, seed, arguments, assertions and saved
schedules from reviewed repository commit
`e1bd8ba3fc07d024b8cb4037aa2263d24044dbb0`. Resolve those paths from that checkout,
not a moving branch. Packaging changes needed to link the frozen SDK or select
the baseline entrypoint must be retained as a diff; business changes require a
new protocol. Both `vulnerable` and `fixed` variants of every row are mandatory.

| Workload ID / fixture | Scenario | Cost-panel saved schedule | Domain assertion | Exhaustive candidates per pass |
| --- | --- | --- | --- | --- |
| `matching` / `fixtures/spring-matching` | `concurrent-assign` | `schedules/concurrent-assign.json`, `sch_7dcb74b1e506` | `active-assignment-is-unique` | 6 |
| `quota` / `fixtures/spring-quota` | `concurrent-reserve` | `schedules/overlap.json`, `sch_71c5162c990b` | `quota-is-conserved` | 70 |
| `job` / `fixtures/spring-job-claim` | `complete-and-reassign` | `schedules/overlap.json`, `sch_739dbe2cd669` | `completion-requires-current-lease` | 70 |

Each scenario has two workers; the matching scenario has two points per worker,
and quota/job have four. Candidate counts follow the existing exhaustive
strategy's order-preserving enumeration: `C(4,2)=6`, `C(8,4)=70`. These are
structural counts, not observed discovery costs. Register every candidate's
ordered steps and content-derived ID before collection; use the existing
lexicographic worker-index order, with no pruning or preferred schedule first.

Quota/job are the required order-dependent cases. Their saved serial schedules
pass on both variants; their overlap schedules violate the named domain
assertion on the vulnerable variant and pass on the fixed variant. Retain the
receipt-count assertions as well. The selected overlap schedules observe one
commit before releasing the next write. Exhaustive exploration also visits
other orders, including overlapping writes; corpus acceptance does not prove
their outcomes or timing. A failure or flaky exploration remains a result; do
not remove those candidates to complete a favorable comparison.

Matching is context, not evidence that scheduling improves detection. The
earlier [Go-native baseline experiment](../../docs/experiments/baseline-comparison.md)
found no detection gap on its matching workload. That observation is not a
measurement of the Spring workloads here. A gap must be observed in this
campaign, and cannot be assumed from the presence of order-dependent fixtures.

All arms use the same pinned MySQL 8.4 image digest and server settings,
migration/seed, two worker arguments and Oracle SQL within a workload/variant.
Pin Java 21 and the resolved Boot, Framework, Connector/J and pool versions;
retain dependency trees and business-class digests for both launch paths.
Preserve the fixture's effective isolation, pool limits and transaction proxy.
Record `VERSION()` and effective session isolation. Preserve T's capacity 2,
arrival timeout 1,000 ms, startup 30,000 ms, cancellation 5,000 ms and stop
20,000 ms. Freeze all other effective settings rather than relying on defaults.

## Arms and comparisons

An execution means one reset followed by both workers, committed completion,
Oracle evaluation and process cleanup. Twenty executions are not twenty
independent host samples. Each execution starts a fresh JVM; each invocation
starts a fresh MySQL fixture and ends with fixture teardown. There is no live
JVM or database reuse across invocations or arms.

| Arm | Registered work in one block | Purpose |
| --- | --- | --- |
| B0 | Provision MySQL, apply migrations and seed, verify fixture readiness, then tear down; no application invocation or domain verdict | Environment cost reference |
| B1 | Run the two workers with no-op sync-points for exactly 20 executions, evaluating every execution, without early stopping on a violation | Equal-count cost reference for T-R |
| B2 | Repeat uncontrolled two-worker executions, at most `K=100` within a 900 s collection budget; evaluate every execution and retain the first violation | Bounded stress discovery and frequency |
| T | Two separately timed invocations: T-R (saved replay, repeat 20) and T-E (exhaustive explore, 3 passes, automatic confirmation repeat 20 on discovery) | Controlled cost, discovery and replay evidence |

The **cost panel** compares B0, B1 and T-R. B1 and T-R have the same execution
count and fixture lifecycle. The **search panel** compares B2 and T-E. T-R is a
known-schedule replay, not a discovery observation; its result never substitutes
for a failed T-E search. Never sum T-R and T-E into a single comparable time or
subtract a 20-execution B1 invocation from a variable-length exploration.

T-E uses the ordinary CLI without `--replay`, with repeat 20 and exploration
passes 3 explicitly pinned in the effective config. It stops at discovery and
performs the existing automatic confirmation; do not add a second confirmation
invocation. If all passes exhaust, the planned counts are 18 for matching and
210 for quota/job. If discovery occurs, count the actual candidates evaluated
plus 20 confirmation executions; a partial confirmation is incomplete. Do not
predeclare a discovery index from the selected overlap schedule.

### Uncontrolled execution and Oracle ownership

B1 and B2 use the same baseline launcher and workflow invocation. In a fresh
Spring context, invoke the original command beans through their transaction
proxies with the original `CommandContext`. Do not enter `WeavegateChild.run`;
the existing [inactive sync-point behavior](../../sdk/java/src/main/java/io/github/weavegate/sdk/Weavegate.java)
returns immediately. Keep the hooks and after-commit callbacks in the business
code. An entrypoint/classpath difference is allowed and must be disclosed;
rewritten SQL, transactions or command implementations are not allowed.

Create both worker tasks before opening one common start latch. It aligns
submission only, before either enters its command proxy. There are no barriers
at reads, writes or commit, no launch delays, sleeps, yield hints, retries or
injected random jitter. OS and database scheduling determine the interleaving.
Both arms receive the same fixture description and known failing schedule as
the experimenter; that knowledge does not change the frozen uncontrolled rule.

Join both workers after proxy return, then evaluate through the existing Go
`internal/oracle` and `internal/oracle/sqlassert` composition using exactly the
configured assertions. A standalone experiment package can use those packages
without changing the engine. Retain the Oracle evaluation and rows. The Java
launcher, collector and analyzer do not implement domain verdicts. In B0,
readiness/seed verification is lifecycle evidence, not an application PASS.

B1 completes 20 executions even after domain violations. B2 also **continues
after its first violation** until 100 executions or its time cap. This fixed
exposure is intentional: stopping on the first success would make `x/K` an
invalid description of detection frequency. Record the first event while
retaining later outcomes. Every execution resets to the seed; there are no
same-JVM retries. A domain violation is an observation, while an operational
failure ends that invocation after cleanup, leaving remaining slots unstarted.

No-reset retries, extra workers, live JVM reuse or fixture-specific delayed
starts would define another stress treatment and need another protocol. A
fresh-JVM stress comparator is deliberately lifecycle matched; it does not
represent the fastest possible persistent application stress harness.

If a Spring B1/B2 launcher requires engine or SDK changes, open an issue and
mark that workload/arm unsupported before registration. Retain its planned
slots as unsupported, with the reason; do not implement the changes as an E5
shortcut. A reduced Go-native matching campaign may be registered separately
with all compared arms using the same Go-native fixture and these same counts,
budgets and accounting rules. It cannot replace the Spring rows, establish an
order-dependent detection advantage, or be subtracted from Spring to infer JVM
cost. New order-dependent Go-native workloads require a protocol amendment.

## Sampling, order and cold/warm strata

For each of the three workloads, two variants and two cache strata, plan
**10 blocks**, each containing B0, B1, B2 and T. This is 120 blocks, 480 arm
slots and 600 timed invocations because T contains T-R and T-E. B0 has no
variant behavior but is measured in each block to provide a local reference.
Do not share or duplicate one observed B0 time across variant rows.

Use one dedicated, fixed Linux x86-64 host for a campaign, with an aggregate
cap of **4 vCPUs and 16 GiB RAM** covering the launcher/CLI, children, MySQL and
observers. Pin its CPU model, OS/kernel, VM image and Docker version; record
physical/VM identity, observed caps, UTC dates and background workload policy.
Run one invocation at a time. A host replacement or changed cap starts another
campaign. Repeated processes and restored VMs on that host are not independent
allocations; multiple dates do not turn this into a multi-host sample.

Build/download all binaries, JARs, dependencies and the MySQL image before
timing. Restore the same prepared **disk-only** VM image before each timed
invocation, with fresh VM memory, no application data volume, run artifacts or
generated startup cache. Preserve evidence outside that image. Register the
restore/boot/readiness commands and verify the clean inventory before use.
Do not claim cold physical storage or CPU caches: the host/hypervisor cache is
outside this control.

- `cold`: no application warm-up after that restore. This means unprimed guest
  application caches with preinstalled dependencies, not cold adoption/download.
- `warm`: after the same restore, run exactly one untimed T-R invocation for
  this workload/variant with repeat 20. Verify its expected result and complete
  cleanup, remove its outputs/data volumes, then start the measured invocation
  in the same guest without flushing its filesystem cache. This common priming
  path applies to B0/B1/B2/T-R/T-E alike; it does not reuse a JVM or database.

Record every warm-up command, result and duration separately. No extra warm-up
is allowed. A failed warm-up makes the corresponding measurement slot
`preparation_failed`; do not repeat warm-up or replace that slot. The warm-up's known
schedule is not an input to measured T-E discovery. Persistent candidate startup
caches are absent on restore; any cache produced by the registered warm-up must
be inventoried, with its construction time, key, size and invalidation method.
If these restore boundaries cannot be achieved, report a separate deviation
campaign with actual cache state; do not silently relabel it cold or warm.

Generate and publish the complete order file before collection. The seed is
the literal UTF-8 string `weavegate-e5-v1-20261019`; hashes below use SHA-256,
literal `|` delimiters and no trailing newline.

1. Enumerate `workload` as `matching`, `quota`, `job`; `variant` as `vulnerable`,
   `fixed`; `cache` as `cold`, `warm`; and block IDs `01` through `10`.
2. Within each workload/variant/cache cell, sort block IDs by the full unsigned
   big-endian digest of `seed|workload|variant|cache|block|arm-order`, breaking a
   tie by block ID. At zero-based rank `r`, use row `r % 4` of the table below.
3. Sort all 120 block keys by the digest of
   `seed|workload|variant|cache|block|sequence`, breaking ties lexicographically
   by the full key. This is the collection sequence; do not reshuffle by result.
4. In odd-numbered block IDs, T-R precedes T-E; in even IDs, T-E precedes T-R.
   Restore/prime separately before each. Expand both into the order file.

| Row | First | Second | Third | Fourth |
| --- | --- | --- | --- | --- |
| 0 | B0 | B1 | T | B2 |
| 1 | B1 | B2 | B0 | T |
| 2 | B2 | T | B1 | B0 |
| 3 | T | B0 | B2 | B1 |

At N=10 each arm occupies each position two or three times; the first eight
ranked blocks balance positions and preceding-arm pairs. This limits, rather
than eliminates, temporal drift. Record actual start order and interruptions.
Use attempt 1 only, with no replacement blocks and no result-driven extension.
Diagnostic reruns are separate feasibility data. Continue later registered
arms after an ordinary failure only when cleanup is proved; quarantine the
allocation and stop the campaign if isolation cannot be established. Preserve
all remaining planned slots as not started with the stop reason.

## Budgets and timing boundaries

Collection caps are B0 **120 s**, B1 and T-R **600 s**, B2 and T-E **900 s**.
The timer begins before launching the arm process, including fixture setup;
B2's cap is not a fresh 900 s of worker-only stress after preparation. The cap
includes evaluation and evidence writing. Expiry initiates cancellation and
bounded cleanup; record actual exit/cleanup time even beyond the cap. Completed
work after the cap is late evidence, not an in-budget detection. Preserve all
product timeouts. Baseline startup/cancellation/stop and fixture cleanup bounds
match the frozen product settings; baseline worker execution has a **30 s**
deadline, reported as an operational limit rather than a scheduling mechanism.

| Metric | Start and end | Accounting |
| --- | --- | --- |
| `invocation_ns` | Collector immediately before process spawn through wait/reap after fixture teardown | Primary total cost; retain spawn failures |
| `environment_ns` | Fixture provision request through DB readiness, migrations and seed | B0 reference and actual span in other arms |
| `reset_ns` | Reset request through verified fresh seed | One per execution; keep setup separate |
| `startup_ns` | Child launch request through application ready for workers | Baseline readiness receipt or T `process_start_to_ready`; includes differing launch machinery |
| `work_ns` | Worker dispatch through committed terminal/join | T `schedule_execute` includes coordination; not pure SQL time |
| `oracle_ns` | Existing Oracle evaluation entry through return | Evaluation errors retained, never PASS |
| `stop_ns`, `teardown_ns` | Child stop through reap, and fixture teardown through completion | Separate spans, including failures |
| `explore_ns`, `replay_ns` | Existing T exploration-pass and replay span boundaries | Inclusive parents, not additional disjoint stages |

Retain monotonic offsets, durations, clock owner and parent/child links. Use
the existing [product timing contract](../../docs/reference/report-schema.md#run-timings)
for T. Its recorder ends before artifact writing; collector spawn-to-exit is
the whole invocation. Compute exclusive coverage by interval unions, not by
adding nested spans. Show unmatched time as remainder. JVM-internal startup
is unavailable unless separately observed; it cannot be inferred by subtraction.
Never subtract timestamps from different processes or compare monotonic origins.

### First violation and usable evidence

Record two different endpoints, with the same invocation origin for each arm:

- **First domain evaluation:** the end of the earliest Oracle evaluation that
  emits the named domain assertion's violation within budget. A different
  assertion, worker exception, timeout or nonzero exit is not a detection.
- **Usable evidence:** the collector's receipt and validation of retained
  domain evidence. For T-E this includes the CLI result and the automatic
  20-repeat confirmation; for B2 retain each execution's Oracle evidence as
  it arrives. A B2 first observation is not a controlled 20-repeat confirmation.

The existing CLI does not stream discovery verdicts to the collector. On a
completed T-E result, locate the discovery execution from the saved schedule,
exploration pass and evaluated-candidate count; its `oracle_evaluate` end is a
local-clock observation, retrospectively supported by domain evidence. Let
`d` be that end minus the `command` span start, `c` the command duration, and
`w` the collector invocation duration. Report the whole-invocation first-event
bound **`[d, d + (w-c)]`**, not an invented aligned timestamp. The unknown
launch/pre-command portion lies within the measured outer remainder. Reject
negative remainder or ambiguous discovery-span mapping as missing timing.
Use the same construction for any baseline event recorded on a child clock;
an event on the collector clock needs no alignment. Retain original spans.

Require the discovery fingerprint to agree with the retained domain evidence.
If the original discovery evaluation cannot be established, for example when
a flaky result retains only different replay evidence, report discovery timing
as unknown rather than assigning that replay's assertion to the discovery.

An event is certainly in budget only when its upper bound is at most the cap;
it is late when its lower bound exceeds the cap. A bound crossing the cap is
`budget_membership_unknown`, reported separately and excluded from the
in-budget detection numerator, with the planned denominator unchanged. Apply
the same rule to exact times by treating them as zero-width intervals.

Do not rank overlapping bounds as a faster discovery. If the process fails
before discovery evidence is retained, first detection is unknown, even if a
log suggests a violation. An observed first violation with later WG090 or
cleanup failure remains visible, but is not stable, usable gate evidence.
Report first observation, completed confirmation and clean final completion
separately. An exhausted search has no first-event time; it is not zero or the
cap. Timeouts and unstarted arms are incomplete, not negative detections.

## Outcomes and denominators

The analyzer aggregates existing Oracle verdicts and checks evidence structure;
it does not decide the domain invariant from receipt values or exit codes.
Retain expected rows from the corpus as validation evidence, including its
secondary receipt assertions, not a replacement verdict implementation.

| Observation | Classification |
| --- | --- |
| T-R vulnerable: expected domain WG001, exit 2, 20/20 matching fingerprints and clean cleanup | Completed controlled reproduction |
| T-R fixed: PASS, exit 0, 20 matching fingerprints and clean cleanup | Completed fixed control |
| T-E vulnerable: expected discovery plus stable automatic repeat 20 | Completed controlled discovery and reproduction |
| T-E vulnerable: exhausted without violation | Complete search without detection; report the unexpected miss |
| T-E fixed: all three passes exhausted with no violation and clean cleanup | Completed fixed search control |
| B1/B2 vulnerable execution: expected domain assertion or PASS | Observed violation or observed non-detection; either can legitimately occur |
| Fixed execution emits a domain violation, or a receipt assertion fails | Unexpected domain/quality failure; disclose, never count as intended detection |
| WG090 / exit 3 | Flaky; separate from stable reproduction and PASS |
| Exit 4/5/130, exception, deadline, missing evidence, reset or cleanup failure | Operational/incomplete; retain cause, time and resource use |
| B0 provision/seed/teardown completes | Lifecycle completion only; domain outcome is not applicable |

A failed B1/B2 execution ends the invocation; domain outcomes alone do not.
Do not treat intended vulnerable exit 2 as a shell failure, or `report` exit 0
as a test PASS. Retain the original
[exit/cleanup contract](../../docs/reference/exit-codes.md).
Unexpected/missing T fingerprints, assertion rows, schedule steps or execution
counts fail quality. Do not relax those checks to make the campaign complete.

For each workload/variant/cache/arm, publish planned, started, complete,
quality-failed, operationally failed, budget-exhausted, preparation-failed,
unsupported and not-started counts. Store one mutually exclusive final status per slot and
separate flags for domain outcomes, first detection, budget expiry and cleanup
so categories cannot silently overlap. T-R and T-E have distinct slot statuses;
T is complete only if both are complete and valid. Preserve slow values, with
no outlier trimming, imputation or replacement after an infrastructure outage.

For started slots, status precedence is operational failure (including unproven
cleanup or missing required evidence), quality failure, budget exhausted, then
complete. Preserve all applicable reason flags even when one status takes
precedence. An unsupported slot never starts; a failed prerequisite is
preparation-failed; other unstarted slots are not-started. A clean cancellation
caused only by the outer collection cap is budget-exhausted, not an operational
failure. B2 is complete only with all 100 valid executions and cleanup within
budget; a cap-limited invocation can still have valid detection evidence.

For each B2 invocation, retain `K=100`, attempted count `a`, valid completed
evaluations `m`, domain violations `x`, and reasons for all `K-m` other slots.
Publish **`x/m`** (N/A if `m=0`) and **`x/100`** together; the latter is a
planned-budget yield, not an estimated violation probability when exposure is
incomplete. Retain valid executions from a cap-limited stress attempt alongside
its incomplete exposure. A fault is not a valid non-violation. Also show the number
of blocks with any in-budget detection over **10 planned blocks**, accompanied
by the number with valid observation exposure. Fixed controls use the same
denominators for unexpected violations and operational failures separately.

For T, report discovery blocks/10, stable confirmation blocks/10, per-invocation
`violation_runs/20`, fingerprint counts, selected schedule ID and actual
exploration executions. Neither B2's uncontrolled `x/m` nor T's selected
schedule's 20/20 is an independent-sample estimate or interchangeable with the
other. Keep B1's 20 outcomes visible too; they are cost-panel observations.

## Descriptive analysis and claim limits

Keep workload, variant and cache strata separate. For each scalar duration,
publish the valid count beside median, p90, minimum and maximum in seconds.
Sort the valid values and use linear interpolation at zero-based index
`(n-1)*p`; median uses `p=.5`, p90 uses `p=.9`. With no valid values, return N/A;
with one, report the value and n=1. At n=10, p90 interpolates between the ninth
and tenth values and is not a population tail estimate.

For the cost panel, compute within-block differences and ratios on complete,
quality-valid pairs, then summarize those derived values by the same rule:

- `T-R - B1` and `T-R / B1`: total controlled-path cost relative to no-op work.
  Show signed differences and do not truncate negative values. Undefined or
  zero denominators produce N/A with the reason.
- B0's provision and total lifecycle time beside the **actually measured**
  environment span in B1/T-R. B0 includes teardown; do not subtract its total
  as if it were the exact environment portion of another invocation.
- B1/T-R whole-invocation duration divided by 20, and their observed per-
  execution stage distributions. The former amortizes setup and teardown;
  the latter does not. Search time divided by variable executions is labeled
  descriptive cost per evaluated execution, never equal-work throughput.

`T-R - B1` includes scheduling, protocol/launch supervision, evidence writing,
blocking recognition and potentially different application/SQL paths induced
by the order. It is **not an isolated causal estimate of sync-point overhead**.
The stage evidence bounds interpretation; missing stages remain unknown.
Go-native/Spring subtraction and subtraction of independent medians are not
permitted ways to manufacture a component cost.

For search, show every block's first-event interval or exact time, exposure
duration, stop reason, detection and confirmation status. Summarize lower and
upper interval endpoints separately for observed detections, labeling them
conditional on detection with their count. Never present a successes-only
median as the time needed by all attempts, impute the cap for misses, or drop
failed attempts from the planned detection denominator. Do not pool the 1,000
planned stress executions per cell into 1,000 independent samples, fit a
survival curve or apply a binomial confidence interval to these repeats.

Publish no inferential interval, p-value or significance label. If B2 detects
as reliably or sooner in these observations, say so; any remaining advantage
is the observed saved schedule, stable replay and retained evidence. If T
fails to confirm, do not claim reproducibility from its discovery alone. If
overhead is large or an arm cannot run, disclose it. Results apply only to this
host, pinned artifacts, fixtures, cache policy and budgets, not arbitrary
Spring applications, all races, production throughput or developer time saved.

## Evidence, rerun and publication obligations

The implementation must retain a versioned raw **JSON** ledger and original
evidence sufficient to rebuild every table without rerunning workloads. The
following are required fields/content, not existing output or a new product
artifact schema:

| File/content | Required information |
| --- | --- |
| Protocol and registration | Frozen protocol, manifest/order hashes, candidate/E1 registration, baseline/collector/analyzer revisions, command and dependency identities, prerequisite evidence |
| Input inventory | Original config, effective overlays, complete candidate list, schedules and IDs, business/launcher diffs, Oracle SQL, schema/seed, all hashes |
| Environment | Requested and observed host/resources/runtime, image digest, SQL version/isolation, cache restore/priming receipts and UTC dates |
| Slot ledger | Campaign, workload, variant, cache, block, arm/subarm, attempt=1, planned/actual order, status/reasons, budget, spawn/exit/cleanup, counts, evidence references |
| Execution ledger | Index, phase, reset/seed evidence, readiness, worker completion, Oracle evaluation/rows, event-clock identity, timings, first-detection evidence, process/session cleanup |
| Commands/logs | Exact argv arrays, cwd, allowlisted environment, tool versions, stdout/stderr, warm-up/preparation/collection/analysis start/end and exit |
| Product evidence | Unmodified retained run directories, schedule, report, observations, fingerprints and timings; explicit absence when an execution failed before publication |
| Analysis | Frozen source/locks, exact replayable analysis command, all derived rows and denominators, deviations and SHA-256 inventory of retained bytes |

Record aggregate CPU seconds and memory observations for the whole invocation
scope where available, plus container creations and retained evidence/cache
sizes. Record the counter scope and, for sampled memory, a fixed 100 ms cadence
and missed-peak limitation. These are descriptive disclosures, not an extra
efficiency endpoint; unavailable counters are null with a reason, never zero.
Restoration, builds, priming and upload are outside invocation cost but their
commands, durations and storage costs remain in the campaign ledger.

Check child exit, connection retirement and fixture removal before reuse or
restore. A clock/deadline is observation or cancellation, never coordination.
Use synthetic data only; omit/redact credentials, DSNs and wire secrets before
retention, record redaction, and hash the sanitized bytes. Lost semantic
evidence makes an observation incomplete; a hash alone cannot recover it.

The follow-up driver must provide one documented experiment command to execute
all supported registered arms and another to regenerate tables solely from the
retained bundle. Freeze their actual argv and prerequisites in the campaign;
this protocol intentionally supplies no command for a runner that does not yet
exist. Its optional CI job runs only when explicitly requested and is not a
required ruleset/gate check. New fixed markers and their `grep -F` checks ship
with that implementation, preserving all existing acceptance checks.

After collection and independent audit, publish one result page with the raw
JSON, source and durable evidence archive before CI artifacts expire. Include
the actual reproduction commands, all planned denominators, missingness,
failures, deviations and claim limits. Add one corresponding README comparison
table following the [README maintenance contract](../../docs/maintainers/readme-maintenance.md).
Those are follow-up deliverables; this protocol changes no measured result,
README figure, engine contract or E1/E2 protocol file.
