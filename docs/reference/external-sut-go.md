# Go external SUT adapter

[`internal/sut/external`](../../internal/sut/external/) implements the Go peer
of [wire v1](external-sut-v1.md). It launches one owned process with
`exec(java, "-jar", jar)` and communicates over framed stdin/stdout. The
[Java peer](external-sut-java.md) is a separate implementation. The CLI can
select this adapter through [configuration](config.md#external-jvm). Its isolated
Go acceptance gate runs in smoke CI.

## Construction and ownership

`external.New(Options, syncpoint.Client)` returns a single-use `sut.Adapter`.
Construct a fresh adapter with the schedule's runtime client for every repeat.
Options declare the Java executable, prebuilt JAR, commands, points, capacity,
startup/cancellation/stop budgets, and an optional correlation run ID. The
adapter generates a random session and distinct invocation IDs. It does not
build a JAR, accept shell commands, attach to a process, or provision a database.
The selected child must not spawn descendants.

`Start` consumes the fixture's [application descriptor](fixture-connection.md).
The password travels only in the private start frame. Start rejects invalid
UTF-8 in configuration and database strings before JSON encoding or launch,
so encoding cannot silently replace their bytes. The reader validates
registration equality before returning a handle. Wire decoding rejects unknown
fields, duplicate keys, invalid Unicode, unsupported versions, malformed
identities, and invalid state transitions. Foreign sessions do not advance the
sequence or reach the runtime. Exact frame duplicates have no repeated effect;
conflicting sequence duplicates and conflicting retired terminals fail the
session. Worker reuse starts a new invocation after the previous stream closes.

Each accepted arrival gets an independent `Client.Arrive` call. The pipe reader
continues handling other workers while a bridge blocks. Cancellation and release
enqueueing share the adapter mutex; neither pipe I/O nor a blocking queue send
holds it. A full bounded control queue fails the session. A release is sent only
after the matching runtime call returns nil and cancellation has not won that
serialization point. This adds no scheduling or verdict logic to the adapter.

## Outcomes, supervision, and private data

Started terminals become one `WorkerResult`; `not_started` becomes one
`UnstartedResult`. Publication and channel closure wait for invocation bridge
tasks to unwind. Unknown transaction or connection cleanup is a session fault,
never an invented terminal. MySQL vendor code and SQLSTATE reach the existing
classifier, including deadlock 1213 and ordinary lock-timeout error 1205.
Cancellation preserves context error identity; a committed nil-error terminal
stays successful even if the enclosing Run is canceled.

A terminal validated before a session fault is still published after its bridge
tasks unwind. The session and Stop remain failed; the known transaction outcome
is retained as evidence. Terminals received after a fault are ignored, and
invocations without a validated terminal close without an invented outcome.

Peer error text is replaced by stable local summaries. This deliberately loses
free-form application detail to keep SQL, JDBC URLs, and credentials out of
public errors. The private stderr tail retains at most 1 MiB and is not exported
or persisted. IDs, durations, frame ordering, and logs are absent from normalized
fingerprints. The real orchestrator tests verify identical fingerprints across
fresh sessions and invalidate provisional evaluation after a late fatal or death.

Startup computes the remaining whole-millisecond budget immediately before
writing start. Expiry of the original startup deadline latches a session fault
and best-effort sends startup fatal; a later stopped cannot restore normal
cleanup. Explicit Start cancellation or Stop before readiness still permits
normal cleanup. Stop establishes one absolute deadline and reserves its second
half for termination/reaping; the stop frame carries only the remaining graceful
budget. Exhausted submillisecond budgets produce no frame. A stopped received
during the stop write waits for successful delivery before taking effect; a
failed or stalled write cannot establish normal cleanup. Normal Stop requires
stopped, stdout EOF, exit zero, closed invocation streams, unwound bridges, and
no session fault. Concurrent callers share that outcome; an earlier caller
deadline can return an error without restarting or extending shared cleanup.

## Isolated Go acceptance

The [acceptance plan](testdata/external-sut-acceptance.json) pins the shared
vectors at `c7cea1f3f5732066d1fb60b6bf6414fdb96019cf`. Tests verify the pinned
SHA-256 before execution. `TestSharedFraming` consumes all framing cases;
`TestSharedLifecycleGo` consumes all 53 Go-applicable shared lifecycle cases and
fails on unknown event arguments or assertions. Orchestrator tests observe
fixture quarantine, reset rejection, and late evaluation invalidation. Other
tests exercise actual blocked/broken OS pipes, process death and reaping,
cancellation races, and the wire matrix.

Run the repeated adapter tests and publish a manifest beside their log:

```bash
mkdir -p /tmp/weavegate-external-evidence
go test ./internal/sut/external -v -count=20 > /tmp/weavegate-external-evidence/go.log
python3 scripts/record-external-sut-go-results.py \
  --log /tmp/weavegate-external-evidence/go.log \
  --output /tmp/weavegate-external-evidence/go.json \
  --revision "$(git rev-parse HEAD)" \
  --command 'go test ./internal/sut/external -v -count=20' \
  --go-version "$(go version)"
python3 scripts/check-external-sut-acceptance.py \
  --results /tmp/weavegate-external-evidence/go.json --require-complete
go test ./internal/sut/external -race -count=20
python3 scripts/test-external-sut-go-results.py
```

The recorder assigns observer checks to individual top-level test executions
using verbose Go test RUN/PASS boundaries. Each check must occur exactly once
in every repetition of the same test; offsetting omissions and duplicates
cannot satisfy the count. Parallel test logs are rejected. Handler references
must name an existing function or receiver method in the referenced Go source;
comments and string literals do not count as declarations. Missing checks stay
incomplete; unknown check IDs, changed handlers, failed logs and incorrect
repetition counts are rejected. The checked-in result template remains
incomplete; CI publishes its filled manifest and log as an artifact. The strict
gate requires all 72 Go rows to pass. The manifest and referenced log are
review evidence, subject to the review limits in the
[acceptance contract](external-sut-acceptance.md). The [fixture quarantine
boundary](fixture-connection.md) rejects Reset after a failed Stop or latched
session fault. CLI launch/budget composition is recorded in
[ADR 0016](../adr/0016-external-cli-composition.md); live paired MySQL evidence
remains [#111](https://github.com/weavegate/weavegate/issues/111).
