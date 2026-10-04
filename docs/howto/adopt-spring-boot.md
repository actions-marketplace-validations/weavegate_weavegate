# Adopt weavegate in a Spring Boot project

This guide adds a weavegate gate to an existing Spring Boot service. It takes a
`@Transactional` workflow that reads, decides and writes, marks the decision
with sync-points, declares the invariant as SQL, reproduces a race locally and
then fails a pull request check until the race is fixed.

The steps use `v0.2.0-rc.1`, the first prerelease that publishes both the Java
integration and a CLI that launches an instrumented JVM. The
[spring-boot-adoption-example](https://github.com/weavegate/spring-boot-adoption-example)
repository followed them in
[pull request #1](https://github.com/weavegate/spring-boot-adoption-example/pull/1),
starting from a plain seat-reservation service. Every output block below is
captured from that repository; run directories and workflow run IDs are
volatile.

## Before you start

The Java integration enforces one tested baseline. Check that your service fits
it before you begin:

| Requirement | Supported |
| --- | --- |
| Java | 21 |
| Spring Boot / Spring Framework | 4.0.8 / 7.0.9 |
| Data access | JDBC, such as `JdbcTemplate`, under Spring's `@Transactional` |
| Database | MySQL 8.4 with InnoDB tables |
| JDBC driver | MySQL Connector/J 9.7.0 |
| Build | Maven; this guide does not cover Gradle builds |
| Local runs and CI runners | A working Docker daemon; this guide was run on Linux x86-64 and `ubuntu-latest` |

The integration supplies the DataSource and a `DataSourceTransactionManager`.
Startup fails if the application context defines another DataSource or
transaction manager, so a JPA transaction manager is not supported. The child
JVM also rejects SQL that commits implicitly, transaction control outside
Spring and several JDBC features. The
[Java peer reference](../reference/external-sut-java.md#opting-in) lists every
rule.

The CLI and the Java integration are released together. Use the same version
for both: `0.2.0-rc.1` for the Java dependency and `v0.2.0-rc.1` for the CLI.

## 1. Install the CLI

Download the Linux archive and its checksums from the
[`v0.2.0-rc.1` release](https://github.com/weavegate/weavegate/releases/tag/v0.2.0-rc.1),
verify the archive, and put the binary on `PATH`:

```bash
base=https://github.com/weavegate/weavegate/releases/download/v0.2.0-rc.1
curl -fsSLO "$base/weavegate_0.2.0-rc.1_linux_amd64.tar.gz"
curl -fsSLO "$base/checksums.txt"
sha256sum -c --ignore-missing checksums.txt
tar -xzf weavegate_0.2.0-rc.1_linux_amd64.tar.gz
export PATH="$PWD/weavegate_0.2.0-rc.1_linux_amd64:$PATH"
weavegate --version
```

```text
weavegate_0.2.0-rc.1_linux_amd64.tar.gz: OK
0.2.0-rc.1
```

[Install weavegate](../install.md) describes the other routes. The CI gate in
step 8 installs the CLI by itself.

## 2. Add the Java integration

Add the dependency from Maven Central. It brings the JDBC starter and
Connector/J with it:

```xml
<properties>
  <java.version>21</java.version>
  <weavegate.version>0.2.0-rc.1</weavegate.version>
</properties>

<dependency>
  <groupId>io.github.weavegate</groupId>
  <artifactId>weavegate-spring</artifactId>
  <version>${weavegate.version}</version>
</dependency>
```

The dependency has compile scope, so the application's own executable JAR
also contains `weavegate-spring` and its transitive dependencies. It is
inactive unless the JVM was started through its bootstrap in step 5: in the
normal application, sync-points return immediately and nothing reads stdin or
changes transactions.

## 3. Mark the contested decision with sync-points

Find the transaction whose correctness depends on state that can change
between a read and a write, and place one sync-point after the read and one
before the write. [Instrument a workflow](instrument.md#start-from-the-contested-decision)
explains where points belong. The example's reservation service:

```diff
+import io.github.weavegate.sdk.Weavegate;
 ...
 public class ReservationService {
+    public static final String AFTER_SEAT_READ = "after_seat_read";
+    public static final String BEFORE_RESERVATION_INSERT = "before_reservation_insert";
 ...
     @Transactional
     public boolean reserve(long seatId, String customer) {
         List<String> open = jdbc.queryForList(
                 "SELECT status FROM seat WHERE id = ? AND status = 'OPEN'", String.class, seatId);
         if (open.isEmpty()) {
             return false;
         }
+        Weavegate.syncPoint(AFTER_SEAT_READ);

         Integer held = jdbc.queryForObject(
                 "SELECT COUNT(*) FROM reservation WHERE seat_id = ? AND status = 'ACTIVE'",
                 Integer.class, seatId);
         if (held != null && held > 0) {
             return false;
         }
+        Weavegate.syncPoint(BEFORE_RESERVATION_INSERT);
         jdbc.update("INSERT INTO reservation (seat_id, customer, status) VALUES (?, ?, 'ACTIVE')",
                 seatId, customer);
         return true;
     }
```

A sync-point blocks without a timeout until weavegate releases it. Do not
replace it with a sleep or put it inside a JDBC call.

## 4. Expose the workflow as a command

weavegate workers call commands, not HTTP endpoints. Add a Spring bean whose
method takes a `CommandContext`, opens one `@Transactional` boundary and calls
the existing service. The service's own `@Transactional` method joins that
transaction, because both use the default `REQUIRED` propagation:

```java
@Component
public class ReservationCommands {
    private final ReservationService reservations;

    public ReservationCommands(ReservationService reservations) {
        this.reservations = reservations;
    }

    @Transactional
    @WeavegateCommand(value = "reserve", points = {
            ReservationService.AFTER_SEAT_READ, ReservationService.BEFORE_RESERVATION_INSERT})
    public void reserve(CommandContext context) {
        reservations.reserve(Long.parseLong(context.params().get("seat_id")), context.worker());
    }
}
```

- The method must be public, non-final, return `void` and be called through
  the Spring proxy.
- `points` lists every sync-point the command may reach. Reaching an
  undeclared point fails the session.
- `context.params()` holds the worker's string arguments from the
  configuration, and `context.worker()` is the worker ID.
- `context.variant()` returns the configured variant name. A real application
  normally ignores it: the code under test is whatever was built.

The bean is part of the normal application context too, where it does
nothing unless weavegate started the JVM.

## 5. Build a second, instrumented JAR

The CLI launches a self-contained executable JAR whose main class calls the
bootstrap. Keep your production JAR's main class and build the instrumented
JAR beside it. Add an entry point:

```java
public final class WeavegateMain {
    private WeavegateMain() {
    }

    public static void main(String[] args) {
        WeavegateChild.run(BookingApplication.class, args);
    }
}
```

Then add a second `repackage` execution with its own classifier and main
class:

```xml
<plugin>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-maven-plugin</artifactId>
  <executions>
    <execution>
      <id>repackage</id>
      <goals>
        <goal>repackage</goal>
      </goals>
    </execution>
    <execution>
      <id>weavegate</id>
      <goals>
        <goal>repackage</goal>
      </goals>
      <configuration>
        <classifier>weavegate</classifier>
        <mainClass>com.example.booking.weavegate.WeavegateMain</mainClass>
      </configuration>
    </execution>
  </executions>
</plugin>
```

`./mvnw -B package` now writes `target/booking-0.0.1-SNAPSHOT.jar`, the
application with its own main class, and
`target/booking-0.0.1-SNAPSHOT-weavegate.jar`, the child JVM for weavegate.
Both contain the same classes and dependencies, including `weavegate-spring`;
only the main class differs. Inside the child, the bootstrap starts a non-web
context and disables SQL initialization, Flyway and Liquibase, so the
production datasource settings and migrations are not used.

## 6. Declare the database and the invariant

weavegate starts a fresh MySQL 8.4 container for every run, applies your
schema migrations and seed, and resets them between schedules. Use synthetic
data that reaches the race, not production data. The example keeps
`db/migration/001_schema.sql` and `db/seed.sql`; the seed opens seat `7`.

Write `.weavegate/config.yaml`. Paths are relative to the file:

```yaml
target:
  db: mysql:8.4
  schema:
    migrations: ../db/migration
    seed: ../db/seed.sql
  sut:
    adapter: external
    variant: main
    external:
      java: java
      jar: ../target/booking-0.0.1-SNAPSHOT-weavegate.jar
      capacity: 2
      startup_timeout_ms: 30000
      cancel_timeout_ms: 5000
      stop_timeout_ms: 20000
scenarios:
  double-booking:
    workers:
      - id: alice
        command: reserve
        args:
          seat_id: "7"
      - id: bob
        command: reserve
        args:
          seat_id: "7"
    sync_points:
      - after_seat_read
      - before_reservation_insert
oracle:
  assertions:
    - id: one-active-reservation-per-seat
      sql: |
        SELECT seat_id, COUNT(*) AS active_reservations
        FROM reservation
        WHERE status = 'ACTIVE'
        GROUP BY seat_id
        HAVING COUNT(*) > 1
        ORDER BY seat_id;
      expect_rows: 0
run:
  arrive_timeout_ms: 1000
```

- `variant` is required for an external adapter; any valid name works when the
  application ignores it.
- `capacity` must cover the scenario's workers.
- The assertion selects the rows that break the invariant and expects none.
  The verdict comes only from this SQL, after both workers finish.
- Every worker in a scenario declares the same `args`.

The [configuration reference](../reference/config.md#external-jvm) defines each
key and its limits.

## 7. Reproduce the race locally

Build the JARs, then let weavegate explore candidate schedules for the two
sync-points:

```bash
./mvnw -B package
weavegate run --config .weavegate/config.yaml --scenario double-booking
```

```text
## weavegate: FAIL (WG001)
scenario: double-booking | schedules explored: 1 | violating: sch_6f1ffd61cc07
assertion: one-active-reservation-per-seat
flaky: false (repeat=20)
replay: weavegate run --config .weavegate/config.yaml --scenario double-booking --variant main --replay sch_6f1ffd61cc07 --repeat 20

error[WG001]: invariant violated under a controlled schedule
  observed:  one-active-reservation-per-seat returned 1 row: active_reservations=2 seat_id=7
  assertion: one-active-reservation-per-seat
  invariant: a declared state invariant must hold under every release schedule the database permits
  reason:    commonly a read-then-write path without a lock or a unique constraint
  help:      add a unique constraint on the contested key
             take a pessimistic lock (SELECT ... FOR UPDATE) before insert
             use an idempotency key on the write
  evidence:  schedule sch_6f1ffd61cc07 · trace.json · observation.json · 1 violating row
.weavegate/runs/run_20261004T132602.997186514Z_aa1b1dc58f521f1cfc517b9ec694b9dc
```

The process exits 2. Both workers read the open seat, found no active
reservation and inserted one. [WG001](../reference/diagnostics/WG001.md)
explains the evidence in the run directory.

The example fixes the race by locking the seat row in the first read:

```diff
-                "SELECT status FROM seat WHERE id = ? AND status = 'OPEN'", String.class, seatId);
+                "SELECT status FROM seat WHERE id = ? AND status = 'OPEN' FOR UPDATE", String.class, seatId);
```

Rebuild and run the report's `replay:` line. It replays the same schedule
against the fixed code:

```text
## weavegate: PASS
scenario: double-booking | schedules explored: 0 | replayed: sch_6f1ffd61cc07
flaky: false (repeat=20)
replay: weavegate run --config .weavegate/config.yaml --scenario double-booking --variant main --replay sch_6f1ffd61cc07 --repeat 20
.weavegate/runs/run_20261004T132741.228467335Z_1c1a4f9fef11a646849c69865cac9208
```

Exploring the fixed code again sweeps every candidate schedule. Exploration
runs three passes by default, so the 18 below counts six candidates three
times:

```text
## weavegate: PASS
scenario: double-booking | schedules explored: 18 (exhausted) | violating: none
flaky: false (repeat=20)
.weavegate/runs/run_20261004T132922.275660269Z_d0c06ad844300cedc5f2f6925ab62d08
```

Both exit 0. A candidate is coordination intent, not a guaranteed release
order: with `FOR UPDATE`, the second worker can wait in the database before it
reaches its first sync-point, so not every candidate runs as written.
[Limitations](../limitations.md#a-saved-schedule-is-coordination-intent)
explains what a PASS covers. Each schedule run starts a new JVM, so an external
run takes seconds per schedule. Add `.weavegate/runs/` to `.gitignore`.

## 8. Gate pull requests in GitHub Actions

Start with exploration. The workflow builds the instrumented JAR with Java 21,
then runs the weavegate action. Referencing the action by the exact tag
`v0.2.0-rc.1` installs CLI `v0.2.0-rc.1`; the CLI starts the child with the
`java` on `PATH`, which `actions/setup-java` provides:

```yaml
name: weavegate
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
  pull-requests: write
jobs:
  gate:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-java@v6
        with:
          distribution: temurin
          java-version: '21'
          cache: maven
      - name: Build the weavegate test JAR
        run: ./mvnw -B package
      - id: weavegate
        uses: weavegate/weavegate@v0.2.0-rc.1
        with:
          config: .weavegate/config.yaml
          scenario: double-booking
```

On the vulnerable code, the check fails with exit 2, uploads the evidence
artifact and posts the stored report as a pull request comment. CI found the
same schedule ID as the local run, because schedule IDs are content-derived.
[Gate GitHub Actions with weavegate](ci-gate.md) documents the inputs,
outputs, artifact contents and comment.

Once a schedule has reproduced the race, commit it and replay it on every
change, next to exploration. Download the artifact as the comment's replay
section shows, copy its `schedule.json` to
`.weavegate/schedules/sch_6f1ffd61cc07.json`, and run the gate twice:

```yaml
jobs:
  gate:
    name: gate (${{ matrix.mode }})
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        include:
          - mode: regression
            replay: .weavegate/schedules/sch_6f1ffd61cc07.json
          - mode: explore
            replay: ''
    steps:
      # checkout, setup-java and the build step as above
      - id: weavegate
        uses: weavegate/weavegate@v0.2.0-rc.1
        with:
          config: .weavegate/config.yaml
          scenario: double-booking
          replay: ${{ matrix.replay }}
          artifact-name: weavegate-evidence-${{ matrix.mode }}
```

Each invocation needs its own `artifact-name`. The example's pull request ran
these checks on its three commits:

| Commit | Code | `regression` | `explore` |
| --- | --- | --- | --- |
| [`563b05d`](https://github.com/weavegate/spring-boot-adoption-example/actions/runs/37206209214) | Vulnerable, exploration only | — | FAIL (WG001), `sch_6f1ffd61cc07` |
| [`df6e07d`](https://github.com/weavegate/spring-boot-adoption-example/actions/runs/37206482086) | Vulnerable, schedule committed | FAIL (WG001), replayed `sch_6f1ffd61cc07` | FAIL (WG001), `sch_6f1ffd61cc07` |
| [`8950c8a`](https://github.com/weavegate/spring-boot-adoption-example/actions/runs/37206624620) | `FOR UPDATE` fix | PASS, replayed `sch_6f1ffd61cc07` | PASS, 6 candidates × 3 passes exhausted |

## 9. Replay CI evidence on another machine

The pull request comment's replay section names the exact commands for your
run. From a fresh clone checked out at the revision the failing run tested,
with the CLI from step 1 installed and the JARs built, download the artifact,
import its schedule and run the report's `replay:` line:

```bash
gh run download <run-id> --repo <owner>/<repository> --name weavegate-evidence --dir weavegate-evidence
mkdir -p .weavegate/schedules
cp weavegate-evidence/schedule.json .weavegate/schedules/<schedule-id>.json
weavegate run --config .weavegate/config.yaml --scenario double-booking --variant main --replay <schedule-id> --repeat 20
```

GitHub keeps workflow artifacts for a limited retention period, 90 days by
default, so download evidence while it exists and commit a schedule you want to
keep, as step 8 does. In the example, the run was `37206209214` and the
schedule `sch_6f1ffd61cc07`; that artifact will expire, but the committed
`.weavegate/schedules/sch_6f1ffd61cc07.json` replays the same schedule. The
replay printed:

```text
## weavegate: FAIL (WG001)
scenario: double-booking | schedules explored: 0 | violating: sch_6f1ffd61cc07
assertion: one-active-reservation-per-seat
flaky: false (repeat=20)
replay: weavegate run --config .weavegate/config.yaml --scenario double-booking --variant main --replay sch_6f1ffd61cc07 --repeat 20
```

The rest of the report, the violating row and exit 2 match the CI run. Only the
`schedules explored` count differs, because CI explored and this run replayed.
A schedule file alone is not enough: keep the configuration, schema, seed and
CLI version that produced it.

## What this guide does not cover

- Gradle builds. The [Java peer reference](../reference/external-sut-java.md#supported-baseline)
  shows the Gradle dependency declaration only.
- Other Java, Spring Boot or MySQL versions, other databases, and JPA.
- Applications that enable `@Scheduled` or `@Async` processing, which the child
  rejects at startup. The [Java peer reference](../reference/external-sut-java.md#opting-in)
  lists the remaining restrictions, and [Limitations](../limitations.md) states
  what an exit 0 does and does not prove.
