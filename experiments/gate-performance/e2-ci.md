# E2: repository CI feedback

Protocol revision: **1**, governed by the [shared contract](README.md).
This planned experiment measures the feedback effect of workflow changes in
[#175](https://github.com/weavegate/weavegate/issues/175). Its primary endpoint
is elapsed time from the first job start to completion of the frozen external
acceptance bundle, on the same engine and verification workload.

## Arms and treatment boundary

Old is the smoke workflow topology at weavegate commit
`36f3f4ff9b82e19d1bea5e286d1ff2297bc53f82` (record its workflow blob hash):
isolated Go and Java jobs precede the paired Spring job, whose probe phases run
serially. New applies the reviewed workflow scheduling/phase partitioning
change. Freeze both workflow files in the campaign archive.

Use **one common execution commit** containing the same engine, SDK, fixture,
test assertions, recorders, vectors and acceptance checkers for both arms.
Where phase selection is needed, both arms use the same phase-capable test
harness: old invokes the complete serial path and new schedules its parts.
Retain the diff from the original test and evidence that the serial path still
checks the same conditions. Changing assertions, timeouts, repeats or fixture
lifecycle is not workflow scheduling. No shared MySQL fixture replaces the
existing per-CLI lifecycle. Do not include a product optimization in only new.

Publish both experimental workflow entry points at that common commit and
dispatch each with the same event/inputs/permissions. Both must check out and
build the common commit, use the same action/tool/dependency identities and
produce matching engine/SDK/application content hashes. Record the workflow
source and checked-out source separately, including `GITHUB_SHA`, head SHA and
manifest revision. Both arms' isolated and paired manifests must refer to their
own workflow run and the common revision; never download the latest successful
manifest from another run to complete a pair.

Record any wrapper changes needed to dispatch old (name, trigger and evidence
capture) and apply equivalent changes to new. Keep all non-treatment jobs,
matrix expansions, cache policies and required-check semantics unchanged.
The confirmatory comparison is workflow structure on this common harness,
not untouched historical PR runs. Full `v0.3.0` versus `v0.2.0` CI runs may be
published separately as total effects, not the isolated effect of #175.

## Frozen acceptance bundle

The logical bundle is independent of how many jobs new creates. Before either
arm runs, archive a task/phase/marker/assertion inventory from the pinned old
workflow, test source and acceptance plan, mapped to jobs and steps in both arms.

| Logical task | Required evidence |
| --- | --- |
| Go isolated | Existing ordinary and race runs at count 20, all fixed-phrase checks, recorder and complete acceptance manifest |
| Java isolated | Existing build and repeated verification at 20, all fixed-phrase checks, recorder and complete acceptance manifest |
| Spring explore | Vulnerable discovery plus 20 confirmation replays, saved schedule and unchanged explore marker |
| Spring replay | Vulnerable × 20 and fixed × 20, unchanged replay evidence/markers |
| Spring rollback | 20 completed rollback probes and their observed lifecycle evidence |
| Spring application death | 20 intentional process-death probes with expected exit/fault, rollback and cleanup evidence |
| Spring cancellation | 20 locking-read cancellation probes with expected exit and cleanup evidence |
| Paired accounting and upload | Observed lifecycle composition, recorder, all same-run/revision isolated links, final `--require-complete`, and successful retained evidence upload |

Fixed-phrase marker text and meaning stay unchanged, including the
`SPRING_EXPLORE_RESULT`, `SPRING_REPLAY_RESULT`, `SPRING_ROLLBACK_RESULT`,
`SPRING_DEATH_RESULT`, `SPRING_CANCEL_RESULT` and `SPRING_LIFECYCLE_RESULT`
families. The launch inventory stores full strings and expected occurrences,
not only these prefixes. Each phase needs its actual logs/assertions, attempt
identity and repetition count. An unconditional final lifecycle line, duplicated
phase log or union of manifests from different runs cannot complete the bundle.
Preserve the
[acceptance accounting contract](../../docs/reference/external-sut-acceptance.md).

## Sampling and cache policy

Run **6 pilot pairs**, then **12 independent confirmation pairs** after workflow,
harness and analyzer freeze. Pilot uses 3 pairs on each of two UTC dates;
confirmation uses 4 pairs on each of three distinct UTC dates. Pilot supports
collection and scheduling debugging only and is never pooled with confirmation.
Twelve pairs are a bounded study of a large feedback change, not a guarantee
of power for a small effect. Do not increase the count after inspecting results.

Each temporal block contains one entire old and one entire new workflow run,
in the shared hash-assigned order. Use a fresh hosted runner for every job.
The two workflow runs execute sequentially and do not contend with one another
or intentionally scheduled repository workloads. Dispatch the second within
five minutes of the first run's observed completion. Record dispatch lag, queue
delay, overlapping external activity and actual runner identities; host load
outside the repository remains uncontrolled. Missed pairing windows remain in
the ledger as invalid pairs, without replacements.

Use the same hosted runner labels/image series, per-job CPU/memory limits,
tool/action versions and runner pool. New may use more simultaneous runners;
that is part of the treatment and must appear as concurrency and runner-minute
cost, not a product throughput gain. Capture the actual CPU model/count,
memory, image version and cache receipts for each job. Within a temporal pair,
an image-version or resource-class mismatch invalidates its controlled
comparison; report the times descriptively and keep the failure denominator.
Equal labels alone do not establish equal hardware. Different physical hosts
remain a limitation even when these observed attributes match.

The confirmation stratum is **prepared build/dependency-cache hit**, on fresh
job disks. Freeze a common cache inventory outside timed workflow execution,
using identical input keys and immutable restored contents in both arms. No
job may restore cached test results or previously completed acceptance evidence;
the tests must execute all repetitions. Preserve the old MySQL pull policy,
record image digest/pull outcomes and apply the same policy in new. The first
arm must not create a cache advantage for the second. A required cache miss
invalidates the pair for the controlled effect; it remains an observed failure
of that stratum, without a replacement. Empty-cache CI is outside this sample.

Freeze existing job and test timeouts. Set a common outer workflow observation
cap of 90 minutes from run creation; exceeding it is censored/incomplete, not a
fast completion. Record cancellation and cleanup outcomes. Do not shorten
death-probe deadlines or remove teardown to fit the observation budget.

## Endpoint boundaries

Fetch and retain every page of jobs/steps JSON **for the explicit run attempt**,
along with workflow metadata, logs, test XML, receipts and all manifests. Primary
data uses the original run's attempt 1 only. Re-running failed jobs is diagnostic
work, not an extra block or a repaired primary observation. Preserve UTC API
timestamp precision; do not invent subsecond accuracy or compare monotonic
clocks across runners.

Define the following using the predeclared logical task mapping:

| Symbol/metric | Definition |
| --- | --- |
| `t_created` | Workflow run creation timestamp |
| `t_first` | Earliest start among all actually started jobs in that workflow attempt |
| `t_bundle` | Latest successful job completion among all jobs needed for the acceptance bundle, including validation, manifest composition and upload |
| Primary `T_validation` | `t_bundle - t_first` |
| Queue-inclusive feedback | `t_bundle - t_created` |
| Initial queue | `t_first - t_created`; later dependency/queue waits remain inside `T_validation` |
| Paired evidence ready | Completion of the final checker and upload steps that establish retrievable complete paired evidence, measured from `t_first` |
| Merge-ready | Last completion among checks actually required by the frozen ruleset, measured from creation; unavailable if this trigger does not execute or satisfy them |
| Runner-minutes | Sum of `(job.completed_at - job.started_at)/60` for every started job in the workflow attempt, not only the acceptance bundle |
| Peak runner concurrency | Maximum overlapping job intervals, with half-open intervals `[start,end)` |

`t_bundle` is unavailable if a required logical task failed, was skipped,
cancelled, lacked evidence, linked the wrong revision or failed upload. Such a
workflow has no completed primary duration even if the API says the workflow
ended; retain elapsed-to-failure/censoring and runner use separately. Internal
intentional death/cancel probes count as successful logical tasks only when
their existing assertions, repetitions and cleanup all pass.

Archive the active ruleset/check names before collection and any drift. Do not
call paired evidence ready "merge-ready" unless the actual required checks
also completed successfully. A workflow-dispatch result is a controlled proxy
for PR feedback, not observed PR queue or branch-protection satisfaction.
Runner-minutes measure occupied runner time, not a bill in currency; include
failed jobs and matrix jobs, exclude jobs that never acquired a runner, and
report missing job-end times as censored usage.

Use provider timestamps for workflow boundaries and step/phase-local durations
only to explain the critical path. Keep each phase's original logs and timing
provenance when composing evidence. Do not sum concurrent phases as wall time
or count their 20 repetitions as 20 independent workflows.

## Analysis and decision

Apply the [shared analysis](README.md#analysis-fixed-for-both-experiments) to
the 12 valid old/new first-attempt confirmation pairs. Report paired geometric
ratios and absolute time differences for `T_validation`, with the date-stratified
10,000-replicate interval, plus per-date values and all planned/observed counts.
Queue-inclusive time, evidence-ready time, merge-ready when available, costs
and phase breakdowns are descriptive secondary endpoints. Retain queue outliers.

The primary rule is **`R <= 0.80` and the 95% interval's upper endpoint `< 1`**,
with all planned confirmation pairs, phase/marker/repetition requirements,
same-run manifests, final completeness checks and uploads complete. Twenty
percent is a chosen practical feedback threshold, not proof the true effect
exceeds 20%. If the rule fails or evidence is incomplete, publish that result;
do not extend the same sample or substitute the fastest subphase.

Report the paired geometric runner-minute ratio `C` and its interval alongside
the primary time ratio. If **`C > 1.10`**, label a successful timing result
"faster feedback with higher runner cost." At or below 1.10, state the observed
ratio without claiming proven cost equivalence. Cost telemetry must be complete
for any favorable confirmatory claim. Show peak concurrency and queue effects
even when `T_validation` improves.

The 12 temporal blocks do not control physical hosted-runner allocation as
strongly as E1, and inference is conditional on these dates, caches, hardware
pool and frozen harness. State that limitation with the result. A shorter CI
critical path does not establish developer hours saved, fewer production bugs,
or a faster engine.
