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

### Changed

- Configuration-only CLI selection of an owned external JVM adapter, with
  launch and registration preflight, bounded startup/stop composition, and
  external JAR provenance in the run manifest.
- A completed run whose diagnostic derivation fails now retains its evidence as
  `artifact_version` 3 and exits 5. Ordinary runs remain version 2, preserving
  the released meaning that version 2 `diagnostics: []` means derivation
  completed and no diagnostic applied.

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
