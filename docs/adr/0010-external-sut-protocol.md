# ADR 0010: External SUT protocol and Spring transaction lifecycle

- Status: Accepted — external CLI selection implemented; paired Spring evidence pending
- Date: 2026-09-08
- Issue: [#107](https://github.com/weavegate/weavegate/issues/107)
- Inspected baseline: `078474f94cad6d1c1ffde0d44fada6853f769a97`

## Context

At the proposal date, [`sut.Adapter`](../../internal/sut/sut.go) was a Go interface,
[`syncpoint.Client`](../../internal/syncpoint/runtime.go) is in-process, and
configuration accepted only `gonative`. Spring support required a transport
boundary without transferring schedule control or verdict logic to the SUT.
This record selected the boundary; its later implementation adds CLI
configuration under [ADR 0016](0016-external-cli-composition.md). It does not
claim completed paired Spring acceptance.

## Decision

Use one child JVM per schedule execution, launched directly with a configured
Java executable and a prebuilt executable JAR. Use length-prefixed UTF-8 JSON
on the child's stdin/stdout as the sole control transport. The Go adapter owns
the child and pipes; stderr carries application logs. The child is a non-web
Spring Boot application with explicit named command and sync-point registration.
The [v1 wire contract](../reference/external-sut-v1.md) and
[conformance cases](../reference/external-sut-conformance.md) define both peers.

Anonymous pipes bind a connection to an owned child without a listening port,
endpoint discovery, authentication service, HTTP polling, or another server
framework. Framing permits bounded reads and multiline JSON without confusing
logs with messages. A single reader and serialized writer per peer multiplex
workers; an arrival blocks only its worker and Go bridge task, never the reader.
The adapter launches an argv array without a shell and does not build the JAR.
Instrumentation is explicitly enabled only for this child test profile. Disabled
SDK arrival calls return immediately, with no coordinator I/O or transaction
changes; normal application startup does not activate the protocol loop.

A new JVM per schedule costs startup time, but provides an explicit reset
boundary for pools, static state, worker threads, and arrival identities.
Attach, remote deployment, reusable JVM sessions, HTTP/gRPC, automatic
instrumentation, production hooks, JPA, reactive transactions, and databases
other than MySQL are outside this planned slice. No alternative launch or
transport mode is implemented alongside this one.

## Spring ownership and completion

Proposed initial support is Java 21, Spring Boot 4.0.x with its managed Spring
Framework 7.0.x dependencies, and MySQL 8.4. This is a deliberately narrow test
matrix, not a claim to cover all compatible JDKs or Boot releases. Boot 4's
upstream Java minimum is 17; choosing 21 here is a project baseline decision.
See the [Boot 4 migration guide](https://github.com/spring-projects/spring-boot/wiki/Spring-Boot-4.0-Migration-Guide).
Implementation must pin a supported patch and record the tested JDK, Boot,
Framework, and Connector/J versions; if the chosen line is no longer maintained
at implementation time, revise this proposal before claiming support.

Use Boot's JDBC starter for JDBC and pool integration, its managed Connector/J
for MySQL, and its managed Jackson JSON implementation for the protocol codec.
An explicit `DataSourceTransactionManager` controls the one fixture DataSource.
These are proposed Java dependencies with defined purposes; no dependency is
added by this ADR. Standard Java process streams and Go's standard library
suffice for transport. No web starter, messaging broker, or instrumentation
agent is needed.

Each registered command is a synchronous call from a nontransactional dispatcher
into a separate proxied Spring bean. The application declares one outer
`@Transactional` boundary with `REQUIRED` propagation and explicit rollback
rules for its failures, including the SDK's cancellation exception. Spring's
transaction manager owns begin/commit/rollback and the connection lease. The SDK
owns invocation identity, cancellation, sync-point gates, and completion
notification; it never commits a transaction itself. JDBC work uses the same
transaction-bound connection through `JdbcTemplate` or `DataSourceUtils`.
Self-invocation must not bypass the proxy; Spring documents this restriction in
[Using @Transactional](https://docs.spring.io/spring-framework/reference/data-access/transaction/declarative/annotations.html).

For v1, one invocation uses one thread and one transaction/connection lease.
Joined `REQUIRED` calls are allowed; `REQUIRES_NEW`, nested/savepoint transactions,
multiple DataSources, manually managed transactions, async work, background DB
writers, and application-managed migrations are unsupported. Readiness must
validate the registered entrypoint's transaction metadata; unsupported behavior
observed during a command is a fatal adapter error. The instrumented test
application must opt into these constraints explicitly.

The SDK records transaction outcome through synchronization, but that callback
cannot publish a terminal. In Framework 7.0.0,
[`AbstractPlatformTransactionManager`](https://raw.githubusercontent.com/spring-projects/spring-framework/v7.0.0/spring-tx/src/main/java/org/springframework/transaction/support/AbstractPlatformTransactionManager.java)
invokes completion callbacks before its final cleanup;
[`DataSourceTransactionManager`](https://raw.githubusercontent.com/spring-projects/spring-framework/v7.0.0/spring-jdbc/src/main/java/org/springframework/jdbc/datasource/DataSourceTransactionManager.java)
returns the connection during cleanup. Thus a callback alone is insufficient
evidence for `WorkerResult`'s connection-return requirement.

The planned SDK wraps the fixture DataSource's connection leases to record
successful delegate `close()` completion and any cleanup exception for the
invocation, even if Spring logs or suppresses that exception. The outer
dispatcher may send `terminal` only after the proxy has returned or thrown,
the transaction outcome is known, and the tracked lease has been returned.
Do not infer rollback from an exception: an after-commit callback can throw
after the database committed. Unknown commit/rollback outcome or failed lease
return sends `fatal`, never a fabricated successful or rolled-back terminal.
The Java implementation must prove these boundaries with the actual pool and
transaction manager, including failures hidden by framework cleanup.

## Engine ownership and extension-point gaps

[`AdapterFactory(syncpoint.Client)`](../../internal/orchestrator/orchestrator.go)
is sufficient for coordination. For each validated wire arrival, the adapter
starts one call to `client.Arrive(ctx, worker, point)`. A nil return means the
orchestrator released that exact arrival; the adapter then sends a release with
its full wire identity. A canceled/error return never sends release. The
orchestrator retains `Register`, `WaitArrive`, `Release`, `Finish`, and `Close`.
Neither the JVM nor the bridge receives the `Runtime` interface. Runtime point
constraints remain unchanged, including rejection of an immediate repeated
arrival at the same released point.

Worker IDs can be reused by `Handle` after its result channel closes; the
orchestrator currently registers each worker once per schedule. A distinct wire
invocation ID protects the general adapter contract without requiring the
runtime to register a terminal worker again. Runtime factories remain fresh per
schedule. Factory closure state can hold launch settings, the declared worker
capacity, and a correlation run ID; session IDs are generated per adapter.

G1 is resolved by the fixture-owned descriptor contract. G2, G5, and G6 are
resolved at the Go boundary by [ADR 0014](0014-adapter-outcome-boundaries.md).
At the proposal date, G3 and G4 were implementation blockers. The table below
records the original gaps and decisions requested at that time.

| Gap | Current code and missing capability | Required follow-up decision |
| --- | --- | --- |
| G1: Connection provisioning (resolved) | [`fixture.DB`](../../internal/fixture/fixture.go) exposes `ConnectionDescriptor` beside `SQL`; both address the same prepared database through its application account. | The [descriptor contract](../reference/fixture-connection.md) defines structured metadata, secret access/redaction, Reset preservation, Teardown invalidation, separate administrator access, and fresh reprovisioning credentials. External adapters consume it without adding another provisioner. |
| G2: Asynchronous adapter failure (resolved) | Handle exposes a typed, latched session fault independently of invocation outcomes. | ADR 0014 observes faults during execution, provisional evaluation, and final cleanup; causes remain Run errors without fabricated worker terminals. |
| G3: Reset after failed shutdown | `Run` defers Stop and joins its error, but returns its run gate even when Stop fails. Replay stops on error; a later direct `Run` can still call Reset. | Define fixture quarantine/invalidation after unproven child or DB-session cleanup; reject reuse until teardown and successful reprovisioning. Process exit alone does not prove server rollback finished. |
| G4: Adapter selection and budgets | [`config`](../../internal/config/config.go) and [`Resolve`](../../cmd/weavegate/resolve.go) only resolve built-in Go entrypoints. Start consumes the existing run budget, which may be too small for JVM startup. | Specify one launch configuration, command/point preflight, capacity, and startup/run/stop budget composition before adding external dispatch. Keep argv and credentials separate; update config docs and validation markers in that change. |
| G5: Invocation never started (resolved) | Invoke returns one InvocationOutcome stream containing a WorkerResult or an UnstartedResult, then closes. | ADR 0014 requires resource return and reservation release; unstarted outcomes never call runtime Finish and are retained separately as run evidence. |
| G6: Operation cancellation versus worker outcome (resolved) | Run retains truthful worker results and separately returns the operation context error. | ADR 0014 sets the final cancellation/fault observation after Stop, collection, and runtime Close; provisional evaluation is discarded on run error. |

G1 was resolved by [#118](https://github.com/weavegate/weavegate/issues/118).
[#119](https://github.com/weavegate/weavegate/issues/119) records the separately
reviewable G2/G5/G6 decisions in ADR 0014 and implements the Go boundary.
[#120](https://github.com/weavegate/weavegate/issues/120) resolved G3 with
fixture quarantine. [ADR 0016](0016-external-cli-composition.md) resolves G4
through [CLI integration #110](https://github.com/weavegate/weavegate/issues/110).
[#121](https://github.com/weavegate/weavegate/issues/121) tracks executable
conformance acceptance across the Go and Java implementations. A wire terminal
is a protocol completion fact; it does not by itself authorize a WorkerResult or
runtime Finish. External mapping still must prove the selected boundaries.

For Java MySQL errors, the bridge can preserve vendor code and SQLSTATE using
the already-used Go MySQL error type so the existing classifier recognizes
1213. That translation needs no verdict logic. Lock timeout 1205 remains an
ordinary worker error. Wire IDs, frame order between workers, elapsed times,
process IDs, connection endpoints, and logs must not enter normalized schedule
or verdict fingerprints.

## Consequences and acceptance boundary

Oracles continue to query committed state through the fixture's Go pool and
alone determine verdicts. A command's successful commit says nothing about
whether an invariant holds. Cancellation and process death can leave outcome
unknown; preserving that uncertainty is more important than manufacturing a
terminal to finish a schedule.

External CLI selection is implemented. The
[shared checklist](../reference/external-sut-conformance.md#implementation-checklist)
still governs complete language and paired acceptance; this ADR alone is not
evidence that either peer has passed every conformance case.
