# CLI reference

The `weavegate` binary has two subcommands: `run` reaches a verdict on a
configured scenario; `report` prints a saved run without re-rendering it.

## `weavegate run`

```text
weavegate run --config <path> --scenario <name> [--replay <id>] [--repeat <n>] [--variant <name>] [--out <dir>]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--config` | `.weavegate/config.yaml` | Path to the run configuration file. |
| `--scenario` | (required) | Scenario name declared in `scenarios:`. Required whether or not `--replay` is set — a replay still needs the scenario's worker and sync-point declarations to execute. |
| `--replay` | (unset) | Schedule ID or file to replay instead of exploring. See [Mode selection](#mode-selection). |
| `--repeat` | config's `run.repeat` | Overrides the configured repeat count. Must be positive if given; `--repeat 0` or a negative value is rejected (exit 5) rather than silently falling back to the config value. |
| `--variant` | config's `target.sut.variant` | Overrides the configured SUT variant. |
| `--out` | `.weavegate` | Run directory base. Every run — pass or violation — writes `<out>/runs/<run_id>/`. |

### Mode selection

Whether `--replay` is present decides the mode; there is no separate mode
flag.

- **Explore mode** (`--replay` absent): sweeps the scenario's candidate
  schedules, stops at the first violation, and replays it `--repeat` times to
  check whether the violation reproduces. If every pass exhausts its
  candidates with no violation, the run passes.
- **Replay mode** (`--replay` present): runs one saved schedule `--repeat`
  times without exploring.

### Preflight boundary

Before creating a fixture or starting a container, `run` loads exactly one
strict YAML document, resolves the scenario and variant, prepares an immutable
snapshot of the migration and seed sources, resolves and validates a replay,
or counts and builds the exploration candidate plan. Explicit empty flag
values and an exploration plan larger than 5,000 candidates are input errors
(exit 5) at this boundary, so none of these failures can provision a database.
For `adapter: external`, this boundary also resolves the Java executable and
prebuilt JAR, validates the JAR manifest, registration names and capacity,
and checks budget arithmetic. The child is launched only after fixture
provisioning. See the [external config keys](config.md#external-jvm).

### `--replay` resolution order

Exactly `sch_` followed by 12 lowercase hexadecimal characters is treated as
a schedule ID. Every other non-empty value is treated literally as a schedule
file path; path characters such as `[` or `*` are not glob patterns. IDs are
resolved in this order:

1. `<out>/runs/<run_id>/scenario.json` — the neutral `schedule` field of each
   saved run under `--out`. Legacy v1 `violating_schedule` fields remain
   readable.
2. `<out>/schedules/*.json` — standalone schedule files in the canonical
   `{"id","steps"}` format. The filename is not significant; each file's
   content-derived `id` is verified with strict JSON decoding.
3. The selected Go-native entrypoint's registered schedules, embedded in the
   binary at build time. This fallback therefore works outside a source
   checkout. The external adapter has no embedded schedules.

Each stage applies its own evidence rule:

| Stage | Candidate rule | Invalid evidence |
| --- | --- | --- |
| ① Saved run | Only directories whose names match the opaque run-ID grammar documented in [report-schema.md](report-schema.md) count as published run evidence. The run-scoped `scenario.json` reader remains tolerant of its other fields and accepts the v2/v3 `schedule` field and the legacy v1 `violating_schedule` field, but the stored steps must hash to the claimed schedule ID. | Temporary or staging directories, malformed run IDs, unreadable entries, and entries that do not prove their content ID are ignored; resolution continues to the next stage. |
| ② Portable file | Every `.json` file is decoded strictly and must prove its content-derived ID. | A malformed or unverified file is an input error (exit 5), not an entry to ignore. |
| ③ Embedded schedule | Every registered schedule is decoded strictly and must prove its content-derived ID. | Malformed or unverified registered content is an input error (exit 5). |

This asymmetry is deliberate: damage to one tool-written run must not block
every replay under that output directory, while a file placed by a person in
`<out>/schedules/` must not be silently ignored.

Resolution stops at the first stage that finds at least one match. Because
every candidate is verified against the requested ID, ambiguity requires two
different schedules to share the same 12-hex content ID; that collision is
rejected as ambiguous (exit 5) rather than guessed at. Damaged or tampered run
evidence is excluded from the candidates instead, and an ID with no remaining
source is an unresolved input error. The `matching-slice` entrypoint registers
only `sch_ba00582f9632`, so the example's discovered `sch_7dcb74b1e506`
resolves through its saved run directory in stage ① or through a copied
portable file in stage ②.

### `run` example

```console
$ weavegate run --config fixtures/matching-slice/.weavegate/config.yaml \
    --scenario concurrent-assign --variant vulnerable
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
.weavegate/runs/run_20260815T172917.706000000Z_bc391ac51234567890abcdef12345678
$ echo $?
2
```

The last stdout line before the exit is always the run directory path. The
`replay:` line spells out the complete command. When its values need no
report-safety escapes, pasting it verbatim from the same working directory
reproduces the same verdict. String arguments use POSIX shell minimal quoting,
so whitespace, single quotes, and shell metacharacters are preserved as
argument data rather than interpreted by the shell. If a control character or
Markdown delimiter requires an escape in `report.md`, the line is a safe
display form and must be reconstructed from the original arguments instead of
pasted. `--out` is deliberately
absent from it (see [`--replay` resolution order](#--replay-resolution-order)
above). The example uses the default `.weavegate` output for both commands. A
reader without the original run directory can place its `schedule.json` in
`.weavegate/schedules/` and, for an unescaped replay line, paste it unchanged.
Selecting a different `--out` searches that output's own `runs/` and
`schedules/` before falling back to the entrypoint's embedded schedules. See
[report-schema.md](report-schema.md) for the full field-by-field contract and
rendering rule.

## `weavegate report`

```text
weavegate report [run_id] [--format json|md] [--out <dir>]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `run_id` (positional) | most recent run under `--out` | Which saved run to print. |
| `--format` | `json` | `json` prints `report.json` verbatim; `md` prints `report.md` verbatim. Any other value exits 5. |
| `--out` | `.weavegate` | Run directory base to look under. |

The stored file is streamed to stdout unchanged — `report` never
re-renders. Omitting `run_id` selects the complete, valid run whose
`manifest.json.started_at` is greatest. A run ID is only a deterministic
tie-breaker for equal timestamps, not an ordering API. Temporary directories,
malformed IDs, incomplete runs, and manifests whose `run_id` does not match
their directory are ignored. An explicit ID must match the opaque run-ID
grammar documented in [report-schema.md](report-schema.md). A missing run,
invalid ID, or unknown format exits 5.

### `report` example

```console
$ weavegate report --format md
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

Because `report` streams the stored artifact, this diagnostic is the same block
written by `run`, not a second rendering. Longer explanations live under
[`docs/reference/diagnostics/`](diagnostics/WG001.md).

## Global behavior

- `weavegate` with no arguments prints help and exits 0.
- An unknown subcommand or an unknown flag exits 5 with a one-line message on
  stderr; Cobra's usage dump is suppressed so the error message stays short.
- Verdict output goes to stdout. `run` streams the stored `report.md` bytes,
  including WG diagnostic blocks such as `error[WG001]`, then prints the run
  directory path as its final line. `report` streams the artifact selected by
  `--format`: `report.json` by default, or `report.md` with `--format md`; WG
  diagnostic blocks are part of its Markdown output.
- Operational messages, including invalid input, fixture failures, and cleanup
  warnings, go to stderr.
- `weavegate --version` prints the build version and exits 0.
- The first SIGINT or SIGTERM cancels the run and begins bounded cleanup; a
  repeated signal uses the operating system's default disposition so the
  process can be terminated immediately.

## Current limits

- Only the built-in `gonative` adapter is supported; `target.sut.adapter`
  accepts no other value yet.
- Only the entrypoints registered in the binary can run — currently
  `matching-slice`. An external fixture cannot be selected from the CLI (see
  [config.md](config.md)).
- `report --timeline` does not exist yet; no flag reserves that name.
