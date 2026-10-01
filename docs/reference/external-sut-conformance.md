# External SUT v1 conformance plan (proposed)

These are constructed design examples for the
[v1 wire contract](external-sut-v1.md), not captured output or passing tests.
The [Go peer](external-sut-go.md) and [Java Spring peer](external-sut-java.md)
have partial executable evidence; neither implementation has complete acceptance. The shared
[JSON vectors](testdata/external-sut-v1.json) are the common input for both future
implementation issues; do not fork language-specific copies of the protocol.
[ADR 0010](../adr/0010-external-sut-protocol.md) lists the engine decisions that
must precede end-to-end enablement.

## Vector format and consumption

`vector_format: 1` describes this test-data format; `wire_version: 1` describes
the transport. Every case has a stable `id`, a named `prefix`, ordered `steps`,
descriptive `covers` tags, explicit `targets` and `execution: isolated`.
`targets` lists the implementations under test, independently: Go, Java, or both.
For each listed target, start a fresh instance against a scripted opposite peer.
A case listing both is two isolated tests, not a combined live-peer run.
Expand `prefixes` recursively before the steps.
A prefix element containing `prefix` expands another named history; all other
elements are steps. Prefixes are finite histories, not arbitrary internal-state
snapshots. Every case starts with fresh peers and fake controllable clocks.

A step's `peer` is the receiver/observer (`go` or `java`). `action: receive`
injects the complete `frame` object. Serialize it as compact UTF-8 JSON and add
the four-byte byte-length header. Preserve the same serialization for an exact
duplicate. `action: local` injects a named lifecycle event with `args` through a
harness seam (runtime release, JDBC barrier, proxy exit, lease return, clock
advance, stderr, or child exit). These events and `expect` labels are test
vocabulary, never wire message types. Receipt can deliberately inject a faulty
peer message, so harnesses must not generate additional implicit receive steps
from a `send_*` expectation. Every receive also declares `delivery`:
`exchange` checks the sender's output when that sender is the target, and injects
the frame when its receiver is the target; `input` only injects into the receiving
target and never requires the other implementation to emit it. Opposite-peer
local steps describe the script and are not assertions against an implementation
that is not listed in targets. This separates adversarial input from compliant
output without silently skipping target assertions.

A target-side `send_*` or `fatal_<kind>` effect always has one later, explicit
`exchange` frame. The guard pairs each obligation with a distinct output and,
for fatal effects, compares the kind. A local effect alone never proves a wire
write.

For example, `readiness_mismatch` targets only Go. Its mismatched ready is an
`input` from a faulty child; Java is not required to echo the wrong registration.
The same audit covers malformed identity/order, stale traffic and faulty-release
cases. A future integration runner must build actual paired scenarios separately;
it must not execute these isolated scripts as if both live implementations could
produce all injected frames.

`expect` lists required observable effects in order; the assertions named
`no_*`, `ignore_*`, and `drop_*` forbid the corresponding side effect. For
example, `client_arrive_once` increments the Go runtime-call count once;
`wake_exact_gate` resumes only the identified Java gate; `no_worker_result`
forbids emitting any WorkerResult for the affected incomplete invocation.
`abort_run` requires a run error, not an ordinary worker failure or a verdict.
`invalidate_evaluation` rejects even a previously obtained passing evaluation.
`quarantine_fixture` requires `reset_rejected` until teardown/reprovisioning.
`operation_context_error` is a run-level assertion for the Go run-level harness,
defined by [ADR 0014](../adr/0014-adapter-outcome-boundaries.md) even when listed
beside a terminal observation; it is not another
Handle result. The standalone Go adapter harness checks only its supplied
context and worker outcome; it cannot claim the run-level assertion passed.
A `completion` event supplies independently observed proxy-exit, transaction,
and lease state; the harness must not treat it as a request to publish terminal.
Its optional `invocation` defaults to the first invocation in the history.
Every compliant terminal has matching preceding completion facts, including
`not_started/not_acquired`; started outcomes require proxy exit. The guard also
requires accepted, no pending bridge, and a released or canceled gate. Go-side
scripts supply these facts independently even when Java is not a target.

`command_exception` injects the specified source exception into the named
invocation, at its `command_body`, `jdbc_operation`, or `after_commit_callback` barrier. Construct
the exception from `args.exception`, including the SQLException vendor code,
SQLSTATE and message; non-SQL exceptions use vendor code 0 and empty SQLSTATE.
The later completion event advances independently controlled proxy/lease
milestones. It must retain the observed exception, not synthesize one from a
terminal frame. The Java harness compares its emitted terminal to the explicit
Go receive frame's body as expected output only. In particular, an after-commit
exception remains an application error with a committed transaction.

Cancellation terminal messages follow the wire's canonical reason mapping,
including cancellation before a transaction starts. They are compared exactly;
no harness should invent a source application exception for a cancel frame.
A command exception such as the rollback case is independently injected through
`command_exception` and remains distinct from SDK-generated cancellation.

`stop_call` starts an asynchronous harness call; its `budget_ms` is the total
local cleanup budget, not the smaller graceful budget advertised on the wire.
`set_single_deadline` must precede any cancel, stop, or best-effort stop write;
otherwise a blocked write would escape the cleanup bound. These effects remain
ordered, including calls that close pipes or force termination without writing.
An optional `call_id` names the particular caller. `call_pending` requires it
not to have returned yet. `check_stop_results` only observes the named calls;
it must not inject or fabricate their return values. It requires every listed
call to have returned the latched failure with `expected_error` as its cause,
never nil. With `expected_error: null`, every named call must instead have
completed successfully; a still-pending call cannot satisfy that check.
The failure comparison ignores incidental wrapper text. The repeated
Stop case checks both concurrent callers, then a third call after completion,
so cached success or an immediate nil from a repeated call cannot pass.

`stderr_bytes.args.segments` supplies exact bytes: decode each `hex`, repeat the
result `repeat` times, and concatenate in order. Assert the total is `byte_count`.
The retained tail must have length `retained_bytes` and SHA-256 `retained_sha256`.
The overflow input starts with one ASCII A followed by one MiB of ASCII B, so
keeping the oldest bytes produces a different digest from retaining the newest.

Go tests exercise Go steps against a scripted child and runtime double; Java
tests exercise Java steps against a scripted engine and controllable command/
DataSource. Each consumes the other peer's steps as the scripted conversation.
The future integration runner must additionally exercise live peers, after the
engine decisions are implemented; this file does not currently define paired
execution. Preserve per-worker
causality; no total order between independent workers is inferred from pipe
traffic. Production IDs are random; the fixed hex IDs and credentials here are
synthetic test inputs only.

`framing` cases bypass object serialization: feed exactly `input_hex` bytes to
the decoder. `read_chunk_sizes` splits the leading reads; feed any remainder in
one final read. `eof: true` closes the stream afterward. Compare a valid decoded
object to `decoded` and require no dispatch before the entire payload arrives.
Also run valid frames coalesced and at every single byte boundary. Invalid cases
must fail before invoking application code. Where `control_hex` is present,
first require that fault-free frame to pass framing/schema validation in a
fresh decoder. Then run `input_hex` in another fresh decoder and require rejection
without dispatch. The malformed UTF-8 byte is inside a start parameter string;
top-level and nested duplicate keys otherwise preserve a valid start frame.
Thus neither a lossy UTF-8 decoder nor a last-key-wins JSON decoder can pass by
rejecting an unrelated schema or syntax defect. These cases exercise decoding;
no database startup is required for a control frame to be schema-valid.

A language implementation must report each case ID targeting that language and
its result. Cases targeting only the other language are not applicable, not
passes. A missing implementation for an applicable case remains failing/incomplete
acceptance work, not silently skipped coverage.
The [acceptance manifest contract](external-sut-acceptance.md) pins the reviewed
vector bytes, enumerates target-side checks and output observations, and records
Go, Java and paired results separately. The checked-in manifests are incomplete;
the accounting checker does not execute these histories.
The repository guard validates the constructed data and declared coverage; executing
protocol semantics and publishing evidence markers belongs to the implementation
issues. When those tests add fixed-phrase markers, add their exact CI checks in
the same implementation PR.

## Maintaining the vectors

Compliant exchanges and fault injection need different assertions: an invalid
input cannot also be a compliant sender's expected output. Coverage also needs
explicit peer and lifecycle prerequisites; one receiver's duplicate handling or
one cleanup deadline does not exercise the other receiver or a competing timer.
Keep these distinctions in the shared data and the repository guard so later
edits cannot silently remove them.

Run the persistent standard-library Python 3 guard from the repository root:

```bash
python3 scripts/check-external-sut-vectors.py
python3 scripts/check-external-sut-vectors.py --self-test
```

The first command checks all expanded histories, scope/delivery metadata,
sequence and invocation prerequisites, deadline ordering, exception sources,
the complete framing inventory, frame value constraints, injected-fault premises,
explicit output obligations, immutable invocation/worker bindings, incremented
arrival identities, watchdog trigger order, and the required role/lifecycle
coverage matrix in the JSON `coverage` object.
It checks expectation names against the closed `EXPECTATIONS` vocabulary in the
guard, terminal cleanup facts and bridge retirement, and the permanent fatal
state. A post-fatal best-effort stop may be modeled only with
`no_stopped_required`; it never restores normal completion. Known cleanup
milestones may still occur locally after fatal.
The second mutates every framing entry and representative lifecycle inputs to
prove the guard rejects regressions. CI runs both and checks their fixed result
markers.
Python is a maintainer/CI prerequisite for this data check, with no third-party
package or engine dependency. This guard is not a Go or Java implementation and
cannot prove network, transaction, timing or race behavior; the implementation
checklist still requires actual peer tests and repeated MySQL evidence.

When changing a rule, inspect both receivers, startup/active/post-terminal
phases, success and failure branches, and competing deadlines. Add an applicable
case with explicit scope and observable input/output, plus a guard regression
where its premise can be checked structurally. Update `coverage` and its guard
predicate together for a new required family. `covers` tags are descriptive;
they do not by themselves satisfy a coverage requirement. Unlisted combinations
remain future implementation checks, never inferred passing coverage.

## Design closure and follow-up ownership

The design acceptance boundary is a consistent selected wire contract,
constructed examples with explicit inputs/outputs, and the documented structural
checks passing. It does not require implementing a complete protocol state
machine in the Python guard. Existing checks and wire requirements remain in
force. New runtime combinations and exhaustive event/argument/assertion dispatch
belong to [executable conformance #121](https://github.com/weavegate/weavegate/issues/121),
which tracks the remaining finite coverage and per-target evidence for #108/#109.
A reproduced contradiction in the selected contract or regression in an existing
checked premise still needs correction in the design/data.

Implementation prerequisites have explicit owners:

| Work | Owner | Required before |
| --- | --- | --- |
| G1 fixture connection descriptor (resolved) | [#118](https://github.com/weavegate/weavegate/issues/118) and the [fixture connection contract](fixture-connection.md) | Real database provisioning for the external adapter |
| G2 session faults, G5 unstarted outcomes, G6 operation cancellation (Go boundary resolved) | [#119](https://github.com/weavegate/weavegate/issues/119) and [adapter outcomes](adapter-outcomes.md) | External Go result mapping must consume the implemented boundary and prove wire conformance |
| G3 quarantine and recovery after uncertain cleanup (Go boundary resolved) | [#120](https://github.com/weavegate/weavegate/issues/120) and the [fixture connection contract](fixture-connection.md) | External execution/repeat enablement still requires live peer evidence |
| G4 launch, configuration and budget composition (resolved) | [ADR 0016](../adr/0016-external-cli-composition.md) and [#110](https://github.com/weavegate/weavegate/issues/110) | External CLI selection; live paired evidence remains #111 |
| Executable assertion dispatch and remaining wire/lifecycle coverage | [#121](https://github.com/weavegate/weavegate/issues/121), implemented by #108/#109 | Language implementation acceptance; live paired evidence remains #111 |

The unresolved rows remain implementation gates. Go boundary tests do not prove
external transport or Java transaction conformance.

## Sequence review

All sequences below are constructed. `E` is the Go adapter, `J` the JVM SDK,
`R` the Go runtime, and `DB` the fixture database. Successful startup is
`E start → J initialize/validate/probe/return probe lease → J ready → E Invoke`.

| Case | Causal sequence | Required observation |
| --- | --- | --- |
| Success | Invoke → accepted → transaction begin → arrive → R.Arrive blocks → orchestrator releases → R.Arrive returns nil → targeted release → commit → proxy exit + lease return → terminal → oracle → stop/stopped/EOF/exit 0 | Exactly one result after cleanup; oracle alone judges database invariants. |
| Incremented arrival | arrival 1 at `after_read` → matching release 1 → arrival 2 at `before_write` → matching release 2 → commit/terminal | One invocation retains its worker binding and increments arrival identity across distinct gates. |
| Rollback | Invoke → accepted → arrive/release → command exception → proxy rolls back → lease return → terminal with error | Rolled-back outcome, error preserved, no early completion from a callback. |
| Database blocking | w1 holds row lock at an arrival; w2's SQL blocks before its arrival → Go WaitArrive timeout → release w1 → w1 commits → w2 reaches its point | Go infers blocking; pipe reader stays available and does not release w2 until its own runtime call returns nil. No Java `db_blocked` message. |
| Cancellation | Go context cancels bridge + sends cancel → Java gate throws → proxy rolls back → lease returns → terminal(cancelled) | No release from cancellation; context failure remains a failed operation. Already-committed work reports committed truthfully. |
| Process death | JVM disappears with a transaction/arrival outstanding → EOF/exit observed → session fault → bounded cleanup/quarantine | No invented WorkerResult; no oracle success, reset, or retry on the uncertain database. Death after terminal but before normal Stop also fails the run. |
| Stale arrival | Session A or invocation i1 retires; session B or invocation i2 uses w1 → delayed A/i1 arrive/release | Old identity cannot call B's runtime or resume i2. Session mismatch does not advance B's sequence; retired invocation messages consume current sequence without side effects. |

`stop_before_ready` exercises the alternate startup path: E sends start (1),
requests Stop and sends stop (2), then receives J's first frame stopped (1)
after cleanup. The local Stop budget is 5000 ms and the wire stop budget is its
2500 ms graceful portion, assuming no elapsed delivery time. EOF and exit 0 complete Stop without returning a Handle or running
a command. `unsolicited_startup_stopped` rejects the same first J frame when E
has not requested Stop.

`cancel_cleanup_deadline` holds rollback, cancels the invocation, and unwinds
the Go bridge. `advance_cancel_cleanup_clock` advances Java's clock from cancel
receipt to 999 ms and then the inherited `cancel_ms: 1000` deadline, without
releasing the rollback barrier. At expiry Java emits cleanup fatal on the
available pipe and forces nonzero exit; it cannot acquire a fresh cleanup grace.
Go receives the buffered fatal, starts bounded local Stop for reaping, and
observes child exit while retaining the fault and quarantine. No Go halfway
clock or later kill substitutes for Java self-termination.

`cancel_before_accepted` requires a distinct `unstarted_outcome`, preserving
cancellation and proving no command/transaction/lease began. It forbids a
WorkerResult and runtime Finish. The Go harness must use ADR 0014
and its implemented UnstartedResult API. Do not satisfy it by closing the
invocation stream without an outcome or by inventing a rollback.

`process_death` continues past fault detection: the canceled runtime call returns
without release, then a named detached Stop closes pipes and observes child
reaping under its single budget. `check_stop_results` observes that call's
transport failure and rejected Reset. `no_bridge_tasks` requires the invocation's
bridge tasks to have exited; the observer must not join or cancel them on the
adapter's behalf. The child-exit event supplies process death, not automatic
reaping or proof of database rollback. No WorkerResult is fabricated.

`retired_terminal_identical` and `retired_terminal_conflict` start after the first
invocation's terminal and result-channel closure, then reuse its worker with a
new invocation. They inject the old terminal under a fresh sequence, exercising
tombstones rather than the exact-sequence duplicate path. The identical body
produces no result, reply, or fatal; `no_new_worker_effect` forbids changing the
new invocation's state or result stream, and that invocation subsequently
completes normally. The conflicting body instead requires an explicit protocol
fatal exchange and a failed run. Both cases target Go against a scripted child.

`commit_wins_cancel` delivers cancel to Java after it has emitted and retired
the committed terminal but before Go receives that terminal. Java must consume
the retired invocation's cancel without changing its stored outcome, issuing
another terminal, rolling back, or raising fatal. Go still receives the original
committed result. The separate `check_operation_result` observer then requires
the enclosing Run's cancellation error under ADR 0014's final observation boundary. It observes
the run-level result after completion, not another return from Handle or another
WorkerResult; it must not manufacture an error from the vector's expectation.

`stop_active_invocation` begins with a worker gated inside its transaction.
Go Stop cancels the bridge and explicitly sends cancel with reason stop, then
stop with the graceful budget. Java's canceled gate supplies the SDK cancellation
exception (`cancelled by stop`) under rollback rules. Controlled completion
milestones forbid terminal until proxy exit and lease return, and forbid stopped
until application/pool cleanup completes. `application_cleanup_complete` supplies
those independently controlled resource milestones; the harness must not invent
them from an expected stopped frame. Go checks the canceled worker result after
bridge unwinding, then requires stopped, EOF and exit 0 before the named Stop
call succeeds or Reset becomes allowed. `stop_still_pending` forbids early
success at intermediate receipt steps.

`cancel_at_arrival` checks retirement on both peers and then calls Invoke for a
new invocation using w1 and a fresh, uncanceled context. `invoke_call` invokes the
Go Handle (its invocation ID comes from the harness's deterministic ID source);
the explicit Java invoke receipt is the corresponding outbound frame. Every valid Java invoke receipt in the shared histories has this preceding
Go call, including the active prefix, database-blocking worker, and successful
worker-reuse case. Go reservations must never be inferred from a Java receive
step or a later accepted frame. Completion
may name `invocation` when it differs from the first invocation in the history.
This adapter-only reuse path cancels only the first invocation's context; it
checks that supplied context directly, not a run-level error. It does not attempt
to reuse a terminal worker in the orchestrator's single-use runtime. The second
scripted command completes without arrivals and must not inherit the first
invocation's cancellation or consume a leaked capacity reservation.

`startup_submillisecond_budget` launches a child blocked on its first start
frame, then advances the fake clock so `startup_before_write.remaining_us` is
999. This value is remaining time to the previously established deadline, not
a replacement budget. The engine must neither round up nor emit a zero-budget
frame; detached cleanup closes pipes and reaps the child without first sending
stop. No application or database work can have started in this case.

`readiness_mismatch` now advances initialization/probe cleanup before injecting
the faulty ready response, explicitly delivers startup fatal to Java, and runs
Go's bounded Stop. With graceful cleanup unproven at the cutoff, it kills and
reaps the child and preserves the startup fault. Child reaping alone does not
certify application/DB cleanup, so Reset remains blocked by quarantine.

`startup_deadline` likewise crosses Go's detached cleanup cutoff, forces
termination, and observes reaping before the startup error returns. The
`version_mismatch` case observes Java's sequence-1 version fatal and nonzero
exit after an injected v2 start; detection without a wire fatal cannot pass. The
`completion_callback_too_early` case withholds terminal output until proxy exit,
commit, and lease return are all observed, then compares the complete terminal
frame at Go. `unknown_commit_outcome` compares Java's transaction fatal at Go
and requires run failure without a WorkerResult.

`java_receives_fatal_active` injects an engine protocol fatal into Java while
a worker is gated. Java must close admission, wake the gate exceptionally and
attempt rollback under its existing cleanup watchdog. Known rollback, proxy
exit and lease-return milestones remain local after fatal: no terminal or normal
stopped is emitted. The case reaches pool/application cleanup and nonzero exit;
Go reaps the child while retaining the original session fault and quarantine.
The watchdog remains a bound if those milestones never complete; they do not
reset its deadline.

`java_fatal_cleanup_watchdog_expires` is the hanging branch of Java fatal
receipt. `hold_cleanup` installs an unreleased rollback barrier in the command's
test transaction manager before fatal arrives. Arming that barrier supplies no
rollback, proxy-exit, lease-return or application-cleanup completion. Fatal must
arm the cleanup watchdog before attempting JDBC cancellation or cleanup that
could block.

`advance_fatal_cleanup_clock.elapsed_ms` sets the fake clock to that elapsed
time from Java's first fatal receipt, not from the most recent clock event.
It only advances time and runs already-scheduled callbacks; the harness must
not directly inject watchdog expiry or child termination. The inherited start
frame declares `cancel_ms: 1000`: at 999 ms the child remains alive with cleanup
blocked, and at 1000 ms Java must force a nonzero exit without waiting on the
barrier or shutdown hooks. No terminal, stopped, or cleanup-success claim is
allowed. Go's 2500 ms forced-kill cutoff has not elapsed, so it cannot satisfy
Java's watchdog assertion. The final child-exit observation reaps the process
and preserves the original fault and fixture quarantine.

`java_startup_watchdog_expires` holds initialization before receiving start,
then advances Java's receipt-relative startup clock to 9999 and 10000 ms.
`java_stop_watchdog_expires` delivers a normal stop, holds application shutdown,
and advances its receipt-relative stop clock to 2499 and 2500 ms. Both require
nonzero self-termination at the advertised deadline with no new cleanup grace.
The Stop case retains the already-emitted worker terminal; it forbids a normal
stopped or cleanup-success claim after expiry.

The `advance_*_clock` events advance only the named Java clock and run callbacks
already armed by that peer. `hold_cleanup.phase` may also be initialization or
application shutdown; those barriers never release in these cases. The peer
harness deliberately holds the Go timer callbacks so a parent kill cannot mask
a missing Java timer. This selects one observable test ordering, not a production
promise that Java always exits before Go's independent cutoff. Conversely,
`startup_deadline` and `duplicate_stop_call` exercise Go supervision against an
unresponsive scripted child; they are not descriptions of a conforming Java
peer. Watchdog vectors provide writable, nonblocking pipes, so fatal delivery
can be checked explicitly; broken-pipe expiry must still force exit without it.

`death_after_terminals` begins oracle evaluation and produces a provisional
PASS before injecting child exit. `begin_evaluation` installs the evaluator's
before-return barrier; `provisional_evaluation` supplies the synthetic evaluator
result while keeping its return blocked. Neither event completes the Run.
After child death must latch a transport fault and invalidate that provisional
result, `complete_evaluation` releases the held evaluator return. The final
run-level observer requires the transport error under G2 even though the
evaluator returned PASS. The harness must observe the fault while the evaluator
is held; it cannot stop supervision at evaluation start or synthesize a failure
from the final expected result. These are lifecycle stubs, not new oracle logic.

`fatal_after_terminals` complements child death by delivering a wire fatal
while provisional PASS is held. It requires the stdout reader, not merely the
process watcher, to remain supervised. `duplicate_invoke_java` injects the exact
previous invoke bytes and forbids redispatch and replies.

`active_stop_cancel_watchdog_expires` delivers reason-stop cancel (1000 ms),
then stop (2500 ms), with rollback held. It requires the earlier deadline to
remain active and forces nonzero exit at 1000 ms; the successful active-Stop case
also checks watchdog arming and deadline retention.

`cancel_wins_release_enqueue` and `release_enqueue_wins_cancel` hold a release
candidate after runtime return but before the serialization point, then order
cancellation and `resume_release_enqueue` on opposite sides of that point.
The first forbids release; the second checks release then cancel in sequence.
The barrier does not itself set cancellation or queue frames. These are Go-target
harness cases; Java receives only the emitted frames as the scripted peer.

## Implementation checklist

The consumers are [Go adapter #108](https://github.com/weavegate/weavegate/issues/108)
and [Java/Spring SDK #109](https://github.com/weavegate/weavegate/issues/109).
Both implementation PRs must link this checklist and the same vector file at the
reviewed commit, copy the relevant acceptance items into their validation plan,
and record results against that revision.
[CLI integration #110](https://github.com/weavegate/weavegate/issues/110) owns
configuration enablement; [Spring evidence #111](https://github.com/weavegate/weavegate/issues/111)
owns the combined MySQL reproduction. Do not mark this design checklist complete
on the strength of prose or a mock-only test.

- [x] Resolve ADR gap G4 for external CLI execution: launch/config/budgets in ADR 0016. G1 and G3 are resolved by the fixture connection contract; G2/G5/G6 are resolved at the Go boundary by ADR 0014. External implementations must still prove their mappings.
- [ ] Consume all shared vector IDs targeting the implementation under test. Go owns runtime mapping, channel closure, process supervision, and error propagation; Java owns framing, dispatch, gates, proxy/lease tracking, and local cancellation. Both test malformed input and duplicate handling.
- [ ] Implement only child-JVM launch with framed stdin/stdout. Test fragmented/coalesced frames, invalid JSON/UTF-8/fields/version, unknown names/IDs, gaps, conflicting duplicates, sequence exhaustion, and capacity exhaustion without changing application state on rejection.
- [ ] Exercise concurrent arrivals and releases with barriers, including a blocked worker while another commits. The pipe reader/writer must remain live; no sleep-based coordination or polling for readiness.
- [ ] Prove fresh sessions for repeats/exploration, stale-session isolation, invocation identity on worker reuse, and no second command execution on duplicate input. Test immediate repeated point rejection remains consistent with the Go runtime.
- [ ] Against the pinned Spring proxy, transaction manager, Connector/J and pool, prove success, rollback, transaction-begin failure, rollback-only, after-commit exception, unknown commit/rollback, and suppressed connection-close failure. Record transaction and lease milestones separately; no terminal before proxy exit and successful lease return.
- [ ] Prove disabled Java instrumentation returns immediately from sync points without reading stdin, launching a protocol loop, or changing transaction behavior.
- [ ] Validate transaction registration and reject unsupported propagation, self-invocation bypass, extra DataSources, background DB writers, and async commands. Explicit test instrumentation must not alter the business transaction's intended invariant.
- [ ] Test cancel before accepted, at arrival, in blocked JDBC, during commit, and after terminal. Prove cancellation cannot be undone by release. Driver cancel requests alone cannot satisfy completion.
- [ ] Test startup failure/deadline, no ready, disconnect at each phase, silent child, blocked pipe writer, child death before and after terminal, nonzero exit, missing stopped, hung shutdown, idempotent Stop, and bounded forced termination/reaping. No elapsed phase can restart the deadline.
- [ ] Verify fatal faults interrupt execution and oracle evaluation, invalidate provisional success, and preserve nonzero run failure. Worker errors (including MySQL 1213/1205) remain terminal facts for existing oracles; no new verdict logic lives in the adapter.
- [ ] Prove Reset follows normal Stop and cannot run after uncertain cleanup until fixture teardown/reprovisioning. Verify no old connection, transaction, JVM thread, or queued bridge call survives into a later schedule.
- [ ] Verify readiness uses the fixture-provided endpoint and application account; probe lease returned, migration/reset owned only by Go, adequate worker/pool capacity, no credentials in argv/environment/artifacts/errors, bounded stderr, and stdout reserved before Boot startup.
- [ ] Add end-to-end vulnerable/fixed MySQL evidence with repeated runs (`-count=N` for Go) and record exact commands, counts, toolchain/dependency versions, and markers. Do not claim deterministic external replay from one pass or adjust waits to hide instability.
- [ ] Update public config/instrumentation docs only when implementation ships. If a new diagnostic is chosen, add its matching reference page and index entry in that implementation PR.
