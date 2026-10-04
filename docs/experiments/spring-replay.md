# Spring paired replay

This page records the paired evidence for the external SUT path. The weavegate
CLI launches an instrumented Spring Boot application as a child JVM and runs it
against real MySQL. The CLI reproduces a transactional invariant violation
under a saved schedule, and the `SELECT ... FOR UPDATE` fix passes under the
same schedule. It is release evidence for one synthetic fixture on one pinned
baseline, not a claim of general race detection.

The application, schema, scenarios and Oracle are described in the
[`spring-matching` fixture](../../fixtures/spring-matching/README.md).

## Supported baseline and measured environment

The versions below come from the paired test's `SPRING_ENVIRONMENT` record.
That record reads the Java runtime that launches the child, the libraries
packaged in the fixture JAR, and the MySQL server's `VERSION()`.

| Component | Version |
| --- | --- |
| Java | 21 (measured: OpenJDK `21.0.8+9-LTS`) |
| Spring Boot / Spring Framework | 4.0.8 / 7.0.9 |
| Transaction manager | `DataSourceTransactionManager` (spring-jdbc 7.0.9) |
| JDBC driver | MySQL Connector/J 9.7.0 |
| Pool | HikariCP 7.0.2 |
| MySQL | `mysql:8.4` image (measured server: 8.4.10) |
| Go | go1.25.0 |
| Build | Apache Maven 3.9.16 through the checksum-pinned `sdk/java/mvnw` |

Measured host: Intel Core Ultra 5 125H (18 logical CPUs), 7.7 GB memory,
WSL2 Linux 6.18, Docker Engine 29.8.0. The local measurements ran on the
working tree of the pull request that added this page, before it was
committed. The `external Spring replay
(paired acceptance)` smoke job repeats the evidence on GitHub-hosted runners for
every push and pull request, and publishes the logs and the paired manifest as
the `external-spring-evidence` artifact.

## Build

From the repository root, with Java 21:

```bash
(cd sdk/java && ./mvnw -B -Dmaven.test.skip=true install)
sdk/java/mvnw -B -f fixtures/spring-matching/app/pom.xml package
go build -o weavegate ./cmd/weavegate
```

## Vulnerable and fixed replay

Each command replays the committed schedule 20 times (the configuration's
default repeat). Every repetition resets the database and launches a fresh JVM
from a private snapshot of the JAR.

```console
$ ./weavegate run --config fixtures/spring-matching/.weavegate/config.yaml \
    --scenario concurrent-assign --variant vulnerable \
    --replay fixtures/spring-matching/schedules/concurrent-assign.json
## weavegate: FAIL (WG001)
scenario: concurrent-assign | schedules explored: 0 | violating: sch_7dcb74b1e506
assertion: active-assignment-is-unique
flaky: false (repeat=20)
replay: weavegate run --config fixtures/spring-matching/.weavegate/config.yaml --scenario concurrent-assign --variant vulnerable --replay sch_7dcb74b1e506 --repeat 20

error[WG001]: invariant violated under a controlled schedule
  observed:  active-assignment-is-unique returned 1 row: active_assignment_count=2 project_request_id=42
  assertion: active-assignment-is-unique
  invariant: a declared state invariant must hold under every release schedule the database permits
  reason:    commonly a read-then-write path without a lock or a unique constraint
  help:      add a unique constraint on the contested key
             take a pessimistic lock (SELECT ... FOR UPDATE) before insert
             use an idempotency key on the write
  evidence:  schedule sch_7dcb74b1e506 · trace.json · observation.json · 1 violating row
.weavegate/runs/run_20261004T091219.851212873Z_5d6b33d5ede5d6c6b62a6951469bf93b
$ echo $?
2
$ ./weavegate run --config fixtures/spring-matching/.weavegate/config.yaml \
    --scenario concurrent-assign --variant fixed \
    --replay fixtures/spring-matching/schedules/concurrent-assign.json
## weavegate: PASS
scenario: concurrent-assign | schedules explored: 0 | replayed: sch_7dcb74b1e506
flaky: false (repeat=20)
replay: weavegate run --config fixtures/spring-matching/.weavegate/config.yaml --scenario concurrent-assign --variant fixed --replay sch_7dcb74b1e506 --repeat 20
.weavegate/runs/run_20261004T091643.383960523Z_5656a388aab1817719ea1890f52fdab4
$ echo $?
0
```

The run directory paths were captured under a temporary `--out` directory and
are shown here under the default `.weavegate`; the rest is unchanged output.

## Repeated paired test

The paired test drives the same configuration through the CLI's run path. It
adds exploration, the lifecycle probes and an independent cleanup observer;
the [fixture README](../../fixtures/spring-matching/README.md#paired-evidence-test)
lists its checks.

```bash
WEAVEGATE_SPRING_PAIRED=1 go test ./cmd/weavegate \
  -run '^TestSpringMatchingPairedReplay$' -v -count=1 -timeout 25m
```

Captured markers from one local run (log prefixes removed; 838 s in total):

```text
SPRING_EXPLORE_RESULT variant=vulnerable exit=2 diagnostic=WG001 schedule=sch_7dcb74b1e506 repeat=20 flaky=false saved=byte_identical
SPRING_REPLAY_RESULT schedule=sch_7dcb74b1e506 variant=vulnerable repeat=20 exit=2 diagnostic=WG001 violation_runs=20 flaky=false
SPRING_REPLAY_DURATION variant=vulnerable repeat=20 elapsed_ms=71146 resets_observed=20
SPRING_REPLAY_RESULT schedule=sch_7dcb74b1e506 variant=fixed repeat=20 exit=0 verdict=PASS violation_runs=0 blocked_runs=20 flaky=false
SPRING_REPLAY_DURATION variant=fixed repeat=20 elapsed_ms=94939 resets_observed=20
SPRING_ROLLBACK_RESULT runs=20 exit=0 assignments=0 jvm=reaped connections=closed
SPRING_DEATH_RESULT runs=20 exit=5 fault=session assignments=0 jvm=reaped connections=closed
SPRING_CANCEL_STATES map[Opening tables:1 statistics:19]
SPRING_CANCEL_RESULT runs=20 during=locking_read exit=130 jvm=reaped connections=closed
SPRING_ENVIRONMENT go=go1.25.0 java=21.0.8+9-LTS mysql=8.4.10 spring_boot=4.0.8 spring=7.0.9 transaction_manager=spring-jdbc-7.0.9 jdbc_driver=9.7.0 pool=7.0.2 weavegate_spring=0.0.0-SNAPSHOT
SPRING_LIFECYCLE_RESULT resets=checked blocked=observed rollback=rolled_back death=rolled_back cancel=cleaned jvm=reaped connections=closed snapshots=removed
```

Exploration, the vulnerable replay and the fixed replay together make 61
schedule executions (one exploration candidate plus a 20-run replay inside
exploration, then two 20-run replays). Every one produced the expected
verdict. Each lifecycle check also ran 20 times: one 20-repetition rollback
replay, 20 application-death runs and 20 canceled fixed replays. Every
cancellation landed while a Connector/J session was executing the locking read.
The server reported that session's state as `statistics` in 19 runs and
`Opening tables` in one. This page does not interpret those states as proof of
a row-lock wait.
The cleanup checks passed in all of these runs.

## Run time

The external path starts one JVM per schedule execution
([ADR 0016](../adr/0016-external-cli-composition.md)). Wall time for the two
replay commands above, three runs each, on the measured host. Each run includes
container provisioning, 20 resets, 20 JVM launches and teardown:

| Command | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| Spring `vulnerable`, `--repeat 20` | 89.0 s | 81.8 s | 92.7 s |
| Spring `fixed`, `--repeat 20` | 97.7 s | 104.1 s | 102.0 s |
| Go-native matching-slice `vulnerable`, same schedule file, `--repeat 20` | 11.8 s | — | — |

Reproduce a row by prefixing its command with `/usr/bin/time -f '%e'`. For
example:

```bash
/usr/bin/time -f '%e' ./weavegate run --config fixtures/spring-matching/.weavegate/config.yaml \
  --scenario concurrent-assign --variant vulnerable \
  --replay fixtures/spring-matching/schedules/concurrent-assign.json
/usr/bin/time -f '%e' ./weavegate run --config fixtures/matching-slice/.weavegate/config.yaml \
  --scenario concurrent-assign --variant vulnerable \
  --replay fixtures/spring-matching/schedules/concurrent-assign.json
```

Subtracting the Go-native run, which shares provisioning and teardown, puts the
external path at roughly 3.5–4 s per vulnerable repetition and 4.3–4.6 s per
fixed repetition on this host. This is a derived estimate, not a separate
measurement. The fixed variant also waits out the 1000 ms block-inference
timeout once per repetition. Expect a 20-repetition Spring replay to take
minutes, not seconds, and size CI timeouts accordingly. The paired smoke job
allows 50 minutes for exploration, both replays and the 20-run lifecycle
probes; the 20 application-death and 20 cancellation runs each provision their
own database container.

## Schedule portability

The Go-native matching-slice exploration and the Spring exploration both stop
at the same candidate, `sch_7dcb74b1e506`. The committed Spring schedule file
replays under the Go-native matching-slice configuration with the same `WG001`
and exit 2 (the last row of the table). Portability is measured only between
these two fixtures, which share worker IDs, point names and the invariant. It
is not a general property of schedules across applications.

## Limits

- One synthetic two-worker, two-point workflow, on the versions above. Other
  JDKs, Boot lines, drivers, pools and databases are untested.
- `timeout_inferred` is not proof of a database lock. The cancellation probe
  shows a server-side locking read in progress in each of its runs, but not
  for the replays it did not interrupt.
- The cleanup observer polls for retired server sessions within a 10-second
  bound. It shows absence at check time; it does not inspect InnoDB
  internals, which the application account cannot read.
- Run times depend on the host and Docker setup; they are not performance
  claims.
