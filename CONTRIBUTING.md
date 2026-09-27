# Contributing to weavegate

Thanks for your interest in weavegate. This guide covers how to report a bug,
propose a feature, contribute a fixture, set up the development environment,
run the checks, and structure commits and pull requests.

## Ways to contribute

- **Report a bug** — open a bug report issue. Include the commit SHA and the
  steps needed to reproduce the behavior.
- **Propose a feature** — open a general issue describing the capability and
  its purpose.
- **Add a fixture (recommended)** — open a fixture contribution issue. See
  [Contributing a fixture](#contributing-a-fixture) below; this is the
  entry point that needs the least context about the rest of the codebase.
- **Improve documentation** — open a general issue first, same as a feature
  proposal. Every pull request in this repository, including a small
  documentation fix, needs an issue number for its branch name and commit
  message (see [Commits and branches](#commits-and-branches)).

## Development environment

The supported Go versions are the minimum Go 1.25.0 toolchain declared in
`go.mod` and the current stable Go toolchain. The smoke workflow's `toolchain`
matrix builds the repository and runs the non-container package set with both.

Raising the supported minimum is one contract change: update `go.mod` (which
moves the matrix entry that uses `go-version-file`), the matrix lower-bound
expectation, and the README source-build prerequisite in the same pull request.
The README coverage badge is also generated evidence, not a hand-edited number;
after producing `cov.out`, refresh it with
`bash scripts/coverage-badge.sh --write`.

Running the fixture, engine, and experiment tests requires Docker, because
they start a real MySQL 8.4 container through Testcontainers. Without Docker
you can still format, vet, and build the code; you cannot run `go test`
against `internal/...`, `fixtures/...`, or `experiments/...`.

`cmd/weavegate`'s Docker-backed integration test follows the same rule, but
`internal/config`, `internal/ci`, and `internal/report` run without Docker,
and `go test ./cmd/... -short` skips the integration test so the rest of the
package's tests — config resolution, exit codes, the root command — still
run without Docker.

## Running checks

Only run commands that exist in this repository:

```bash
gofmt -l cmd internal fixtures experiments
go vet ./...
go build ./...
go test ./internal/... -count=1     # Docker required
go test ./fixtures/... -count=1     # Docker required
go test ./experiments/... -count=1  # Docker required
go test ./cmd/... -count=1          # Docker required
go test ./cmd/... -short -count=1   # no Docker; skips the integration test
```

Changes to the Go external peer also run its repeated, event-coordinated tests
and evidence recorder checks (no Docker required):

```bash
go test ./internal/sut/external -v -count=20
go test ./internal/sut/external -race -count=20
python3 scripts/test-external-sut-go-results.py
```

The [Go adapter reference](docs/reference/external-sut-go.md) explains how to
publish the generated acceptance manifest. Its missing observers remain
incomplete; passing package tests does not complete the external implementation
gate or authorize fixture reuse after uncertain cleanup.

Changes to the proposed external SUT protocol or its shared vectors also run
the standard-library Python 3.9+ guard (Python is needed for this maintainer/CI
check only; it is not an engine or adapter dependency):

```bash
python3 scripts/check-external-sut-vectors.py
python3 scripts/check-external-sut-vectors.py --self-test
python3 scripts/test-external-sut-acceptance.py
```

These commands check constructed data and the declared role/lifecycle coverage,
not a running Go/Java protocol implementation. The smoke docs job runs them and
checks their fixed result markers. See the
[conformance plan](docs/reference/external-sut-conformance.md) for scope, input/output
rules, and the implementation evidence still required.
The acceptance-accounting test verifies the pinned revision in local Git history;
use a full-history checkout for that check. It tests manifest completeness and
evidence references, not an external adapter. See the
[acceptance manifest contract](docs/reference/external-sut-acceptance.md) for
per-target inventories and the separate `--require-complete` implementation gate.

Changes to the Java Spring peer in `sdk/java` run its checksum-pinned Maven
wrapper with Java 21. Docker is required for its MySQL and child-process tests.
The recorder test needs only Python:

```bash
(cd sdk/java && ./mvnw -B verify)
(cd sdk/java && ./mvnw -B verify -Dweavegate.repetitions=20)  # acceptance evidence
python3 scripts/test-external-sut-java-results.py
```

See the [Java peer reference](docs/reference/external-sut-java.md) for recording
a result manifest from the repeated run.

## Determinism and evidence rules

- A test that claims something about engine behavior must produce the same
  result on repeated runs. Rule out probabilistic reproduction with
  `-count=N`, not a single lucky pass.
- Tests emit fixed-phrase markers that the smoke workflow checks with
  `grep -F`. If you change a marker string, update the workflow in the same
  pull request.
- Do not make a check pass by weakening it — widening a wait, deleting an
  assertion, or loosening a tolerance to hide a failure is not an acceptable
  fix.

## Commits and branches

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  and end with the issue number, for example
  `feat(scenario): persist discovered schedules #28`.
- Branch names follow `feat<issue>/<slug>`, `fix<issue>/<slug>`, or
  `docs<issue>/<slug>`.
- Keep history readable. Avoid squashing a branch's work into a single commit
  once it has already been reviewed in pieces.
- A pull request that pins a commit from its own branch in the external-SUT
  acceptance plan must use a merge commit. Squash and rebase merges discard the
  pinned commit's ancestry, so a later full-history checkout cannot verify it.

## Pull requests

Keep the pull request template's section order as it is. In the `Validation`
section, record only the checks you actually ran, marked `PASS`, `FAIL`, or
`SKIP` with a reason. Do not mark a check you did not run as passing.

## Responding to review feedback

Write each review reply so it is concise and understandable without reopening
the full thread. Start with one of these decision tags and an outcome-oriented
summary:

- `[Accepted]` means the feedback was implemented. Link the pushed commit, then
  use about three bullets for the problem, the change, and the evidence or
  behavior preserved.
- `[Partially accepted]` means the concern was addressed with narrower scope or
  a different implementation. Link the pushed commit, then cover what changed,
  which boundary remains and why, and the evidence or remaining tradeoff.
- `[Rejected]` means no implementation change was made. Use about three bullets
  for the reason, the governing contract or invariant, and the tradeoff or
  follow-up path.

Resolve the thread only after posting the reply and, for accepted or partially
accepted feedback, after the linked commit is pushed and accessible. This is a
project convention; GitHub does not enforce it.

### Accepted example

[Accepted] Release signal notification before cancellation

in commit [6d960e8](https://github.com/weavegate/weavegate/commit/6d960e89506e25e54d4651d5c0001f8f7c24b113)

- Signal notification now stops before cancellation, restoring the default
  disposition while detached cleanup runs.
- The normal-return path also stops notification before `os.Exit`; first-signal
  exit 130 behavior is unchanged.
- Injected signal seams verify the ordering, and the reference marker records
  `second_signal=default`.

### Partially accepted example

[Partially accepted] Bound pool closes without reordering teardown

in commit [50064de](https://github.com/weavegate/weavegate/commit/50064de4b8b11151e07c702b2e557c092eff6928)

- The teardown deadline no longer depends on `sql.DB.Close` implementation
  details; both pool closes now use a deadline-aware helper.
- Pool-close attempts still start before container termination to avoid driver
  errors; after a timeout, teardown proceeds without waiting for closure, so
  server-first reordering was not adopted.
- A blocking stub driver verifies the bound and subsequent termination; the
  close-attempt budget allows clean shutdown when possible without delaying
  container cleanup indefinitely.

### Rejected example

[Rejected] Keep one opaque run-ID grammar because no legacy CLI shipped

- The project has no released predecessor or committed legacy run directories;
  the older form existed only in unreleased branch history.
- v1 compatibility covers artifact content, and replay does not apply the
  report command's run-ID grammar.
- A second grammar would widen the path-validation surface; a documented
  migration remains the follow-up if pre-1.0 IDs ever need support.

## Documentation is part of the change

If a pull request changes a user-visible contract — a diagnostic code, a
report or trace format, or a public interface — the [documentation](docs/README.md) update for
that contract ships in the same pull request. Diagnostic codes and their
reference pages are one-to-one: a diagnostic code without a reference page
does not merge. The pull request template's `Docs` section is the checkpoint
for this rule.

A pull request that adds a Markdown page under `docs/` must include one
corresponding top-level line in `docs/README.md` with the form
`- [title](path.md) — description`; the smoke workflow enforces this inventory
rule, including a nonempty description. A direct Markdown list link with the
wrong separator or indentation is rejected as a malformed entry. Reference-style
links and links that occur mid-line are outside the index entry contract and do
not satisfy the inventory requirement. Percent escapes in destinations are
decoded, so tracked Markdown page paths under `docs/` must not contain a literal
`%`, which would make encoded and literal destinations ambiguous. Tracked page
paths must not contain ASCII control characters, which cannot be represented by
the guard's one-line index-entry contract. CommonMark backslash escapes in link
destinations are normalized before comparison; percent-encode a literal
backslash when it precedes escapable punctuation in a tracked filename.

The diagnostic prefix is `WG`; every new code ships with a matching
`docs/reference/diagnostics/WGxxx.md` page.

## Contributing a fixture

A fixture is data, not engine code. If adding a fixture seems to require an
engine change, that is not a fixture problem — it is an extension-point gap,
and it should be discussed in an issue before writing code.

The layout below reflects what exists today in `fixtures/matching-slice/`,
which you can use as a reference implementation:

| Path | Role |
| --- | --- |
| `fixtures/<name>/README.md` | The domain and the invariant it verifies |
| `fixtures/<name>/db/migration/*.sql` | Synthetic schema |
| `fixtures/<name>/db/seed.sql` | Seed data |
| `fixtures/<name>/schedules/*.json` | Saved coordination schedules (content-addressed) |
| `fixtures/<name>/sut/` | A Go-native system-under-test, with both the vulnerable and the fixed code path |
| `fixtures/<name>/*_test.go` | Fixture lifecycle test — schema, seed, and reset behavior |
| `fixtures/<name>/sut/*_test.go` | The oracle declaration and the reproduction evidence |

Rules:

- Schema and data must be synthetic. Do not add a real service's schema or
  real data.
- See [`fixtures/matching-slice/README.md`](fixtures/matching-slice/README.md)
  for a worked example of the domain and invariant description this layout
  expects.

## Releasing

Before tagging a release, the person creating it must complete this checklist
manually. The release workflow mechanically verifies the date substitution in
item 5; the behavioral and content checks remain manual because the release
decision requires a human to inspect the complete behavior.

1. Build the CLI from the exact commit to be tagged.
2. Replay the vulnerable matching-slice schedule and confirm the expected
   `WG001` diagnostic, violating rows, trace, and exit 2.
3. Replay that same schedule against the `SELECT ... FOR UPDATE` variant and
   confirm PASS with exit 0. This is the required vulnerable → diagnostic → fix
   → PASS inspection.
4. Regenerate `NOTICE` with `./scripts/gen-notice.sh`, as recorded in its
   header. If the file changes, commit the updated inventory in the same pull
   request; otherwise, continue with the existing inventory.
5. Replace the `YYYY-MM-DD` placeholder in the matching
   [`CHANGELOG.md`](CHANGELOG.md) release heading with the actual tag date. The
   release workflow verifies the substitution and fails before publishing
   anything if it is missing. Manually verify that the section contains only
   changes already merged into the tag.
6. Read the README as it will appear in the tagged archive and confirm that
   every release-status statement remains true after the tag is published.
7. Create a dry-run archive with
   `goreleaser release --snapshot --clean --skip=publish`, inspect it with
   `tar -tzf`, and extract it. Confirm that extraction creates one top-level
   directory, the README release-archive installation commands work from inside
   it, and the bundled `fixtures/matching-slice/.weavegate/config.yaml` path
   matches the README run example.
8. Confirm that the release workflow will use that CHANGELOG section as its
   release notes, then create the tag manually. The workflow publishes that
   section as the release body and verifies the remote body before succeeding;
   retries replace the body from CHANGELOG, so manual edits are not the source
   of truth. Do not tag if any earlier item is incomplete.
9. After the tag and release exist, remove their two temporary entries from
   `.lycheeignore` so the compare and release URLs return to external-link
   validation.
10. After the tag and release artifacts exist, add the release badge and
   CHANGELOG link to the README. Never advertise a release that has not been
   published.

## License

By contributing, you agree that your contributions are licensed under the
same [Apache-2.0 license](LICENSE) that covers the rest of the project.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Repository working conventions

For the design invariants, package boundaries, and other rules that apply to
any change to this repository, see `AGENTS.md` at the repository root.
