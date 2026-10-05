# Gate run time

This experiment measures how long a `v0.2.0` gate takes for a small Spring Boot
service, on a GitHub-hosted runner and on a local host, and where that time
goes. It compares the external JVM path with the Go-native reference fixture
running the same workload shapes. The figures size CI timeouts and are the
baseline for the planned `v0.3.0` work in
[#172](https://github.com/weavegate/weavegate/issues/172); they are not a
performance benchmark.

## Setup

- **Release:** CLI `v0.2.0`, Java integration `io.github.weavegate:weavegate-spring:0.2.0`.
- **Spring Boot workload:** the
  [spring-boot-adoption-example](https://github.com/weavegate/spring-boot-adoption-example)
  seat-reservation service from [Spring Boot adoption](../howto/adopt-spring-boot.md):
  two workers, two sync-points, scenario `double-booking`, schedule
  `sch_6f1ffd61cc07`, `arrive_timeout_ms: 1000`. The fixed code locks the seat
  row with `SELECT ... FOR UPDATE`; the vulnerable code does not.
- **Go-native workload:** the bundled
  [`matching-slice`](../../fixtures/matching-slice/README.md) fixture, scenario
  `concurrent-assign`, its committed schedule, `arrive_timeout_ms: 250`.
- **CI runner:** GitHub-hosted `ubuntu-latest` (image `ubuntu-24.04`
  `20260927.320`, Docker 28.0.4, 16 GB memory as reported by Docker), running
  the example's [workflow](https://github.com/weavegate/spring-boot-adoption-example/blob/4abdf2769810bafa7e6b69c960fc69b125bdd458/.github/workflows/weavegate.yml)
  with `weavegate/weavegate@v0.2.0`.
- **Local host:** Linux x86-64 under WSL2, 18 vCPUs, 8 GB memory, Docker 29.8.0,
  MySQL image already pulled.

Each workload runs a fixed number of schedule runs. A replay runs the saved
schedule `--repeat` times (20). Exploration of the fixed code sweeps six
candidates over the default three passes (18 runs); exploration of the
vulnerable code stops at its first candidate and replays it 20 times to check
for flakiness (21 runs). Every run resets the database, and every external run
starts a new JVM.

## CI gate on a GitHub-hosted runner

Each row is one gate job of the example, measured in three attempts of the
same workflow run: fixed code in
[run 37263131854](https://github.com/weavegate/spring-boot-adoption-example/actions/runs/37263131854)
(pull request #5) and vulnerable code in
[run 37263151356](https://github.com/weavegate/spring-boot-adoption-example/actions/runs/37263151356)
(pull request #6). The gate step installs the CLI, runs weavegate and uploads
the evidence; the job also checks out the code, sets up Java and builds the
JARs (4–17 s).

| Workload | Schedule runs | Gate step (s) | Job (s) | Exit |
| --- | --- | --- | --- | --- |
| Fixed, regression replay | 20 | 110, 110, 106 | 128, 120, 118 | 0 |
| Fixed, exploration | 18 | 100, 101, 98 | 129, 115, 107 | 0 |
| Vulnerable, regression replay | 20 | 87, 78, 90 | 112, 103, 116 | 2 |
| Vulnerable, exploration | 1 + 20 | 67, 95, 88 | 85, 117, 106 | 2 |

A gate job took 85–129 s, of which the weavegate step took 67–110 s. The two
jobs run in parallel; from the first job's start to the last job's end, each
attempt took 113–129 s.

## Local host and where the time goes

The same workloads ran three times each with the release CLI. `Prepare` is
the time from start until MySQL was ready, and `Schedules` the time from then
until the container was stopped, both read from the run's log timestamps at
one-second resolution.

| Workload | Total (s) | Prepare (s) | Schedules (s) | Per schedule run |
| --- | --- | --- | --- | --- |
| Spring fixed, regression replay (20) | 91.4, 89.3, 84.7 | 12, 9, 9 | 77, 78, 74 | ≈ 3.8 s |
| Spring fixed, exploration (18) | 79.8, 78.3, 77.8 | 9, 9, 10 | 68, 67, 66 | ≈ 3.7 s |
| Spring vulnerable, regression replay (20) | 65.8, 67.0, 66.7 | 8, 8, 8 | 56, 57, 56 | ≈ 2.8 s |
| Spring vulnerable, exploration (21) | 68.9, 68.7, 67.6 | 9, 10, 9 | 58, 57, 57 | ≈ 2.7 s |
| Go-native fixed, regression replay (20) | 18.6, 17.5, 16.6 | 9, 9, 8 | 7, 7, 7 | ≈ 0.35 s |
| Go-native fixed, exploration (18) | 17.8, 17.2, 17.3 | 10, 9, 9 | 6, 6, 7 | ≈ 0.35 s |
| Go-native vulnerable, regression replay (20) | 15.7, 14.0, 12.6 | 12, 11, 10 | 2, 2, 1 | ≈ 0.1 s |
| Go-native vulnerable, exploration (21) | 17.6, 13.6, 12.1 | 14, 10, 9 | 2, 2, 1 | ≈ 0.1 s |

- Preparing MySQL costs about 8–14 s per gate run on either path.
- On the external path, each schedule run costs about 2.7–3.8 s, against
  0.1–0.35 s for Go-native. The external path starts and stops a JVM and a
  Spring context for every run, which keeps runs isolated from each other;
  this experiment does not split that cost further.
- Fixed runs are slower than vulnerable runs because `FOR UPDATE` blocks the
  second worker, and the orchestrator recognizes the block only after the
  arrive timeout. The example sets `arrive_timeout_ms: 1000`; matching-slice
  sets 250. The [configuration reference](../reference/config.md#timing)
  describes this trade-off.
- A gate's run time therefore grows with schedule runs × per-run cost:
  candidates × passes for exploration and `--repeat` for replay.

## Reproduce

Local timings, from a checkout of the example at the fixed or vulnerable
revision with the `v0.2.0` CLI on `PATH` and the JARs built:

```bash
measure() {
  label=$1; shift
  for i in 1 2 3; do
    t0=$(date +%s.%N)
    weavegate run "$@" > "$label.$i.out" 2> "$label.$i.err"
    rc=$?
    printf '%s exit=%d seconds=%.1f\n' "$label" "$rc" "$(echo "$(date +%s.%N) - $t0" | bc)"
  done
}
measure regression --config .weavegate/config.yaml --scenario double-booking \
  --replay .weavegate/schedules/sch_6f1ffd61cc07.json
measure explore --config .weavegate/config.yaml --scenario double-booking
```

Give each workload its own label, such as `fixed-regression`, so its logs are
kept. For Go-native, run the same function from a weavegate checkout with
`--config fixtures/matching-slice/.weavegate/config.yaml --scenario concurrent-assign`,
`--variant fixed` or `--variant vulnerable`, and
`--replay fixtures/matching-slice/schedules/concurrent-assign.json` for the
replay. The prepare and schedule split comes from the testcontainers lines on
`<label>.<i>.err`: the first timestamp, the MySQL `Container is ready` line and the
`Stopping container` line.

CI timings, for each attempt of a workflow run:

```bash
gh api repos/weavegate/spring-boot-adoption-example/actions/runs/<run-id>/attempts/<attempt>/jobs \
  -q '.jobs[] | "\(.name) job=\((.completed_at|fromdate)-(.started_at|fromdate))s gate=\(.steps[]
      | select(.name|test("weavegate/weavegate"))
      | (.completed_at|fromdate)-(.started_at|fromdate))s"'
```

## Boundary

- One two-worker, two-point workload per path, on the hosts above. Run time
  depends on the runner, Docker, the image cache and the application's
  startup time; a larger Spring context starts more slowly.
- The CI rows are three attempts on GitHub-hosted runners assigned per job,
  not a controlled benchmark.
- Shortening external gate runs is planned for `v0.3.0`
  ([#172](https://github.com/weavegate/weavegate/issues/172)); these numbers
  describe `v0.2.0`.
