# ADR 0015: Java application execution boundary

- Status: Accepted; isolated Java acceptance passed, CLI and paired replay pending
- Date: 2026-09-24
- Issue: [#141](https://github.com/weavegate/weavegate/issues/141)

## Decision

The Java peer runs a trusted, reviewed fixture application. Its JDBC proxies
guard transaction controls, known session effects, timeout settings, handle
navigation, and statement cancellation. They do not parse MySQL or sandbox
arbitrary application code. Admission accepts one `SELECT`, `INSERT`, `UPDATE`,
or `DELETE` statement per JDBC call after stripping ordinary comments and
exposing MySQL executable comments. A semicolon outside a quoted value is
rejected, including a trailing semicolon. JDBC batch addition and execution
are rejected before delegation. Known session-changing SELECT forms
and named-lock functions are rejected before delegation. Session-variable reads
and writes are rejected, as are result-set mutation methods. The SQL admission
rule is a guard against identified escapes, not proof that every admitted
expression is free of side effects.

The fixture author must review all command SQL and database objects. Only
transactional InnoDB tables are supported. Stored routines, triggers, events,
user-defined functions, nontransactional tables, server-side prepared SQL,
multi-statements, session variables, temporary tables, and SQL whose result
depends on wall-clock or random state are outside the supported contract.
Application code must not obtain another DataSource, raw driver connection,
thread, process, or network path to the fixture database. Changes to fixture
schema, functions, triggers, SQL modes, or connector configuration require
another review and real-MySQL acceptance run. JDBC interception cannot prove
these fixture and application obligations at runtime.

Only selected synchronous `void` commands dispatched through one inspectable
Spring proxy are supported. Registration confirms exactly one matching
`REQUIRED` transaction interceptor bound to the SDK manager, and inserts the
failure observer immediately inside that interceptor, scoped to the matching
static pointcut when commands use separate advisors. Static nonmatching
advisors may surround it. Synchronous command-specific advice can run inside
the observer; advice outside the transaction has the same no-JDBC obligation
as all other application work outside an invocation transaction. For JDK
proxies, pointcuts and transaction attributes are evaluated against the invoked
interface method; implementation-only matches do not establish a transaction
boundary. Matching runtime transaction pointcuts, frozen or opaque proxies,
async returns, self-invocation and work outside the command proxy call are
unsupported. The method exposed by a JDK proxy must be present on its
interface. A CGLIB command method must be overridable.

Terminal publication requires independent proxy-exit, transaction-outcome and
lease-return observations. Commit/rollback failure and suppressed lease-close
failure are fatal and produce no terminal. A callback that accesses JDBC after
commit or rollback is rejected even if Spring still binds its connection.

## Consequences

The isolated Java gate can establish behavior only for the reviewed finite
cases and pinned MySQL, Spring, driver, and pool versions. Repeating a suite
cannot fill missing shared wire cases. CLI composition remains #110, paired
live replay remains #111, and fixture quarantine remains #120. The boundary
must be consumed by those changes; it does not claim their evidence.
