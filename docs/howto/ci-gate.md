# Gate a GitHub Actions job with weavegate

The repository root contains a [composite action](../../action.yml) that runs a
published weavegate CLI release on a Linux runner with Docker. It verifies the
release archive against that release's `checksums.txt`, runs the selected
configuration and scenario, uploads the available evidence, writes a job
summary, and only then decides whether the step passes. The gate needs no
write permission or secret; only the optional
[pull request comment](#pull-request-comment) needs `pull-requests: write`.
Use an ordinary `pull_request` workflow; this example does not use
`pull_request_target`.

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
| `comment` | `'true'` (default) posts the stored report as a [pull request comment](#pull-request-comment); `'false'` disables it. |
| `github-token` | Token used only to post that comment. Defaults to the job's `github.token`. |

The action records the real CLI `exit-code`, and publishes `verdict` only from
a complete saved report. It also exposes `run-directory`, `schedule-id`,
`report-path`, `evidence-directory`, and `artifact-url` when available, plus
`comment-outcome` and `comment-url` for the pull request comment. `verdict`
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

## Pull request comment

The action in this source tree posts the run's stored `report.md` as a pull
request comment. The commit pinned in the examples on this page predates that
step; pin a reviewed commit that contains it to get comments.

On a pull request event, after the evidence upload and before the gate
decision, the action posts **one new comment per run**. It never edits or
deletes an earlier comment, so each comment stays tied to the run that
produced it. Grant the job the permission the comment needs:

```yaml
permissions:
  contents: read
  pull-requests: write
```

The comment is a fixed wrapper around the stored report. Only the key facts
are visible at first; the report and the replay steps are collapsed sections
the reader opens:

1. A heading with the process exit code, then one line with the report
   verdict, the schedule ID, and links to the evidence artifact and its
   workflow run.
2. A collapsed section holding the bytes of `report.md`, unchanged, between
   the lines `<!-- weavegate-report-begin -->` and
   `<!-- weavegate-report-end -->`, in a fenced `text` block whose fence is
   longer than any backtick run in the report. The report is shown as literal
   text: nothing in it is rendered as Markdown, and no report value is copied
   anywhere else in the comment. The verdict on the visible line is printed
   only when it is exactly `PASS`, `FAIL`, or `FLAKY`.
3. When the run saved a schedule, a collapsed section with the steps to
   download the artifact, import `schedule.json` into `.weavegate/schedules/`,
   and run the report's `replay:` line from the repository root.
4. One closing line saying that `comment: 'false'` turns the comment off.

This is the comment the `reusable-gate` job posted on the pull request that
introduced the step, with the workflow run ID, artifact ID, and weavegate run
ID replaced by placeholders:

`````markdown
<!-- weavegate-gate-comment v1 -->
### weavegate gate: process exit code 2

Stored `report.md` of run `<run_id>`, unchanged and shown as literal text:

<!-- weavegate-report-begin -->
````text
## weavegate: FAIL (WG001)
scenario: concurrent-assign | schedules explored: 0 | violating: sch_ba00582f9632
assertion: active-assignment-is-unique
flaky: false (repeat=20)
replay: weavegate run --config fixtures/matching-slice/.weavegate/config.yaml --scenario concurrent-assign --variant vulnerable --replay sch_ba00582f9632 --repeat 20

error[WG001]: invariant violated under a controlled schedule
  observed:  active-assignment-is-unique returned 1 row: active_assignment_count=2 project_request_id=42
  assertion: active-assignment-is-unique
  invariant: a declared state invariant must hold under every release schedule the database permits
  reason:    commonly a read-then-write path without a lock or a unique constraint
  help:      add a unique constraint on the contested key
             take a pessimistic lock (SELECT ... FOR UPDATE) before insert
             use an idempotency key on the write
  evidence:  schedule sch_ba00582f9632 · trace.json · observation.json · 1 violating row
````
<!-- weavegate-report-end -->

**Evidence:** [download the artifact](https://github.com/weavegate/weavegate/actions/runs/<run-id>/artifacts/<artifact-id>) of [workflow run <run-id>](https://github.com/weavegate/weavegate/actions/runs/<run-id>). It holds the run directory `runs/<run_id>/` with every run file and, at its root, the saved `schedule.json`.

**Replay schedule `sch_ba00582f9632`:**

1. Check out the revision this workflow run tested and install weavegate `v0.1.0-alpha`.
2. Download the artifact and import its schedule from the repository root:

   ```sh
   gh run download <run-id> --repo weavegate/weavegate --name weavegate-gate-vulnerable --dir weavegate-evidence
   mkdir -p .weavegate/schedules
   cp weavegate-evidence/schedule.json .weavegate/schedules/sch_ba00582f9632.json
   ```

3. From the repository root, run the command on the report's `replay:` line. If that line contains a backslash escape it is a display form; rebuild the command from its original argument values.

The [CI gate how-to](https://github.com/weavegate/weavegate/blob/main/docs/howto/ci-gate.md#pull-request-comment) describes this comment and the replay in full.
`````

The commands in step 3 contain only the workflow run ID, the repository name,
the `artifact-name` input, and the schedule ID, and each must match a closed
grammar before it is printed; otherwise the step is described in words. The
`replay:` line keeps the meaning defined by the
[report schema](../reference/report-schema.md#reportmd): a line without a
backslash escape is pasted unchanged, and a line with one is rebuilt from its
original argument values.

**Size limit.** GitHub rejects a comment longer than 65,536 characters. The
action embeds the report only when the complete comment is at most 65,536
UTF-8 bytes, which is never more than that limit allows. A larger report is
not cut: the comment then states the report's size, says it was not truncated,
and points to `runs/<run_id>/report.md` in the evidence artifact. The same
fallback applies to a report a comment cannot carry byte for byte — one that
is not valid UTF-8, contains a control character other than a line feed, or
does not end with a line feed.

**The comment never decides the gate.** `comment-outcome` reports what
happened, and the gate step reads none of it:

| `comment-outcome` | Meaning |
| --- | --- |
| `posted` | The comment exists; `comment-url` links to it. |
| `disabled` | `comment` is `'false'`. No request was made. |
| `skipped` | Nothing could be posted: the event is not a pull request, the run stored no report, no token is available, or GitHub answered 401, 403, or 404 because the token cannot write pull request comments. |
| `failed` | The request did not complete or GitHub answered another status. |

Pull requests from forks get a read-only `github.token`, so their comment is
`skipped` while the gate result, the job summary, and the evidence artifact
are unchanged. A job without `pull-requests: write` behaves the same way. The
token is sent only in the request's authorization header; it is not written to
the log, the job summary, the outputs, or the comment, and the request does
not follow redirects.

To build your own reporting, set `comment: 'false'` and read the outputs in a
later step. The following fragment is constructed, not run by this
repository's CI. It passes the report by path and the other outputs through
the environment, so no report content becomes shell source:

```yaml
- id: weavegate
  uses: weavegate/weavegate@<reviewed commit containing the comment step>
  with:
    version: v0.1.0-alpha
    config: fixtures/matching-slice/.weavegate/config.yaml
    scenario: concurrent-assign
    comment: 'false'
- name: Post a custom comment
  if: always() && steps.weavegate.outputs.report-path != ''
  env:
    GH_TOKEN: ${{ github.token }}
    PR_NUMBER: ${{ github.event.pull_request.number }}
    REPORT_PATH: ${{ steps.weavegate.outputs.report-path }}
    ARTIFACT_URL: ${{ steps.weavegate.outputs.artifact-url }}
  run: |
    {
      printf 'weavegate evidence: %s\n\n' "$ARTIFACT_URL"
      sed 's/^/    /' "$REPORT_PATH"
    } > "$RUNNER_TEMP/weavegate-comment.md"
    gh pr comment "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --body-file "$RUNNER_TEMP/weavegate-comment.md"
```

The unit checks for these cases run without Docker and print one fixed marker
that the smoke workflow's `docs` job requires:

```bash
python3 -B scripts/test-actions-gate.py
```

The `reusable-gate` job posts a live comment from its vulnerable run when a
pull request changes `action.yml` or `scripts/actions-gate.py`, reads the
comment back through the API, and compares the embedded report with
`report.md` byte for byte. The `gate-replay` job then follows the comment's
instructions in a fresh checkout: it downloads the artifact, imports the
schedule, runs the report's `replay:` line without a shell, and requires exit 2
and an identical `report.md`. The matching-slice schedule is also built into
the CLI, so that job shows the documented steps work, not that the imported
file was the lookup stage that resolved the schedule.

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
