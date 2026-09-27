# Gate a GitHub Actions job with weavegate

The repository root contains a [composite action](../../action.yml) that runs a
published weavegate CLI release on a Linux runner with Docker. It verifies the
release archive against that release's `checksums.txt`, runs the selected
configuration and scenario, uploads the available evidence, writes a job
summary, and only then decides whether the step passes. The action needs no
write permission or secret. Use an ordinary `pull_request` workflow; this
example does not use `pull_request_target`.

The following workflow is runnable in this repository. It uses the committed
[matching-slice configuration](../../fixtures/matching-slice/.weavegate/config.yaml)
and its `concurrent-assign` scenario. The action is pinned to a commit that
contains `action.yml`; the CLI release is selected separately. The pinned
commit belongs to the change that introduced this action, so that change must
be merged with a merge commit to keep the referenced SHA in `main`'s history.
If the action is later changed, pin a reviewed commit containing that change.

```yaml
name: weavegate gate
on: pull_request
permissions:
  contents: read
jobs:
  gate:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v7
      - id: weavegate
        uses: weavegate/weavegate@afc6b4fa493602bd64b6401c5bae0c6f7bf108b2
        with:
          version: v0.1.0-alpha
          config: fixtures/matching-slice/.weavegate/config.yaml
          scenario: concurrent-assign
          variant: vulnerable
          replay: sch_ba00582f9632
          artifact-name: weavegate-evidence
      - name: Read outputs even when the gate fails
        if: always()
        env:
          EXIT_CODE: ${{ steps.weavegate.outputs.exit-code }}
          VERDICT: ${{ steps.weavegate.outputs.verdict }}
          ARTIFACT_URL: ${{ steps.weavegate.outputs.artifact-url }}
        run: printf 'exit=%s verdict=%s artifact=%s\n' "$EXIT_CODE" "$VERDICT" "$ARTIFACT_URL"
```

This example deliberately gates on the vulnerable variant: a reproduced
`WG001` yields process exit 2 and fails the job after uploading its evidence.
Change `variant` to `fixed` to replay the same schedule and get exit 0 and a
passing job. The smoke workflow's `reusable-gate` job runs both cases. A runner
must provide a working Docker daemon so Testcontainers can start MySQL 8.4.
The current published binary has the built-in `matching-slice` Go adapter;
other application adapters are not available through this release.

## Inputs, outputs, and gate policy

| Input | Use |
| --- | --- |
| `version` | Required published release tag, such as `v0.1.0-alpha`. The action chooses the runner's Linux architecture and verifies the archive SHA-256 against `checksums.txt`. |
| `config`, `scenario` | Required CLI configuration path and scenario name. Relative paths are resolved in the caller's checkout. |
| `variant` | Optional `--variant` override. |
| `replay` | Optional literal schedule ID or file path for `--replay`. Omit to explore. |
| `repeat` | Optional `--repeat` override. |
| `artifact-name` | Artifact name; choose a unique value if the action runs more than once in a workflow. |

The action records the real CLI `exit-code`, and publishes `verdict` only from
a complete saved report. It also exposes `run-directory`, `schedule-id`,
`report-path`, `evidence-directory`, and `artifact-url` when available. `verdict`
is the report's scenario headline, so it can say `PASS` alongside exit 4 after
a cleanup failure or exit 5 for retained version-3 diagnostic-derivation
evidence. **Use the gate result or `exit-code`, not `verdict` alone, for CI.**
The step passes only when the CLI exits 0, a complete version-2 PASS report
exists, and the artifact upload succeeds. Exits 2 (reproduced violation), 3
(flaky), 4 (fixture/database failure), 5 (input/output failure), and 130
(interruption) all fail. An absent report, partial run directory, failed
install, or failed upload also fails; none becomes a synthetic PASS.

The uploaded artifact contains `install.txt` when installation reached the
archive, `stdout.log` and `stderr.log` when the process launched,
`status.json`, the run directory when one was published, and a root
`schedule.json` copy when the run saved a schedule. Thus a preflight failure
still retains its process logs; a diagnostic derivation failure retains its
version-3 evidence and exit 5. The job summary states when a report or other
evidence is missing. Outputs can be read in a later `if: always()` step even
when the gate step failed.

## Download and replay the exact schedule

The artifact can be downloaded from the failed run's Actions page, or from a
later workflow run. For a later run, grant that job `actions: read` and pass
the producer run ID as a `workflow_dispatch` input. From a fresh checkout of
this repository, these steps import the portable schedule and replay it:

```yaml
permissions:
  contents: read
  actions: read
steps:
  - uses: actions/checkout@v7
  - uses: actions/download-artifact@v8.0.1
    with:
      name: weavegate-evidence
      run-id: ${{ inputs.producer-run-id }}
      github-token: ${{ github.token }}
      path: imported
  - name: Import the saved schedule
    shell: bash
    run: |
      test -f imported/schedule.json
      mkdir -p .weavegate/schedules
      cp imported/schedule.json .weavegate/schedules/producer.json
  - id: replay
    uses: weavegate/weavegate@afc6b4fa493602bd64b6401c5bae0c6f7bf108b2
    with:
      version: v0.1.0-alpha
      config: fixtures/matching-slice/.weavegate/config.yaml
      scenario: concurrent-assign
      variant: fixed
      replay: .weavegate/schedules/producer.json
      repeat: '20'
      artifact-name: weavegate-fixed-replay
```

The action passes the imported file path directly to `--replay`. The CLI
checks the file's content-derived ID, then replays those exact steps. A raw
CLI invocation using default `--out .weavegate` can instead use the schedule
ID from `schedule.json` after this import, because it searches
`.weavegate/schedules/*.json`; see the
[replay resolution order](../reference/cli.md#--replay-resolution-order).
Keep the original config, fixture SQL, selected variant, and CLI release
available in the reader's checkout; a schedule file alone does not carry
those inputs.
