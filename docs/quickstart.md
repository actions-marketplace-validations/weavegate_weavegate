# Quickstart

You need a running Docker daemon. The first run starts a real MySQL 8.4
container, so allow extra time if Docker must pull the image.

This tutorial uses the bundled reference fixture to reproduce a database
race, then replays the same schedule against its fixed implementation. It does
not modify the fixture or your database.

## 1. Install weavegate

Choose [a release archive, source checkout, or go install](install.md). Follow
that guide through `weavegate --version`, then run the commands below from the
directory containing `fixtures/matching-slice/.weavegate/config.yaml`.

## 2. Reproduce the violation

Time: about 20 seconds after the MySQL image is available.

Run the intentionally vulnerable variant:

```bash
weavegate run --config fixtures/matching-slice/.weavegate/config.yaml \
  --scenario concurrent-assign --variant vulnerable
```

The run fails because its SQL oracle observes two active assignments for the
same request. The following excerpt is captured output; the run directory is a
volatile identifier and will differ:

```text
## weavegate: FAIL (WG001)
scenario: concurrent-assign | schedules explored: 1 | violating: sch_7dcb74b1e506
assertion: active-assignment-is-unique
flaky: false (repeat=20)
error[WG001]: invariant violated under a controlled schedule
  observed:  active-assignment-is-unique returned 1 row: active_assignment_count=2 project_request_id=42
```

Check the process status immediately after the run:

```console
$ echo $?
2
```

Expected exit code: `2`. Keep the `sch_7dcb74b1e506` schedule ID printed after
`violating:`; the next step reuses it.

## 3. Replay the fixed variant

Time: about 15 seconds after the MySQL image is available.

Replay that exact schedule 20 times against the implementation that locks the
request row before deciding whether to insert:

```bash
weavegate run --config fixtures/matching-slice/.weavegate/config.yaml \
  --scenario concurrent-assign --variant fixed \
  --replay sch_7dcb74b1e506 --repeat 20
```

Captured output excerpt:

```text
## weavegate: PASS
scenario: concurrent-assign | schedules explored: 0 | replayed: sch_7dcb74b1e506
flaky: false (repeat=20)
```

Check the status:

```console
$ echo $?
0
```

Expected exit code: `0`. The schedule that deterministically violated the
invariant now passes on every replay. See the
[determinism experiment](experiments/determinism.md#repeated-result) for the
recorded evidence and [WG001](reference/diagnostics/WG001.md) for the diagnostic
contract.
