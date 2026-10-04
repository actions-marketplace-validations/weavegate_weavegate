# Spring matching fixture

The spring-matching fixture is a synthetic Spring Boot application with a
duplicate-assignment race. The weavegate CLI launches it as an external JVM
and runs it against real MySQL. It contains no production data and does not
reproduce a service's private schema.

The domain invariant is the one the Go-native
[matching-slice fixture](../matching-slice/README.md) checks:

> One active `project_request` has at most one active assignment.

## Layout

| Path | Role |
| --- | --- |
| `db/migration/001_schema.sql` | Synthetic InnoDB schema: `project_request` and `assignment` |
| `db/seed.sql` | One active request, `id=42` |
| `.weavegate/config.yaml` | External JVM launch, scenarios and the SQL assertion Oracle |
| `schedules/concurrent-assign.json` | The discovered violating schedule, `sch_7dcb74b1e506` |
| `app/` | The instrumented Spring Boot application (Maven) |

The schema, seed, scenarios and invariant are data. The application uses only
the public API of the Java integration (`io.github.weavegate:weavegate-spring`):
`WeavegateChild.run`, `@WeavegateCommand`, `CommandContext` and
`Weavegate.syncPoint`. It holds no verdict logic. The verdict comes from the
`active-assignment-is-unique` SQL assertion in the configuration.

## Workflow and variants

`assign` is one `@Transactional` (`REQUIRED`) command on a Spring bean. Spring's
proxy owns the transaction, and every query runs through `JdbcTemplate`:

1. Read the active request.
2. Arrive at `after_read_request`.
3. Count existing active assignments; if one exists, commit without writing.
4. Arrive at `before_insert_assignment`.
5. Insert the assignment and commit.

The variant comes from the start frame. The two variants differ only in the
first read:

```sql
-- vulnerable
SELECT status FROM project_request WHERE id = ? AND status = 'ACTIVE';

-- fixed
SELECT status FROM project_request WHERE id = ? AND status = 'ACTIVE' FOR UPDATE;
```

The `concurrent-assign` scenario runs this command as workers `w1` and `w2`
with the same two sync points as matching-slice. Exhaustive exploration of the
vulnerable variant stops at the same first candidate as matching-slice
exploration, with the same content-addressed ID, `sch_7dcb74b1e506`. One
schedule file therefore replays under both the Go-native and the external
adapter. This portability is measured only for these two fixtures, which share
worker IDs and point names.

Two more scenarios are lifecycle probes, not part of the vulnerable/fixed
evidence. `assign-then-fail` throws after the insert, so Spring rolls the
transaction back. `assign-then-halt` halts the JVM after the insert, with the
transaction still open.

## Build and run

The application depends on the Java integration at the same revision. Until a
release is published to Maven Central, install it from `sdk/java` first. Both
builds use the repository's checksum-pinned Maven wrapper and need Java 21:

```bash
(cd sdk/java && ./mvnw -B -Dmaven.test.skip=true install)
sdk/java/mvnw -B -f fixtures/spring-matching/app/pom.xml package
```

This produces `app/target/spring-matching.jar`, a Spring Boot executable JAR
whose dependencies are nested inside it. The configuration launches that file.
With Docker available, explore the vulnerable variant from the repository root,
then replay the committed schedule against each variant:

```bash
go build -o weavegate ./cmd/weavegate
./weavegate run --config fixtures/spring-matching/.weavegate/config.yaml \
  --scenario concurrent-assign --variant vulnerable
./weavegate run --config fixtures/spring-matching/.weavegate/config.yaml \
  --scenario concurrent-assign --variant fixed \
  --replay fixtures/spring-matching/schedules/concurrent-assign.json
```

The first command exits 2 with `WG001`. The second exits 0 with PASS. Both
repeat 20 times by default. Every repetition resets the database and starts a
new JVM. The captured output and run times are in
[Spring paired replay](../../docs/experiments/spring-replay.md).

In the fixed replay, `w1` holds the row lock while `w2`'s locking read waits.
When `w2` does not reach `after_read_request` within the 1000 ms
`arrive_timeout_ms`, the orchestrator records a timeout-inferred blocked state.
That timeout is not proof of a database lock. The paired test separately sees
a Connector/J session executing the locking read on the server.

## Paired evidence test

`TestSpringMatchingPairedReplay` in `cmd/weavegate` drives this fixture through
the CLI's run path. It needs Docker, Java 21 and the built JAR, and runs only
when opted in:

```bash
WEAVEGATE_SPRING_PAIRED=1 go test ./cmd/weavegate \
  -run '^TestSpringMatchingPairedReplay$' -v -count=1 -timeout 25m
```

It explores the vulnerable variant and requires the discovered schedule to equal
`schedules/concurrent-assign.json` byte for byte. It replays that schedule 20
times against each variant and checks the exit codes and saved artifacts: the
vulnerable variant must produce `WG001` in all 20 runs, and the fixed variant
must pass with a blocked wait in every run. A single fingerprint must cover all
20 runs of each replay. The lifecycle checks repeat 20 times as well: the
`assign-then-fail` probe as one 20-repetition replay, and the `assign-then-halt`
probe and a fixed replay canceled while a locking read is executing as 20
separate CLI runs each, because both end their run.

A test-only observer wraps the CLI's MySQL fixture. Before every reset and
before teardown, it requires that no child JVM is still running. It also
requires that no Connector/J session of the application account remains within
a 10-second bound, and it records the committed assignment count. After the
rollback and application-death probes, no assignment is committed. After the
run, no JAR snapshot remains. The test emits these fixed markers, which the
`external Spring replay (paired acceptance)` smoke job checks:

```text
SPRING_EXPLORE_RESULT variant=vulnerable exit=2 diagnostic=WG001 schedule=sch_7dcb74b1e506 repeat=20 flaky=false saved=byte_identical
SPRING_REPLAY_RESULT schedule=sch_7dcb74b1e506 variant=vulnerable repeat=20 exit=2 diagnostic=WG001 violation_runs=20 flaky=false
SPRING_REPLAY_RESULT schedule=sch_7dcb74b1e506 variant=fixed repeat=20 exit=0 verdict=PASS violation_runs=0 blocked_runs=20 flaky=false
SPRING_ROLLBACK_RESULT runs=20 exit=0 assignments=0 jvm=reaped connections=closed
SPRING_DEATH_RESULT runs=20 exit=5 fault=session assignments=0 jvm=reaped connections=closed
SPRING_CANCEL_RESULT runs=20 during=locking_read exit=130 jvm=reaped connections=closed
SPRING_LIFECYCLE_RESULT resets=checked blocked=observed rollback=rolled_back death=rolled_back cancel=cleaned jvm=reaped connections=closed snapshots=removed
```

## Current boundary

This evidence covers one declared two-worker, two-point scenario, this schema
and seed, MySQL 8.4, and the pinned Java, Spring Boot, Connector/J and HikariCP
versions packaged in the JAR. It does not show that weavegate finds races in
arbitrary Spring applications. As with matching-slice, a saved schedule records
release intent, and database locking can change the realized release order.
