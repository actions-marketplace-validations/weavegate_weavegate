# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`YYYY-MM-DD` in a release heading marks a section that has not been tagged yet.
Replacing it with the tag date is part of the release process, and the release
workflow fails before publication if the placeholder remains.

[Unreleased]: https://github.com/weavegate/weavegate/compare/v0.1.0-alpha...HEAD
[0.1.0-alpha]: https://github.com/weavegate/weavegate/releases/tag/v0.1.0-alpha

## [Unreleased]

### Added

- A composite GitHub Action at the repository root that gates a job with a
  published weavegate release. It verifies the release archive against that
  release's `checksums.txt`, runs the selected configuration and scenario,
  uploads the available evidence, and only then applies the CLI exit code. The
  step passes only for exit 0 with a complete version-2 PASS report and a
  successful evidence upload. `docs/howto/ci-gate.md` has the workflow and the
  input and output contract.
- The composite GitHub Action posts the run's stored `report.md` as a new pull
  request comment, unchanged and as literal text, with the evidence artifact
  link and the steps to import and replay the saved schedule. The `comment`
  input disables it, and `comment-outcome` and `comment-url` report the
  result. A report too large for a comment is replaced by an artifact pointer
  instead of being truncated. A comment that cannot be posted, as on a fork
  pull request or a job without `pull-requests: write`, never changes the gate
  result. `docs/howto/ci-gate.md` describes the wrapper and the permission.

### Changed

- Configuration-only CLI selection of an owned external JVM adapter, with
  launch, registration, and start-frame-size preflight, bounded startup/stop
  composition, and external JAR provenance in the run manifest. Each JVM
  launches a verified, self-contained JAR snapshot, and repeated sessions share
  one wire run ID. Preflight requires `Main-Class` in the manifest's main section
  and rejects a manifest `Class-Path` dependency; snapshot copying observes the
  run deadline.
- A completed run whose diagnostic derivation fails now retains its evidence as
  `artifact_version` 3 and exits 5. Ordinary runs remain version 2, preserving
  the released meaning that version 2 `diagnostics: []` means derivation
  completed and no diagnostic applied.
- `report.md` now renders every variable value through one Markdown safety
  boundary, which settles the rendering question the `0.1.0-alpha`
  compatibility notes left open. Non-printable runes and malformed UTF-8 bytes
  appear as escapes, and Markdown delimiters and dollar signs that would
  create markup, tables, references, or math are escaped, so a value can no
  longer split a field or introduce Markdown syntax. JSON artifacts retain
  valid UTF-8 values, but JSON encoding replaces malformed UTF-8 bytes with
  U+FFFD. A `replay:` line that needs no escape is still
  pasteable unchanged; one that contains an escape is a display form, and the
  command must be rebuilt from the original argument values.
  `docs/adr/0011-report-markdown-rendering-boundary.md` records the rule.

### Fixed

- A binary installed with
  `go install github.com/weavegate/weavegate/cmd/weavegate@<version>` now
  reports its module version in `weavegate --version` and in the run
  manifest's `weavegate_version` instead of `0.0.0-dev`. Release archives keep
  their linker-set version, and a source-checkout build still reports
  `0.0.0-dev`.

## [0.1.0-alpha] - 2026-09-02

### Added

- A deterministic sync-point runtime for coordinating worker execution.
- Exhaustive saved-schedule exploration and repeated replay.
- SQL assertion oracles that retain violating rows as evidence.
- `weavegate run` and `weavegate report` CLI commands.
- Six base run artifacts: manifest, scenario, observation, trace, JSON report,
  and Markdown report, plus a portable `schedule.json` when a run replays or
  discovers a schedule.
- Three-step replay lookup across saved run evidence, portable files under
  `<out>/schedules/`, and schedules built into the selected entrypoint, so an
  unchanged replay line can travel without its original run directory.
- `WG001` assertion-violation and `WG090` determinism diagnostics.

### Compatibility

This first tag fixes the following for `artifact_version` 2:

- The field names and meanings of `artifact_version` 2, as described in
  [`docs/adr/0007-artifact-version-policy.md`](https://github.com/weavegate/weavegate/blob/v0.1.0-alpha/docs/adr/0007-artifact-version-policy.md).
- The meaning of exit codes 0, 2, 3, 4, 5, and 130, as described in
  [`docs/reference/exit-codes.md`](https://github.com/weavegate/weavegate/blob/v0.1.0-alpha/docs/reference/exit-codes.md).
- The `WG` diagnostic namespace and the `WG001` and `WG090` codes.
- The versioned top-level directory that a release archive extracts into.

It does not fix, and a later release may change without a compatibility note:

- How `report.md` renders a variable field that has to stay on one line (#43).
- Whether a completed run keeps its artifacts when diagnostic derivation
  fails (#44).
