# Local E1 preparation measurements

This directory records a **descriptive local tuning campaign** for
[#172](https://github.com/weavegate/weavegate/issues/172). It supplies stage
observations for choosing a later optimization. It does not complete the
[registered E1 pilot](../e1-gate.md), change protocol revision 1, or establish
an improvement over `v0.2.0`. No optimized candidate is measured.

The shared harness enforces the registered E1 phase counts and date slots.
This narrow runner keeps the authorized same-day local tuning observations in
a separate ledger, preserving that harness and the registered E1 protocol.

## Results and evidence

The [generated tables](results/local-r2.md) and [full reduction](results/local-r2.json)
come from [the original run bundle](results/local-r2.tar). Each workload
retains its own denominator and dispersion; the whole-gate ratio is descriptive.
The [first stopped bundle](results/stopped-r1.tar.gz) and
[its accounting](results/stopped-r1.json) remain separate. The
[correction note](AMENDMENT.md) identifies the preflight error and the second
registration. [FINDINGS.md](FINDINGS.md) states the measured scope and limits.

The first registration was public at commit
`530233d9bba03e7f0d7c42199a61e00b8861c08c`, before its two failed attempts.
The second was public at `8c939a224852aa6f8e85324a7d17ab8819eb0649`, before
any of its measurements. `registration/` and `registration-r2/` preserve both.
The original collector/analyzer source is unchanged from those commits.
The later renderer only formats the already-defined descriptive estimates.

Recompute both reductions and the result tables, with no Docker or downloads:

```bash
python3 -B experiments/gate-performance/local-pilot/verify_results.py
```

This verifies every retained file against the bundle's checksum inventory,
derives results from original command receipts and artifacts, and compares
the regenerated JSON and Markdown byte for byte with the committed outputs.
CI checks its fixed `LOCAL_PILOT_EVIDENCE_RESULT` marker.

The completed campaign uses an uncompressed tar: the repository's diagnostic
namespace scan found a coincidental legacy token in gzip bytes. Keeping the
original text uncompressed permits that scan without changing its rules or any
evidence file or checksum. The smaller stopped campaign retains its gzip bundle.

## Registered scope

Before the first measurement, publish this document, the scripts,
`instrumentation.patch`, and `registration/` in a Git commit. There are **8
pairs for each of the four example workloads**, 32 pairs / 64 CLI invocations
in total. The seed and SHA-256 assignment algorithm are those in the shared
protocol, with phase `tuning` and one date slot of eight blocks. Each workload
has four B-first and four B+-first pairs. The frozen order is executed once,
without warm-up gates, replacement attempts, outlier trimming, or optional
stopping based on timing. Unexpected artifacts or uncertain cleanup stop the
campaign; unstarted slots remain in the denominator.

B is the checksum-verified published Linux amd64 `v0.2.0` binary. B+ is source
`ee10c7de2114086f14258037703dbd4b104e5908` plus only the `cmd/` and `internal/`
changes reviewed in [PR #178](https://github.com/weavegate/weavegate/pull/178).
The full applied patch is retained. B+ uses the release's Go 1.25.0, disabled
CGO, and `-s -w` linker settings. Its version string identifies instrumentation.
Both arms use **the same application JAR and Maven Central SDK 0.2.0** for a
given workload. The two business-source revisions are exactly those in E1.
The only application configuration change pins `mysql:8.4` to its digest.
The registration includes copied synthetic sources, all six candidate IDs in
order, resolved JAR dependency hashes, build logs, executable hashes, and exact
argv/environment. Repeat 20, three exploration passes, workers, assertions,
and all product deadlines remain unchanged.

The authorized local environment differs from E1: one shared WSL host and one
UTC date, existing background containers, host CPU/memory limits, and prepared
dependencies without VM/page-cache restoration. Both arms hash their inputs
before invocation, which can warm file pages. No aggregate CPU, memory, or
disk-write counters are available. These deviations prevent an E1 quality
pass, independent-sample inference, and any resource-efficiency claim. The
registered independent-allocation confirmation and its counts are unchanged.

## Observation and interpretation

The collector records monotonic spawn-through-wait duration, exit code,
stdout/stderr, and the original product run directory for every invocation.
The outer timeout is 600 seconds, with SIGINT and a bounded cleanup grace on
expiry. Docker container IDs and snapshot JVM PID/start-time inventories are
retained before/after; `docker wait` observes a remaining reaper's exit. No
unrelated container is removed. These checks and `cleanup_failed=false` are
limited cleanup observations, not a replacement for the full repeated Go/Java
acceptance manifests required by E1 confirmation.

Pair checks compare the five deterministic artifacts byte for byte (including
absence of `schedule.json` on exhausted exploration), plus schema/seed,
isolation, engine, adapter, variant, image, and JAR identity. Expected mode,
repeat, exploration counts, violation counts, flaky flag, schedule ID and WG001
assertion are checked against the product's Oracle evidence. B+ span counts
also check 20 replay executions, 18 fixed exploration executions, or 21
vulnerable discovery/confirmation executions. This tool does not evaluate SQL
or introduce verdict logic.

Analysis partitions intervals on the Go recorder's single clock. Nested
intervals are assigned to the most specific named stage; simultaneous intervals
at the same depth count once (different categories receive an explicit overlap
category). Database migration/seed children remain within provision/reset.
The command remainder is bookkeeping, and whole-process time outside the
command span remains `outside_timing`. All exclusive categories sum exactly
to whole-process time. Cumulative inclusive span work is retained separately.
`block_recognition` is arrival-wait classification, not measured lock duration;
`process_start_to_ready` includes JVM/application startup and protocol work,
not Spring-internal startup alone. The latter is unavailable.

Report every workload separately: observed/planned pairs, medians, IQR,
sample standard deviation, paired geometric B+/B ratio and log-ratio sample
standard deviation, plus per-gate stage seconds and wall fractions. All are
descriptive; no confidence interval, significance claim, or optimization
decision is made. B+/B combines instrumentation, local build differences,
cache effects and shared-host variation. There is no claim it isolates pure
instrumentation overhead. Whole-gate pairs, not internal schedule executions,
are the units used for dispersion. Original evidence is sealed with SHA-256;
the analyzer verifies it and derives results again from the original files.

## Reproduction

Python 3.9+, Go 1.25.0, JDK 21, Docker, Git and `gh` are required for collection.
Use a new workspace and evidence destination. The commands used to prepare
the current inputs are in [PREPARATION.md](PREPARATION.md). After building:

```bash
python3 -B experiments/gate-performance/local-pilot/test_pilot.py
python3 -B experiments/gate-performance/local-pilot/freeze.py \
  /tmp/weavegate-a1 experiments/gate-performance/local-pilot/registration
```

Publish the registration commit before collecting. The following is a command
template; substitute its public commit SHA. The collector refuses an existing
destination or changed pinned input. A later independent repetition needs a
new registration and destination and is not pooled with this campaign.

```bash
python3 -B experiments/gate-performance/local-pilot/pilot.py collect \
  experiments/gate-performance/local-pilot/registration/plan.json \
  /tmp/weavegate-a1/evidence --registration PUBLIC_COMMIT_SHA
python3 -B experiments/gate-performance/local-pilot/pilot.py analyze \
  /tmp/weavegate-a1/evidence /tmp/weavegate-a1/results.json
```

The actual second collection command was:

```bash
python3 -B experiments/gate-performance/local-pilot/pilot.py collect \
  experiments/gate-performance/local-pilot/registration-r2/plan.json \
  /tmp/weavegate-a1/evidence-r2 --registration 8c939a2
```

The published bundle retains original run artifacts and command/cleanup
receipts, not executable binaries or generated application JARs. Their hashes,
resolved dependency hashes, sources and build commands are retained; the
published B binary remains available from the `v0.2.0` release. Rebuilding
inputs for a new measurement requires a new freeze, not pretending the rebuilt
bytes are the old artifacts. The two Go build identities are retained beside
the results as `baseline-build-identity.txt` and `bplus-build-identity.txt`.
