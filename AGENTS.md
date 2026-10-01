# Repository working conventions

## Purpose

This file states the boundaries and invariants that anyone changing code in
this repository — a human contributor or a coding agent — is expected to
keep. `CONTRIBUTING.md` covers the procedure for proposing a change; this
file covers the judgment calls that procedure does not spell out. Read both;
see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution process.

## Repository shape

The engine is a set of Go packages, driven by the `weavegate` CLI in
`cmd/weavegate`.

| Package | Responsibility |
| --- | --- |
| `cmd/weavegate` | The `weavegate` CLI — `run` and `report` subcommands, config resolution, exit codes |
| `internal/config` | Loads and validates `.weavegate/config.yaml` |
| `internal/ci` | Maps a run outcome to a process exit code |
| `internal/report` | Writes the run directory artifacts and renders `report.md` |
| `internal/fixture` | Starts the database container, applies migrations and seed data, resets state between runs |
| `internal/sut` (+ `gonative`) | The adapter boundary for the workflow under test |
| `internal/syncpoint` | The sync-point runtime — arrive/release, blocked and terminal states |
| `internal/scenario` | Scenario definitions, schedule validation, candidate enumeration strategies |
| `internal/orchestrator` | Schedule execution, replay, repeat, exploration |
| `internal/oracle` (+ `sqlassert`) | Verdicts — judgment happens here, and only here |
| `internal/trace` | Normalized execution evidence |
| `fixtures/` | Scenarios expressed as data |
| `experiments/` | Standalone reproduction experiments |

Describe only what exists today. If you need to mention planned work, mark it
explicitly as planned rather than writing about it as if it already ran.

## Design invariants

- Fixtures are data, adapters implement a protocol, oracles compose.
- If adding a new fixture, oracle, schedule strategy, or adapter requires an
  engine change, that is a sign the design is wrong at that point — raise it
  as an issue instead of forcing the code to fit.
- Verdict logic does not live in the orchestrator or in a fixture. It lives
  in an oracle.

## Determinism rules

- The same schedule produces the same verdict, every time.
- Wait times (`sleep`) are not a coordination mechanism.
- Rule out probabilistic reproduction by repeating the run
  (`-count=N`), not by trusting a single pass.
- Do not weaken a check to make a failure go away.

## Evidence rules

- Tests emit fixed-phrase markers, and CI checks for them with `grep -F`. If
  a marker string changes, the workflow that checks for it changes in the
  same pull request.
- A quantitative claim about behavior ships with the command used to
  reproduce it.

## Documentation rules

- A change to a user-visible contract ships its documentation update in the
  same pull request.
- README content or visual changes follow the maintainer contract in
  [`docs/maintainers/readme-maintenance.md`](docs/maintainers/readme-maintenance.md),
  including its source-of-truth and partial-update rules.
- A diagnostic code and its reference page are one-to-one; a diagnostic code
  without a reference page does not merge.
- A design decision that cannot be inferred from the code alone gets a short
  note, not left implicit.
- Example output blocks in public documentation are real tool output. Volatile
  identifiers such as run IDs, timestamps, and temporary paths may be replaced;
  a block that is not captured output must be explicitly labeled illustrative
  or constructed.

## Before proposing a change

Run what applies to the change:

```bash
gofmt -l cmd internal fixtures experiments
go vet ./...
go build ./...
go test ./internal/... -count=1     # Docker required
go test ./fixtures/... -count=1     # Docker required
go test ./experiments/... -count=1  # Docker required
go test ./cmd/... -count=1          # Docker and JDK required for external CLI integration
go test ./cmd/... -short -count=1   # no Docker; skips the integration test
```

State plainly in the pull request which of these you ran and which you
skipped, and why.

## Out of bounds

- Weakening a check so that it passes instead of fixing what it caught.
- Adding a new dependency without a stated reason.
- Committing secrets or real service data.
- Describing an unverified capability in public documentation.
