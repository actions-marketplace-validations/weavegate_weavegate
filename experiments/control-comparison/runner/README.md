# E5 execution and retained analysis

This implements the drivers for the [revision 1 protocol](../README.md), whose
published bytes remain unchanged. It is experiment infrastructure, not an E5
result or an E1 confirmation candidate. Measurement still requires the reviewed
E1 candidate, acceptance evidence and registered infrastructure. The optional
`E5 control feasibility` workflow runs only through `workflow_dispatch`; it is
not a required measurement gate.

The tools require Linux, `bubblewrap` (`bwrap`), Go from `go.mod`, Python 3.9+, Java 21 and Docker with
the MySQL 8.4 image already present. Python uses only its standard library. The
launcher compiles against the dependencies already inside each fixture JAR;
there is no new engine or SDK dependency. Execution requires mount/user namespace
permission, including for a launcher executed from a sealed memfd. CI uses `sudo`
for these experiment checks to accommodate the hosted image's pathname-based
user-namespace policy. Offline analysis does not create namespaces or need `bwrap`.

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
The `fixture_provision` interval includes the version/isolation query, initial
seed snapshot and explicit readiness check, and closes on every failure path
before teardown begins.
Each execution also retains `oracle_ends_ns`, keyed by assertion ID. A wrapper
records completion immediately after each successful Oracle call; the existing
Oracle set still owns declaration order, normalization, verdicts and fingerprints.
Baseline first-event bounds use the named domain assertion's endpoint rather
than `oracle_end_ns`, which includes any later receipt assertions.

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
redaction module's `plan.py` dependency. Runner and shared imports take precedence
over ambient Python packages and their resolved locations are checked before use,
including modules already loaded by Python startup code. Entrypoints execute the
local import guard by absolute path so a cached guard cannot replace it. Before
dependency imports, frozen source directories must contain exactly the registered
files and the `shared/` directory; caches, extra files and symlinks are rejected.
Runner and shared dependencies compile directly from `.py` bytes, ignoring cached
bytecode even with an external `PYTHONPYCACHEPREFIX`. `-B` alone only prevents cache
writes. These checks do not sandbox arbitrary Python startup code.
Freeze generates `source_inventory` in the manifest with the exact required
local/shared module names and SHA-256 digests. The public manifest hash therefore
binds the source bytes too. Collection and analysis reject missing, added or
changed modules, including when invoked from the frozen directory itself.
It generates exactly 120 blocks and 600 invocation slots with the protocol's hash ordering. Changed
counts, candidate order, settings or caps are rejected for measurement.
Every measurement T-R/T-E executable must hash to `candidate.cli_sha256`,
including the T-R command used for warm-up. An unused candidate pin does not
authenticate a command that invokes a different executable.

Freeze registers the `execution_runtime` path and digest for `bwrap` and retains
its bytes alongside input pins. Before every hook, warm-up or measured command,
the collector copies each applicable pin into a sealed memfd, then hashes those
sealed bytes. The launcher uses read-only copies in a private mount tree at the
original absolute paths, including files referenced inside configs. Input
directories such as classpaths and migrations contain only registered files;
adding a host file or replacing a file or ancestor after validation cannot change
the command's inputs. Cleanup snapshots only its trusted `execution_pins` subset.
Missing namespace support or a changed runtime fails closed.

Snapshot construction is preparation outside invocation timing; namespace setup
is inside the measured invocation. The collector preserves the original argv,
cwd and environment in its command receipt. The workload gets its own process
session: cap cancellation signals that session, allowing normal detached cleanup
and preserving its exit code; forced termination owns both it and the namespace
supervisor. Existing output directories are host mounts. Hooks write new retained
files under `{trial_dir}`, not beside frozen inputs in the private cwd tree.
Unpinned OS libraries, runtime files, services and output storage remain host
resources requiring the registered environment and independent audit. This is
input integrity, not isolation from hostile code or an operator controlling the
collector account. The read-only snapshot does not make an unlisted dependency
registered.

To prepare a measurement manifest, use the template's schema and replace its
development artifacts/commands with the actual E1 candidate and registered
infrastructure. Do this **before** freezing or running any warm-up. The local
preparer does not provision a VM, confer candidate status on a development build,
or manufacture successful prerequisite evidence.

Measurement inputs also have to match `fixture_contract.py`, an independent
reference generated from the protocol's immutable Git tree. It fixes the complete
effective config except deployment paths, image digest and variant, and fixes
migration/seed bytes and the compiled fixture class inventory. Changing inputs
and their manifest hashes together cannot replace this reference. These checks
run at freeze and live-input boundaries. Both launch paths must match the same
reviewed class inventory. Their complete non-SDK library names and digests must
match `REFERENCE_BUILD['libraries']` for the workload, including Spring, HikariCP
and Connector/J. The SDK has its own E1-registered digest; the reference compiler
SDK is not a substitute for that identity. Added, removed, duplicated or changed
classpath JARs and changed product `BOOT-INF/lib` inventories are rejected even
when every replacement has a new manifest pin. Product `BOOT-INF/classes` entries
must match the reviewed fixture inventory, so loose classes or resources cannot
override those libraries either.

The reference retains original source hashes, compiler identity/flags and build
library hashes. The following maintainer command reproduces it using the
preparer's extracted libraries and existing Go input exporter; it reads fixture
sources directly from the protocol commit, not the working tree:

```bash
python3 -B experiments/control-comparison/runner/record_fixture_contract.py \
  --baseline /tmp/e5-input/baseline --classpaths /tmp/e5-input \
  --javac "$(command -v javac)" --output /tmp/fixture_contract.py
diff -u experiments/control-comparison/runner/fixture_contract.py \
  /tmp/fixture_contract.py
```

Use the retained compiler/dependency identities for byte-identical regeneration.
If the eventual E1 SDK or a packaging change changes the compiled classes, rebuild
from the same immutable source and review the reference diff before freezing a
measurement campaign. A campaign manifest cannot nominate its own reference.
This reference build is not E1 registration or acceptance evidence.

| Manifest field | Required content |
| --- | --- |
| `schema`, `protocol_revision`, `phase`, `campaign_id`, `seed` | `1`, `1`, `measurement`, unique ASCII ID (`[A-Za-z0-9][A-Za-z0-9._-]*`), fixed protocol seed |
| `protocol` | Original protocol `path` and SHA-256; its published hash is checked |
| `fixture_revision` | The reviewed fixture commit named by the protocol |
| `pins` | Absolute path to SHA-256 for every binary, launcher, library, script, source, config, SQL, schedule and prerequisite file used |
| `source_inventory` | Generated by freeze: exact local/shared Python source names and SHA-256 digests, bound by the registered manifest hash |
| `inputs` | Each workload/variant's effective config, candidates, expected domain rows, counts, JAR/SDK and business-class hashes |
| `commands` | Every workload/variant and B0/B1/B2/T-R/T-E command, including exact `argv`, absolute `cwd`, explicit `env`, timeout and cleanup grace |
| `candidate`, `e1_registration` | Matching CLI/SDK digests, source revision and E1 public registration URL/revision |
| `environment` | `host_id`, `vm_image`, `cpu_model`, `kernel`, `docker`, `background_policy`, `mysql_image`, `vcpus: 4`, `memory_bytes: 17179869184` |
| `prerequisites` | Pinned completion-record paths for `go_acceptance`, `java_acceptance`, `paired_acceptance`, `corpus_acceptance`, `baseline_feasibility` |
| `hooks` | `prepare`, `warm_cleanup`, `observe`, `cleanup` command specifications; cleanup declares its trusted `execution_pins` subset |
| `unsupported` | Optional registered `workload/arm` to `{issue_url, reason}` object; planned denominators are retained |
| `deviations` | Optional list of nonempty limitation descriptions, retained verbatim in results JSON and escaped for the Markdown report |

An unsupported declaration requires a nonempty reason and an absolute HTTPS issue
URL with a host and path, without credentials or whitespace. Unknown workload/arm
keys, extra fields, boolean values and bare strings fail before freezing. The
same checks apply during collection and offline analysis; a consumed unsupported
slot must retain the exact declaration and cannot contain a measured command.
Issue availability and justification remain part of the independent audit.

Each prerequisite completion record contains `schema: 1`, its `kind`, the exact
`candidate` object (CLI/SDK digests and `source_revision`), `fixture_revision`,
and absolute `command` and `stdout` paths. The command path identifies an original
collector-style JSON receipt with `argv`, `started`, `finished`, `interrupted`,
`exit`, `timeout`, `error` and `containment_proven`. Only successful, finished,
contained commands qualify. The record, receipt, output and referenced originals
must all be pinned and retained before freeze. Candidate `source_revision` must
match E1 registration's `source_revision`.

Go, Java and paired records also name `results`, the original acceptance manifest.
Their command must invoke `check-external-sut-acceptance.py` with
`--results <that-path> --require-complete`; its output must report complete
acceptance for the correct target. The acceptance manifest must report that
candidate source revision, at least 20 repetitions, passing checks and
hash-matching original artifacts. Corpus output must contain all eight existing
schedule/variant result markers with repeat 20. Baseline feasibility output must
contain all six workload/variant result markers with 20 executions and the proxy,
inactive-hook, reset and cleanup checks. Freeze and live validation check these
records; offline analysis checks their retained bytes without accessing the
original machine.

These checks authenticate the recorded completion and identities, not the truth
of an externally supplied execution history. The existing acceptance checker
owns its complete check inventory, and independent audit still establishes
that the recorded commands actually ran against the registered candidate.
Baseline source and compiled artifacts are likewise reviewed and registered per
campaign before collection, as required by the protocol; no universal baseline
binary digest is established by revision 1.

Baseline counts are 20/100; T replay is 20 and exploration is three passes with
automatic confirmation. Freeze commands and all their transitive inputs.
Measurement validation checks literal argv/environment files, classpath
directories and Java `lib/*` entries, plus the effective config's Java executable,
JAR, migrations and seed. These checks repeat at live-input boundaries so adding
an unpinned class or library after freeze also stops collection. Baseline commands
declare exactly one explicit `--classpath` (fixture/launcher class directories,
reference dependency JARs or Java `lib/*`). Directory entries may contain only
the reviewed fixture classes and the two `BaselineMain` launcher classes. Extra
classes and resources cannot shadow the registered libraries.
The complete fixture class inventory and the SDK JAR on that classpath must match
`business_classes` and `sdk_sha256`; duplicate or shadow fixture/SDK classes fail
validation even when pinned. The application JAR must match `jar_sha256`, and its
embedded fixture classes and SDK must match those same identities. Measurement
configs must all use `environment.mysql_image`, including its SHA-256 digest.
Baseline and product receipts must retain that same image identity. Each measurement
command names exactly one explicit `--config` file, using JSON-subset YAML as emitted
by the preparer. Validation compares its entire decoded content with the effective
snapshot, including worker arguments, sync points and timeouts, at freeze and live
input boundaries. Every measurement command also declares exactly one `--out`
destination with the literal value `{trial_dir}/raw`, including the T-R warm-up
command. Single-dash and equals forms are checked too; missing, duplicate,
traversing or other-slot destinations fail before collection. Commands and hooks
require finite positive numeric `timeout_seconds` and, when supplied,
`cancel_grace_seconds`; booleans and numeric strings are rejected. Omitted
cancellation grace retains the shared executor's 25-second default.
Measurement baseline commands cannot use `--java`, `--jar` or
`--image` overrides (including single-dash and equals forms); those values must
come from the checked config. The preparer may still use overrides while exporting
that config before freeze. Scenario names and argument keys retain their original
spelling.
This restricted experiment input format lets the Python standard-library checker
use the same data accepted by the Go YAML loader without another parser dependency.
Literal absolute paths and explicit `./` or `../` input paths must already exist
and be pinned; a hook cannot reserve an absent script for preparation to generate.
Runtime output paths use the declared placeholders. Ordinary `PATH`, home and
temporary-directory environment settings are not interpreted as file inputs.
Infrastructure URLs are not local file paths. All live dependencies are checked
again before warm-up and warm cleanup, as well as before the other lifecycle
commands. Bare dynamic imports and subprocess lookup still require explicit
inventory and audit.
Dynamic imports, subprocess lookup and other runtime dependencies still require
explicit pins and audit. A
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
| `warm_cleanup` | After the one warm-up, `warm-cleanup.json`: `outputs_removed: true`, `data_removed: true`, `cache_preserved: true`, `warmup_command_sha256`, `startup_caches` |
| `observe` | `observation.json`: registered `host_id`, `resources`, original runtime/version/cache/counter evidence |
| `cleanup` | `cleanup.json`: `proven: true` plus newly retained proof of child exit, retired connections and fixture removal |

Original hook proof files live under `proof/` and must be newly retained by
their respective hooks. Before preparation, warm cleanup, observation and
final cleanup, the collector records existing file digests and device/inode
identities. A proof cannot reuse an earlier inode under a new pathname or use
a hard-link alias, including an alias to a file outside the slot. Each hook
must preserve the original files' bytes and identities. These checks detect
observable reuse; independently auditing whether a new file actually proves
the claimed operation remains necessary.
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

The warm cleanup receipt binds to the SHA-256 of that slot's original
`warmup/run.command.json`. Its `startup_caches` list explicitly records any
generated startup caches. Each entry names `path`, `key`, `size_bytes`, `sha256`,
`construction_seconds`, `invalidation`, and a `proof` path in the receipt's fresh
evidence inventory. Missing metadata, duplicate paths and references to another
warm-up are rejected before measurement. The inventory may be empty: revision 1
defines warm by the common priming invocation and preserved guest filesystem
cache, and does not require a generated startup-cache artifact. The trusted hook
and independent audit establish actual guest cache preservation.

`resources` contains exactly five counters: `cpu_seconds`, `memory_bytes`,
`container_creations`, `evidence_bytes` and `cache_bytes`. Each counter is an
object with either `value: null` and a nonempty `reason`, or a nonnegative finite
`value` and a nonempty `scope` identifying what was counted. Counts and byte
sizes are integers; CPU seconds may be fractional. Zero is an observed value,
never a replacement for an unavailable counter. CPU and memory scopes describe
the whole invocation, including Go, Java, MySQL and observers; if that aggregate
cannot be measured, disclose null and the reason.

Observed memory also declares `method: sampled` or `method: high_water_mark`.
Sampled memory requires `cadence_ms: 100` and a nonempty `limitation` describing
missed peaks between samples. A high-water counter has no sampling metadata.
The collector validates this schema before accepting the receipt, and offline
analysis repeats validation. Missing or malformed measurement resources make
the slot operationally failed; derived counters become null with a reason, and
the rejected receipt remains in the sealed originals. Feasibility collection
discloses unavailable counters explicitly.

The schema checks and hashes cannot independently establish that a VM was
restored, that a prerequisite command passed or that public registration was
published at the claimed time;
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
SIGTERM during collection uses the same catchable interruption as SIGINT, so
command execution terminates its process group before cleanup. Signals are
deferred before recording an active slot until its recovery handler owns it,
including unsupported slots. Cleanup and checkpoint writes defer signals until
the slot and progress seals exist. The collector then seals the interruption stop
before any further slot, preserving completed cleanup without running it again.
An interruption before allocation does not launch a command or cleanup hook.

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
Both collection and analysis compare the executing runner and shared module bytes
with `frozen/source` before processing slots. Use the retained tools for an older
bundle; a changed checkout cannot analyze it under the same campaign identity.
Analysis holds a shared campaign lock through validation, reduction and output
writing. It fails immediately while collection holds the exclusive lock;
concurrent analyses may use separate output directories.
Analysis opens each output ancestor through a retained no-follow parent
descriptor and creates missing directories relative to those handles. It retains
the entire chain through sealing, so replacing an ancestor before output creation
cannot redirect creation into the campaign. Both reports and their checksum
inventory are written through the output handle, with ancestor, output and
campaign pathname checks through sealing. Replacing an output pathname stops
publication without writing into or resealing its replacement. Report files use
atomic replacement so a substituted file symlink cannot overwrite originals.
Both operations also lock the campaign directory inode, so replacing `.lock` cannot
admit a competing collector or analyzer. Path identity checks detect replacement
at collection boundaries and before analysis output is written.
The collector also holds no-follow directory descriptors for frozen inputs,
progress, slots and the active slot. Command receipts and observer output use
the original directory handle, so replacing a pathname cannot redirect recovery
writes into another campaign area. A replaced active or control directory stops
collection with the existing unfinished checkpoint; it is never resealed or
retried. Offline validation also rejects symlinked bundle and slot directories.
New slots, warm-up directories, analysis directories and missing output parents are first opened in
a private random staging namespace. The empty staging inode is pinned before
Linux `renameat2(RENAME_NOREPLACE)` publishes its name relative to the retained
parent descriptor. No public pathname is reopened to acquire the new directory;
publication refuses even an existing empty destination and checks the published
inode before writing. Unsupported filesystems fail closed without a replacing
rename fallback. Staging bootstrap, like the collector process itself, requires
an account under the collector operator's control; it is not a sandbox against
another process with that account's full permissions.
The retained slot or warm-up inode identity also accompanies every command and
stream observer. Reopening its registered pathname must match that identity
before a receipt, stream record or command output can be written.
Every collection boundary compares the exact prior-slot directory set and each
slot's seal digest with the progress checkpoint. Deleting a slot, inserting one,
or rewriting and resealing earlier evidence stops collection before another
measured command; the checkpoint mapping must also match the collector's retained
in-memory mapping. Analysis and collection bind the frozen manifest and order
bytes to the retained public registration hashes, even if someone regenerates the
local frozen-directory seal.
Source inventory and bytes are rechecked at each boundary, and the complete
frozen seal must still match the collector's initial digest.
Analysis output must resolve outside the campaign, including through symlinks;
an output path within frozen inputs, progress or sealed slots is rejected before
creating files.
Completed analysis directories contain a `SHA256SUMS` inventory covering both
`results.json` and `report.md`; regeneration includes the same deterministic seal.
Both files retain the authenticated `source`: manifest and order digests, frozen
inventory digest, checkpoint digest, public registration, candidate CLI/SDK and
source identity, E1 registration, and fixture/source revisions when declared.
Feasibility keeps undeclared candidate/registration identities null. A repeated
campaign label cannot substitute for these hashes, and the checkpoint identifies
the exact collected prefix. The source also includes a shell-quoted replay
command and its argv using the frozen tool. Its deterministic sibling destination
`<bundle-name>-analysis-replay` must be new; select another new directory if it
already exists. After relocating a bundle, update the command paths accordingly.
Malformed command receipts and product/baseline JSON structures are operational slot failures and
remain in the 600-slot accounting, rather than aborting the entire reduction.
Command fields are validated before reduction: lifecycle flags are booleans,
duration is finite and nonnegative, exit is an integer or null, and error is text
or null. Invalid receipts retain `command_receipt_valid=false` and null command
metrics; an explicitly true `started` flag still contributes to started counts.
Product execution and fingerprint counts must be integers, not booleans or text.
The collection checkpoint applies the same command schema to measured, warm-up
and lifecycle-hook receipts before accepting process containment. A missing flag
cannot be treated as proof that a command never started, even when host cleanup
succeeded; malformed command evidence stops collection without a replacement.
Completed artifacts require exactly one command timing root with all spans
attached to it, nonnegative integer clock fields and a root duration no greater
than the finite outer invocation duration. Missing, duplicate or overlong roots
fail evidence validation. A killed
baseline with no final receipt still retains its published per-execution
observations and explicitly unavailable stage timing.
Baseline receipts must match the requested arm and variant. Product observations
must declare `replay` for T-R and `explore` for T-E, including warm-up. Evidence
from another slot or mode cannot count as a completed observation.
Baseline and product domain rows use the same string-normalized comparison
against the frozen expectation. Every measurement workload/variant requires the
nonempty, protocol-specific `expected_domain_rows`, including fixed controls.
Omission or an empty list is never a wildcard. Unexpected rows remain recorded
observations and make the invocation quality-failed.

The collector observes baseline execution JSON every 100 ms and retains its
receipt times/hashes. Publication and polling delay remain visible. Product
discovery is retrospective: its original `oracle_evaluate` span is mapped to
the discovered schedule and checked against the retained discovery fingerprint.
The saved discovery must be the final explored execution and retain a discovery
fingerprint and domain evidence, including for fixed slots. A clean saved schedule
cannot replace the fixed arm's three exhaustive passes. A missing or different
candidate is a quality failure and cannot receive stable confirmation. An
accepted discovery must occur within one to three registered exploration passes;
extra passes cannot contribute detection or confirmation observations. An
exhausted search requires zero violation runs, no assertion violations, an empty
confirmation fingerprint map, no discovery fingerprint, a non-flaky outcome and
exit zero; contradictory evidence cannot count as an unexpected miss.
When confirmation reproduces zero violations, the CLI retains the original
discovery's assertion rows. That mapped discovery keeps its first-event bound
even though its fingerprint is absent from confirmation and the outcome is flaky.
When violating replays have different fingerprints, attribution remains ambiguous
and timing stays unknown, as the protocol requires.
Unknown alignment uses the protocol's interval bounds; absent or ambiguous
timing stays unknown. Domain discovery, receipt of usable evidence, stable
confirmation and clean final completion are separate fields.
Cell `stable_confirmations` counts validated confirmation blocks even if later
observation or cleanup fails; status counts retain that operational failure.
Valid product receipt timing and in-budget detection likewise survive those later
failures; malformed receipt times or invalid evidence never enter that numerator.
A structurally valid, fully reproduced fixed-arm domain violation also retains
its detection and receipt timing while the unexpected outcome makes the slot
quality-failed. Receipt validity is separate from expected-outcome classification;
this does not admit inconsistent counts, traces, domain rows or execution stages.
Inconsistent domain rows, execution stages or traversal evidence invalidate
confirmation instead of entering that numerator.
Command receipts retain `collector_start_ns`, the collector monotonic origin used
for invocation duration. Product validation records its elapsed time from that
same origin as a zero-width interval; validation after the cap is late even when
the process exited within budget. Baseline stream receipts retain their collector
origin and exact receive offsets after JSON reading and hashing; offline analysis
checks their envelope, hashes and execution evidence before mapping them to that
same invocation origin. Collector origins are never compared with child clocks.
Invalid receipt times fail the slot while retaining the planned accounting.
The detection numerator requires both an in-budget domain event and an in-budget
usable-evidence endpoint. Baselines use the first valid violating execution's
stream receipt; product arms use the final validated receipt. Late or unknown
receipts remain in the per-slot evidence but do not count as in-budget detections.
An incomplete baseline execution cannot supply the first valid domain-event time.

Requested steps are checked against their resolved trace events. Matching's
fixed path legitimately reports inferred database blocking and skips its final
insert point after a successful worker terminal, as established by the existing
paired acceptance test. Those events remain explicit counts; an unresolved
block, missing release or unexplained skip fails quality. Successful terminal
records must identify each scheduled worker exactly once. Missing, duplicated
or foreign worker IDs fail quality even when the schedule steps resolve.

Preparation-failed slots retain the preparation error, cleanup error, integrity
errors and interruption/unproven-cleanup reasons even if no measured command
started. Unsupported slots also retain lifecycle failures alongside their
declared unsupported reason. Cleanup errors after a measured command remain
operational failures with their original detail.
If any preparation, warm-up, measured, observation or cleanup command cannot prove
process-group termination, the campaign stops even when a later allocation
cleanup receipt succeeds. The original command receipt and containment reason
remain available to offline analysis, and no later slot can reuse that allocation.

Registration defers SIGINT/SIGTERM until its retained registration and progress
checksum agree. An interruption at that boundary leaves all slots unstarted,
and the same registration can resume collection. Slot activation and final
cleanup likewise defer signals until their recovery checkpoints are sealed.

Statuses use operational failure, quality failure, budget exhausted, complete
precedence. Every nonzero baseline exit outside a collection timeout is an
operational failure, including B0, even when its inner receipt has no error. All
measured commands must have finished without interruption. These failures retain
earlier domain observations and first-event bounds; clean cap cancellation keeps
its budget-exhausted classification. Failed/slow values remain in per-slot JSON
and the started-duration statistics. When cancellation is active, a reaped JVM
terminated by SIGTERM or HotSpot's normal exit 143 is expected cancellation.
Forced kills, reap deadlines, unrelated exits and connection/cleanup failures
remain errors even when cancellation is active. A contained, finished product
command that exits 130 solely on the collection cap may have no published run. Its absence is
explicitly `collection_cap_before_publication`; independent allocation cleanup
proof is still required. Partial published runs, unrelated exits, malformed
evidence and unproven cleanup remain operational failures.
The same absence classification applies to a baseline terminated by TERM or KILL
at its collection cap before the deferred final receipt is published, with
finished command containment and independent allocation cleanup proven. Driver
cleanup remains unproven in the row; completed execution receipts still count
toward baseline exposure. Ordinary exit codes, malformed final receipts and
execution errors do not qualify. T-R requires zero explored schedules and zero
exploration passes; exploration evidence cannot enter replay cost summaries.
Complete-pair cost summaries use paired differences/ratios, never a
difference of medians. B2 retains attempted/valid/violating counts, `x/m`,
`x/planned`, and missing exposure. Repeated executions are not independent host
samples. Results carry no confidence intervals or significance claims.
The report's `Baseline exposure / 10` column shows B1/B2 blocks with at least one
valid evaluation beside the planned detection denominator. Zero usable exposure
is therefore visible even when the detection count is also zero; other arms show
`N/A` in this baseline-only column.
The search report separates B2 exposure from T-E's actual explored-candidate
count, `violation_runs/20` confirmation result, fingerprint counts and selected
schedule. Feasibility reports use their declared confirmation count. Exhausted
searches explicitly show that confirmation did not run; zero or partial violating
confirmations remain visible. Stable confirmation requires all planned replays
to violate with one matching fingerprint, the requested schedule and a valid
worker trace, independently of later cleanup or operational failures.
Stage summaries include `remainder`, one scalar per complete slot: invocation
duration minus the union of measured stage intervals. It appears with median,
p90, minimum, maximum and sample count in JSON and the timing table. B1 and T-R
amortized costs each use their own declared execution count, including feasibility
runs with different counts.
T-E uses `evaluated_executions`, the validated count of actual search plus
confirmation executions, including exhausted searches with no confirmation.
JSON and Markdown summarize this descriptive cost per evaluated execution with
valid count, median, p90, minimum and maximum, using complete slots only. It is
not equal-work throughput. Both outputs also publish the frozen campaign's
registered deviations, including feasibility's unestablished VM and cache strata.

This infrastructure does not publish E5 measurements, update E1 figures or add
a README comparison table. Those follow the registered campaign and its audit.
