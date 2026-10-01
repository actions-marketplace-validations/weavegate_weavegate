# Configuration reference

`weavegate run --config <path>` reads exactly one YAML document. Decoding is
strict: an unrecognized key — including a typo — or a trailing second YAML
document is rejected rather than silently ignored.

## Keys

| Key | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `target.db` | string | yes | — | Must start with `mysql:`; only MySQL is supported today. |
| `target.schema.migrations` | string (path) | yes | — | Directory of `*.sql` migration files, applied in filename order. Relative to the config file's own directory, not the current working directory. |
| `target.schema.seed` | string (path) | yes | — | Seed SQL file, applied after migrations. Same path-resolution rule as `migrations`. |
| `target.sut.adapter` | string | yes | — | `gonative` or `external`. Selects the adapter composition. |
| `target.sut.entrypoint` | string | `gonative` only | — | A built-in entrypoint ID, **not a path** (see [Built-in entrypoints](#built-in-entrypoints)). Forbidden for `external`. |
| `target.sut.variant` | string | yes | — | For `gonative`, one of the entrypoint's variants; for `external`, a valid wire name sent to the child. Overridable with `--variant`. |
| `target.sut.external` | object | `external` only | — | The owned JVM launch; forbidden for `gonative`. See [External JVM](#external-jvm). |
| `scenarios.<name>.workers` | list | yes, ≥1 | — | Each worker has `id`, `command`, and `args` (see [Worker args](#worker-args)). |
| `scenarios.<name>.sync_points` | list of strings | yes, ≥1 | — | The sync-point order every worker is coordinated against. |
| `oracle.assertions` | list | yes, ≥1 | — | Each assertion has `id`, `sql`, and `expect_rows`. |
| `oracle.assertions[].id` | string | yes | — | Must match `^[a-z0-9][a-z0-9-]*$`, and be unique within the list. |
| `oracle.assertions[].expect_rows` | int | yes | — | Must be `0`. The only implemented oracle is a zero-row SQL assertion. |
| `run.repeat` | int | no | `20` | How many times a schedule is replayed to check determinism. Must be positive if set. |
| `run.arrive_timeout_ms` | int | no | `3000` | See [Timing](#timing) below — this value has an outsized effect on run duration. |
| `run.explore_passes` | int | no | `3` | How many full candidate sweeps explore mode runs before declaring PASS. See [exit-codes.md](exit-codes.md) for what a PASS across N passes actually claims. |

`report:` is not a supported section yet — it is reserved for a future
PR-comment feature. Strict decoding therefore rejects a `report:` key the same
way it rejects any other unknown key, independently of the artifact count. Run
directories contain six base artifacts and a seventh `schedule.json` when a
schedule is present (see [report-schema.md](report-schema.md)).

Diagnostic codes are not declared by configuration. Weavegate derives them
from the Oracle kind or engine signal that produced the verdict and loads their
text from its embedded rule table. In particular, adding a `diagnostic:` key to
an assertion or anywhere else is rejected by strict decoding. See the
[WG001 reference](diagnostics/WG001.md) for the current assertion mapping.

## Built-in entrypoints

`target.sut.entrypoint` selects a Go adapter compiled into the binary — it
cannot point at an arbitrary directory, because the `gonative` adapter is a
Go function, not something that can be loaded dynamically from a path. The
only entrypoint registered today:

| ID | Adapter | Variants | Schedules directory |
| --- | --- | --- | --- |
| `matching-slice` | `gonative` | `vulnerable`, `fixed` | `fixtures/matching-slice/schedules` |

An unregistered entrypoint or unsupported Go-native variant is rejected with
the list of known values.

## External JVM

`adapter: external` launches exactly one prebuilt executable JAR per schedule
run with `java -jar <jar>`. Its child uses [wire v1](external-sut-v1.md) over
owned stdin/stdout. The JAR must contain `Main-Class` in the manifest's main
section and be self-contained; a nonempty manifest `Class-Path` entry is
rejected because the private JAR snapshot does not include sibling files.
Duplicate manifest entries are rejected because the JVM may select a different
entry from the one checked during preflight. The following is a **constructed
configuration example**, not captured output:

```yaml
target:
  db: mysql:8.4
  schema:
    migrations: ../db/migration
    seed: ../db/seed.sql
  sut:
    adapter: external
    variant: vulnerable
    external:
      java: java
      jar: ../app/seat.jar
      capacity: 2
      startup_timeout_ms: 30000
      cancel_timeout_ms: 5000
      stop_timeout_ms: 10000
```

| Key | Rule |
| --- | --- |
| `java` | Required executable path, or a bare command found on `PATH`. Paths containing a directory component are relative to the config directory. |
| `jar` | Required readable, regular, self-contained `.jar` file with exactly one manifest, `Main-Class` in its main section, and no nonempty `Class-Path`. Relative to the config directory. |
| `capacity` | Required integer from 1 to 1024, at least the selected scenario's worker count. |
| `startup_timeout_ms`, `cancel_timeout_ms`, `stop_timeout_ms` | Required positive integers at most 2147483647. Cancellation cannot exceed stop. |

There are no configurable shell arguments, environment overrides, credentials,
or wire-version switches. The fixture's application database account travels
only in the private start frame. The selected scenario supplies sorted unique
commands, ordered sync points, and scenario-wide worker parameters. The child
must confirm exactly those commands, points, and capacity at `ready`, before
any worker starts. Unknown commands in the application are detected at that
startup check; they cannot be inspected from a JAR during static preflight.
Preflight rejects a selected scenario whose encoded start data leaves less
than 4 KiB for the fixture-supplied database descriptor within the 1 MiB wire
frame limit. Each child launches from a private JAR snapshot whose bytes match
the digest recorded in `manifest.sut_sha256`. The run stops waiting for a
snapshot when its deadline expires; a blocked filesystem operation may finish
cleanup afterward.
For an external adapter, replay IDs resolve from saved runs or portable
schedule files; there is no embedded external schedule registry.

## Worker args

`scenarios.<name>.workers[].args` supplies the command parameters
(`map[string]string`) each worker runs with. The underlying engine's
`SUTConfig.Params` is scenario-wide, not per-worker, so **every worker in a
scenario must declare the same `args`** — a mismatch is rejected at load
time rather than silently using one worker's value and ignoring the rest.

```yaml
workers:
  - id: w1
    command: assign
    args:
      request_id: "42"
  - id: w2
    command: assign
    args:
      request_id: "42"   # must match w1's args exactly
```

## Timing

For `gonative`, four orchestrator timeouts are derived from
`run.arrive_timeout_ms` as fixed multiples: block-inference = 1×, step = 20×,
run = 60×, stop = 20×. The
default `arrive_timeout_ms` (3000) is safe but slow for a scenario with a
lock-blocked path — the `matching-slice` example config sets it to `250`,
because the "fixed" variant's blocked worker waits out this exact timeout on
every run.

For `external`, block inference and step retain 1× and 20×. The run deadline
is `startup_timeout_ms + 60 × arrive_timeout_ms`; it includes reset, launch,
startup, execution, and Oracle evaluation. Startup also has its own budget,
capped by the remaining run deadline. Stop uses `stop_timeout_ms` in a
detached cleanup context. The adapter reserves half for termination/reaping
and sends only its remaining graceful portion in the wire stop frame. All
arithmetic is checked before provisioning. See [ADR 0016](../adr/0016-external-cli-composition.md).

Measure the effect on the same replay rather than inferring it from one run.
The following commands time 20 repeats with the committed 250ms value, then
with a temporary config beside the original whose relative fixture paths stay
valid:

```bash
CFG=fixtures/matching-slice/.weavegate/config.yaml
FAST_OUT=$(mktemp -d)
/usr/bin/time -f 'arrive=250ms elapsed=%e seconds' \
  weavegate run --config "$CFG" --scenario concurrent-assign \
  --variant fixed --replay sch_ba00582f9632 --repeat 20 --out "$FAST_OUT"
test $? -eq 0

SLOW_CFG=$(mktemp fixtures/matching-slice/.weavegate/timing.XXXXXX.yaml)
trap 'rm -f "$SLOW_CFG"' EXIT
sed 's/arrive_timeout_ms: 250/arrive_timeout_ms: 3000/' "$CFG" > "$SLOW_CFG"
SLOW_OUT=$(mktemp -d)
/usr/bin/time -f 'arrive=3000ms elapsed=%e seconds' \
  weavegate run --config "$SLOW_CFG" --scenario concurrent-assign \
  --variant fixed --replay sch_ba00582f9632 --repeat 20 --out "$SLOW_OUT"
test $? -eq 0
```

Wall-clock results depend on the host; record both emitted timing lines when
making a quantitative comparison.

## Exploration candidate limit

The built-in exhaustive strategy counts the complete candidate space before
the fixture is created. The CLI accepts at most 5,000 candidates. A larger
scenario exits 5 during preflight instead of starting a container or lazily
discovering the excess after partial execution. The limit is not currently a
config key or CLI flag.

## Validation

Validation finishes before a database container starts. Errors name the
rejected key or indexed item; placeholders below show the shape of values that
vary with the input. Strict decoding rejects unrecognized keys, including
typos, rather than ignoring them. A second YAML document is also rejected with
`multiple YAML documents are not supported`. These file-wide rules are stated
outside the key table because they do not belong to one configuration key.

| Key | Enforced rules | Error form |
| --- | --- | --- |
| `scenarios` | At least one scenario is required, and a scenario name cannot be blank. | `scenarios: at least one scenario is required`; `scenarios: scenario name is blank` |
| `target.db` | The value is required and must start with `mysql:`. | `target.db is required`; `target.db "<value>" must have prefix "mysql:"` |
| `target.schema.migrations`, `target.schema.seed` | Both values are required. | `target.schema.<key> is required` |
| `target.sut.adapter` | The value is required and must be `gonative` or `external`. | `target.sut.adapter is required`; `target.sut.adapter "<value>" is not supported` |
| `target.sut.entrypoint` | Required for `gonative`; forbidden for `external`. Built-in IDs reject `/` and `.`. | `target.sut.entrypoint is required`; `target.sut.entrypoint is only supported with adapter gonative` |
| `target.sut.external` | Required for `external`; forbidden for `gonative`. Its keys and supported combinations are checked before provisioning. | `target.sut.external is required for adapter external`; `target.sut.external is only supported with adapter external` |
| `target.sut.variant` | The value is required. | `target.sut.variant is required` |
| `scenarios.<name>.workers` | At least one worker is required. | `scenarios["<name>"]: at least one worker is required` |
| `scenarios.<name>.workers[].id` | Every ID is required and must be unique within the scenario. | `workers[<index>]: id is required`; `workers[<index>]: duplicate worker id "<id>"` |
| `scenarios.<name>.workers[].command` | Every command is required. | `workers[<index>] "<id>": command is required` |
| `scenarios.<name>.workers[].args` | Every worker's map must exactly match the first worker's map. | `worker "<id>": args must match worker "<first-id>"'s args` |
| `scenarios.<name>.sync_points` | At least one sync-point is required; names cannot be blank or duplicated within the scenario. | `at least one sync-point is required`; `sync_points[<index>]: name is blank`; `sync_points[<index>]: duplicate sync-point "<name>"` |
| `oracle.assertions` | At least one assertion is required. | `oracle.assertions: at least one assertion is required` |
| `oracle.assertions[].id` | Every ID must match `^[a-z0-9][a-z0-9-]*$` and must be unique. | `invalid id "<id>": must match ^[a-z0-9][a-z0-9-]*$`; `duplicate assertion id "<id>"` |
| `oracle.assertions[].sql` | SQL cannot be blank. | `oracle.assertions[<index>] "<id>": sql is required` |
| `oracle.assertions[].expect_rows` | The key is required, and its explicit value must be exactly `0`. | `oracle.assertions[<index>] "<id>": expect_rows is required`; `oracle.assertions[<index>] "<id>": expect_rows must be 0, got <value>` |
| `run.repeat`, `run.arrive_timeout_ms`, `run.explore_passes` | Each value must be positive. Defaults are applied before validation when a key is omitted. | `run.<key> must be positive, got <value>` |

The `run.*` keys above receive defaults when omitted. `expect_rows` has no
default, so omitting it is an error rather than an implicit zero-row assertion.

## Example

The committed example, `fixtures/matching-slice/.weavegate/config.yaml`:

```yaml
target:
  db: mysql:8.4
  schema:
    migrations: ../db/migration
    seed: ../db/seed.sql
  sut:
    adapter: gonative
    entrypoint: matching-slice
    variant: vulnerable
scenarios:
  concurrent-assign:
    workers:
      - id: w1
        command: assign
        args:
          request_id: "42"
      - id: w2
        command: assign
        args:
          request_id: "42"
    sync_points:
      - after_read_request
      - before_insert_assignment
oracle:
  assertions:
    - id: active-assignment-is-unique
      sql: |
        SELECT project_request_id, COUNT(*) AS active_assignment_count
        FROM assignment
        WHERE status = 'ACTIVE'
        GROUP BY project_request_id
        HAVING COUNT(*) > 1
      expect_rows: 0
run:
  arrive_timeout_ms: 250
```
