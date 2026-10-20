# Spring job-claim fixture

A synthetic job has a running lease identified by epoch 1. One command completes
that lease while another reassigns it to epoch 2. The domain invariant is
**a completed job must have been completed by its current lease**. A running job
has no completion epoch. A second SQL assertion requires one completion receipt
and one reassignment receipt, including rejected operations.

Both command beans use Spring `REQUIRED`, `REPEATABLE_READ` transactions and the
SDK-owned DataSource. Both workers receive `job_id=42, expected_epoch=1`; worker
and invocation identifiers never serve as ownership tokens. Worker `w1` invokes
`complete`, and `w2` invokes `reassign`.

The vulnerable completion checks ownership only in its snapshot, then writes
`DONE` using the job ID alone. A stale owner can therefore complete a job already
reassigned to epoch 2. The fixed completion rechecks epoch and running state in
the `UPDATE` predicate. Reassignment always uses that predicate and only advances
a still-running epoch; it is identical in both variants. A failed predicate is
a rejected operation, not a worker error.

| Path | Role |
| --- | --- |
| `db/migration/001_schema.sql`, `db/seed.sql` | Synthetic job and operation receipts |
| `.weavegate/config.yaml` | External application, commands and SQL assertion Oracles |
| `app/` | Instrumented Spring Boot application; no verdict logic |
| `schedules/serial.json` | Completion commits before reassignment reads the job |
| `schedules/overlap.json` | Both read epoch 1, then reassignment commits before completion writes |

Build from the repository root with Java 21:

```bash
sdk/java/mvnw -B -f sdk/java/pom.xml -Dmaven.test.skip=true install
sdk/java/mvnw -B -f fixtures/spring-job-claim/app/pom.xml package
go build -o weavegate ./cmd/weavegate
```

With Docker available:

```bash
./weavegate run --config fixtures/spring-job-claim/.weavegate/config.yaml \
  --scenario complete-and-reassign --variant vulnerable \
  --replay fixtures/spring-job-claim/schedules/serial.json
./weavegate run --config fixtures/spring-job-claim/.weavegate/config.yaml \
  --scenario complete-and-reassign --variant vulnerable \
  --replay fixtures/spring-job-claim/schedules/overlap.json
./weavegate run --config fixtures/spring-job-claim/.weavegate/config.yaml \
  --scenario complete-and-reassign --variant fixed \
  --replay fixtures/spring-job-claim/schedules/overlap.json
```

The expected exit codes are 0, 2 (`WG001`), and 0. Every command repeats 20 times
by default, resetting MySQL and starting a fresh JVM each time. The
[corpus reference](../../docs/reference/spring-regression-corpus.md) describes
the repeated acceptance test, retained database observations and commit barrier.
All schema and data are synthetic.
