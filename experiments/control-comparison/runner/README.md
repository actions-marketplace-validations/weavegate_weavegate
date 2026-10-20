# E5 execution and retained analysis

This implements the drivers for the [revision 1 protocol](../README.md), whose
published bytes remain unchanged. It is experiment infrastructure, not an E5
result or an E1 confirmation candidate. Measurement still requires the reviewed
E1 candidate, acceptance evidence and registered infrastructure. The optional
`E5 control feasibility` workflow runs only through `workflow_dispatch`; it is
not a required measurement gate.

The tools require Linux, Go from `go.mod`, Python 3.9+, Java 21 and Docker with
the MySQL 8.4 image already present. Python uses only its standard library. The
launcher compiles against the dependencies already inside each fixture JAR;
there is no new engine, SDK or analysis dependency.

## Build and verify feasibility

From the repository root, build the unchanged SDK and fixture applications:

```bash
sdk/java/mvnw -B -f sdk/java/pom.xml -Dmaven.test.skip=true install
for fixture in spring-matching spring-quota spring-job-claim; do
  sdk/java/mvnw -B -f "fixtures/$fixture/app/pom.xml" package
done
docker pull mysql:8.4
python3 -B experiments/control-comparison/runner/test_runner.py
go test ./experiments/control-comparison/driver -race -count=20
python3 -B experiments/control-comparison/runner/smoke.py /tmp/e5-feasibility
```

Use a new output directory each time. `smoke.py` runs B1 twenty times for each
workload/variant, then the first complete block of the frozen order: B2, T-R,
T-E, B1, B0 for matching/fixed. B2 performs 100 executions. It compares two
offline analyses byte for byte. Other planned slots remain explicitly unstarted.
`--count 2` is a shorter **feasibility-only** check; it changes B1/B2/replay
counts, never a registered measurement. Exhaustive exploration remains three
passes. These checks do not measure cold/warm VM strata, provide a confirmation
sample or replace the existing peer/corpus acceptance suites.

The smoke workflow retains build logs and original evidence even after failure.
The ordinary docs job runs only the constructed accounting tests and checks
their fixed marker. No workload measurement is added to required PR checks.

## Driver boundaries

`driver/` imports the existing fixture, config, scenario and SQL Oracle packages.
It provisions one fresh fixture per invocation, records its initial seed and
checks every subsequent reset against that seed. B0 ends after readiness and
teardown. B1/B2 create a fresh JVM for every execution and persist an execution
JSON after every attempt, including failures. Only `internal/oracle` evaluates
domain assertions; the driver retains its complete evaluation and fingerprint.

`launcher/BaselineMain.java` loads the original business classes extracted from
the application JAR. Its separate classpath includes that JAR's exact library
bytes and one additional launcher class. It registers Hikari with maximum pool
size three and minimum idle zero, matching capacity two plus a probe connection.
It invokes the original annotated bean methods through Spring transaction
proxies using the original worker arguments and `CommandContext`. Matching's
default transaction isolation is checked through the actual RR session; quota
and job retain their explicit RR annotations.

The baseline uses Spring's `JdbcTransactionManager`, whereas the enabled product
uses its SDK tracking transaction manager. The baseline never enters
`WeavegateChild.run`; hooks and after-commit callbacks remain in business code and
return through the SDK's existing inactive path. Both tasks exist before one
start latch opens. There is no read/write/commit latch, delay, jitter or retry.
This launch/supervision difference is part of the total controlled-path cost,
not an isolated estimate of sync-point overhead.

The driver waits for both proxies to return before Oracle evaluation, checks
that no pool lease remains, closes the context/pool, reaps the JVM and checks
that the application's database sessions retired before any reset. Startup,
worker execution, cancellation and stop bounds are 30, 30, 5 and 20 seconds;
fixture teardown has the same detached 30-second bound as the CLI. A worker or
lifecycle failure stops the invocation. A domain violation alone does not stop
B1 or B2. The outer collector owns cancellation at the protocol's arm cap.

Database credentials travel on private stdin, never in argv or retained input
files. Captured Java stderr replaces the generated password and JDBC URL before
retention. All data are synthetic. Infrastructure hooks must also sanitize their
own outputs before they enter the bundle; do not put credentials in their argv
or allowlisted environment values.

## Prepare and freeze

The local preparer supplies a **feasibility template**, including effective
configs, the complete Go-exported candidate inventory, dependency and business
class hashes, exact argv arrays and live input pins:

```bash
python3 -B experiments/control-comparison/runner/prepare.py /tmp/e5-input
python3 -B experiments/control-comparison/runner/tool.py freeze \
  /tmp/e5-input/manifest.json /tmp/e5-campaign
```

Freeze retains all pinned bytes, deduplicated by SHA-256, and copies the analyzer
and shared process/bundle primitives into `frozen/source/`, including the
redaction module's `plan.py` dependency. Shared imports take precedence over
ambient Python packages and their resolved locations are checked before use.
It generates exactly 120 blocks and 600 invocation slots with the protocol's hash ordering. Changed
counts, candidate order, settings or caps are rejected for measurement.
Every measurement T-R/T-E executable must hash to `candidate.cli_sha256`,
including the T-R command used for warm-up. An unused candidate pin does not
authenticate a command that invokes a different executable.

To prepare a measurement manifest, use the template's schema and replace its
development artifacts/commands with the actual E1 candidate and registered
infrastructure. Do this **before** freezing or running any warm-up. The local
preparer does not provision a VM, confer candidate status on a development build,
or manufacture successful prerequisite evidence.

| Manifest field | Required content |
| --- | --- |
| `schema`, `protocol_revision`, `phase`, `campaign_id`, `seed` | `1`, `1`, `measurement`, unique ID, fixed protocol seed |
| `protocol` | Original protocol `path` and SHA-256; its published hash is checked |
| `fixture_revision` | The reviewed fixture commit named by the protocol |
| `pins` | Absolute path to SHA-256 for every binary, launcher, library, script, source, config, SQL, schedule and prerequisite file used |
| `inputs` | Each workload/variant's effective config, candidates, expected domain rows, counts, JAR/SDK and business-class hashes |
| `commands` | Every workload/variant and B0/B1/B2/T-R/T-E command, including exact `argv`, absolute `cwd`, explicit `env`, timeout and cleanup grace |
| `candidate`, `e1_registration` | Matching CLI/SDK digests, source revision and E1 public registration URL/revision |
| `environment` | `host_id`, `vm_image`, `cpu_model`, `kernel`, `docker`, `background_policy`, `mysql_image`, `vcpus: 4`, `memory_bytes: 17179869184` |
| `prerequisites` | Pinned paths for `go_acceptance`, `java_acceptance`, `paired_acceptance`, `corpus_acceptance`, `baseline_feasibility` |
| `hooks` | `prepare`, `warm_cleanup`, `observe`, `cleanup` command specifications; cleanup declares its trusted `execution_pins` subset |
| `unsupported` | Optional workload/arm to issue URL and reason; planned denominators are retained |

Baseline counts are 20/100; T replay is 20 and exploration is three passes with
automatic confirmation. Freeze commands and all their transitive inputs.
Measurement validation checks literal argv/environment files, classpath
directories and Java `lib/*` entries, plus the effective config's Java executable,
JAR, migrations and seed. These checks repeat at live-input boundaries so adding
an unpinned class or library after freeze also stops collection. Baseline commands
declare exactly one explicit `--classpath` (directory/JAR entries or Java `lib/*`).
The complete fixture class inventory and the SDK JAR on that classpath must match
`business_classes` and `sdk_sha256`; duplicate or shadow fixture/SDK classes fail
validation even when pinned. The application JAR must match `jar_sha256`, and its
embedded fixture classes and SDK must match those same identities. Measurement
configs must all use `environment.mysql_image`, including its SHA-256 digest.
Baseline and product receipts must retain that same image identity. Effective config
snapshots must describe the command's actual config; dynamic imports, subprocess
lookup and other runtime dependencies still require explicit pins and audit. A
measurement T command invokes the registered CLI directly; transport wrappers
cannot replace that executable. Infrastructure hooks and their remote
observations belong in the inventory. The collector and evidence storage must
survive restoration outside the guest. Aggregate
resource caps include Java, Go, MySQL and observers.

Hooks receive single-pass `{trial_dir}`, `{campaign_dir}`, `{workload}`,
`{variant}`, `{cache}`, `{block}`, `{arm}` substitutions. They are trusted
infrastructure adapters, not arbitrary user code sandboxes. Commands inherit
only the declared environment. Every receipt below includes a nonempty
`evidence` object mapping relative original-file paths to their sanitized SHA-256.
Exit zero or an unsubstantiated boolean is insufficient.

| Hook | Required retained receipt |
| --- | --- |
| `prepare` | `preparation.json`: registered `host_id`, `disk_image`, `fresh_memory: true`, `clean_data: true`, observed caps |
| `warm_cleanup` | After the one warm-up, `warm-cleanup.json`: `outputs_removed: true`, `data_removed: true`, `cache_preserved: true` |
| `observe` | `observation.json`: registered `host_id`, `resources`, original runtime/version/cache/counter evidence |
| `cleanup` | `cleanup.json`: `proven: true` plus newly retained proof of child exit, retired connections and fixture removal |

Original hook proof files live under `proof/`; observation and cleanup proofs
must be newly retained by their respective hooks. Neither can cite an earlier
preparation or warm-up file as evidence of a later observation or cleanup.
Cleanup can run after unrelated input drift when every dependency in its
separately pinned subset still matches; the campaign stops afterward.
The subset must include the interpreter, script and all transitive cleanup
inputs. Freeze checks literal file arguments (including relative paths,
`--option=path`, path lists and directory contents) and file-valued environment
entries against `execution_pins`; cleanup repeats that check before execution.
Imports, subprocesses selected through `PATH` and dynamically opened files still
require explicit dependency pins and independent audit. The runner does not
infer arbitrary program dependencies.

The runner performs exactly one T-R warm-up after restoration for warm slots,
validates its expected outcome and cleanup, and retains it separately. Its
command receipt must show a started, finished, uninterrupted execution with no
operational error or timeout and proven process containment. Clean exit 0 and
domain-violation exit 2 remain valid. A failed warm-up consumes the slot as
preparation-failed before the measured command can run. Hooks must preserve
evidence outside the restored image and verify no live database/data volume
remains.
Resource values unavailable at the aggregate scope are null with reasons. Report
sampled memory at 100 ms with its missed-peak limitation. The schema checks and
hashes cannot independently establish that a VM was restored, that a prerequisite
command passed or that public registration was published at the claimed time;
the frozen commands and original evidence need the protocol's independent audit.

## Collect and regenerate

Publish `frozen/` in a commit or immutable archive before collection. A separate
registration JSON names `manifest_sha256`, `order_sha256`, `registered_at` (UTC),
`revision` and `public_url`. The hashes cover `frozen/manifest.json` and
`frozen/order.json`. Before consuming the first slot, collection also stores the
registration identity in the sealed progress checkpoint. Every subsequent
checkpoint preserves it. Missing or replaced `registration.json` fails both
continuation and offline analysis; it cannot be recreated with a later
registration after collection starts.

These are command templates for a real, previously prepared measurement bundle:

```bash
python3 -B /path/to/campaign/frozen/source/tool.py collect /path/to/campaign \
  --registration /path/to/registration.json
python3 -B /path/to/campaign/frozen/source/tool.py analyze /path/to/campaign \
  /path/to/new-analysis-directory
```

The first command runs all supported registered slots in order. It admits only
attempt one. A sealed, completed earlier slot is skipped on continuation; an
unfinished slot is never retried. Concurrent collectors, changed input/source
pins, changed registration and stopped campaigns are rejected. Unproven cleanup
quarantines the allocation and preserves remaining slots as not started. A new
feasibility ID is required for diagnostic reruns. `--max-slots` exists only for
feasibility, including the automated five-arm check above.
Final input or evidence integrity failures are sealed on the active slot as
operational errors and also stop the campaign, even when cleanup succeeds.

The sealed `progress/checkpoint.json` records the consumed slot seals, any active
attempt, registration identity, completion and stop state. A slot also seals its
own stop reason before the checkpoint advances. `stopped.json` is a convenience copy: omitting it cannot
clear quarantine or erase an interruption. Both collection and analysis require
the exact ordered prefix recorded by the checkpoint; missing middle or final
slots, inconsistent stop/completion state and unfinished checkpoints fail closed.
Keep `progress/` with the bundle during transfer. Bundles frozen before this
checkpoint format must use their original frozen tools; they cannot be resumed
or analyzed by the updated runner.

Analysis reads only the frozen manifest/order and sealed original slot evidence;
it does not run Java, Go, Docker or registered hooks. `results.json` contains all
600 slot statuses, first-event bounds, per-execution outcomes, resource
disclosures, stage durations and within-block pairs. `report.md` renders the
stratified accounting and timing table. Regenerating into another new directory
produces byte-identical files. Corrupt or missing sealed originals fail analysis.
Analysis holds a shared campaign lock through validation, reduction and output
writing. It fails immediately while collection holds the exclusive lock;
concurrent analyses may use separate output directories.
Baseline receipts must match the requested arm and variant. Product observations
must declare `replay` for T-R and `explore` for T-E, including warm-up. Evidence
from another slot or mode cannot count as a completed observation.
Baseline and product domain rows use the same string-normalized comparison
against the frozen expectation. Unexpected rows remain recorded observations
and make the invocation quality-failed.

The collector observes baseline execution JSON every 100 ms and retains its
receipt times/hashes. Publication and polling delay remain visible. Product
discovery is retrospective: its original `oracle_evaluate` span is mapped to
the discovered schedule and checked against the retained discovery fingerprint.
The saved discovery must be the final explored execution. A missing or different
candidate is a quality failure and cannot receive stable confirmation. An
exhausted search requires zero violation runs, no assertion violations, a
non-flaky outcome and exit zero; contradictory evidence cannot count as an
unexpected miss.
Unknown alignment uses the protocol's interval bounds; absent or ambiguous
timing stays unknown. Domain discovery, receipt of usable evidence, stable
confirmation and clean final completion are separate fields.

Requested steps are checked against their resolved trace events. Matching's
fixed path legitimately reports inferred database blocking and skips its final
insert point after a successful worker terminal, as established by the existing
paired acceptance test. Those events remain explicit counts; an unresolved
block, missing release or unexplained skip fails quality.

Statuses use operational failure, quality failure, budget exhausted, complete
precedence. Failed/slow values remain in per-slot JSON and the started-duration
statistics. Complete-pair cost summaries use paired differences/ratios, never a
difference of medians. B2 retains attempted/valid/violating counts, `x/m`,
`x/planned`, and missing exposure. Repeated executions are not independent host
samples. Results carry no confidence intervals or significance claims.

This infrastructure does not publish E5 measurements, update E1 figures or add
a README comparison table. Those follow the registered campaign and its audit.
