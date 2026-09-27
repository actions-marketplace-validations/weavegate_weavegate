# Java Spring external SUT peer (in development)

[`sdk/java`](../../sdk/java/) implements the Java peer of
[wire v1](external-sut-v1.md) for an explicitly instrumented Spring Boot test
application. Its isolated Java acceptance gate passes on the pinned stack. It
is not published as a package, and the `weavegate` CLI does not select it;
CLI composition and paired live replay remain under
[#110](https://github.com/weavegate/weavegate/issues/110) and
[#111](https://github.com/weavegate/weavegate/issues/111).
[Issue #141](https://github.com/weavegate/weavegate/issues/141) defines this
JDBC/SQL/Spring support matrix and its isolated acceptance evidence.
The support contract below is for a trusted, reviewed fixture application on
the pinned stack. The guards are not a general MySQL parser or an application
sandbox. [ADR 0015](../adr/0015-java-execution-boundary.md) records that
decision and the fixture author's obligations.

## Execution support matrix

| Boundary | Supported path and owner | Rejected or outside support | Evidence |
| --- | --- | --- | --- |
| Transaction outcome | One SDK `DataSourceTransactionManager` `REQUIRED` transaction per command; SDK records driver commit or rollback | Application JDBC commit, rollback, auto-commit, savepoints; nested or suspended transactions | `SpringTransactionsTest.springTransactionBoundaries` against MySQL 8.4; `TrackingHandlesTest` for rejected entrypoints |
| Lease ownership and navigation | One tracked lease on the worker thread; JDBC `Connection`, `Statement`, `ResultSet` navigation returns tracked proxies | Second or foreign-thread lease, vendor `unwrap`, metadata access, result-set mutations, JDBC outside the transaction or startup probe | `SpringTransactionsTest.springTransactionBoundaries`; `TrackingHandlesTest.jdbcNavigationCannotEscapeTracking`, mutation rejection and lease tests |
| Cancellation and completion | Tracked statement execution, row navigation and close are cancellable; proxy exit, known transaction outcome and returned lease precede terminal | Detached work, retained handles, completion callbacks performing JDBC after transaction outcome | `SpringTransactionsTest.springTransactionBoundaries`, `postCommitCallbacksCannotPerformJdbcWork`, `TrackingHandlesTest.resultSetAndStatementCloseRemainCancellable` |
| Timeouts and session state | Zero JDBC timeout settings; one reviewed statement per call | Nonzero JDBC query, network, validation or login timeout; named locks, session variables, SQL transaction control, server-side `PREPARE`/`EXECUTE`, `CALL` | `TrackingHandlesTest` timeout, named-lock and SQL admission tests; `SpringTransactionsTest.rejectedSessionSqlLeavesPooledConnectionUnchanged` on MySQL; fatal/rollback checks |
| SQL and database objects | Trusted single-statement `SELECT`, `INSERT`, `UPDATE`, `DELETE` over reviewed transactional InnoDB fixture tables | JDBC batches, DDL, `SET`, `SELECT ... INTO`, multi-statements, stored routines, triggers, events, UDFs, nontransactional or temporary tables, nondeterministic SQL | SQL admission and batch rejection unit tests and MySQL rollback checks cover identified rejected forms; fixture review owns database-object restrictions |
| Spring command dispatch | Selected public `void` methods via inspectable JDK or CGLIB proxy; one matching SDK transaction advisor; failure observer immediately inside it | Static/final methods, async returns, matching runtime transaction pointcuts, opaque/frozen proxies, self-invocation | `RegistrationTest` for shapes and advice; `SpringTransactionsTest` for actual JDK/CGLIB proxies and transactions on MySQL |

The listed tests prove only their stated cases. A fixture author must inspect
the schema and every command SQL for stored functions, triggers, other server
side effects, nontransactional engines and nondeterminism. The application must
not open another driver connection, launch background work or use another
database access path. The SQL guard recognizes specified escape forms; it
cannot establish those obligations for arbitrary expressions or objects.

## Supported baseline

The Maven build enforces one tested baseline. Versions come from the Spring Boot
parent's dependency management; the build fails on other Java or Maven versions.

| Component | Version | Purpose |
| --- | --- | --- |
| Java | 21 | Project baseline selected by [ADR 0010](../adr/0010-external-sut-protocol.md) |
| Spring Boot / Framework | 4.0.8 / 7.0.9 | Non-web application context, `@Transactional` proxies |
| Transaction manager | `DataSourceTransactionManager` (spring-jdbc 7.0.9) | Begin, commit, rollback and connection cleanup |
| Pool | HikariCP 7.0.2 | The single fixture DataSource |
| JDBC driver | MySQL Connector/J 9.7.0 | MySQL 8.4 fixture access and statement cancellation |
| Codec | Jackson 3.1.5 | Strict JSON with duplicate-key and trailing-value rejection |
| Build | Apache Maven 3.9.16 through `./mvnw`, checksum-pinned ZIP (`unzip` required for bootstrap) | Reproducible build and test runner |

Test-only dependencies are Spring Boot's test starter, the JUnit launcher API
for execution evidence and Testcontainers for a real MySQL 8.4 server.

## Opting in

Instrumentation is inactive unless the child JAR's `main` method calls the
bootstrap. An ordinary `SpringApplication.run` never reads stdin, starts a
protocol thread or changes transactions, and inactive sync points return
immediately.

```java
public final class InstrumentedMain {
    public static void main(String[] args) {
        WeavegateChild.run(MyApplication.class, args);
    }
}
```

The bootstrap reserves stdout for frames before Spring Boot starts, redirects
`System.out` to stderr and disables the banner. It reads `start`, then builds a
non-web context with the fixture DataSource and a transaction manager supplied
by the SDK. Credentials arrive only in the start frame. SQL initialization,
Flyway and Liquibase are disabled. Startup fails if the context contains another
DataSource or transaction manager, or enables `@Scheduled` or `@Async`
processing; processor beans are detected by type, regardless of bean name.

Commands are synchronous public, non-final instance methods returning `void` on
proxied Spring beans; asynchronous return types are rejected at registration. The bean
class itself may be package-private; the selected proxy method is made reflectively
accessible before dispatch. Static methods and proxies without accessible,
matching transaction advice are rejected. JDK proxies are inspected through their
target class, and commands must be exposed on a proxy interface for dispatch.
Static transaction pointcuts that do not match the command are ignored; each
matching advisor receives an observer scoped to its command pointcut. Matching
runtime pointcuts remain unsupported. Synchronous command-specific advice may
run inside the transaction and failure observer. Advice outside the transaction
must not access fixture JDBC or defer work past proxy return.
For a JDK proxy, static pointcuts and transaction attributes are checked against
the interface method that Spring actually invokes; an advisor or attribute source
matching only the implementation method does not establish a runtime transaction.
Each needs one `@Transactional` boundary with `REQUIRED` propagation, no
wall-clock timeout, and rollback behavior for `WeavegateCancelledException`
(the default rule for runtime exceptions does).
The dispatcher calls the bean proxy, so self-invocation cannot bypass it.

```java
@Service
public class SeatCommands {
    private final JdbcTemplate jdbc;

    public SeatCommands(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Transactional
    @WeavegateCommand(value = "assign", points = {"after_read"})
    public void assign(CommandContext context) {
        jdbc.queryForObject("SELECT taken_by FROM seat WHERE id = 1 FOR UPDATE", String.class);
        Weavegate.syncPoint("after_read");
        jdbc.update("UPDATE seat SET taken_by = ? WHERE id = 1", context.worker());
    }
}
```

Readiness validates every requested command and point against the selected
command registrations and requires matching transaction advice to resolve to
the SDK-owned transaction manager. It retains each command's declared point set,
then completes a database probe that returns its lease. Every runtime arrival
must be both requested for the session and declared by the invoked command.
Application startup callbacks cannot lease the fixture database; only the SDK's
readiness probe may lease without an invocation. A probe lease left open before
ready or after application closure prevents normal stopped. One
invocation runs on one worker thread with one transaction and one connection
lease. Nested transactions, `REQUIRES_NEW` suspension, a second lease, database
use outside an invocation or the readiness probe, retained JDBC handle use from
another thread and sync points outside the worker's proxy call are session failures.
Rejected outside-invocation leases are closed before application code can use
them. Application JDBC is permitted only after the SDK transaction begins;
SDK-owned begin and cleanup operations retain access to their connection.
Application calls to JDBC auto-commit, commit, rollback or savepoint controls
are rejected through both connection methods and direct or prepared SQL;
SQL-level `PREPARE`, `EXECUTE`, and prepared-statement deallocation are also
rejected before delegation because they can hide transaction control. Direct
`CALL` and JDBC callable statements are rejected because a procedure can commit
internally. `GET_LOCK`, `RELEASE_LOCK`, and `RELEASE_ALL_LOCKS` SQL calls are
rejected because named locks outlive transaction completion and pool lease return.
Only the SDK-owned transaction manager may use transaction controls.
Application connection methods that change session settings or terminate the
connection, including `abort`, catalog/schema, client info and sharding keys,
are rejected. JDBC factories for LOBs, arrays, SQLXML and structs are rejected
because their returned resources would escape tracking. Result-set object,
stream and resource getters, result-set updates, inserts and deletes, and
statement `closeOnCompletion` are likewise unsupported; scalar getters,
current result-set column metadata and explicit
tracked close are the supported path.
SQL that can commit implicitly or act outside the transaction, including DDL,
table locks, account management, `SET` session changes and administrative
statements, is rejected before JDBC delegation as a fatal unsupported adapter
operation. Admission accepts only a single `SELECT`, `INSERT`, `UPDATE` or
`DELETE` statement; `Statement` and `PreparedStatement` batch additions and
executions are rejected before delegation. Semicolons outside quoted values
are rejected. A `SELECT`
with `INTO`, a session-variable reference or a recognized session-changing
function is rejected before delegation. Ordinary comments are skipped while
MySQL executable comments retain their body for this check. These guards do
not certify arbitrary function calls or fixture schema objects. SQL optimizer
`MAX_EXECUTION_TIME` hints, nonzero JDBC statement query timeouts, connection
network timeouts, connection validation timeouts and DataSource login timeouts
are unsupported because they
would make a schedule depend on wall-clock time; zero continues to mean no timeout.

Standard JDBC `unwrap` returns the tracking proxy when that interface is
supported; vendor-specific unwrapping is rejected. Statement and result-set
navigation retain tracked handles, including `getConnection()` and
`getStatement()`. JDBC metadata access is rejected because metadata queries
cannot be registered for statement cancellation. This also prevents a metadata
handle from being retained during startup. Blocking result-set navigation
and result-set or statement close operations register their owning statement for
cancellation, so streaming row drains cannot bypass statement cancellation.
Both `getMoreResults` overloads
also retain cancellation while they drain a current result.
After the first start binds the session identity, every schema-valid foreign
frame is stale-dropped before direction and sequence checks.

## Completion and cancellation

`Weavegate.syncPoint` installs the gate and sends `arrive` under the peer's
serialization point, then blocks without a timeout. Only a release for the exact
invocation, worker, arrival and point wakes it. Cancellation latches its first
reason irreversibly, wakes the gate with `WeavegateCancelledException`, arms the
`cancel_ms` watchdog and requests cancellation of executing JDBC statements. A
release racing cancellation is consumed without resuming the worker.

A terminal is sent only after three independent milestones: the command proxy
returned or threw, the transaction manager recorded commit or rollback, and the
tracked connection's `close()` returned. Spring runs completion callbacks before
it returns the connection, so callbacks are never terminal evidence. Application
JDBC work in callbacks after commit or rollback is rejected; SDK-owned connection
cleanup may still return the lease. A failed commit or rollback is an unknown
outcome and sends a transaction fatal. A
connection close failure that Spring logs and suppresses sends a cleanup fatal.
Neither case produces a terminal. An exception after commit reports `committed`
with an application error. MySQL vendor code and SQLSTATE come from the
underlying `SQLException`, not Spring's translated message.
If transaction begin fails, its driver evidence is recorded before Spring
returns the lease, so a concurrent cancellation cannot replace the earlier
application or MySQL error.

The existing transaction interceptor remains responsible for transaction rules.
An observer immediately inside it records a command-body failure before Spring
starts cleanup. Synchronizations are wrapped before commit (and refreshed before
the driver commit for callbacks added during preparation), preserving callback
order and propagating their original exceptions. Only propagating before-commit
and after-commit failures become source evidence; Spring's suppressed completion
errors remain suppressed. This records failure versus cancellation order without
using callback completion as terminal evidence.

When a driver exception escapes the command boundary, it contributes a fixed
`MySQL operation failed` or `database operation failed` summary to wire evidence;
vendor code and SQLSTATE remain available. Application code still receives the
original driver exception, and an exception it catches does not become worker
failure evidence. A caught MySQL deadlock (error 1213) is different: InnoDB has
already rolled back the whole transaction, so the session reports an unknown
transaction outcome and fails rather than reporting a later commit. Locally
authored application exception messages must already
respect the wire contract's no-secrets/no-SQL-literals requirement.

Startup, cancellation, stop and post-fatal watchdogs force a nonzero exit at
their deadline without waiting for rollback, pool closure or shutdown hooks. A
later deadline never extends an earlier one. A watchdog fatal write is best
effort and bounded by 100 ms, after which the process halts even if stdout is
blocked. After fatal, no terminal or `stopped` is emitted; known cleanup
completes locally and the process exits 1. Exit 0 happens only after `stopped`
is flushed and stdout is closed. The peer remains STOPPING during this drain;
queue overflow, failed writes/flush/close, or an expired Stop deadline retain a
failed session and cannot produce exit 0.
The forced-halt path performs no stderr flush, because stderr backpressure
cannot be allowed to extend an absolute watchdog deadline.

## Validation and remaining acceptance

Run from `sdk/java` with Docker available for the MySQL and child-process tests:

```bash
./mvnw -B verify -Dweavegate.repetitions=20
```

Each repetition is a separate JUnit execution. A launcher listener writes
execution boundaries, versions and observer records to
`target/weavegate-evidence/java.log`. Record a manifest beside that log and the
captured Maven output:

```bash
mkdir -p /tmp/weavegate-java-evidence
(cd sdk/java && ./mvnw -B verify -Dweavegate.repetitions=20 \
  -Dweavegate.evidence=/tmp/weavegate-java-evidence/java.log) > /tmp/weavegate-java-evidence/build.log
python3 scripts/record-external-sut-java-results.py \
  --log /tmp/weavegate-java-evidence/java.log \
  --build-log /tmp/weavegate-java-evidence/build.log \
  --output /tmp/weavegate-java-evidence/java.json \
  --revision "$(git rev-parse HEAD)" \
  --command './mvnw -B verify -Dweavegate.repetitions=20 -Dweavegate.evidence=/tmp/weavegate-java-evidence/java.log'
python3 scripts/check-external-sut-acceptance.py --results /tmp/weavegate-java-evidence/java.json --require-complete
python3 scripts/test-external-sut-java-results.py
```

The tests verify the pinned vector SHA-256 before execution. All 40 lifecycle
cases and 11 framing cases that target Java run against a scripted engine with
controllable host, clock, exit and threads. Unknown events, arguments,
assertions, exception classes and phases fail before injection. The recorder
accepts a check only if it occurs exactly once in every passing repetition of
the same test and names an existing Java method on its actual declaring type.
Nested types use their enclosing names (for example, `Outer.Inner.observe`);
methods of sibling or anonymous types cannot validate that reference. CI records
the exact Maven argument array, including the evidence path, beside its logs.

Independent tests run the production bootstrap in child JVMs over real pipes.
They cover the success lifecycle through `stopped`, stdout EOF and exit 0;
EOF during startup, active, post-terminal and Stop phases; and broken and
blocked writers. Spring tests use the pinned stack against MySQL 8.4, including
a JDK proxy selected with `--spring.aop.proxy-target-class=false`. They observe
proxy exit, driver commit or rollback and physical close outside the
SDK, and inject begin, commit, rollback and close failures beneath lease
tracking.

The shared vectors now include 12 Java wire matrix histories, covering unknown
commands and points, direction and binding errors, semantic duplicates,
released and retired inputs, capacity and sequence limits. The wire matrix
observer executes each history and records its requirement row. The strict
`--require-complete` gate runs after the 20-repetition Java suite in CI. This
is isolated peer acceptance under [#109](https://github.com/weavegate/weavegate/issues/109);
CLI launch and budget composition remain
[#110](https://github.com/weavegate/weavegate/issues/110); live paired MySQL
evidence remains [#111](https://github.com/weavegate/weavegate/issues/111).

At implementation revision `8a7e0686c93b76f064b8f886320f997ca62fa501`,
this command ran 2,023 tests with zero failures, errors or skips:

```bash
mkdir -p /tmp/weavegate-141-final
(cd sdk/java && ./mvnw -B verify -Dweavegate.repetitions=20 \
  -Dweavegate.evidence=/tmp/weavegate-141-final/java.log) \
  > /tmp/weavegate-141-final/build.log 2>&1
```

The recorder used that revision and the captured build and Java logs. Its
manifest passed `--require-complete`:

```text
EXTERNAL_SUT_ACCEPTANCE_RESULT target=java manifest=valid acceptance=complete pass=60 fail=0 incomplete=0
```

The checked-in Java result file remains the intentionally incomplete template;
CI publishes the filled manifest and its referenced logs as separate artifacts.
