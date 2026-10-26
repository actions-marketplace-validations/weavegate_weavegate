# Experiment collector and analyzer

Standalone Python 3.9+ tools freeze a campaign, collect each assigned pair once,
and reproduce accounting and the published [E1/E2 analysis](../README.md) from
retained evidence. Collection requires Linux, `/proc`, POSIX process groups and
file locks. The tools use only the standard library and add no engine dependency
or `weavegate` subcommand. E3/E4 can reuse collection with separately registered
protocols and counts; their statistical and holdout rules remain future work.

## Run the constructed example

Run from the repository root with new destination directories:

```bash
python3 -B experiments/gate-performance/harness/demo.py /tmp/weavegate-demo
python3 -B experiments/gate-performance/harness/tool.py verify /tmp/weavegate-demo/campaign
python3 -B /tmp/weavegate-demo/campaign/analysis/source/tool.py analyze \
  /tmp/weavegate-demo/campaign /tmp/weavegate-demo-reanalysis
cmp /tmp/weavegate-demo/analysis/results.json /tmp/weavegate-demo-reanalysis/results.json
cmp /tmp/weavegate-demo/analysis/report.md /tmp/weavegate-demo-reanalysis/report.md
python3 -B experiments/gate-performance/harness/test_harness.py
```

The demo is **constructed evidence**, not a performance observation. It runs one
of the 32 assigned E1 pilot blocks and leaves every other slot missing. Its
`input/manifest.json` is a complete schema example; unconditional demo checks
and artificial counters are unsuitable for a measurement campaign.

## Design and ownership

| Module | Owns |
| --- | --- |
| `plan.py` | Manifest shapes, protocol requirements, registration syntax and live input pins |
| `execution.py` | Exact invocation, monotonic timing, bounded process-group termination and durable command receipts |
| `storage.py` | Collector file ownership and immutable evidence at each hook boundary |
| `redaction.py` | Payload sanitization; control-field exemptions require explicitly owned paths |
| `collector.py` | One-shot slot admission, ordered hooks, unconditional trusted cleanup and sealing |
| `evidence.py` | The shared reduction from original commands, observations and cleanup to trial accounting |
| `analysis.py` | Paired statistics, decisions and reports from the reduced evidence |
| `ci.py` | Original-attempt provider snapshots and workflow endpoint arithmetic |

The design separates observations from derived views. `receipt.json` remains the
observer's original sanitized bytes. The collector's duration is joined in
memory; it never edits that original to insert a measurement. `trial.json` and
the aggregate ledgers are cached views. Both collection and offline analysis
use `evidence.trial`, including for failures, so a cached classification cannot
create a success, hide a started attempt or supply a timing measurement.

Each completed hook establishes an immutable boundary. Later hooks may add
originals in the active arm, but cannot replace retained originals, earlier
command receipts, another arm, campaign inputs or aggregate ledgers. Keep
mutable monitoring state and temporary build files outside the retained arm;
copy their final evidence into the arm when ready. The collector checks these
boundaries before and after hooks and before sealing. An observed violation is
permanent even if a later hook restores the bytes. Trusted cleanup is still
attempted; the original checksum inventory is preserved and resealing refused.
This is integrity checking among trusted hooks, not an OS security sandbox
against processes running as the same user.

The evidence layout is bundle format `5`, recorded in
`analysis/dependencies.json`; the input manifest remains schema `1`. Earlier
development bundles retain their own archived analyzer. This analyzer rejects
older layouts rather than interpreting their cached trials as original evidence.

## Register and collect

Prepare infrastructure-specific hooks for allocation/restoration, resource
measurement, existing acceptance checks and teardown. These tools do not supply
cloud provisioning, experimental workflows or a universal resource monitor.
Hooks preserve existing Oracle verdicts; campaign accounting does not judge
business invariants. Missing telemetry or lifecycle proof cannot be replaced
with unconditional checks.

These commands are templates requiring a real manifest and registration:

```bash
python3 -B experiments/gate-performance/harness/tool.py freeze campaign-input.json campaign
python3 -B experiments/gate-performance/harness/tool.py collect campaign \
  --block example-fixed-replay-01-001 --registration registration.json
python3 -B campaign/analysis/source/tool.py analyze campaign campaign-analysis
```

Freeze validates the complete plan and pins before creating the private campaign
directory. It copies protocols, artifacts and executable Python sources, assigns
all blocks using the published SHA-256 algorithm and seals the inventory.
After freeze and before collection, publish the frozen plan, order, protocols
and pins in a public commit or immutable archive. Supply a registration object:

| Field | Contract |
| --- | --- |
| `campaign_sha256`, `order_sha256` | SHA-256 of the retained `campaign.json` and `order.json` bytes |
| `registered_at` | UTC timestamp preceding all collection commands |
| `revision` | Nonempty public registration revision |
| `public_url` | Absolute HTTP/HTTPS URL with a hostname and no credentials, whitespace or control characters |

The receipt cannot change after collection begins, including optional nested
metadata. Canonical JSON comparison distinguishes `true`, `1` and `1.0`, while
ignoring object key order and whitespace. Syntax, hashes and temporal precedence
are checked; public availability and timestamp authenticity require
independent audit. The demo registration is explicitly constructed.

Run blocks in assigned sequence within each workload/date slot, on their UTC
dates. Date eligibility is checked before preparation and the measured command.
An expired date remains `not_started`; any preparation still receives cleanup.
There is no replacement attempt. Existing blocks, unfinished prior blocks,
changed live inputs or collector sources, concurrent collectors and stopped
campaigns are rejected. E1 requires fresh dedicated allocations for independent
pairs. Register allocation/host/cache/image/resource facts for paired comparison;
independence and counter scope also require audit of the frozen hooks.

## Manifest schema 1

JSON rejects duplicate keys and nonfinite numbers. Versions, counts and exit
codes are integers, not booleans. Hashes are lowercase 64-character SHA-256.
Portable paths are relative, without parent traversal, NUL or symlinks.
Workload IDs use lowercase ASCII letters, digits and hyphens, starting with a
letter or digit. Every composed `<workload>-<date-slot>-<block-number>` name must
fit within 255 bytes, including numeric suffixes wider than their minimum two
and three digits. Overlong names are rejected before freeze creates a campaign.

| Field | Contract |
| --- | --- |
| `schema`, `campaign_id`, `experiment`, `phase`, `protocol_revision` | Schema `1`, nonempty identity/phase, `E1`–`E4` and positive protocol revision; E1/E2 use revision `1` |
| `seed`, `strata`, `dates` | Nonempty seed; workload-to-positive-count arrays; distinct increasing canonical UTC dates (`YYYY-MM-DD`). E1/E2 use the published seed, counts and workloads |
| `arms`, `inputs`, `runtime`, `environment`, `observation`, `sources` | Nonempty inventory objects covering the [shared protocol](../README.md#required-campaign-manifest); completeness and accuracy require audit |
| `protocols` | Retained filename to `{path, sha256}`; E1/E2 require exact copies of the three published protocol files |
| `pins` | Unique `{path, sha256, role}` entries copied relative to the manifest into `pins/` |
| `execution_pins` | Unique `{path, sha256, role}` entries for absolute live inputs; pin every executable, hook, JAR and input used |
| `commands` | Every workload, both `baseline`/`candidate` arms, and all five hook specifications |
| `expected_exit`, `expected_facts` | Every workload and arm: nonempty integer exit lists and nonempty expected-fact objects |
| `quality_checks`, `resource_metrics`, `compare_facts` | Unique nonempty string lists; E1/E2 include the published quality/resource inventory |
| `analysis_metrics` | Optional unique nonempty metric names for descriptive E1/E2 estimates |
| `redact_env` | Unique environment variable names whose secret values must be supplied at collection; may be empty |
| `deviations` | Optional string list; any entry prevents a favorable confirmatory claim |
| `ci` | E2 `repository` (`owner/repo`), common lowercase 40-hex `execution_sha`, and logical task mappings for both arms |

A command has `argv` (nonempty string array with a nonempty executable), `cwd`
(absolute directory, or a path beginning with `{campaign_dir}` or `{trial_dir}`
as its first component), `env` (explicit string-to-string allowlist), positive
`timeout_seconds` and optional positive `cancel_grace_seconds` (default 25).
Environment names must be nonempty and cannot contain `=`; arguments, cwd,
environment names and values cannot contain NUL. No shell is introduced.
`{trial_dir}`, `{campaign_dir}`, `{block_id}` and `{arm}` expand once. Unknown
tokens and braces inside inserted paths remain literal. Relative working
directories are rejected before freeze creates a bundle. Supply an existing
absolute executable, or a bare executable name with explicit `env.PATH` containing
only absolute directories. Freeze resolves bare names and retains the absolute
path in `campaign.json` and subsequent command receipts. Every resolved executable
must be an executable regular file at freeze and every live-pin check, including
admission before a block is consumed and trusted cleanup. It must appear in
`execution_pins`; missing pins, relative paths and executable
placeholders are rejected before creating the bundle. A later PATH shadow cannot
select another binary. Pin interpreter scripts, JARs and other dependencies too;
the harness does not discover transitive dependencies. The parent environment
is otherwise not inherited.

The five hooks run in this order:

| Hook | Boundary |
| --- | --- |
| `prepare` | Restore the registered allocation/cache state and reset counters |
| `run` | Whole E1 CLI invocation, cold-adoption wrapper, or E2 dispatch/retention wrapper |
| `observe` | Retain original product evidence, counters, environment and `receipt.json` |
| `check` | Run pinned existing acceptance checks and validate the frozen expectations |
| `cleanup` | Prove teardown/restoration with `cleanup.json` and original evidence |

E1 warm `run.timeout_seconds` is 600; cold adoption is 1,800. E2's wrapper
watchdog must exceed 5,400 to reserve dispatch/retention overhead; its provider
cap remains 5,400 seconds from workflow creation. Hooks must enforce that
provider deadline and cancellation. Product timeouts remain unchanged.

Live pins are checked before each non-cleanup hook. Cleanup may declare its own
nonempty `execution_pins` subset of registered paths, including its executable;
without it, all live pins are required. This permits trusted teardown after
unrelated input drift. The complete live pin set is checked both before teardown
and after the cleanup command and its proof validation. Drift at either boundary
prevents allocation reuse and stops the campaign, including after the final arm.
Changed cleanup dependencies are never executed.
SIGINT/SIGTERM delivery is deferred across cleanup pin verification, the bounded
cleanup command and its final evidence checkpoint, so a signal cannot bypass
trusted teardown or interrupt its startup.

## Receipts and failure behavior

An observation `receipt.json` contains:

| Field | Required evidence |
| --- | --- |
| `checks` | Required quality names mapped to boolean `true` after actual validation |
| `facts` | Frozen expected facts and paired comparison facts; nested booleans differ from numbers |
| `metrics` | Numeric nonnegative finite counters in frozen units; invalid or absent readings stay unknown |
| `environment` | Nonempty object of observed host/allocation/runtime/cache identities |
| `resource_method` | Nonblank description of counter scope, reset/timer boundaries and collection overhead |
| `evidence` | Nonempty relative-path-to-hash map of sanitized retained originals |
| `ci_snapshot` | E2 provider snapshot path, included in `evidence` |

E1 resources are `cpu_seconds`, `peak_memory_bytes`, `containers_created`,
`peak_containers`, `disk_write_bytes`, `artifact_bytes` and `cache_bytes`.
E2 resources are `runner_minutes` and `peak_runner_concurrency`.
For cold adoption, the collector supplies `adoption_seconds`; the observer's
separate `gate_seconds` requires `gate_timer` with `source: "monotonic"`,
`boundary: "cli_spawn_to_exit"`, matching numeric `seconds` and a hashed original
`evidence` path. The wrapper duration cannot substitute for the nested CLI timer.

The collector validates the sanitized receipt and every referenced original
immediately after `observe`, before invoking `check`. Missing or invalid
observation evidence skips `check` and retains a quality failure; later hooks
cannot supply evidence retroactively. Trusted cleanup still runs.

Cleanup requires successful command execution plus `cleanup.json` with
`proven: true` and a nonempty `evidence` path/hash map. Exit 0 alone is inadequate.
Observation and cleanup evidence cannot reference the collector's exact control
or command paths at the arm root, or hard-link aliases of those files. Independent
nested originals with those names remain valid. The observation boundary keeps
the redaction control file live until validation; a rejected alias remains a
quality failure when the bundle is copied or control files are later replaced.
The collector records paths, hashes and file identities in `cleanup-inputs.json`
immediately before teardown. Both `cleanup.json` and every referenced cleanup original must be newly retained by
that step; the inventory also records pre-cleanup device/inode identities to
reject hard-link aliases of earlier files, including command receipts,
stdout/stderr and control receipts. Control files stay live until cleanup proof
validation; their replacement, including the final redaction count, happens
afterward so freed inodes cannot be reused by new cleanup originals. Validation
also rejects aliases of live controls, including `cleanup-inputs.json`, which
cannot inventory its own inode before it is written. Earlier product evidence,
command output or control receipts cannot substitute for teardown evidence.
Recorded device/inode numbers are used only at the live cleanup boundary.
Offline analysis requires the sealed successful boundary result and rechecks
hashes, fresh paths and aliases against the current retained files. It never
compares copied files to inode numbers from the collection filesystem, where
unrelated files can have the same numbers. A failed collection-time cleanup
check remains failed after copying, even when the copy breaks hard links.
Malformed observations, operational exits and timeouts remain failed attempts;
the mate still runs after proven cleanup. Unproven cleanup leaves the mate not
started, writes `stopped.json` and prohibits further collection.

Command receipts retain `collector_start_ns`, the collector monotonic origin used
for `seconds`. It permits receipt-time alignment within the same collector only;
it must not be compared with child-process clocks or another collector's origin.
Command receipts persist `started` after successful process creation and retain
`finished`, `interrupted`, exit, error, timestamps and elapsed seconds. The
collector defers cancellation during spawn until it owns the child handle;
the child does not inherit a blocked signal mask. Collection runs on the main
Python thread. Interrupted atomic start checkpoints accept either owned version.
Before closing command output descriptors, the collector verifies that each
stdout/stderr path still identifies the file it opened. Removal or replacement,
even with identical bytes, is a permanent integrity failure and prevents sealing.
The
watchdog starts before spawn and waits only its remaining budget. Timeout sends
TERM to the process group, waits one grace bound, then KILL with one further
shared bound for leader reaping and group death. If the group vanishes before
TERM, the leader is still reaped and group absence checked; the receipt remains
a timeout without an extra execution error. A normally exiting leader with
live descendants also fails and triggers termination. `/proc` polling observes
termination only; it does not coordinate workloads. Processes that detach from
the group and external resources still require the registered cleanup proof.
An unproven process-group termination quarantines the campaign even if a hook
supplies a successful cleanup receipt.

SIGINT/SIGTERM interrupt collection through cleanup and sanitization; the CLI
returns 130 after preserving evidence. Failed spawn stays unstarted; an
interrupted running command stays started with its actual elapsed time. Atomic
JSON replacement and the corresponding ownership update defer signals together,
so a collector write cannot be mistaken for hook tampering. Block admission also
defers delivery until registration, slot metadata and the initial guard are ready;
an admission signal seals a stopped campaign with both arms unstarted.
Final aggregate generation
and sealing defer signal delivery; a signal at that boundary adds and seals
the stopped record before returning. An interruption writes
`stopped.json` even after proven cleanup; the unexecuted mate remains in missing
attempt accounting and further collection is prohibited. SIGKILL, power loss or failed
sanitization can leave an unsealed bundle: preserve it for audit, do not repair
or rerun it as a primary observation. No claim of crash recovery is made.

## Sanitization and bundle layout

Secrets belong only in `redact_env` values, never public manifests, ordinary
`env`, argv, paths, pins or registration. Before consuming a block, collection
checks all retained file paths and bytes, decoded JSON, registration and generated
control metadata, including both arms' expanded argv/cwd/env. A configured secret
in those public values rejects admission without changing the frozen bundle or
running hooks. This includes campaign and manifest source paths, relocated
campaign paths and short secrets that collide with public data. Credentials are
supplied at collection, so freeze alone does not provide this check; review the
frozen inputs before publishing them.
Empty values and values contained in the literal `[REDACTED]` marker also fail
before entering a slot. Longer overlapping
values are replaced first. JSON string redaction preserves numeric/boolean
syntax. Both the reserialized JSON and the bytes to be retained are checked for
remaining secret variants. A secret represented as a number, boolean, null or
protected control value rejects sanitization instead of changing its type or
public meaning. Cleanup is still attempted, and the rejection prevents sealing
even if cleanup removes the offending payload. Non-JSON payloads receive byte
redaction, including JSON-escaped forms.
No payload is exempt merely because its filename resembles a control file.
Only the collector's exact owned paths retain public identity/control fields.
Additional payload encodings and domain-specific sanitization belong in hooks.
Hook-owned relative file and directory paths containing a configured secret
are rejected, including empty directories and collisions with short secret
values. Paths are never silently renamed. The diagnostic omits the leaking
name, cleanup is attempted and resealing remains forbidden even if cleanup
removes that path. Collector-owned public control paths retain their spelling
after admission validates their public inputs.

Sanitization precedes evidence checkpoints. Unchanged bytes are not rewritten.
Changed payloads use an atomic temporary-file replacement, preserving external
originals when a hook retained hard links. Each retained link is sanitized separately.
If replacement changes a proof's original bytes, its old hash fails; the tool
never repairs the semantic proof to manufacture validity. `redactions.json`
accumulates replacements across hooks. A redaction that cannot be written
prevents sealing. Symlinks, FIFOs, sockets and other non-regular entries cannot
be sealed. Review the private sanitized bundle before publishing it.

Frozen files are `campaign.json`, `order.json`/`order.csv`, `protocol/`, `pins/`
and `analysis/source/`. Each `blocks/<block>/<arm>/` retains original hook files
alongside collector-owned `attempt.json`, `trial.json`, `redactions.json`,
`cleanup-inputs.json` and
`<step>.command.json`/`.stdout`/`.stderr` for the five steps. These control names
are reserved at the arm root; nested originals are ordinary payloads.
`attempt.json` stores admission/interruption, observation-boundary failures and
cleanup-input state.
`trials.csv`, `commands.jsonl`, `events.jsonl` and `environments.jsonl` inventory
all planned arms and actual commands. All four ledgers use atomic replacement,
preserving originals that hooks hard-linked to an earlier ledger.
Collection and analysis lock both the campaign directory inode and
`collection.lock`; concurrent analyses share these locks. Replacing the lock file
cannot admit a second writer while a current reader or writer holds the directory
lock. Path-to-inode checks detect replacement, and hook-boundary checks refuse to
reseal the damaged bundle. These checks remain local coordination, not an OS sandbox.
`SHA256SUMS` is a JSON inventory of every other entry: files map
to SHA-256 values and directories map to `null`. Empty directory addition,
removal and file/directory replacement are authenticated at hook boundaries
and during verification.

## E2 provider evidence

A pinned observer can retain the original completed attempt with:

```bash
python3 -B experiments/gate-performance/harness/tool.py ci-snapshot \
  --repository weavegate/weavegate --run-id 123 --attempt 1 --output ci.json
```

`123` is a placeholder. Authenticated GitHub CLI is required for this helper.
Retain logs, XML, workflow files and same-run acceptance manifests separately;
provider success cannot replace the existing `--require-complete` checks.

`ci.tasks.baseline` and `.candidate` map `go`, `java`, `explore`, `replay`,
`rollback`, `death`, `cancellation`, `accounting`, `upload` and any extra required
tasks to exact nonempty `{job, step}` names. All pages, original attempt 1,
revision, required successful jobs/steps, started-job runner identities and end
times must be present. Run IDs, job IDs and job `run_id` values must be positive
integers; booleans, floats and strings are rejected before identity comparison.
Every page's `total_count` must be a nonnegative integer, agree across pages and
match the number of distinct retained jobs; booleans and floats are rejected.
Failed/matrix jobs contribute runner cost; skipped jobs
do not. Concurrency uses half-open intervals, not summed parallel wall time.

Provider timestamps derive `validation_seconds`, `queue_inclusive_seconds`,
`initial_queue_seconds`, `runner_minutes` and `peak_runner_concurrency`; supplied
metrics must agree exactly. The primary ends at the last required job's
completion. The outer cap also includes the later of run `updated_at` and any
started job completion; GitHub exposes no workflow `completed_at`.
`ci_created_at` and `ci_completed_at` facts must match these provider boundaries.
The second workflow must begin within 300 seconds after the first completes.
Both queue metrics always appear in E2 reports. Merge-ready/evidence-ready
endpoints require separately frozen mappings and cannot rename the primary.

## Reproduce analysis

Analyze into a **new directory outside the input bundle**. Analysis holds a
shared campaign lock from input verification through output sealing. A busy
collector causes analysis to fail before creating output. Analysis verifies
inventory, plan, order and registration, then reconstructs every trial from
original command/observation/cleanup evidence. Any retained registration is
validated, including before the first command; collection evidence requires a
registration. A newly frozen bundle can be analyzed before registration.
Commands must match frozen argv,
cwd and public environment, their registered UTC date and watchdog. The bundle
can move: command verification uses its retained original execution location.
No live checkout, executable pin or provider connection is needed for analysis.
The archived entry point disables bytecode writes so importing its modules does
not change the input. Reproduction command receipts also specify `-B`.
For E3/E4 the output is `accounting_only`, with no statistical estimates until
their protocols and corresponding analyzers are implemented.

`results.json` and `report.md` show every planned/started/outcome denominator,
reason, excluded pair, registered deviation and failed-attempt cost. E1/E2
estimates include per-date effects, arm medians/IQRs, paired geometric ratios,
absolute differences and deterministic 10,000-replicate date-stratified bootstrap
intervals. Missing durations are never zero, workloads/phases are never pooled,
and fewer than two valid pairs on any planned date suppresses the interval.
Zero resource counters remain observed, with an undefined ratio. Extreme finite
values retain their differences and counts if a ratio cannot be represented;
unrepresentable bootstrap ratios suppress the whole ratio interval.

Confirmation requires every planned pair and required quality/resource evidence,
primary ratio at most E1 0.85 / E2 0.80, and upper 95% ratio bound below 1.
Registered deviations prevent a favorable claim. Resource ratios above 1.10
qualify an improvement with higher measured cost (all seven E1 resources; E2
runner-minutes). Undefined ratios prevent resource-efficiency claims, without
turning observed zero counters into missing measurements. Secondary endpoints
remain descriptive. There are no p99, productivity or general Spring-population
claims; E2 remains conditional on the registered hosted-runner pool/dates/cache.

Outputs also retain every trial, analyzer source, exact command and input hash.
Corrected analyses retain old input/output and disclose the new source; a changed
experiment requires a new campaign. Checksums cannot establish honest telemetry
or a complete checker: audit the hooks, inventory, registration and originals,
and publish unfavorable, incomplete and stopped campaigns too.
