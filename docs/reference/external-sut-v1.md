# External SUT wire contract v1

This is v0.2.0 work from [ADR 0010](../adr/0010-external-sut-protocol.md).
The CLI can select the external adapter using the
[configuration reference](config.md#external-jvm). The
[Go peer](external-sut-go.md) and [Java Spring peer](external-sut-java.md) each
pass their isolated acceptance gate, and one synthetic Spring fixture has paired
MySQL replay evidence on the pinned baseline; see
[Spring paired replay](../experiments/spring-replay.md). That evidence does not
cover other applications or versions. Normative words describe what conforming
implementations must do. Artifact versions and wire versions are
independent. [Shared conformance cases](external-sut-conformance.md) accompany
this contract.

## Transport, framing, and version

The Go engine peer (E) owns one local child JVM peer (J). E writes child stdin;
J writes stdout. Every frame is a four-byte unsigned big-endian byte length,
followed by exactly that many bytes of one UTF-8 JSON object. Length must be
1–1,048,576 bytes. Read partial headers/payloads until complete; never assume a
pipe read equals a frame. Reject invalid UTF-8, duplicate JSON keys at any
level, trailing JSON values, unknown fields/types, and invalid required fields.
Zero/oversized lengths fail before allocating the declared buffer. EOF in a
header/payload is fatal; EOF on a frame boundary is also fatal except during
verified normal shutdown. Flush each frame. Stdout banners or log text are
protocol corruption.

Every message has exactly `v`, `type`, `run`, `session`, `seq`, and `body`.
`v` is the integer `1`; there is no version fallback or downgrade. An incompatible
version fails startup/session without running a command. `type` selects the
closed body shapes below. No field is optional unless explicitly stated; null
is invalid except `terminal.error`. Integers have no fractional/exponent form.
Strings must contain valid Unicode scalar values. Names are nonempty, at most
128 UTF-8 bytes, and contain no control characters or leading/trailing whitespace.
Parameter maps have name keys and string values; frame size bounds their size.

The enabled child bootstrap reserves stdout and reads `start` before creating
its Spring context/DataSource. Its control reader remains available during
initialization so Stop can cancel startup. E starts the startup deadline before
launch, bounded by both its configured startup budget and the remaining Start
context. Immediately before writing start, E computes `startup_ms` as whole
milliseconds remaining to that original startup deadline, rounded down. If less
than 1 ms remains, E sends no start frame and fails startup; 0 is never sent and
rounding up is forbidden. If the child was already launched, E closes the pipes
and terminates/reaps it under the detached bounded cleanup context. It sends no
stop as a first frame to an uninitialized peer. J's receipt-relative startup
watchdog cannot extend E's original deadline; transport delay consumes that
budget as well. J arms its startup watchdog before initializing the application.
If initialization is still incomplete at `startup_ms`, it best-effort sends
startup fatal and forces nonzero exit; it must not start a new cancellation
grace after that exhausted deadline. No command can
run before E validates ready. Startup failure sends fatal when possible; Stop
during startup may produce `stopped` without `ready` if cleanup is proven.
In that path, `stopped` is the first J frame with `seq: 1`; E accepts it only
after requesting Stop. Start returns no Handle and cannot admit an invocation.
An unsolicited `stopped` is a protocol error, even with the expected sequence.

## Identity, order, and duplicate handling

| Field | Meaning and scope |
| --- | --- |
| `run` | E-generated 32 lowercase hex characters; correlation across one replay/exploration operation, independent of artifact run IDs. Never a release authority. |
| `session` | E-generated fresh random 128-bit value as 32 lowercase hex characters per adapter/schedule execution, including each repeat. One child serves exactly one session. |
| `invocation` | E-generated 32 lowercase hex characters, never reused in a session. Bound immutably to one worker and command. |
| `worker` | Scenario worker name. Unique among live invocations; reusable only after prior terminal acceptance and Go result channel closure. |
| `arrival` | Positive decimal string without leading zeros, starting at `"1"` and increasing by one per invocation, maximum `"100000"`. |
| `point` | Registered sync-point name; compared exactly, without case folding. |
| `seq` | Integer 1–100000, increasing by one independently in each direction, starting at 1 with E `start` and J `ready`, startup `fatal`, or `stopped` after a pre-ready Stop. Covers all message types. |

A release targets `(run, session, invocation, worker, arrival, point)`, never
just a worker or point. Each invocation has at most one outstanding arrival.
The bridge binds a private Go context and runtime client to that session; it
never reassigns a queued message or bridge task to a new runtime.

Validate framing, schema, and version first. A well-formed foreign run/session
frame on an established session is dropped without touching current sequence
state or the runtime (record only a sanitized stale-message counter). The child
binds its identity from its first valid `start`. It may not rebind. The engine
already knows the expected identity before reading ready.

Within the current session, process messages in sender order. Retain a digest
of each accepted frame payload until Stop; an already-seen sequence with exactly
the same payload bytes is ignored without repeating effects or replies. Different
bytes for an existing sequence, a gap, or an unseen lower sequence is fatal.
No reconnect, resend timer, automatic invocation retry, or reply replay exists
in v1: pipes are reliable while alive and ambiguity ends the session. This
bounded duplicate rule only protects against accidental duplicate writes.
Senders must fail instead of wrapping the sequence limit.

After sequence validation, unknown invocation/worker, changed immutable binding,
unknown command/point, wrong direction, and invalid state are fatal. Retain
completed invocation tombstones until Stop: a late `arrive`, `release`, `cancel`,
or identical terminal body for a retired invocation is consumed with no effect;
a conflicting terminal body is fatal. An old invocation must never affect a
new invocation using the same worker. For a live invocation, a release for an
already-released arrival is ignored only if its full identity matches a recorded
release. Future arrival numbers, changed points, release before arrival, and
second concurrent arrivals are fatal. A semantic duplicate `invoke` or `start`
under a new sequence is fatal; only exact sequence duplicates are ignored.

## Message catalog

The `I` body fields below mean `invocation` and `worker`. `A` means `I` plus
`arrival` and `point`. These are notation for the table, not nested wire fields.

| Type | Direction | Exact body fields | Required behavior |
| --- | --- | --- | --- |
| `start` | E → J | `variant` (name), `params` (string map), `commands` (distinct name array), `points` (distinct name array), `capacity` (integer 1–1024), `database` (object below), `startup_ms`, `cancel_ms` (positive integers ≤ 2147483647) | First E frame. Configure application, validate registration and transaction profile, establish pool, and ping the fixture before readiness. |
| `ready` | J → E | `commands`, `points`, `capacity` | First J frame on successful startup. Exactly echo the validated start arrays/order and capacity; DB probe lease returned, application initialized, no worker running. E verifies equality before Start returns a Handle. |
| `invoke` | E → J | `I`, `command` (name) | Only after ready; reserve worker and invocation, enqueue one worker. Uses start's variant/params. Reject excess capacity, unknown command, or an already-active worker. |
| `accepted` | J → E | `I` | Reserve invocation before sending; precedes all arrivals/terminal for it. Acknowledges dispatch, not transaction completion. E may return a result channel from Invoke before receiving this. |
| `arrive` | J → E | `A` | Worker gate is installed before sending. E validates identity, then calls its session's `Client.Arrive` once in an independent bridge task. |
| `release` | E → J | `A` | Send only after that bridge call returns nil while the invocation is still uncanceled. J wakes only the gate with the exact identity. Error or cancellation from Arrive never authorizes release. |
| `terminal` | J → E | `I`, `transaction`, `connection`, `error` | Only after accepted and verified cleanup; started transactions also require proxy exit. No outstanding arrival. One terminal per invocation. Rules below. |
| `cancel` | E → J | `I`, `reason` (`context` or `stop`) | Cancel one invocation, including one awaiting accepted. Wake its gate with an exception, request JDBC cancellation, and await transaction cleanup. This is not release or completion. |
| `stop` | E → J | `budget_ms` (remaining graceful budget, positive integer ≤ 2147483647) | Close admission permanently, cancel all active invocations, await cleanup, close pool and application. Valid during startup as well as after ready. |
| `stopped` | J → E | empty object | All invocation terminals sent, no worker/lease remains, pool/application closed. Sent only in response to Stop; may be the first J frame (`seq: 1`) when startup was stopped before ready. Flush, close stdout, and exit 0. Cannot be sent after fatal. |
| `fatal` | Either | `kind`, `message` | Session failure, never a worker terminal. `kind` is `version`, `protocol`, `startup`, `transport`, `transaction`, `cleanup`, or `shutdown`; message is sanitized, at most 1024 UTF-8 bytes. Close admission and begin bounded cleanup. Best effort only if writing remains possible. |

`database` has exactly `driver` (`mysql`), `host` (nonempty string), `port`
(integer 1–65535), `name`, `username`, and `password` (strings; name/username
nonempty). It describes the fixture's application account and host-mapped port,
not a Go DSN or JDBC URL. The child constructs a JDBC URL with correctly encoded
components and supplies credentials separately. The launch profile uses only
this ephemeral local test database; it does not accept arbitrary JDBC options.

`terminal.transaction` is `committed`, `rolled_back`, or `not_started`;
`connection` is `returned` or `not_acquired`. `not_acquired` is legal only with
`not_started`; all other outcomes require `returned`. `not_started/returned`
covers a failed transaction begin after a lease was acquired. Unknown outcome
or uncertain cleanup is fatal, with no terminal for that invocation.

`error` is null only for `committed/returned`. Otherwise it is an object with
exactly `kind` (`application`, `mysql`, `cancelled`), `message` (sanitized string,
≤1024 UTF-8 bytes), `mysql_code` (integer 0–65535), and `sql_state` (string).
For `mysql`, code is nonzero and SQLSTATE is five uppercase ASCII letters/digits;
otherwise code is 0 and SQLSTATE is empty. A committed transaction may carry an
error, for example an exception after commit; report the observed outcome.
For `error.kind: cancelled`, the message is canonical: reason `context` maps
to `cancelled by context`, and reason `stop` maps to `cancelled by stop`. The
first cancellation reason latched for the invocation determines this message;
a later Stop or duplicate cancel cannot overwrite it. Cancellation triggered
by stop without a preceding cancel uses reason stop. Java may retain local
exception detail internally but sends only this summary for cancellation.
Explicit rollback without an application exception still needs an application
error indicating rollback. Wire kinds are not diagnostic codes or verdicts.

E emits one `InvocationOutcome` containing a `WorkerResult` then closes its
channel only for a validated
`committed` or `rolled_back` terminal after all invocation bridge tasks have
unwound. A `not_started` wire terminal must instead use the separate asynchronous
`UnstartedResult` defined by [ADR 0014](../adr/0014-adapter-outcome-boundaries.md);
it cannot publish WorkerResult or call runtime Finish. The
[Go outcome contract](adapter-outcomes.md) defines stream closure and worker reuse.
Null error maps to nil;
non-null maps to an error, preserving MySQL vendor metadata (1213 deadlock,
1205 ordinary error) and cancellation identity. E measures Duration locally
from dispatch to terminal receipt; it is volatile and not sent on the wire.
Adapter-wide faults use the typed session latch defined by ADR 0014, never `WorkerResult{Err:nil}` or an ordinary
worker error that would permit an oracle verdict to stand as a successful run.

## Cancellation, failures, and bounded stop

Cancellation is irreversible. The Go adapter cancels the invocation's bridge
context and sends `cancel`; Java atomically records cancellation, wakes any
sync-point gate exceptionally, and prevents future gates/commands from starting.
A racing release cannot clear that flag; a release received for a canceled gate is consumed without resuming it. Cancellation latching and release enqueueing share one per-invocation
serialization point (a lock or actor). Under that same serialization, release
checks the latched state and context and either appends its frame or suppresses
it; cancellation latches irreversibly and appends cancel. A check outside that
critical section is advisory only. The writer preserves this queue order and
assigns sequences only to appended frames. Never hold the serialization while
doing pipe I/O or a blocking queue send; a bounded queue that cannot accept a
frame fails the session rather than releasing the ordering constraint.

If cancellation wins that point, release cannot be queued, including when its
runtime call already returned nil. If release wins, its frame may precede cancel
and Java may resume before receiving cancellation; do not claim retroactive
suppression. Observing context.Done and committing the cancellation latch are
not the same event. The harness must test both orderings at the enqueue barrier. The command must allow the cancellation
exception to cross its proxy boundary under rollback rules. JDBC cancel/interrupt
is a request, not proof that a driver or server has stopped. A transaction already
committed must truthfully return `committed`, with nil WorkerResult.Err when the
wire error is null. Reporting cancellation of the enclosing operation is a
separate run-level obligation defined by ADR 0014; Handle does not publish a
second asynchronous error. Run retains the operation context error through final
cleanup without rewriting the database outcome as rollback.

Any non-cancellation error from the runtime bridge is a fatal protocol error; it is never converted into release or a successful terminal.

`cancel_ms` is a local cleanup grace from cancellation receipt; it neither
extends the Go operation nor coordinates workers. Failure to confirm cleanup
within it becomes fatal and triggers process termination. A canceled outstanding
arrival is retired on both peers before terminal acceptance; E waits for its
canceled bridge call to unwind. If Java has sent an arrival that E has not yet
read when cancellation starts, E consumes it in the canceled invocation without
calling the runtime. Cancellation remains allowed until terminal retirement;
a later cancel is consumed by the tombstone rule.

Normal Stop first establishes its absolute cleanup deadline, before any
potentially blocking work or control-frame write. It then closes Go admission,
cancels each live invocation's bridge context,
and queues cancel with reason `stop` for those invocations before the stop frame.
The child's stop handling also cancels any still-active invocation idempotently;
it must not depend on a separate cancel to close admission or begin cleanup.
For each active invocation, reason-stop cancellation arms the same `cancel_ms`
watchdog as reason-context cancellation, before requesting JDBC cancellation.
A later stop frame adds its shutdown bound but cannot replace or extend an
earlier cancellation deadline: the earliest active deadline governs forced exit.
Canceled bridge calls must unwind before Go accepts the corresponding terminals.

Stop uses a cleanup context independent of the canceled run. E sets a single
absolute stop deadline from that context and reserves the final half of the
initial remaining budget for forced termination and reaping. The halfway point
is the graceful cutoff. Immediately before writing stop, E sets `budget_ms` to
the whole milliseconds remaining until that cutoff, rounded down, not the
remaining total Stop budget. For example, with an initial 5000 ms and no elapsed
time, stop advertises 2500 ms. If fewer than 1 ms remain, E skips the frame and
starts forced termination. Time spent writing or delivering the frame consumes
the grace; J must begin cleanup immediately, and its receipt-relative watchdog
cannot postpone E's authoritative cutoff. J arms this stop watchdog before
closing the pool/application or performing other blocking shutdown work. If
shutdown remains incomplete at `budget_ms`, J best-effort sends shutdown fatal
and forces nonzero exit without restarting the budget. At the cutoff E kills the child if
graceful completion is unproven; it never grants a fresh budget per phase.
Writes, draining stderr,
waiting for workers, child exit, and reaping all respect the same deadline.
Stop is idempotent; repeated calls retain the original failure and cannot
restart the child. Concurrent callers whose own contexts remain live wait for
the same cleanup outcome; later calls return that latched outcome. A caller's
earlier context expiry may return its context error, but cannot return nil,
clear the shared failure, or restart/extend cleanup. An expired budget returns
an error promptly. The supported
child must not spawn descendants; if implementation allows any, it must own and
terminate the whole process tree under this same bound.

Either peer losing its control stream cancels all local invocation contexts
and wakes gates exceptionally. On coordinator loss, J starts a watchdog bounded
by `cancel_ms`, attempts rollback and pool cleanup, and terminates itself even
if a driver cannot be interrupted. Forced exit is never evidence of a known
transaction outcome. E independently supervises child exit and quarantines the
fixture after unproven cleanup.

After sending or receiving fatal, a peer may close its control stream and is
not required to read or respond to a later stop. Fatal permanently disables
normal completion; no stopped response can clear it. J emits no new worker
terminals after fatal: known cleanup milestones remain local and must not
manufacture a result for an already failed session. Previously emitted terminal
facts are not rewritten. If still active, J wakes gates exceptionally and its
worker must unwind under rollback rules; cleanup retains the same proxy/lease
barriers. After bounded cleanup J closes the stream and exits nonzero; a
watchdog expiry forces exit even when cleanup is unproven. E still performs local
bounded Stop, with a best-effort stop write only while the pipe is available.
That write does not establish or refresh J's cleanup watchdog: any existing
cancellation/cleanup deadline remains authoritative (an expired deadline stays
expired). If no cleanup deadline exists, J bounds fatal cleanup by `cancel_ms`
from fatal detection and arms that watchdog before attempting JDBC cancellation
or potentially blocking cleanup. The watchdog runs independently of worker and
cleanup threads; expiry forces nonzero process termination without waiting for
transaction rollback or application shutdown hooks. Failed writes retain the original fault; E proceeds to
termination/reaping and never waits for a post-fatal stopped acknowledgment.

Watchdog fatal writes are best effort and must not delay forced exit if the
pipe is blocked or broken. An expiry never grants another cleanup grace.

Normal Stop requires `stopped`, stdout EOF, exit 0, no active worker/lease, and
no latched session fault. EOF before stopped (even exit 0), nonzero exit, broken
pipe, stalled writes/reads at the governing deadline, malformed messages, or
process death latch a run error. A silent live peer is bounded by startup/run/
stop deadlines, not by a heartbeat or a database-blocking inference. Fatal after
all terminals still invalidates run success; fault delivery must stay active
through oracle evaluation and Stop (G2). Failed or unproven cleanup quarantines
the fixture (G3); killing the JVM does not prove server-side rollback is complete.

## Provisioning, reset, logs, and evidence

E provisions and resets migrations/seed before starting J. The
[fixture connection contract](fixture-connection.md) supplies the application
descriptor from that same prepared fixture. J disables
schema creation, migration runners, web listeners, scheduled/background DB
writers, and health jobs that mutate the database. The worker executor and pool
have at least the declared capacity; unrelated application work cannot consume
worker leases. Start fails if any declared command/point or transaction profile
is unsupported. Readiness means the context is initialized and a DB probe has
completed and returned its lease; it is not a log-line match or a fixed delay.

After all worker terminals, existing Go oracles query committed state. Stop
closes the JVM pool and reaps the child before another Reset. Successful Stop
proves no client work remains; failed cleanup requires teardown/reprovisioning,
not an attempt to race DROP DATABASE against unknown old transactions. New
sessions never inherit sockets, command state, tombstones, or runtime arrivals.

Start sends credentials only through the owned stdin pipe. Do not put them in
argv, environment variables, temporary config, traces, report artifacts, or
exceptions. Reserve stdout before Boot starts; disable the banner and route all
logging to stderr. E continuously drains stderr with a bounded 1 MiB tail;
overflow discards oldest log bytes without blocking a worker. Raw logs and start
frames are not persisted by default. Any explicit future log export must redact
credentials/JDBC URLs, use private file permissions, and remain separate from
normalized evidence. Error messages carry only sanitized summaries, no stack
traces, SQL literals, or raw driver connection strings. These rules assume an
explicitly instrumented trusted test application, not isolation from hostile code.

The wire carries execution facts only. Session IDs, counters, pipe ordering,
process timings and log text are excluded from deterministic fingerprints.
The orchestrator continues to infer `db_blocked` from its existing WaitArrive
behavior; Java sends neither that state nor an oracle verdict. Transport latency
can influence timeout observations, so the future repeated integration checks
must demonstrate stable evidence without weakening timeouts or using sleeps.
