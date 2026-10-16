# ADR 0005: Volatile run metadata boundary

- Status: Accepted
- Date: 2026-08-16

## Context

A run directory is the CLI's evidence: `manifest.json`, `scenario.json`,
`observation.json`, `trace.json`, `report.json`, and `report.md`. The
determinism guarantee this project makes — the same schedule produces the
same verdict, every time — needs to be checkable at the file level, not just
asserted in prose. That means two runs of the same config and the same CLI
flags should be able to write byte-identical files, and a test can prove it
with a plain diff instead of a hand-written field-by-field comparison.

But a run also needs a place to record *which* run this was and *when* it
happened — a run ID and a start timestamp. Every file that carries either of
those can never be byte-identical across two runs by construction, no matter
how deterministic the underlying execution is. The two goals — file-level
determinism, and a durable record of run identity — pull in opposite
directions for any file that tries to do both.

## Decision

Two of the six files are volatile: `manifest.json` (it holds `run_id` and
`started_at` directly) and `report.json` (it merges manifest, scenario, and
observation into one file, so it inherits manifest's volatile fields by
construction). The remaining four — `scenario.json`, `observation.json`,
`trace.json`, and `report.md` — carry no run identity or timestamp, and are
byte-identical for two runs given identical config content and identical CLI
flags.

Two exceptions keep the deterministic set clean rather than shrinking it
further:

- `trace.json`'s event model was already wall-clock-free before this
  feature existed — there is no `t_ms` field to strip. A timeline view that
  needs wall-clock offsets is a `report --timeline` feature yet to be built,
  not part of this file.
- `report.md`'s `replay:` line derives `--config` from the value the user passed
  on the command line, never from a normalized absolute path. Normalizing it
  would make the file depend on the filesystem location a run happened to
  execute from, which is exactly the kind of incidental variation the
  deterministic set exists to exclude. ADR 0011 later defines the Markdown
  safety representation for values that need escaping.

## Consequences

- Run-stage intervals are additive `manifest.timings` metadata, also inherited
  by `report.json`. They use one local monotonic origin and identify nested
  spans, so consumers can distinguish inclusive work from exclusive wall
  coverage. The collection scope ends before artifact writing; a whole-process
  measurement remains the collector's responsibility. Timing never enters the
  five deterministic artifacts (including the later portable schedule file)
  or normalized execution fingerprints. See the
  [timing contract](../reference/report-schema.md#run-timings).
- A determinism regression test is a rerun-and-diff on four files, not a
  bespoke comparator: write the same run twice and `bytes.Equal` the
  deterministic four.
- `manifest.json` and `report.json` are excluded from that comparison by
  design — a test that expected them to match would be testing the wrong
  thing.
- A reader who wants "one file with everything" still has `report.json`; a
  reader who wants a byte-for-byte determinism check uses the other four.
  Nothing satisfies both properties in a single file.
- If a future format needs a single deterministic file with everything, that
  is a new artifact, not a change to `report.json`'s existing volatile
  contract.

## What didn't work

1. **Every file carries a timestamp.** This is the natural default and it
   is what makes a golden-file or rerun-diff comparison impossible for any
   of the six files — there would be nothing left to check but structure.
2. **Strip time from every file, including `manifest.json`.** This makes
   all six files comparable, but then nothing in the run directory records
   when the run happened, which is itself evidence a reader reasonably
   expects from a manifest.
3. **Keep `report.json` in the deterministic set by excluding manifest's
   volatile fields from the merge.** This was rejected because it defeats
   `report.json`'s reason for existing — a single file with the complete
   picture of a run, including *which* run it was. A deterministic
   `report.json` would just be `scenario.json` and `observation.json`
   concatenated under a different name.

Five-of-six was on the table before four-of-six: keeping `report.json`
deterministic and accepting that its `manifest` block silently omits
`run_id`/`started_at` while the standalone `manifest.json` keeps them. That
asymmetry — the same field present in one file and missing from its own
merge — was considered more confusing than a plainly-stated two-file
exception.
