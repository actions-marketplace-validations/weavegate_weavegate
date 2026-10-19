# Spring regression fixtures

The corpus contains three business mechanisms implemented through the existing
external Spring adapter and SQL assertion Oracle. The two new fixtures require
no engine or SDK changes. Each has schema, seed, application, configuration and
portable schedules under `fixtures/`.

| Fixture | Mechanism | Invariant | Fix |
| --- | --- | --- | --- |
| [spring-matching](../../fixtures/spring-matching/README.md) | Read-check-insert assignment | At most one active assignment per request | Lock the request before checking assignments |
| [spring-quota](../../fixtures/spring-quota/README.md) | Lost decrement from a stale quota snapshot | Remaining quota plus accepted units equals capacity | Conditional atomic decrement and affected-row acceptance |
| [spring-job-claim](../../fixtures/spring-job-claim/README.md) | Stale owner completes a reassigned job | Completion epoch equals the current lease epoch | Recheck epoch and running state in the completion update |

Matching is the existing control. Quota tracks resource conservation and request
results; job-claim tracks ownership and a state transition. They are not renamed
copies of the assignment uniqueness check. This foundation does not establish
holdout detection rates, test-asset costs, stress-test superiority or performance
improvements. Those experiments remain planned separately.

## Snapshot and commit boundaries

Both new applications explicitly declare `REPEATABLE_READ`, `REQUIRED`
transactions. There is one SDK-managed DataSource and one transaction/connection
lease per command. Both workers have identical arguments within a scenario.
Only the saved schedule changes between serial and overlapping replays of a
variant; schema, seed, JAR and isolation remain the same.

Each command has four points, in this order:

1. `before_read`: the transaction has begun, but no application query has run.
2. `after_read`: the first nonlocking table read has captured the quota or job
   snapshot. The receipt later records the values read here.
3. `before_write`: no application writes have occurred. Separating this from
   `after_read` lets a schedule confirm both reads before allowing either write.
4. `after_commit`: a Spring `TransactionSynchronization.afterCommit` callback
   arrives after commit, still inside the command proxy on its worker thread.

InnoDB establishes the REPEATABLE READ snapshot at the first consistent read,
not merely at transaction begin. Starting both workers at `before_read` therefore
does not force the same snapshot. See the
[MySQL 8.4 consistent-read contract](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html).

The commit callback performs no JDBC, starts no transaction and returns only
after its point is released. It proves that the write is committed before the
other worker is released to read. It does **not** prove connection return:
Spring still owns cleanup after the callback. See
[Spring's callback contract](https://docs.spring.io/spring-framework/docs/7.0.9/javadoc-api/org/springframework/transaction/support/TransactionSynchronization.html#afterCommit())
and the SDK's [completion contract](external-sut-java.md#completion-and-cancellation).
The SDK emits a terminal only after proxy exit, transaction completion and lease
return; the test separately checks JVM exit and server session absence before
each reset and teardown.

No point lies between a command's first write and its commit. A worker
released at `before_write` runs its update, its receipt insert and the commit
before it next arrives, at `after_commit`. Any schedule over these points,
selected or enumerated, therefore never parks a worker that holds InnoDB row
locks. Interleavings with uncommitted writes, lock waits and deadlocks are
outside these fixtures.

## Selected schedules

`serial.json` releases all four points of `w1` before allowing `w2` to read.
Both variants pass: quota accepts only the first request, and job completion
prevents subsequent reassignment of that completed lease.

Both `overlap.json` files first release `w1/before_read`, `w1/after_read`,
`w2/before_read`, `w2/after_read`. Both snapshots are now established and both
workers are parked before writing. Quota then commits `w1` before `w2` writes;
job-claim commits reassignment (`w2`) before completion (`w1`) writes.

| Fixture / schedule | Vulnerable post-state | Fixed post-state |
| --- | --- | --- |
| Quota / serial | PASS: remaining 0, one accepted request, observed quotas `1,0` | Same |
| Quota / overlap | WG001: remaining 0, two accepted requests, observed quotas `1,1` | PASS: remaining 0, one accepted request, observed quotas `1,1` |
| Job / serial | PASS: epoch 1 DONE by epoch 1; reassignment rejected | Same |
| Job / overlap | WG001: epoch 2 DONE by stale epoch 1 | PASS: epoch 2 RUNNING; stale completion rejected |

Each command reads its row once, before any write. The overlapping violations
come from the application acting on a value read before the other worker's
commit; they do not depend on REPEATABLE READ reusing that snapshot for a later
statement. The corpus makes no claim that its outcomes are sensitive to the
isolation level.

These are two deliberately selected schedules per new fixture, not exhaustive
schedule coverage. The test requires their realized releases to equal the saved
intent exactly, with no timeout-inferred or skipped steps. It checks a single
normalized fingerprint across all repetitions and records receipt snapshots to
make the first-read premise inspectable. No sleep or timeout is used to choose
the application interleaving.

A saved schedule ID is content-addressed over its steps alone. The two
`serial.json` files have identical steps and therefore share
`sch_3d0889b6860a`; a schedule identifies a corpus case only together with its
fixture.

## Reproduce and retain the acceptance evidence

From the repository root, with Go, Docker and Java 21:

```bash
sdk/java/mvnw -B -f sdk/java/pom.xml -Dmaven.test.skip=true install
sdk/java/mvnw -B -f fixtures/spring-quota/app/pom.xml package
sdk/java/mvnw -B -f fixtures/spring-job-claim/app/pom.xml package
WEAVEGATE_SPRING_CORPUS=1 WEAVEGATE_SPRING_CORPUS_OUT=/tmp/spring-corpus-evidence \
  go test ./cmd/weavegate -run '^TestSpringRegressionCorpus$' -v -count=1 -timeout 30m
```

`TestSpringRegressionCorpus` runs each of the eight fixture/schedule/variant
combinations 20 times through the CLI run path. Each repetition resets MySQL and
starts a fresh JVM. The test checks PASS/exit 0 or WG001/exit 2, the violating
assertion and the exact values of its violating row, byte-identical saved
schedules, fingerprints and terminal success. It also verifies the seed after every reset, captures the committed
state before the next reset or teardown, and requires no surviving child JVM,
Connector/J sessions or JAR snapshots. Polling for server session retirement is
only a cleanup observation, never worker coordination.

The output directory receives a new `corpus-*` subdirectory per test execution.
Under each fixture/order/variant, `runs/` contains the ordinary seven CLI
artifacts, including `manifest.json`, `scenario.json`, `observation.json`,
`trace.json`, `schedule.json`, `report.json` and `report.md`.
`database-observations.json` separately retains one committed-state observation
per repetition, including the first-read receipt values. It is test evidence,
not an additional CLI artifact contract. Manifest JAR/schema/seed digests must
agree across the compared schedules and variants of each fixture.

The `Spring regression corpus` workflow builds the applications from the same
revision, runs this command and checks all eight `SPRING_CORPUS_RESULT` markers
with `grep -F`. Its `spring-regression-corpus` artifact retains run directories,
database observations, logs, the command, source revision, JAR checksums and the
executed MySQL image metadata. It does not replace or weaken the existing
`external Spring replay (paired acceptance)` smoke job, which continues to
verify matching and the protocol lifecycle probes.

These replays cover Java 21, the Boot 4.0.8 baseline pinned in the POMs and MySQL
8.4 with the declared schema and inputs. The environment marker reports the
actual nested JAR versions. Fixture acceptance is not the later E3/E4/E5
evaluation, and the selected schedules are not a claim about arbitrary Spring
applications or other isolation levels.
