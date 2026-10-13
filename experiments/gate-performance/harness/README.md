# Experiment collector and analyzer

These standalone Python 3.9+ tools implement collection/accounting shared by
E1–E4 and the published [E1/E2 analysis](../README.md). They use only the Python
standard library; Linux/POSIX process groups and file locks are required for
collection. They add no engine dependency or `weavegate` subcommand.
E3/E4 collection accepts a separately frozen protocol and explicit counts;
there is no E3/E4 confirmatory decision rule in these tools yet.

## Try the tooling

Run from the repository root. This runnable demo generates **constructed**
commands, receipts and counters. It executes no product and measures no product
performance. It exercises one of the 32 preassigned E1 pilot blocks and leaves
all other slots explicitly missing. Choose new destination directories:

```bash
python3 -B experiments/gate-performance/harness/demo.py /tmp/weavegate-demo
python3 -B experiments/gate-performance/harness/tool.py verify /tmp/weavegate-demo/campaign
python3 -B /tmp/weavegate-demo/campaign/analysis/source/tool.py analyze \
  /tmp/weavegate-demo/campaign /tmp/weavegate-demo-reanalysis
cmp /tmp/weavegate-demo/analysis/results.json /tmp/weavegate-demo-reanalysis/results.json
python3 -B experiments/gate-performance/harness/test_harness.py
```

The generated `input/manifest.json` is a complete, constructed schema example.
Its hook's unconditional checks and artificial resource values are for this
demo only. They are not suitable evidence for a measurement campaign.

## Register and collect a real campaign

Prepare a private staging directory containing the manifest and all portable
pins. Supply real infrastructure-specific preparation, monitoring, evidence
validation and teardown commands before freezing. These tools do not provision
cloud allocations, restore memory/cache snapshots, dispatch experimental
workflows, or supply a universal cgroup monitor. Those operations depend on the
registered infrastructure and belong in pinned campaign hooks. A campaign
cannot pass with missing telemetry, lifecycle proofs or existing acceptance
checks; do not replace those checks with unconditional booleans.

The following are command templates; the manifest, public registration receipt
and host setup must exist first:

```bash
python3 -B experiments/gate-performance/harness/tool.py freeze campaign-input.json campaign
python3 -B experiments/gate-performance/harness/tool.py collect campaign \
  --block example-fixed-replay-01-001 --registration registration.json
python3 -B campaign/analysis/source/tool.py analyze campaign campaign-analysis
```

After `freeze` and before any `collect`, publish `campaign.json`, `order.json`,
protocol copies and pins in a public commit or immutable archive. Create
`registration.json` with `campaign_sha256` and `order_sha256` (SHA-256 of retained
bytes), `registered_at` (UTC), `revision` and `public_url`. The collector checks
hashes and temporal precedence and retains this receipt unchanged. It does not
authenticate an archive's timestamp or prove that the URL was public; an
independent auditor must verify that registration evidence. The demo uses an
explicitly constructed registration receipt.

Run blocks in `order.json` order within each workload/date slot, on their
registered UTC dates. The exact arm order is generated with the shared
SHA-256 algorithm, including odd-date balancing. E1/E2 counts, workloads,
seed and outer run budgets must match protocol revision 1. Collection refuses
an already started block, a modified live pin or collector source, an unfinished
prior block, a changed registration receipt and a concurrent writer.

Use one freshly assigned allocation for each E1 pair. Each arm's `prepare`
restores the registered snapshot and resets counters before its measured
invocation. Record allocation/host IDs, actual CPU/memory caps, image/cache
receipts and date in observed evidence. Declare those facts in `compare_facts`
and validate them in the pinned checker. Reused hosts/allocations and other
protocol deviations must be registered under `deviations`; any such deviation
keeps confirmation reporting descriptive. The collector does not certify cloud
allocation independence from a local process.

Both arms run despite ordinary first-arm failures, after proven cleanup. An
unproven cleanup marks the mate not started and stops the entire campaign with
`stopped.json`. Never remove a block directory, stopped marker or failure to
resume/retry. Diagnostic reruns use separate tuning campaigns and never enter
these first-attempt ledgers. Interruptions retain the entered slot and attempt;
forcible termination may leave an unsealed bundle. Preserve that directory for
audit rather than repairing its primary observations or replacing the block.

## Manifest schema 1

JSON rejects duplicate keys and nonfinite numbers. All hashes are full lowercase
SHA-256 without a prefix. Paths in retained evidence and portable pins are
relative, cannot traverse `..`, and cannot traverse symlinks.

| Field | Contract |
| --- | --- |
| `schema`, `campaign_id`, `experiment`, `phase`, `protocol_revision` | Schema `1`, unique campaign identity, `E1`–`E4`, registered phase/revision. E1/E2 support `pilot`, `confirmation`, and E1 `adoption-cold`. |
| `seed`, `strata`, `dates` | Published seed for E1/E2, workload-to-date-slot count arrays, distinct increasing UTC dates. All registered slots are created before measurement. |
| `protocols` | Filename to `{path, sha256}`; E1/E2 require exact copies of the three protocol files shipped with this collector. |
| `pins` | `{path, sha256, role}` entries copied from the manifest's directory to `pins/`. Include release checksums, binaries/JARs, inputs, dependency locks/trees, treatment diffs and hook/checker sources. |
| `execution_pins` | `{path, sha256, role}` entries for absolute live paths checked at freeze, before collection, before the measured command and before cleanup. Pin every executable, hook, JAR and input actually used. |
| `sources` | Collector/analyzer source commits and dependency locks, protocol commit, registration provenance. The collector also archives its actual Python sources. |
| `arms`, `inputs`, `runtime`, `environment`, `observation` | Nonempty inventories from the shared protocol's required campaign manifest. Include all identities, build commands, settings, resource boundaries, cache policy, expected evidence and redaction policy. Their experiment-specific completeness is a checker/auditor obligation. |
| `commands` | Workload → `baseline`/`candidate` → `prepare`, `run`, `observe`, `check`, `cleanup` command specifications. |
| `expected_exit`, `expected_facts` | Workload → arm → allowed existing exit codes / exact expected receipt facts. E1 enforces exit 0 for fixed workloads and exit 2 for vulnerable workloads, plus the registered `schedule_runs` and `flaky: false` expectations. E2 enforces wrapper exit 0 after existing probe assertions succeed. |
| `compare_facts` | Receipt fact keys that must match within every pair, such as normalized evidence hashes, schedule inventory, workload counts, image/cache/resource class and allocation identity. Do not compare volatile IDs or treatment-specific identities. |
| `quality_checks` | Required receipt checks. E1/E2 require `outcome`, `assertions`, `schedules`, `repetitions`, `normalized_evidence`, `isolation`, `lifecycle`, `resources`, `environment`; add the full campaign inventory, including acceptance-completeness checks. |
| `resource_metrics`, `analysis_metrics` | Required resources and optional descriptive numeric endpoints, with units/provenance declared in the observation inventory. Missing values remain unknown; zero is retained and its ratio is undefined. |
| `redact_env` | Environment variable names whose required values are passed to hooks and removed from retained bytes and command receipts. Values are never in the manifest. |
| `ci` | E2 repository, common `execution_sha`, and `tasks` mapping for each arm (below), plus workflow/action/ruleset pins in the inventories. |
| `deviations` | Disclosed departures from the registered design; prevents favorable confirmatory claims. |

A command specification contains exact `argv` (array), `cwd`, `env` (explicit
allowlisted values) and `timeout_seconds`. No shell is introduced by the
collector. Tokens `{trial_dir}`, `{campaign_dir}`, `{block_id}`, `{arm}` expand
at execution. Use absolute executable paths or explicitly supply `PATH`.
Parent environment values are not inherited except names in `redact_env`.
Secrets must be supplied only by those environment variables, never argv,
ordinary `env` values, pins, inventories or registration fields. Declare any
additional sanitization in the observer; wire payloads and database DSNs are
outside the collection contract.

`run` is the whole CLI invocation for warm E1, or the end-to-end adoption wrapper
for `adoption-cold`. For cold adoption the collector records `adoption_seconds`
from the wrapper, and the observer must retain separate actual
checkout/build/install/gate/upload spans. Its `gate_seconds` requires a separate
`gate_timer` receipt with `source: "monotonic"`, `boundary: "cli_spawn_to_exit"`,
`seconds` equal to that metric and `evidence` naming a hashed original timer
file. The wrapper's duration is never relabeled as the nested CLI duration.
E2's `run` is a pinned observer/dispatch wrapper for one complete workflow.
The 600/1,800/5,400-second observation budgets never modify product timeouts.
On expiration the process group receives TERM, followed by KILL if it has not
exited after the registered `cancel_grace_seconds` (default 25); cleanup still
runs, and the attempt is incomplete. Grace/teardown beyond the cap remains in
the command/event receipts. A process exit does not establish resource cleanup.

## Hook evidence

`observe` writes original product run directories, logs, resource-counter
receipts, environment identities and `receipt.json` under `{trial_dir}`. It
must retain the existing Oracle's outputs; it must not decide business verdicts.
`check` invokes pinned existing acceptance checkers and validates the frozen
expectations against those originals, including repeats, schedule IDs,
assertion rows, flakiness, normalized evidence and lifecycle obligations.
Its successful exit and actual logs are retained. A `receipt.json` contains:

- `checks`: required check names mapped to boolean `true` only after their
  actual validations succeed.
- `facts`: observed contract values, including every expected and paired fact.
- `metrics`: numeric values in the frozen units. E1 requires `cpu_seconds`,
  `peak_memory_bytes`, `containers_created`, `peak_containers`,
  `disk_write_bytes`, `artifact_bytes`, `cache_bytes`; E2 requires
  `runner_minutes`, `peak_runner_concurrency`.
- `environment`: observed host/allocation/runtime/cache identities.
- `resource_method`: counter scope, reset/timer boundaries and collection
  overhead; include CLI descendants and MySQL, with the protocol's 100 ms
  cadence and sampled-peak limitation if sampling memory.
- `evidence`: relative file paths mapped to hashes of sanitized retained bytes.
  Include all original logs, product artifacts, counter receipts and manifests.
- `ci_snapshot`: E2's retained provider snapshot path.

The collector supplies E1's primary duration from its own monotonic clock and
checks evidence hashes. The analyzer rechecks receipts, original evidence,
commands and cleanup rather than trusting the derived trial classification.
The quality interface composes campaign checkers; a boolean alone does not
establish the underlying Oracle, isolation or telemetry semantics.

`cleanup` always runs, including after prepare/run/observe/check failures. It
writes `cleanup.json` with `proven: true` and `evidence` path/hash entries for
observed teardown/restoration. Exit 0 alone is insufficient. Missing proof,
checker/pin drift or corrupted cleanup evidence quarantines the allocation and
stops collection. Redaction runs before retention hashes are sealed. If it
changes evidence bytes whose receipt hashes were not already sanitized, the
observation is incomplete, rather than silently repairing its semantic proof.
Keep public data synthetic. Review the sanitized private bundle before upload.

## E2 provider boundaries

Use the helper in the pinned observer after the workflow ends. This command
requires authenticated GitHub CLI and reads only the explicit original attempt:

```bash
python3 -B experiments/gate-performance/harness/tool.py ci-snapshot \
  --repository weavegate/weavegate --run-id 123 --attempt 1 --output ci.json
```

`123` is a placeholder, not an observed run. The helper retains run metadata
and every jobs/steps JSON page in one snapshot. The observer separately retains
logs, XML, workflow files, cache/resource receipts and same-run acceptance
manifests; API success cannot substitute for `--require-complete` or upload.

`ci.tasks.baseline` and `.candidate` map logical task names `go`, `java`,
`explore`, `replay`, `rollback`, `death`, `cancellation`, `accounting`, `upload`
to exact `{job, step}` names in the frozen workflow. Additional required logical
tasks are allowed. Provider calculation rejects incomplete/duplicated pages,
wrong attempt/run/revision, failed/skipped required jobs or steps and missing
runner end times. It derives `validation_seconds`, `queue_inclusive_seconds`,
`initial_queue_seconds`, `runner_minutes`, `peak_runner_concurrency` from UTC
provider timestamps. Failed and matrix jobs contribute runner cost; skipped
jobs do not. Concurrency uses half-open intervals and does not sum parallel
jobs as wall time. It validates all derived receipt values exactly.

Receipt facts `ci_created_at` and `ci_completed_at` identify each workflow's
provider boundaries; the analyzer requires the second run's creation to follow
the first run's completion by at most 300 seconds. Register required image,
cache and resource-class facts for paired equality and validate workflow hashes
and run IDs in the campaign checker. Merge-ready and evidence-ready timings
need their separate frozen ruleset/step mappings in the observer; the tool does
not rename the primary endpoint as either.

## Bundle and deterministic analysis

`campaign.json`, `order.json`/`order.csv`, `protocol/`, `pins/`, and
`analysis/source/` are frozen. `blocks/<block>/<arm>/` retains command receipts,
stdout/stderr, trial rows, observer/checker originals and cleanup proofs.
`trials.csv`, `commands.jsonl`, `events.jsonl`, `environments.jsonl` inventory all
slots and actual commands. A private `collection.lock` prevents concurrent
local writers. `SHA256SUMS` is a sorted JSON path-to-hash inventory of every
other bundle file; JSON preserves unusual filenames unambiguously.

Analyze into a **new directory outside the input bundle**. `results.json`
contains every planned/started/completion/failure denominator, reason counts,
paired missingness, per-date estimates, medians/IQRs, geometric paired ratios,
absolute differences and deterministic 10,000-replicate date-stratified
bootstrap intervals. `trials.json`/`trials.csv` retain every planned slot and
failed attempt's elapsed duration/exit. `report.md` is a concise derived table.
Output also retains the analyzer source, exact command and input inventory hash.
Moving the input bundle does not change the numeric tables.

No missing duration is zero, no attempt is replaced, and phases/workloads are
never pooled. Fewer than two valid pairs in any planned date suppresses the
interval. Zero counters retain their absolute differences and an N/A ratio.
All planned confirmation pairs, resources and quality evidence must be complete
before the E1 0.85 / E2 0.80 ratio threshold and upper interval bound `< 1`
can produce a favorable decision. Resource ratios above 1.10 are disclosed
in `higher_resource_cost`. Undefined ratios appear in
`undefined_resource_ratios` and prevent a resource-efficiency claim; present
zero counters do not invalidate the timing estimate. Secondary endpoints remain descriptive. There are
no p99, productivity, general Spring population or proven resource-equivalence
claims. E2 remains conditional on hosted-runner dates/cache/hardware pool.

Checksums detect changes, not dishonest observations or an incomplete checker.
Independent audit of frozen hooks, inventories, registration and original
receipts remains necessary. Correcting analysis retains old inputs and outputs
and records the new analyzer source; changing the experiment requires a new
campaign. Publish unfavorable, incomplete and stopped campaigns too.
