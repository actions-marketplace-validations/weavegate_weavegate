# Adapter outcomes and run finalization

The Go contracts in [`internal/sut`](../../internal/sut/sut.go) and
[ADR 0014](../adr/0014-adapter-outcome-boundaries.md) distinguish invocation facts
from session failure and operation cancellation. These contracts are implemented
for the Go-native adapter, the external wire adapter and the orchestrator; the
[Java Spring peer](external-sut-java.md) is the child side of the external
adapter.

## Invocation lifecycle

`Handle.Invoke` returns either a synchronous error with no stream, or one
`InvocationOutcome` stream. A valid stream publishes exactly one outcome and
closes. The outcome has exactly one non-nil field:

| Field | Required facts | Coordinator behavior |
| --- | --- | --- |
| `Worker` | Command transaction committed or rolled back; worker connection returned. | Preserve WorkerResult, call runtime Finish, and retain the existing worker-error classification. |
| `Unstarted` | Command transaction never began; acquired resources returned; non-nil cause and matching worker ID. | Retain UnstartedResult separately, abort execution with its cause, and never call Finish or invent a terminal. |

Go-native connection acquisition is asynchronous. Input validation, unknown
commands, active worker-ID conflicts, and already-canceled contexts are
synchronous rejections. Connection acquisition failure or cancellation before
command acceptance produces an unstarted outcome after Invoke returns.
Acquisition-failure completion inspects the parent context while holding the
worker-local ordering lock, so an observable custom cause cannot be replaced by
internal cleanup cancellation. When database/sql returns the raw worker-context
cancellation sentinel for connection acquisition and that context carries a
distinct run failure, the adapter preserves the run failure while masking the
cleanup-only sentinel. Cancellation observation and command acceptance
share the same lock immediately before the command call. If cancellation wins,
the command is not called. If acceptance wins, the command reports whether its
transaction began and reached a known committed or rolled-back state through
`gonative.CommandResult`. A transaction-creation failure produces an
UnstartedResult after the connection is returned; a command that began its
transaction produces a WorkerResult only after it reports completion and the
connection was returned. Cancellation still reaches the called command through
its context. Independent acquisition or transaction-start errors and cancellation
causes remain discoverable with `errors.Is`; the parent is observed through the
same worker-local ordering lock when either failure is finalized. The adapter
holds the worker reservation until cleanup and stream closure, then permits reuse.
An explicit rollback proves completion only when it returns nil; `sql.ErrTxDone`
can mean database/sql's asynchronous context rollback is still unresolved and
remains retained as evidence for that unknown outcome.

Unknown transaction outcome or unproven resource cleanup requires a session
fault; neither outcome type can represent it. A fault is latched before closing
an affected stream without an outcome. Go-native fault publication and Invoke
admission share the adapter-state lock, so a fault rejects every invocation that
has not already been reserved. An empty invocation stream belongs to a session
fault only when that fault was latched before closure. An empty healthy stream,
multiple outcomes, malformed identities or outcome variants, and streams left
open at cleanup are protocol errors. Worker completion can advance runtime
coordination before stream closure, but oracle evaluation waits for all streams
to close. A runtime
Finish error immediately cancels execution waits while the collector still checks
for closure and extra outcomes. A malformed first outcome is retained and cancels
execution the same way. The first extra outcome also cancels execution as soon as
multiplicity is proven, while every remaining value is still drained until closure
or collector cancellation so unbuffered producers can finish.

## Session fault and cancellation observation

Every started Handle exposes `SessionFaults`: a notification channel and a typed
`SessionFault` retaining its cause. `FaultLatch` supplies a concurrency-safe
implementation with a usable zero value. The first non-nil failure closes the
notification channel and remains visible to all current and late observers.
Run rejects nil and typed-nil fault surfaces before invoking their methods.
It also rejects a non-nil `SessionFault` with a nil cause as an adapter protocol
error before treating that value as a cancellation cause.
Subsequent failures do not replace it. Adapters and observers must not mutate a
published fault or cause. Closing the notification channel while `Err()` remains
nil after a post-notification recheck is an adapter protocol error and invalidates
provisional evaluation. The recheck distinguishes a malformed surface from a
fault published between the observer's initial read and notification wait.
Successful Stop completes fault publication; failed Stop cannot prove cleanup or
rule out later failures.

Run observes faults through execution, provisional oracle evaluation, and Stop.
A fault cancels the execution/evaluation context with its original cause.
Evaluators must honor their context. Run also checks the latch synchronously,
so an evaluator returning success after cancellation cannot finalize that result.

The operation context is observed on every return path after its nil check,
including evaluator validation, the run-gate wait, and fixture reset. The run
deadline remains observable through Stop, collector shutdown, and runtime Close.
Final synchronous context observation, together with the fault latch, is Run's
success boundary. Cancellation after this boundary is outside the completed
operation. The `Err` read defines that boundary; Run reads a custom cause only
when the same observation already saw cancellation. Stop receives a detached
context with the configured stop budget so
cancellation does not skip cleanup. The final boundary preserves both the
operation context error and a distinct custom cancellation cause across these
phases.
The execution context supplied to `Start` remains active through `Stop` after a
successful run. Cleanup cancels it before `Stop` only when an existing run error
must abort outstanding commands; the final deferred release still cancels it
after cleanup returns.

Collectors stay active through Stop and drain available evidence before shutdown.
After collector cancellation, each collector permits one ready final outcome and
one ready closure-or-multiplicity probe before stopping. This observes the full
boundary of a valid buffered outcome while preventing a continuously readable
faulty stream from blocking cleanup indefinitely.
Canceled and failed runs retain known worker and unstarted results in scenario
order. A committed result keeps its nil worker error even when Run returns the
operation context error. Evaluation and run fingerprint are provisional and are
cleared on any run error. If a trace observer rejects an event, trace recording
stays stopped at that event while cleanup still collects worker facts. Outstanding
commands receive that run failure as their cleanup cancellation cause, avoiding a
spurious caller-interruption classification.

The Go-native adapter observes that parent directly under the worker ordering
lock before any post-command path closes without an outcome, including an
invalid start/completion report, an unknown outcome, or a completed-transaction
cleanup fault. Its fault retains the run cause and cleanup error while masking a
generic `context.Canceled` used only to wake the accepted command.

## Errors and verdicts

Worker errors remain evidence for the oracles. Unstarted, protocol, execution,
evaluation, session, operation-context, and Stop errors fail Run; independent
causes are joined rather than masked. Collection errors are ordered by scenario
worker order, ahead of operation cancellation even when the parent cancellation
reaches execution first. `errors.Is` and `errors.As` retain the underlying causes
and typed session/unstarted errors. Internal cancellation used to wake runtime waiters
reports its actual cause instead of introducing an unrelated operation-canceled
error. Only oracles judge invariants; the coordinator does not create violations.

No new diagnostic, CLI configuration key, or report artifact version is introduced.
The [fixture connection contract](fixture-connection.md) defines the G3 quarantine
boundary: failed Stop or a latched session fault invalidates fixture reuse until
teardown and successful reprovisioning. A failed Stop does not make the fixture
safe to reset, even if the child process has exited.

## Reproducing the evidence

The focused suite uses channel barriers rather than sleeps for ordering. Run it
without Docker:

```bash
go test ./internal/orchestrator ./internal/sut ./internal/sut/gonative ./fixtures/matching-slice/sut \
  -run 'TestOutcome|TestRunPreservesCancellationBeforeFinalizationSetup|TestFaultLatch|TestGoNativeUnstartedWorkerCleanup|TestGoNativeAsyncUnstarted|TestGoNativeCleanupSessionFault|TestGoNativeMasksAcceptedCommandCancellationSentinel|TestWorkerAcceptance|TestAssignBeginFailureReportsUnstarted|TestAssignUnknownTransactionCompletionFaultsSession' \
  -v -count=20
go test -race ./internal/orchestrator ./internal/sut ./internal/sut/gonative ./fixtures/matching-slice/sut \
  -run 'TestOutcome|TestRunPreservesCancellationBeforeFinalizationSetup|TestFaultLatch|TestGoNativeUnstartedWorkerCleanup|TestGoNativeAsyncUnstarted|TestGoNativeCleanupSessionFault|TestGoNativeMasksAcceptedCommandCancellationSentinel|TestWorkerAcceptance|TestAssignBeginFailureReportsUnstarted|TestAssignUnknownTransactionCompletionFaultsSession' \
  -count=20
```

The actual MySQL transaction tests cover both terminal boundaries. One commits
an update, signals the commit barrier, then observes cancellation before returning
the command's nil error. The other cancels an active transaction, observes
database/sql's completed automatic rollback, and verifies that the resulting
`sql.ErrTxDone` remains an unknown outcome and a session fault:

```bash
go test ./internal/sut/gonative \
  -run 'TestGoNativeMySQL/(preserves_commit_before_cancellation|stops_an_active_worker)' \
  -v -count=20
```

These fixed test markers are checked with `grep -F` in the smoke workflow:

- `SUT_FAULT_LATCH_RESULT`
- `SUT_SESSION_FAULT_RESULT`
- `SUT_CANCEL_BOUNDARY_RESULT`
- `SUT_UNSTARTED_RESULT`
- `SUT_OUTCOME_ERRORS_RESULT`
- `SUT_OUTCOME_STREAM_RESULT`
- `SUT_ASYNC_UNSTARTED_RESULT`
- `SUT_COMMAND_ACCEPT_RESULT`
- `SUT_COMMIT_CANCEL_RESULT`
- `SUT_STOP_RESULT`

The markers describe Go boundary and MySQL adapter evidence. They do not claim
execution of the shared external wire vectors or Java lifecycle conformance.
