# Gate performance experiment protocols

Protocol revision: **1**. These are prospective measurement contracts for
[`#172`](https://github.com/weavegate/weavegate/issues/172) and
[`#175`](https://github.com/weavegate/weavegate/issues/175), published through
[`#176`](https://github.com/weavegate/weavegate/issues/176). They contain no new
observations. The standalone [collector and analyzer](harness/README.md) now
implement frozen campaign collection and E1/E2 analysis. Measurement campaigns
remain follow-up work; the runnable demo contains constructed evidence only.

- [E1: product gate time](e1-gate.md) fixes the released baseline, workloads,
  sampling plan and product performance claim.
- [E2: repository CI feedback](e2-ci.md) fixes the workflow comparison,
  acceptance bundle, sampling plan and feedback/cost claim.

This page is the shared contract. Normative numbers below are design choices,
not measured improvements or release acceptance requirements. Unfavorable,
inconclusive and stopped campaigns must be published too. Existing
[gate run-time observations](../../docs/experiments/gate-run-time.md) remain
historical context, not observations in either new sample.

## Freeze and amendment rules

There are two freezes. This protocol freezes the questions, sample counts,
thresholds, exclusions and analysis before pilot results. A later public
campaign manifest supplies concrete artifacts and infrastructure before the
first measured attempt in that phase. Future candidate hashes cannot be known
now; the manifest may fill identities, dates and execution receipts, but cannot
change the statistical rules. Record the Git commit and SHA-256 of all three
protocol files. The campaign manifest and order file must have a public commit
or immutable archive timestamp predating its first measurement.

Pilot and tuning are separate phases with separate campaign IDs. Use them to
check collection, decompose cost and select an implementation. Freeze the
candidate and analyzer before confirmation. Pilot/tuning observations never
enter confirmation estimates. Do not inspect accumulating confirmation effects
to choose when to stop, extend sampling, change workload, select a candidate or
switch a primary comparison. Operational status may be inspected for cleanup
and evidence failures.

An amendment records its reason, prior protocol/campaign hashes, observations
already seen, and new revision before new collection. Preserve the old campaign
and report it separately, including stopped campaigns. Changing code, a sample
count, an endpoint or an exclusion after seeing confirmation outcomes requires
a new confirmation campaign; do not pool the old data into it. Correcting an
analysis bug retains the original raw data and both analyzer versions and
outputs, with the correction disclosed. It cannot redefine the estimand.

## Required campaign manifest

Before collection, publish a machine-readable manifest and the files whose
hashes it names. Resolve all artifact identities, requested settings and
assignment rules in this inventory before the first attempt. Actual host IDs,
runtime version receipts, cache hits and timestamps are appended as execution
evidence when assigned or observed; they cannot retroactively change the
registered settings. A missing prerequisite stops collection.

| Inventory | Required values |
| --- | --- |
| Identity | Experiment, protocol revision and hashes, phase, campaign ID, registration commit/time, collector and analyzer source commits and dependency locks |
| Arms | Full CLI/engine, SDK, SUT and harness commits; release checksums; executable/JAR hashes; exact build commands and dependency trees; reviewed treatment diff |
| Inputs | Scenario, ordered schedule files and IDs, worker arguments/count, sync-points, migration, seed, oracle SQL and expected rows, effective configuration and all timeout/budget values, each with hashes |
| Runtime | MySQL image digest and observed `VERSION()`, JDK/Boot/Framework/JDBC/pool versions, transaction isolation and pool limits, Go/build tool versions |
| Environment | OS/kernel/architecture, CPU model/count, effective CPU and memory caps, Docker version, runner image/version and pool, cache snapshot/key/hit policy, background load policy and concurrency |
| Assignment | Planned block IDs, phase/workload/stratum, UTC date slots, allocation/host IDs when assigned, arm order, order seed and file hash, attempt limits |
| Observation | Exact argv arrays, working directories, environment allowlist, timer and resource-counter boundaries, evidence paths and redaction rules, acceptance inventory and checker hashes |
| CI additions | Both workflow files and hashes, common execution revision, logical task-to-job/step mapping, action versions, ruleset snapshot, trigger, runner labels and per-job timeout/resource limits |

Pin movable references (including `mysql:8.4`, action tags and runner labels)
to their resolved identities in the evidence. Record observed infrastructure
drift, not just the requested label. E1 arm inputs differ only by the declared
CLI/SDK treatment and necessary packaging; E2 differences follow its narrower
[treatment boundary](e2-ci.md#arms-and-treatment-boundary).

## Assignment and independence

The unit is a pair of whole gate or workflow invocations in a preassigned
block, not a schedule run, JVM, test case, job or internal repeat. Run both
arms even if the first fails, unless isolation/cleanup is unproven; in that case
record the second as not started. Never reuse a quarantined fixture. The second
arm starts after observed teardown and restoration, not after a sleep.

The fixed seed string is `weavegate-e1-e2-v1-20261013`. Generate the entire
order file before collection with this language-independent procedure:

1. Give blocks IDs `001`, `002`, etc. within each
   experiment/phase/workload/date stratum. Use experiment `E1` or `E2`, phase
   `pilot`, `confirmation` or `adoption-cold`, and the E1 workload table's IDs
   or E2 workload `external-acceptance`. Distribute counts over dates as
   specified by E1/E2 before assigning arms.
2. For each ID, compute SHA-256 of the UTF-8 string
   `seed|experiment|phase|workload|date-slot|block-id|order`, with literal pipes,
   the seed above and no final newline. Date slots are `01`, `02`, etc.
3. Sort by the full digest interpreted as an unsigned big-endian integer,
   breaking a tie by block ID. Assign the first half baseline-first and the
   remainder candidate-first. For an odd count, the baseline-first count is
   the ceiling on odd date slots and the floor on even slots.
4. Order blocks within a date by the same hash construction with suffix
   `sequence` instead of `order`. Archive the expanded order file; hash
   assignment is independent of runtime outcomes.

For the E1 pilot, candidate means instrumentation-only B+. For E2, baseline
and candidate mean old and new workflow. Do not shuffle again to find a more
convenient order. No arm runs concurrently with its mate. Independent E1
blocks use fresh dedicated allocations; E2 uses temporal pairs on hosted
runners and explicitly has weaker control over hardware.

If E1 allocations are reused on a self-hosted machine, record that deviation
and cluster by host and UTC day. Do not call repeated processes on that host
independent samples. Such a campaign is descriptive under this revision;
it cannot substitute for the registered independent-allocation confirmation.

## Attempt accounting and evidence validity

All planned slots and actual invocations appear in the ledger. The primary
sample uses **attempt 1 only**, with no replacement pairs. A provider outage,
operator error or harness failure is retained with its cause. Any diagnostic
rerun is a separately identified tuning attempt, never a replacement or an
extra independent sample. Do not delete slow observations or discard the
first gate as warm-up. A campaign may stop for unproven cleanup, lost evidence
or unavailable infrastructure; publish its incomplete denominator and reason.

Use the product's existing Oracle and
[exit contract](../../docs/reference/exit-codes.md), not new verdict logic:

| Observation | Experiment accounting |
| --- | --- |
| Exit 0, complete evaluation and evidence | Expected PASS only for a declared passing workload |
| Exit 2, expected assertion's SQL violation and complete evidence | Expected domain violation only for a declared vulnerable workload |
| Exit 3 / WG090 | Flaky; neither stable detection nor a passing observation |
| Exit 4, 5, unexpected 130, budget expiration | Operational/incomplete observation, never a fast success |
| Intended death/cancel probe | Valid only under E2's frozen lifecycle assertions and cleanup evidence |
| Missing arm, artifact, phase, required marker or resource observation | Explicitly missing; never zero duration or inferred success |

An unexpected PASS, assertion, schedule, repetition count or normalized
evidence mismatch is a quality failure. Exit status alone, a successful
`report` command, or one final lifecycle marker cannot establish validity.
Compare domain evidence, schedule IDs, flakiness and cleanup using existing
contracts. Exclude only documented volatile fields (run IDs, timestamps,
temporary paths and timing metadata permitted by the artifact contract).
Do not delete lifecycle events or normalize away changed outcomes. Freeze
the comparison projection before confirmation and retain the original files.

Time ratios use complete, valid pairs only. A resource-specific estimate
requires both resource observations too, with its own denominator. Show per
arm counts of planned, started, complete, quality-failed, operationally failed
and missing attempts, with reason codes and paired missingness. Also show
elapsed time and consumed resources for failed attempts; a censored duration
is a lower bound, not a completed gate time. No imputation or time outlier
trimming is allowed. Missing required evidence prevents a positive claim even
when complete-pair times look favorable.

## Analysis fixed for both experiments

For valid first-attempt pairs `i = 1..n`, let `B_i` and `W_i` be positive
durations of baseline/old and candidate/new. Compute:

```text
L_i = ln(W_i / B_i)
R = exp(sum(L_i) / n)
improvement = 1 - R
D_i = W_i - B_i
D = sum(D_i) / n
```

Negative `D` means seconds saved. Report `R`, `1-R`, `D`, both arm medians and
IQRs, each date's estimates, and every denominator. Resource and runner-minute
ratios use the same paired geometric-mean formula on their complete pairs;
retain zero counter readings as data and mark an undefined ratio N/A.
Do not substitute a ratio of arithmetic means. Never pool workloads, cache
strata or phases into one headline effect.

Use 10,000 paired bootstrap replicates. Resample whole blocks with replacement,
separately within each planned date slot, keeping that date's observed valid
pair count. Recompute `R` and `D` for each replicate, then take the 2.5th and
97.5th percentiles. The improvement interval is `[1-R_upper, 1-R_lower]`.
Quantiles use linear interpolation at zero-based index `(m-1)*p` in the sorted
`m` values; IQR uses the same definition at `p=.25` and `.75`.

For deterministic resampling, sort eligible block IDs within each stratum.
For each replicate `r=00000..09999` and draw `j=00000..n_stratum-1`, hash
`seed|experiment|phase|workload|metric|date-slot|r|j|bootstrap` as above.
Map its unsigned integer `h` to index `floor(h*n_stratum/2^256)`. Use metric
name `gate_seconds` for E1 and `validation_seconds` for E2; use the ledger's
metric name for other estimates. A replicate uses the same sampled pair list
for its ratio and absolute difference. Archive analyzer inputs and outputs so
another implementation can reproduce them to floating-point rounding.

With fewer than two valid blocks in any planned date slot, do not compute a
confirmatory interval; report descriptive values and insufficient data. With
zero valid pairs, all paired estimates are N/A. Small-sample bootstrap coverage
is approximate; neither the chosen sample counts nor these intervals guarantee
power, non-inferiority or a tail-latency bound. No p99 claim is permitted.
E1 and E2 are separate questions, each with one primary endpoint. Secondary
workloads/metrics are descriptive, without significance labels or selecting
the best one to rescue a failed primary endpoint. A joint or additional
confirmatory claim requires a new protocol with multiplicity control.

The E1/E2 decision rules require all planned confirmation pairs to be valid,
the full quality inventory to pass, and required resource measurements to be
present. This is an observed evidence requirement, not proof that the failure
rate or quality is equivalent in an unobserved population. If a requirement
fails, publish complete-pair estimates with the failure/missingness table but
make no positive confirmatory improvement claim.

## Evidence bundle and publication

The following is the protocol's **planned bundle layout**, not product CLI
output. The [collector reference](harness/README.md#sanitization-and-bundle-layout)
documents its concrete schema, including per-block directories. It must live
outside product artifacts; adding an experiment field does not change the [report schema](../../docs/reference/report-schema.md).

```text
campaign/
  protocol/                 # exact copies of the three frozen Markdown files
  campaign.json             # registration and artifact pins
  order.csv                 # every planned slot and arm order
  environments.jsonl        # requested and observed environment per allocation
  trials.csv                # every slot/attempt, including not-started arms
  commands.jsonl            # actual argv, cwd, allowlisted env, start/end, exit
  events.jsonl              # local monotonic durations and event relationships
  artifacts/<trial-id>/     # original run directories, logs, counters, CI JSON/XML
  analysis/                 # source/lock, commands, derived tables, decisions
  SHA256SUMS                # all other bundle files, sorted by relative path
```

The trial ledger includes experiment, phase, campaign/block/trial/workload IDs,
arm, date/allocation/host, attempt, planned/actual order, cache state, duration,
timer source, completion classification, exit, assertion/schedule IDs,
actual schedule-run/repeat counts, quality check results, resource values and
units, missing/censoring reason, and evidence paths/hashes. CI rows also link
run ID, attempt, head/execution SHA, workflow hash and logical task identities.

Record durations with a monotonic clock on the process that measures an
interval. UTC timestamps identify attempts and CI service boundaries, not
local duration arithmetic. Never subtract Go and JVM monotonic timestamps.
Resource methods must cover children and database processes, specify units and
sampling/missed-peak limitations, and be identical across arms. Missing
telemetry is unknown, not zero.

Only synthetic fixture data is public. Allowlist environment fields; do not
capture credential-bearing wire payloads or database connection strings.
Redact secrets before retention and record what was removed; never publish
raw credentials to preserve a hash. Hash the retained sanitized bytes and
keep the semantic evidence needed for audit. An unresolvable evidence loss
makes that observation incomplete.

Publish raw ledgers, commands, logs, original artifact contents, CI jobs/steps
JSON and test XML, environment identities, hashes and executable analysis in a
durable repository or release archive before CI artifacts expire. A link to
an expired workflow artifact is insufficient. Include the exact collection
and analysis commands from `commands.jsonl`, prerequisites and exit handling;
the [collector reference](harness/README.md) supplies runnable tool commands and
the hook/evidence contract. Each campaign must freeze its actual infrastructure
commands and prerequisites before measurement.

Every result page must carry the registered comparison, observed and planned
counts, all failures/exclusions/deviations, effect and uncertainty, resources,
quality inventory, decision against the fixed thresholds and limitations.
Do not turn feedback time into developer labor savings or extrapolate two
small applications to all Spring services.
