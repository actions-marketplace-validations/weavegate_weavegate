# Write a Fixture

A fixture is a synthetic workflow and its data, not a copy of a production
schema. Build it from five pieces in this order: scenario, schema, seed, oracle,
and configuration. The existing
[matching-slice fixture](../../fixtures/matching-slice/README.md) is the
reference implementation.

This guide covers the fixture's technical shape. Follow
[CONTRIBUTING](../../CONTRIBUTING.md#contributing-a-fixture) for proposal,
branch, commit, test, and pull-request rules.

## 1. Define the scenario

Write down one domain invariant and one concurrent workflow that can violate
it. A useful scenario names:

- the command each worker invokes;
- the arguments shared by those workers;
- the semantic sync-points each worker may reach;
- a vulnerable and a fixed implementation of the same workflow.

### Implement and register the built-in workflow

The scenario must have executable worker commands. Implement the instrumented
Go-native workflow and its command registry under `fixtures/<name>/sut/`; the
configured command names must be keys returned by that registry. Keep the
sync-point dependency behind the adapter protocol and use a production no-op,
as shown in the [instrumentation guide](instrument.md).

The current CLI compiles entrypoints into the binary rather than loading them
dynamically. Register a CLI-runnable fixture in
[`cmd/weavegate/registry.go`](../../cmd/weavegate/registry.go) with:

- a `NewAdapter` factory that binds the fixture registry to the sync-point
  client;
- the supported vulnerable and fixed variant names; and
- the fixture's embedded saved-schedule filesystem.

This is built-in composition wiring, not fixture-specific logic in an engine
package. The matching-slice [`registry.go`](../../cmd/weavegate/registry.go)
entry and [fixture SUT registry](../../fixtures/matching-slice/sut/registry.go)
are the current executable example.

Keep the worker set and point order small enough to explain. The reference
scenario has two `assign` workers and two points:
`after_read_request` and `before_insert_assignment`. Its committed
[`schedule`](../../fixtures/matching-slice/schedules/concurrent-assign.json)
records worker/point pairs, while the fixture README explains what each
release means in the domain.

Do not use sleeps to coordinate workers. The same schedule must produce the
same verdict on repeated runs.

## 2. Add the schema

Put ordered migration files under `fixtures/<name>/db/migration/`. Use only
synthetic tables, names, and constraints needed to express the invariant. A
migration must build an empty fixture database from scratch.

For example, the matching-slice
[`001_schema.sql`](../../fixtures/matching-slice/db/migration/001_schema.sql)
defines requests, sessions, and assignments. Its intentionally non-unique
request index permits the anomaly that the fixture is designed to expose; the
fixture README states that choice rather than presenting it as a recommended
production schema.

## 3. Add the seed

Put deterministic starting data in `fixtures/<name>/db/seed.sql`. Use fixed,
synthetic identifiers so commands, assertions, and expected evidence can refer
to the same rows. The reset path must restore exactly this state between runs.

The reference [`seed.sql`](../../fixtures/matching-slice/db/seed.sql) starts
with request `42`, no matching sessions, and no assignments. Its lifecycle
test verifies seed, mutation, and reset rather than relying on container
restart behavior.

## 4. Declare the oracle

An oracle owns the verdict. Write a read-only SQL assertion that returns zero
rows when the invariant holds and evidence rows when it does not. Keep verdict
logic out of the scenario, fixture workflow, and orchestrator.

The reference `active-assignment-is-unique` oracle groups active assignments by
request and returns only counts greater than one. The query is visible in the
fixture's [configuration](../../fixtures/matching-slice/.weavegate/config.yaml),
and its behavior is documented under
[SQL assertion verdict](../../fixtures/matching-slice/README.md#sql-assertion-verdict).

Give every assertion a stable lowercase ID. Make the returned columns useful
as evidence: they are preserved in `observation.json` and rendered in the
report. See the [report schema](../reference/report-schema.md#observationjson).

## 5. Wire the configuration

Create `fixtures/<name>/.weavegate/config.yaml` and connect the earlier pieces:

1. point `target.schema.migrations` and `target.schema.seed` at the fixture's
   database files;
2. select the supported SUT adapter and the entrypoint and variant registered
   in step 1;
3. declare the scenario workers, arguments, and ordered `sync_points`;
4. add the oracle assertion ID, SQL, and `expect_rows: 0`;
5. set a measured arrival timeout only when the default is inappropriate.

Paths are resolved relative to the configuration file. Worker arguments must
match across a scenario, and unknown YAML keys are rejected. The complete
contract is in the [configuration reference](../reference/config.md).

## Verify the fixture

Add tests that prove schema setup, seed state, mutation, reset, vulnerable
violation, fixed PASS, and repeated replay. Emit stable result markers for CI
and run the applicable Docker-backed checks from
[CONTRIBUTING](../../CONTRIBUTING.md#running-checks).

If a fixture, oracle, schedule strategy, or adapter requires an engine change,
stop. That is an extension-point gap, not permission to force fixture-specific
logic into the engine. Open an issue describing the missing boundary before
writing engine code. The CLI recognizes registered Go-native entrypoints and
the [external JVM adapter](../reference/config.md#external-jvm). It does not
dynamically load Go fixture code or inspect application commands from a JAR
before startup.
