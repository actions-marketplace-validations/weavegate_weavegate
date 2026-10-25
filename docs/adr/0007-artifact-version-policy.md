# ADR 0007: Artifact version policy

- Status: Accepted
- Date: 2026-08-19

> Amended by [ADR 0012](0012-diagnostic-derivation-failure-evidence.md): a
> completed run whose diagnostic derivation fails is also written with
> `"diagnostics": []` and exits 5, but its run-scoped JSON uses
> `artifact_version` 3. Version 2's released empty-array meaning remains
> unchanged.

## Context

The diagnostic feature adds `diagnostics` to every newly written
`observation.json` and to the observation nested in `report.json`. A review
asked whether that always-emitted field required increasing
`artifact_version`. The repository documented that current files use version
2 and that the reader accepts the legacy version 1 schedule field, but it did
not define what kind of change the version number signals.

This matters because version 2 predates the first release. Earlier development
artifacts can omit fields that current version 2 writers always emit, including
`oracles`, `timeouts`, and now `diagnostics`. Treating every addition as a new
version would make the number track ordinary implementation progress rather
than compatibility boundaries.

## Decision

Increase `artifact_version` only for a breaking schema change: renaming or
removing a field, or changing the meaning of an existing field. The move from
version 1 to version 2 is the precedent because `violating_schedule` became the
neutral `schedule` field.

Adding a field does not increase the version. Consumers must ignore unknown
fields and tolerate an additive field being absent from an older artifact of
the same pre-release version. Version 2 remains an in-progress format until
the first release, which freezes its compatibility baseline. After that
release, existing field names and meanings cannot change without a version
bump; additive fields remain compatible under the same tolerance rule.

## Consequences

- `diagnostics` remains an additive version 2 field, and the writer continues
  to emit `"diagnostics": []` when derivation completed and no diagnostic
  applied.
- A run retained after diagnostic derivation fails uses artifact version 3, so
  it does not broaden the released version 2 empty-array meaning.
- Pre-release version 2 directories can have different field sets. There is no
  released consumer of the older shape, so this does not break a published
  compatibility promise.
- The version number remains a useful signal for readers that need migration
  logic instead of increasing for routine additions.
- The existing reader continues to accept version 1 `violating_schedule` while
  newly written version 2 and 3 artifacts use the neutral `schedule` field.
- Optional `manifest.timings` adds volatile performance intervals under the
  same v2/v3 policy. Older artifacts may omit it, and no existing field's
  meaning changes. Its addition does not change deterministic artifacts or
  fingerprint inputs; see [run timings](../reference/report-schema.md#run-timings).
- Optional `manifest.sut_jvm_tuning` records the external launch policy under
  the same additive v2/v3 rule. Its absence in an older artifact does not
  identify that artifact's compiler policy. It is not a fingerprint input.

## What didn't work

Increasing the version for every added field was considered and rejected. It
would advance the format to version 5 or 6 through ordinary pre-release work
and turn the number into development noise rather than a breaking-change
signal.

There is a real counter-argument. This project treats omission and an explicit
empty array as different facts: an absent `diagnostics` field cannot prove that
diagnostics were derived and found empty, while `"diagnostics": []` can. An
older version 2 artifact and a current version 2 artifact therefore cannot make
that distinction from the version number alone.

That objection did not fail because the distinction was unimportant. It was
accepted because no released consumer of the earlier version 2 shape existed
at the time. The first release subsequently froze v2 as a public contract, so
ADR 0012 uses version 3 rather than giving v2's empty array another meaning.
