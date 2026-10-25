# External JVM startup optimization

- Status: **A3 candidate implemented; A4 review and E1 confirmation pending**.
- Date: 2026-10-19.
- Issue: [#172](https://github.com/weavegate/weavegate/issues/172).
- Evidence: [local tuning campaign](../../experiments/gate-performance/local-pilot/README.md), merged in [PR #182](https://github.com/weavegate/weavegate/pull/182).
- Inspected implementation: `b916f8273a569bc061c2fad5a664e114eb854391`.

## Recommendation

Prioritize reducing **process start to ready while retaining a fresh JVM for
every schedule execution**. Begin with the existing self-contained JAR and
SDK startup path, preserving the application's effective context and all
readiness checks. This targets the largest observed stage in every workload
while preserving the isolation boundary selected by
[ADR 0010](../adr/0010-external-sut-protocol.md).

The A3 implementation follows this direction with a fixed HotSpot compilation
option, `-XX:TieredStopAtLevel=1`, and the explicit `jvm_tuning: none` escape
path. The [local implementation evidence](../../experiments/gate-performance/startup/README.md)
separates Spring initialization, registration and database probing before
comparing complete gates with an unmodified application JAR. The launch
contract and throughput tradeoff are recorded in
[ADR 0016](../adr/0016-external-cli-composition.md).

This is an implementation candidate, not a frozen E1 treatment or an audited
performance claim. The original A2 proposal and its comparison of alternatives
below remain the design rationale. An extracted layout, startup cache,
parallel execution or JVM reuse still requires its own contract proposal.
The local campaigns do not complete E1's registered pilot or establish that
this direction will meet E1's target.

## Evidence and its limits

The checksummed second campaign contains 32 completed B/B+ pairs, eight per
workload, and 64 completed gate invocations. B is the published `v0.2.0` CLI;
B+ adds timing instrumentation. Within each workload both arms used the same
application JAR and released SDK. The retained deterministic artifacts matched
in all pairs, with expected outcomes and execution counts and no new container
or snapshot JVM left after the recorded cleanup observation. The first
campaign remains separate: two preflight failures and no completed gates.

The following table is derived from the
[reduction](../../experiments/gate-performance/local-pilot/results/local-r2.json).
Durations are medians over eight B+ whole gates. Each wall fraction is computed
per gate before taking its median; these medians are not additive. The
[complete tables](../../experiments/gate-performance/local-pilot/results/local-r2.md)
retain IQR, sample standard deviation, denominators and other stages.

| Workload | B+ gate seconds | Start-to-ready seconds / wall % | Block-recognition seconds / wall % |
| --- | ---: | ---: | ---: |
| Fixed replay, 20 executions | 110.10 | 64.69 / 58.75% | 21.32 / 19.37% |
| Fixed exploration, 18 executions | 90.54 | 51.90 / 57.54% | 19.12 / 21.11% |
| Vulnerable replay, 20 executions | 115.28 | 88.45 / 77.02% | 2.91 / 2.47% |
| Vulnerable exploration, 21 executions | 116.87 | 91.62 / 77.53% | 3.05 / 2.61% |

`process_start_to_ready` measures the Go adapter's launch-through-ready
boundary, including protocol and supervision work. In the
[Java startup path](../../sdk/java/src/main/java/io/github/weavegate/sdk/Peer.java),
readiness follows Spring initialization, registration validation and a database
probe. The span is not a measurement of Spring alone. Similarly,
`block_recognition` measures initial arrival classification, including timeout
inference, not database lock duration.

Thus the evidence supports the broad startup-cost premise, but does not prove
that Spring initialization dominates it or quantify the causal effect of
`FOR UPDATE`. Workloads ran in successive groups on one shared WSL host on
one UTC date. Their differences cannot isolate business-code effects.

The primary fixed-replay B+ gate has sample SD 7.25 s; its start-to-ready stage
has sample SD 6.59 s. Its paired geometric B+/B ratio is 0.9648. That ratio
combines instrumentation, build differences, cache effects and host variation;
it is neither an optimization result nor pure instrumentation overhead. All
observations remain included. Internal repeats and same-host gates are not
independent allocation/day samples.

Missing evidence includes JVM-internal timing, aggregate CPU/memory/disk
counters, the registered resource cap and cache restoration, independent
allocations across two dates, and complete repeated acceptance manifests.
The bundle retains executable/JAR identities and build inputs, but not those
binary bytes. Artifact equality and the local cleanup observations do not
replace E1's complete quality gate. No power, confidence-interval, resource
efficiency or general Spring-service claim follows from these data.

## Compare the four directions

| Direction | Evidence in its favor | Cost or unresolved boundary | Proposed disposition |
| --- | --- | --- | --- |
| Reduce startup inside each fresh JVM | Start-to-ready is the largest measured stage in all four workloads. | Internal attribution and achievable savings are unknown. Preserve registration, transaction proxies, database probe and equivalent application initialization. | First implementation direction, with a measured prototype before candidate freeze. |
| Improve block recognition | Initial arrival classification is substantial in fixed replay and exploration. | It is small in vulnerable workloads. Earlier classification needs truthful control evidence and must preserve normalized artifacts; shortening `arrive_timeout_ms` is ineligible. | Reconsider after startup tuning, with an explicit control/evidence design. |
| Run independent executions concurrently | Repeated startup and waits could overlap. | The pilot has no resource headroom measurements. Each execution needs isolated database state, adapter/runtime and cleanup; ordered exploration and first-violation accounting must remain identical. | Defer until equivalent work and resource-bounded isolation are demonstrated. |
| Reuse a JVM between executions | Could avoid repeated initialization. | The pilot measures no reuse treatment. Pools, static state, threads and session identities would cross today's reset boundary. | Last resort; a new ADR and isolation evidence must precede implementation/candidate freeze. |

For scale only, use the illustrative model `gate reduction = stage fraction
× fractional reduction of that stage`, holding every other cost fixed. With
the fixed-replay median fractions, a hypothetical 15% gate reduction requires
about **25.5%** less start-to-ready time, or **77.5%** less block-recognition
time if that were the only change. Even eliminating the entire measured
block-recognition category would represent only about 2.5–2.6% in the
vulnerable workloads under this model. This is a sensitivity calculation on
B+ observations, not a forecast for W versus B or a substitute for E1's paired
analysis. Stage work may move elsewhere instead of disappearing.

Other fixed-replay median wall fractions are provision 8.66%, adapter stop
6.52%, teardown 1.81%, reset 1.07%, and JAR snapshot 0.45%. They remain possible
secondary targets; none alone offers the model's 15% reduction even if removed
entirely. Required readiness, reset, stop/reap and quarantine checks cannot be
removed to obtain that saving.

## What the preferred direction permits

The first tuning candidate should reduce redundant work in the measured
bootstrap path while retaining one fresh JVM, the same application behavior
and the current self-contained JAR contract. Add volatile local timing around
the Java startup components before choosing the specific patch. Attribute
deferred work to the whole gate: moving initialization from `ready` to the
first command is not itself a gate-time improvement. Preserve invalid
registration rejection before readiness and the database probe's lease return.

The SDK already starts a non-web context, disables application migrations,
and sets minimum idle pool connections to zero in
[`SpringHost`](../../sdk/java/src/main/java/io/github/weavegate/sdk/SpringHost.java).
Those are existing behavior, not new optimization opportunities. Narrowing
the component scan, changing beans or enabling lazy initialization cannot be
assumed equivalent merely because this small fixture retains its verdict.
Any such proposal must establish the application's initialization and
transaction semantics, including failures.

CDS and Spring AOT remain possibilities to investigate, not implemented features.
For the pinned Java 21 / Boot 4.0.8 baseline, Spring's documented
[CDS procedure](https://docs.spring.io/spring-boot/4.0/reference/packaging/aot-cache.html)
requires a training run, an extracted application and an archive parameter.
That page documents Boot's JVM AOT-cache route for Java 25 and above; it is not
a Java 21 drop-in. Spring's
[extracted layout](https://docs.spring.io/spring-boot/4.0/reference/packaging/efficient.html)
references external libraries through the JAR manifest.

Today's [preflight and snapshot code](../../cmd/weavegate/external.go)
rejects a manifest `Class-Path` and snapshots a single self-contained JAR for
each execution. The [launcher](../../internal/sut/external/process.go) passes
`java -jar <snapshot>` with the selected compiler policy. Consequently the documented extracted/CDS path
does not fit the existing contract. Do not bypass validation with an undocumented
wrapper or environment flag. Propose the bundle identity, immutable snapshot,
launch configuration and adoption contract first if that path is needed.
Retaining a fresh JVM avoids the JVM-reuse change to ADR 0010; it does not
exempt a new packaging mode from contract review.

A cache proposal must identify its compatibility key, creation and invalidation
commands, storage and rebuild cost, actual cache use, and behavior after a JAR,
SDK or JDK change. Both warm and empty-cache adoption costs must be recorded.
Application source, hooks, SQL, effective configuration and the pinned runtime
remain matched except for the declared CLI/SDK treatment and necessary
packaging. Any new adoption steps need the guide and external example updated
in the same release. A Java-version upgrade would need a separate experimental
comparison rather than being hidden inside the candidate treatment.

## Implementation handoff and decision boundary

The implementation and its follow-up review/confirmation must:

1. Retain the additional startup attribution and prototype results as separate
   tuning evidence, including unsuccessful candidates. Do not count them as
   E1 pilot or confirmation observations or alter the registered sample sizes.
2. Demonstrate improvement in whole-gate time with the same work and inputs;
   preserve verdicts, schedule IDs, flaky detection, deterministic artifacts,
   repeat/pass counts, timeouts, isolation and cleanup. Exercise cancellation,
   startup failure and uncertain shutdown as well as the successful path.
3. Complete repeated Go/Java and paired Spring acceptance with real manifests
   and every `--require-complete` check before freezing W. New timing remains
   outside normalized evidence and follows the artifact-version contract.
4. Resolve the registered pilot's outstanding obligations separately, then
   freeze the reviewed candidate and complete
   [E1 revision 1](../../experiments/gate-performance/e1-gate.md): published B
   versus W, fixed workloads and counts, identical aggregate resource cap,
   cache restoration, resource counters and empty-cache adoption accounting.

The existing E1 decision rule remains `R <= 0.85` with the 95% interval's upper
endpoint below 1 and its complete quality/resource obligations. Local tuning
neither changes that rule nor guarantees a pass. A small, inconclusive or
negative result is reported under the same protocol.

If startup work cannot produce a useful whole-gate reduction without changing
application semantics or imposing disproportionate adoption costs, record that
result and return to the maintainer with the measured alternatives. Do not
silently add parallel execution, earlier block inference or JVM reuse. A
parallel design must preserve the existing
[serialized fixture/reset boundary](../../internal/orchestrator/run.go) through
isolated execution resources and retain the
[ordered exploration contract](../../internal/orchestrator/explore.go), including
discovery/confirmation counts and cleanup of any speculative work. A reuse
design needs a new ADR and proof of state isolation before it can be selected.

Merging the implementation does not complete #172 or authorize an E1
performance claim. A4 review, candidate freeze, E1 confirmation and audit
remain separate work; public headline measurements stay at their current
evidence-backed values until that work is complete.

## Reproduce the evidence and sensitivity calculation

Run from the repository root with Python 3.9+. The first command verifies the
original bundles and reproduces both reductions and the complete result tables;
the second prints the inputs used above and the explicitly hypothetical model.
No new gate invocations or downloads are required.

```bash
python3 -B experiments/gate-performance/local-pilot/verify_results.py
python3 -B - <<'PY'
import json
from pathlib import Path

path = Path("experiments/gate-performance/local-pilot/results/local-r2.json")
estimates = json.loads(path.read_text())["estimates"]
for workload, estimate in estimates.items():
    seconds = estimate["exclusive_stage_seconds"]
    fractions = estimate["exclusive_stage_wall_fraction"]
    print(workload, "pairs", estimate["artifact_matched_pairs"],
          "/", estimate["planned_pairs"])
    print("  B+ gate median/SD", estimate["Bplus_seconds"]["median"],
          estimate["Bplus_seconds"]["sample_sd"])
    print("  B+/B geometric ratio", estimate["Bplus_over_B_geometric_mean"])
    for stage in ("process_start_to_ready", "block_recognition",
                  "fixture_provision", "adapter_stop", "fixture_teardown",
                  "fixture_reset", "jar_snapshot"):
        print(" ", stage, "median seconds", seconds[stage]["median"],
              "SD", seconds[stage]["sample_sd"],
              "median wall %", 100 * fractions[stage]["median"])
primary = estimates["example-fixed-replay"]["exclusive_stage_wall_fraction"]
for stage in ("process_start_to_ready", "block_recognition"):
    print("Hypothetical stage reduction % for 15% gate reduction:", stage,
          100 * 0.15 / primary[stage]["median"])
PY
```
