# ADR 0014: Adapter faults, unstarted invocations, and run cancellation

- Status: Accepted
- Date: 2026-09-13
- Issue: [#119](https://github.com/weavegate/weavegate/issues/119)

## Context

ADR 0010 identified three independent result boundaries needed before external
execution can be enabled. A worker result requires a completed transaction and
returned connection. Session failure and operation cancellation cannot truthfully
be encoded as that worker result. This decision changes the Go adapter boundary;
it does not implement the external transport. Fixture quarantine is the separate
G3 boundary implemented under [#120](https://github.com/weavegate/weavegate/issues/120).

## G2: Latched session faults

Every started Handle exposes a read-only session fault surface. Its notification
channel closes once, and its typed error retains the first failure and its cause
for every observer, including observers arriving after notification. The adapter
must latch a fault before closing an invocation stream whose outcome is unknown.
Unknown transaction outcome or unproven connection cleanup never produces a
WorkerResult. Successful Stop must finish publishing session faults before returning; a
failed Stop provides only an observed fault snapshot and does not prove cleanup.
The fixture-owned quarantine contract for failed Stop and latched faults is
recorded in [the fixture connection reference](../reference/fixture-connection.md).
The Go-native adapter serializes fault publication with invocation admission
under its adapter-state lock, so an invocation reserved before the fault may
finish but no later invocation can enter the faulted session.

Run observes faults during execution and provisional oracle evaluation by
canceling their shared execution context. It also reads the latch synchronously
before evaluation and after Stop and collector cleanup. An evaluator must honor
its context; even an evaluator that returns success after cancellation cannot
make that provisional success final. Fault notification remains independent of
invocation streams, including after all workers have completed. Closing the
notification channel without a latched typed error is a protocol failure and
cannot authorize a successful run. Observers read the typed error again after
receiving notification so a fault published between their first read and wait
is not mistaken for that protocol failure. A non-nil typed fault with a nil cause
is also a protocol failure rather than a usable cancellation cause.

## G5: One invocation stream with distinct outcomes

Invoke returns one stream of InvocationOutcome, containing exactly one of a
WorkerResult or an UnstartedResult, followed by closure. A synchronous rejection
returns an error and no stream. An asynchronous UnstartedResult carries the worker
identity and cause and proves that no command transaction began and all acquired
resources were returned. Failed or unknown cleanup is a session fault instead.
The Go-native command boundary reports transaction start and completion
explicitly. Failure to acquire a connection or begin a command transaction
therefore remains unstarted, including when cancellation races either operation;
independent operation and cancellation causes are joined rather than masked. A
raw context cancellation returned by connection acquisition is treated as a
worker wakeup when the worker context carries a distinct run failure: the run
failure remains discoverable without misclassifying cleanup as caller
cancellation. A failed Commit or Rollback leaves completion unknown, latches a
session fault, and
publishes no invocation outcome. A command that reports no started transaction
and no cause is also an adapter session fault. When a command reports that its
transaction never started, the adapter observes parent cancellation under the
worker-local ordering lock before publishing the unstarted outcome.
Every post-command path that closes without an outcome uses that same ordering
boundary and masks a generic cancellation used only to wake cleanup, including
invalid start/completion reports and completed-transaction cleanup faults.
`sql.ErrTxDone` from an explicit rollback does not prove completion because
database/sql may have claimed the transaction for asynchronous context rollback
before the driver reports its result. The error remains in the unknown-outcome
cause as concrete evidence even though it cannot prove completion.
Neither an unstarted outcome nor a stream closure calls runtime Finish or creates
a rollback, worker terminal, or oracle verdict. The coordinator aborts execution,
collects the outcome, and returns its cause as a run error.

Adapters release worker reservations atomically with stream closure, after
cleanup, permitting identity reuse only after closure. Collectors verify identity,
exclusive outcome shape, a non-nil unstarted cause, and exactly one outcome plus
closure. An empty stream is valid only when its session fault was already
latched; an empty healthy stream, multiple outcomes, or an unfinished stream is
a protocol error. A truthful
WorkerResult authorizes Finish immediately; oracle evaluation additionally waits
for stream closure. A first-outcome validation failure or Finish failure remains
a run error and immediately cancels execution waits while the collector continues
validating stream closure and multiplicity. The first additional outcome proves
multiplicity and cancels execution immediately; collectors still drain every
additional value until closure or collector cancellation. Shutdown
keeps collectors alive through Stop so unbuffered producers can finish. After
cancellation, each collector permits one ready final outcome and one ready probe
that observes closure or multiplicity. This retains the complete boundary while
bounding a continuously readable faulty stream.

## G6: Cancellation and finalization

The operation context remains observable from evaluator validation and the
run-gate wait through fixture reset, execution, collection, evaluation, Stop,
collector shutdown, and runtime Close. The configured run deadline begins after
the run gate and remains observable through the same execution and cleanup
phases. Final synchronous context/latch observation is the success boundary.
The first operation-context `Err` read defines that boundary, and its custom
cause is joined only when the same observation saw cancellation. Cancellation
after an active observation belongs to the caller, not this completed Run.
Internal execution
cancellation for faults or unstarted outcomes is separate from operation
cancellation and does not invent a context.Canceled error for the caller. A
custom parent cancellation cause remains discoverable alongside
`context.Canceled` across all operation phases. Cleanup triggered by an existing
run failure cancels outstanding commands with that failure as its cause, rather
than manufacturing an interruption classification.
On successful execution, the context supplied to adapter `Start` remains active
through `Stop`; only an existing run failure cancels it before cleanup begins.

Committed worker evidence retains its nil worker error even when cancellation
wins the operation. Collected worker and unstarted evidence is retained in
scenario order on unsuccessful runs, including outcomes published during Stop.
Provisional evaluation and run fingerprint are cleared whenever Run returns an
error; only oracles decide invariant results.

## Precedence and aggregation

| Surface | Treatment |
| --- | --- |
| Worker error | Preserved in WorkerResult and terminal evidence; alone does not abort Run or replace oracle judgment. |
| Unstarted/protocol/execution/evaluation error | Run error; preserve collected evidence. |
| Session fault | Typed Run error with original cause, even if evaluation returned success. |
| Operation cancellation/deadline | Join the operation context error; never rewrite a worker result. |
| Stop failure | Join the cleanup error; cannot mask any preceding cause. |

Run errors dominate provisional verdicts. Independent errors are joined in
execution, scenario-ordered collection, Stop, session-fault, operation-context
order; already represented causes need not be duplicated. errors.Is/errors.As
remain usable for all retained causes. No total priority discards another error.
Worker errors remain evidence when any of these errors coexist.

Scenario-ordered collector errors remain ahead of operation cancellation even
when parent cancellation reaches the execution context before a collector
failure. Finalization rebuilds the collector portion from retained evidence and
then appends the independently observed operation context.

## Validation scope

Barrier-driven Go tests cover faults before, during, and after evaluation,
commit-before-cancel, cancellation before acceptance, stream closure, and combined
errors. Exact commands and fixed CI markers are recorded in the
[adapter outcome reference](../reference/adapter-outcomes.md). These Go boundary
tests do not claim Java transaction or external transport conformance.
