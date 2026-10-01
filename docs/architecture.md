# Architecture

Weavegate separates control, application execution, and judgment. Fixtures and
adapters describe what to run; the orchestrator controls when workers advance;
oracles alone decide whether the resulting state is acceptable.

## Execution flow

```mermaid
flowchart TD
    Input[config + scenario + schedule] --> CLI[CLI/control layer]
    CLI -->|provision| Fixture[fixture]
    CLI -->|construct with provisioned DB| Orchestrator[orchestrator]
    Orchestrator -->|reset per schedule| Fixture
    Orchestrator -->|invoke workers| Adapter[adapter]
    Adapter --> SUT[SUT]
    SUT -->|Arrive| Runtime[sync-point runtime]
    Runtime -->|targeted release| SUT
    SUT -->|terminal results| Orchestrator
    Orchestrator -->|normalized trace + terminals| Oracle[oracle evaluation]
    Oracle -->|complete results + trace + terminals| Fingerprint[fingerprint + replay aggregation]
    Oracle -->|oracle violations| Diagnostics[diagnostic rules]
    Fingerprint -->|engine determinism signal| Diagnostics
    Oracle -->|oracle results| Reports[reports]
    Fingerprint -->|fingerprint counts| Reports
    Diagnostics -->|diagnostics| Reports
```

The CLI/control layer asks the fixture package to provision MySQL from an
immutable migration and seed snapshot, then constructs the orchestrator with
that database and fixture handle. The handle contains both the Go pool and a
[fixture-owned application connection descriptor](reference/fixture-connection.md)
for adapters that cannot consume that pool. The orchestrator requests a reset
before each schedule; it does not provision the fixture. The adapter starts the
selected application integration and invokes worker commands on dedicated connections.
The orchestrator executes a validated schedule through the sync-point runtime
and waits for every worker's terminal state before evaluating the oracle set.
Only then does it fingerprint the complete oracle evaluation together with the
normalized trace and terminals. Replay aggregates those fingerprints before
diagnostic rules classify both oracle violations and the resulting engine
determinism signal. The report package writes the public artifacts.

## Boundaries

| Boundary | Owns | Must not own |
| --- | --- | --- |
| Fixture | Synthetic schema, seed, reset behavior, scenario data, and prepared-database connection ownership | Engine control flow or verdict logic |
| CLI/control layer | Config resolution, fixture provisioning and cleanup, dependency composition | Per-schedule coordination or invariant judgment |
| Adapter | Starting and stopping a SUT; asynchronous worker invocation | Schedule policy or invariant judgment |
| Sync-point runtime | Named arrival, targeted release, and worker lifecycle state | Database assertions |
| Orchestrator | Exploration/replay execution, per-run fixture reset, deadlines, normalized trace and terminal collection | Fixture provisioning, fixture-specific business rules, or verdicts |
| Oracle | Post-run invariant evaluation and deterministic evidence rows | Worker release order or fixture provisioning |
| Diagnostic rules | Stable code, explanation, and help text for oracle-violation and engine-signal triggers | Discovering violations or computing determinism |
| Report | Conversion to the documented artifact schema and Markdown rendering | Re-evaluating the run |

The public [report schema](reference/report-schema.md) is owned by the report
package rather than being a serialization of internal engine structs. The
adapter contract is visible in [`internal/sut`](../internal/sut/sut.go), and
the oracle contract in [`internal/oracle`](../internal/oracle/oracle.go).
The [adapter outcome contract](reference/adapter-outcomes.md) separates worker
transaction facts, unstarted invocations, session faults, and operation
cancellation. Evaluation remains provisional through final cleanup.

## Extension points

| Extension | Add | Keep unchanged |
| --- | --- | --- |
| Fixture | Synthetic data, workflow commands, scenarios, and evidence tests under `fixtures/` | Orchestrator and oracle protocols |
| Adapter | An implementation of the SUT adapter/handle protocol plus composition wiring | Scheduling and report packages |
| Oracle | An implementation of the oracle interface and its evidence normalization | Orchestrator verdict logic, because none belongs there |
| Schedule strategy | Candidate enumeration behind the scenario strategy contract | Fixture workflows and oracle evaluation |
| Diagnostic | One embedded rule and its one-to-one reference page | Oracle semantics and report schema |

These are extension points, not promises of runtime plugin loading. The CLI
supports its registered `matching-slice` Go-native entrypoint and an owned
external JVM selected through
[configuration](reference/config.md#external-jvm). It does not load arbitrary
Go code or discover Java application commands before child startup.

Adding a fixture, oracle, schedule strategy, or adapter should not require a
change to an engine package. If it does, treat that as a missing boundary and
raise an issue instead of placing fixture-specific behavior in the engine.
