# Report schema reference

Every `weavegate run` — pass or violation — writes six base files to
`<out>/runs/<run_id>/`. A run that discovered or replayed a schedule writes a
seventh file, `schedule.json`; an exhausted exploration with no selected
schedule omits it.

```text
<out>/runs/<run_id>/
├── manifest.json      (volatile)
├── scenario.json       (deterministic)
├── observation.json    (deterministic)
├── trace.json          (deterministic)
├── report.json         (volatile)
├── report.md           (deterministic)
└── schedule.json       (deterministic; present only when scheduled)
```

Every run-scoped JSON document for an ordinary run carries
`"artifact_version": 2`. If diagnostic derivation fails after execution, all
run-scoped JSON in the retained directory carries `"artifact_version": 3`.
The portable `schedule.json` instead uses the standalone schedule contract
accepted by `--replay`, with only `id` and `steps`. The database schema identity
remains the separate `manifest.schema_version` field. Replay lookup reads the
v2/v3 neutral `schedule` field and the legacy v1 `violating_schedule` field.

## Artifact version policy

`artifact_version` changes for breaking schema changes: renaming or removing a
field, or changing what an existing field means. The v1-to-v2 change is the
precedent because `violating_schedule` was renamed to the neutral `schedule`
field. Adding a field does not bump the version; consumers must ignore fields
they do not know and tolerate their absence in older artifacts of the same
pre-release version.

The first release froze v2's compatibility baseline: existing names and
meanings require a version bump to change, while additive fields remain
compatible under the tolerance rule above. Version 3 is reserved for a run
retained after diagnostic derivation failed, preserving v2's meaning that an
empty diagnostics array followed successful derivation. The legacy v1
`violating_schedule` reader remains supported.

## Volatile vs. deterministic

Two files carry per-run identity and are expected to differ between two runs
of the same config: `manifest.json` (run ID, start time) and `report.json`
(a merge of manifest, scenario, and observation, so it inherits manifest's
volatile fields). The remaining five files, including `schedule.json` when a
schedule is present, are **byte-identical** for two runs given the same config
content and the same CLI flags. Two identical runs with different volatile
fields but identical deterministic files is the
expected, correct outcome — see
[docs/adr/0005-volatile-run-metadata-boundary.md](../adr/0005-volatile-run-metadata-boundary.md)
for why the boundary sits here rather than, say, keeping every file
deterministic or stripping timestamps everywhere.

## `manifest.json`

| Field | Type | Notes |
| --- | --- | --- |
| `artifact_version` | int | `2` for an ordinary run; `3` for a run retained after diagnostic derivation failed. The value is consistent across every run-scoped JSON file in one directory. |
| `run_id` | string | Opaque identity: `run_<YYYYMMDDTHHMMSS.nnnnnnnnnZ>_<32 lowercase hex>`. The timestamp is readable context and the suffix is 128 random bits; ordering comes from `started_at`, with run ID used only to break identical-time ties. |
| `started_at` | string (RFC3339, UTC) | |
| `weavegate_version` | string | The release linker value when built with `-ldflags "-X main.version=..."`; otherwise the module version embedded by `go install github.com/weavegate/weavegate/cmd/weavegate@<version>`, or `0.0.0-dev` for a source-checkout build. |
| `schema_version` | string | Full `sha256:<64 lowercase hex>` of the prepared migration snapshot. Each sorted file is framed as decimal byte length of name + LF, name bytes, decimal byte length of content + LF, content bytes. |
| `seed_data` | string | Full `sha256:<64 lowercase hex>` of the prepared seed bytes. |
| `isolation_level` | string | The literal result of `SELECT @@global.transaction_isolation`, read from the fixture's managing connection immediately after provisioning — the **global** value, not the session value a worker connection might override. If the SUT changes its session isolation level, this field will not reflect that. |
| `engine` | string | Distinct storage engines actually in use by the provisioned schema's tables, read from `information_schema.tables` immediately after provisioning and joined with `,` when more than one engine is present. Not assumed from the config or migrations. |
| `adapter`, `variant`, `image` | string | The resolved adapter, SUT variant, and database image for this run. |
| `sut_sha256` | string, optional | For `adapter: external`, `sha256:<64 lowercase hex>` of the selected executable JAR, checked at preflight and against a private snapshot used for each child launch. Absent for `gonative`. Its addition is compatible with v2/v3 under the additive-field policy. |
| `cleanup_failed` | bool | `true` when fixture teardown failed after this run completed (see [exit-codes.md](exit-codes.md#cleanup-failures-never-mask-a-verdict) for how this can raise a passing run's exit code to 4). It never changes `report.md`'s headline or `scenario.json`/`observation.json`'s content — those describe the scenario's own, deterministic outcome — but it records, on the run this actually happened to, why the process exit code can be higher than the headline alone would suggest. `false` on every ordinary run. |

Both digests come from the same immutable prepared source that `Provision` and
every `Reset` apply. The CLI does not re-read migration or seed paths while
building the manifest.

## `scenario.json`

The configured scenario (`name`, `workers`, `sync_points`, `params`) plus the
neutral `schedule` that actually ran: the schedule exploration discovered and
replayed, or the schedule a direct replay was given. The field makes no claim
that the schedule violated an oracle; `observation.json` contains the verdict
facts. The key is omitted when an exhausted exploration has no schedule.

`params` is the effective worker `args` from the config (every worker in a
scenario must declare the same `args`, so one map covers all of them; `{}`
when none are set). It is recorded here, not only in the config, so the
evidence still shows which parameters produced this verdict even if the
referenced config is later edited or deleted.

The example below is an ordinary version 2 run. A retained diagnostic failure
has the same scenario fields with `artifact_version` 3. Every field here is
`internal/report`'s own JSON contract, not the engine's
internal types re-serialized — the report package defines its own
`Worker`/`Schedule`/`CoordinationStep` shapes and converts into them, so this
schema stays independent from unrelated changes to the engine's internal
field names or casing:

```json
{
  "artifact_version": 2,
  "name": "concurrent-assign",
  "workers": [{"id": "w1", "command": "assign"}, {"id": "w2", "command": "assign"}],
  "sync_points": ["after_read_request", "before_insert_assignment"],
  "params": {"request_id": "42"},
  "schedule": {
    "id": "sch_7dcb74b1e506",
    "steps": [{"worker": "w1", "point": "after_read_request"}, "..."]
  }
}
```

## `schedule.json`

When `scenario.json` has a `schedule`, the run directory also contains that
schedule in the canonical standalone `{"id","steps"}` format accepted by
`weavegate run --replay <path>`. Its content-derived ID is verified while the
file is written. The file can therefore be copied on its own without carrying
the run-scoped `artifact_version` or the rest of `scenario.json`.

## `observation.json`

| Field | Type | Notes |
| --- | --- | --- |
| `artifact_version` | int | `2` for an ordinary run; `3` for a run retained after diagnostic derivation failed. |
| `mode` | string | `explore` or `replay`, describing how the schedule was selected. |
| `schedules_explored` | int | The number of candidates **actually evaluated**, not the total size of the candidate space. Explore mode stops at the first violation, so this is often smaller than the full candidate count. |
| `explore_passes` | int | How many full sweeps ran before stopping — 1 if a violation was found on the first pass, up to `run.explore_passes` if every pass exhausted its candidates. `0` in replay mode, where no exploration happens. |
| `assertion_violations` | array of objects | One entry per distinct violation found across the replay runs, plus exploration's own discovery run when replay never reproduced it (the 0/repeat flaky case — see `flaky` below). Each entry is `{"oracle_id": "...", "rows": [...]}` — `oracle_id` names the assertion, and `rows` is that oracle's own evidence rows, so a saved verdict shows *which* rows violated it, not only that it did. Each row is a plain object keyed by the query's column names, restricted to deterministic, JSON-safe scalar values (no `NaN`/`Inf`, no non-UTF-8 strings). Two violations sharing an `oracle_id` are only collapsed into one entry when their rows are also identical; a later run reproducing the same assertion with different rows is kept as a separate entry so each stays individually auditable. `[]` when nothing violated. |
| `diagnostics` | array of objects | Named verdict classifications derived from Oracle violation kinds and engine signals. Each entry contains `code`, `severity`, `title`, `observed`, optional `assertion`, `invariant`, `reason`, `help`, and `evidence` (`schedule_ref`, `rows`, optional `evidence_sets`, `trace`, optional `observation`). Declaration order is preserved and an engine-derived WG090 is last. Always emitted. In version 2, `[]` means derivation completed and no diagnostic applied; in version 3, `[]` identifies evidence retained after derivation failed. See [diagnostic references](diagnostics/WG001.md) and [exit codes](exit-codes.md#completed-execution-and-run-directories). |
| `oracles` | array of objects | Every configured assertion's effective `id`, `sql`, and `expect_rows` — the same fields as `oracle.assertions` in config, snapshotted here so a saved verdict stays auditable against the query that produced it even after the referenced config is edited or deleted. `[]` when the scenario declares none. |
| `repeat` | int | The effective repeat count used (config default or `--repeat` override). |
| `timeouts` | object | The effective arrive timeout and its four derived orchestrator deadlines, in milliseconds: `arrive_timeout_ms` (config default or resolved value), `block_inference_timeout_ms` (equal to `arrive_timeout_ms`), `step_timeout_ms` (20×), `run_timeout_ms` (60×), `stop_timeout_ms` (20×) — see [Timing](config.md#timing) for how these are derived. These deadlines affect terminal timing classifications and normalized fingerprints, and therefore potentially the `flaky` verdict, so they are snapshotted here even after the referenced config is edited or deleted. |
| `violation_runs` | int | How many of the `repeat` replay runs had at least one violation. |
| `flaky` | bool | See [exit-codes.md](exit-codes.md#the-flaky-determination). |
| `fingerprints` | object (string → int) | How many of the `repeat` replay runs produced each distinct normalized execution fingerprint. A run can be flaky when Oracle results, normalized trace events, or terminal state diverge, including with zero assertion violations anywhere, so this is the evidence of *why* such a run is flaky. One entry, equal to `repeat`, when every run agreed. |
| `discovery_fingerprint` | string, optional | The fingerprint of exploration's discovery run. Emitted only when exploration found a schedule; omitted for direct replay and exhausted exploration. Compare it with `fingerprints` to audit discovery/replay determinism. This field does not claim that both complete traces are preserved. |

`assertion_violations` preserves each original column name and value; JSON
encoding escapes control characters in the artifact. The separate Markdown
rendering rule below applies when those values reach `report.md`; diagnostic
production does not alter structured evidence for presentation safety.

Diagnostic evidence names every artifact that supports the diagnostic;
`schedule_ref` identifies the executed schedule and is not an artifact pointer.
An assertion diagnostic always points at `observation.json`, because its
violating rows exist in `assertion_violations`. It additionally points at
`trace.json` only when the saved trace comes from a run where that assertion
was violated. Engine-derived WG090 points only at `observation.json`, where
`fingerprints` and `discovery_fingerprint` record the determinism comparison;
one trace cannot demonstrate that comparison.

One assertion diagnostic represents one evidence set: its `observed` and
`rows` describe that set, and its artifact pointers name all saved evidence
that supports the assertion. When `evidence_sets` is greater than one, the
complete, individually auditable list remains in `assertion_violations`.

```json
"assertion_violations": [
  {
    "oracle_id": "active-assignment-is-unique",
    "rows": [{"project_request_id": 42, "active_assignment_count": 2}]
  }
]
```

**Fields not emitted yet:** `extra_rows`, `missing_rows`, `stale_rows`,
`constraint_violations`, `aborted_transactions`, `retries`, and
`recoverable` are not present in this file. They belong to oracles
(differential, schema-constraint, fault injection) that are not implemented.
They are omitted rather than filled with `0` or `[]`, because a `0` cannot be
told apart from "not measured" — printing them would claim a check that
never ran.

## `trace.json`

An ordinary version 2 trace has this shape; a retained diagnostic failure uses
the same fields with `artifact_version` 3:

```json
{"artifact_version": 2, "schedule_ref": "...", "events": [...], "terminals": [...]}
```

Like `scenario.json`, this is `internal/report`'s own `Event`/`WorkerTerminal`
shapes, converted from the engine's internal trace types rather than
re-serializing them directly.

The underlying trace model is deliberately wall-clock-free — there is no
`t_ms` field on an event. A timeline view that needs one is a future
`report --timeline` feature, not part of this schema.

The saved run is whichever run best supports the reported observation: a
replay run with an assertion violation when one exists; failing that,
exploration's own discovery run when replay never reproduced it (the
0/repeat flaky case); failing that, a replay run whose fingerprint
diverged from the others (a direct `--replay` can be flaky purely from
execution-fingerprint divergence, with zero assertion violations anywhere —
see `fingerprints` above); and only the first replay run when none of the
above found anything to show.

## `report.json`

`{"artifact_version": 2, "manifest": ..., "scenario": ..., "observation": ...}`
is the ordinary version 2 shape; a retained diagnostic failure uses version 3
at the top level and in each nested run-scoped document. The file merges the
three documents above for a reader who wants everything in a single fetch.
Because it inherits `manifest`'s volatile fields, it is not part of the
deterministic set even though `scenario` and `observation` alone would be.

## `report.md`

Every runtime string inserted into `report.md` passes through one boundary in
`internal/report`. Non-printable runes use Go escape spelling (`\n`, `\t`,
`\x1b`, `\u200b`), and malformed UTF-8 bytes use `\xNN` without being replaced
by U+FFFD. Active Markdown punctuation is backslash-escaped; dollar signs use
`\x24` because GitHub treats backslash-escaped dollar pairs as math. Intraword
underscores such as those in `sch_...` and column names remain unchanged;
delimiter underscores and leading list markers are escaped. The renderer itself
appends every physical newline. These rules apply equally to the summary, replay
command, and every diagnostic field, so a value cannot forge a report line,
emit terminal controls, or introduce Markdown structure. The JSON artifacts
retain the original structured values. See
[ADR 0011](../adr/0011-report-markdown-rendering-boundary.md) for the boundary
and its replay tradeoff.

```text
## weavegate: FAIL (WG001)
scenario: concurrent-assign | schedules explored: 1 | violating: sch_7dcb74b1e506
assertion: active-assignment-is-unique
flaky: false (repeat=20)
replay: weavegate run --config fixtures/matching-slice/.weavegate/config.yaml --scenario concurrent-assign --variant vulnerable --replay sch_7dcb74b1e506 --repeat 20

error[WG001]: invariant violated under a controlled schedule
  observed:  active-assignment-is-unique returned 1 row: active_assignment_count=2 project_request_id=42
  assertion: active-assignment-is-unique
  invariant: a declared state invariant must hold under every release schedule the database permits
  reason:    commonly a read-then-write path without a lock or a unique constraint
  help:      add a unique constraint on the contested key
             take a pessimistic lock (SELECT ... FOR UPDATE) before insert
             use an idempotency key on the write
  evidence:  schedule sch_7dcb74b1e506 · trace.json · observation.json · 1 violating row
```

The headline records the semantic verdict as `PASS`, `FAIL`, or `FLAKY`. On an
ordinary completed run these correspond to exit 0, 2, or 3, but the headline
does not mirror every process result: cleanup can raise PASS to exit 4, and
diagnostic derivation failure retains the semantic headline while exiting 5
(see [exit-codes.md](exit-codes.md)). A stable violation appends its first
violation code to `FAIL`. A flaky run appends WG090 to `FLAKY`, even when a
WG001 block precedes WG090 in the body, because the determinism failure takes
verdict priority. Every diagnostic is rendered below the unchanged summary
lines. An exhausted exploration with no diagnostic has the previous output
shape and no `replay:` line because there is no schedule to reproduce:

```text
## weavegate: PASS
scenario: concurrent-assign | schedules explored: 18 (exhausted) | violating: none
flaky: false (repeat=20)
```

A passing direct replay retains its neutral schedule and labels it
`replayed:`, never `violating:`. It also includes the runnable replay line:

```text
## weavegate: PASS
scenario: concurrent-assign | schedules explored: 0 | replayed: sch_7dcb74b1e506
flaky: false (repeat=20)
replay: weavegate run ...
```

The `replay:` line, when present, spells out every value it needs (`--config`
as the user passed it, `--scenario`, `--variant`, `--replay`, `--repeat`). The
command builder uses POSIX shell minimal quoting, which preserves whitespace,
single quotes, and shell metacharacters as literal argument data. When the
result needs no report-safety escapes, pasting it verbatim from the same
working directory reproduces the same verdict. `--config` is never normalized
to an absolute path, so this file stays byte-identical between two runs
regardless of where in the filesystem they happened to execute.

If any command text needs a control-character or Markdown escape, the rendered
line is a safe representation rather than a pasteable command. Use the original
argument values to rerun that case; removing backslashes from the report is not
a general reconstruction rule. This exception is necessary because a POSIX
shell preserves a newline-bearing argument with a literal newline, which would
violate the report's one-line boundary.

`--out` is deliberately absent from this line: it is not part of the
deterministic contract (two runs with different `--out` values must still
produce byte-identical `report.md`), so pasting the replay line uses `--out`'s
default (or whatever `--out` the paste is run with) rather than the value the
original run happened to use. A reader replaying from the same directory as
the original run — the common case — still finds the schedule through stage
① of `--replay` resolution. A reader without the original run directory can
place its `schedule.json` in `.weavegate/schedules/` and paste an unescaped
line unchanged. When replay uses a different `--out`, lookup examines that
output's `runs/` and `schedules/` before falling back to schedules embedded in
the entrypoint. See [cli.md](cli.md#--replay-resolution-order) for the canonical
resolution order.

## File and directory modes

Directory mode `0755` and file mode `0644` are upper bounds. The process
`umask` may remove permissions from those requested modes, and weavegate does
not widen the resulting permissions afterward. Writing uses a same-filesystem
temporary directory under `<out>/runs/` and an atomic rename, so a partial
failure during writing never leaves a half-written run directory behind. A
pre-existing destination run directory is an output error and is preserved;
on success, every artifact written for the run appears together.
