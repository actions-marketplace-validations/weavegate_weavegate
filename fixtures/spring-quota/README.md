# Spring quota fixture

Two synthetic requests each reserve one unit from an account whose capacity is
one. The domain invariant is **remaining quota plus accepted receipt units
equals capacity**, with remaining quota between zero and capacity. A second SQL
assertion requires a receipt for each request, including a rejected request.

`QuotaCommands.reserve` uses one Spring `REQUIRED`, `REPEATABLE_READ` transaction
and the SDK-owned DataSource. Both workers receive `account_id=42, units=1`.
The vulnerable path writes a value computed from its first snapshot read. Two
requests can both be accepted while the second write loses the first decrement.
The fixed path uses `UPDATE ... SET remaining = remaining - ? WHERE remaining >= ?`
and accepts the request only if that current-row update succeeds.

| Path | Role |
| --- | --- |
| `db/migration/001_schema.sql`, `db/seed.sql` | Synthetic account and receipt data |
| `.weavegate/config.yaml` | External application, workers and SQL assertion Oracles |
| `app/` | Instrumented Spring Boot application; no verdict logic |
| `schedules/serial.json` | First request commits before the second snapshot read |
| `schedules/overlap.json` | Both requests read the original quota before either writes |

Build from the repository root with Java 21:

```bash
sdk/java/mvnw -B -f sdk/java/pom.xml -Dmaven.test.skip=true install
sdk/java/mvnw -B -f fixtures/spring-quota/app/pom.xml package
go build -o weavegate ./cmd/weavegate
```

With Docker available, replay the same vulnerable application against both
schedules, then replay the overlapping schedule against the fix:

```bash
./weavegate run --config fixtures/spring-quota/.weavegate/config.yaml \
  --scenario concurrent-reserve --variant vulnerable \
  --replay fixtures/spring-quota/schedules/serial.json
./weavegate run --config fixtures/spring-quota/.weavegate/config.yaml \
  --scenario concurrent-reserve --variant vulnerable \
  --replay fixtures/spring-quota/schedules/overlap.json
./weavegate run --config fixtures/spring-quota/.weavegate/config.yaml \
  --scenario concurrent-reserve --variant fixed \
  --replay fixtures/spring-quota/schedules/overlap.json
```

The expected exit codes are 0, 2 (`WG001`), and 0. Every command repeats 20 times
by default, resetting MySQL and starting a fresh JVM each time. The
[corpus reference](../../docs/reference/spring-regression-corpus.md) describes
the repeated acceptance test, retained database observations and commit barrier.
All schema and data are synthetic.
