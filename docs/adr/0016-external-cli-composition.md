# ADR 0016: External CLI composition and budgets

- Status: Implemented for external wire v1 CLI selection
- Scope: [issue #110](https://github.com/weavegate/weavegate/issues/110)

The CLI accepts `target.sut.adapter: external` with one owned `java -jar`
launch. `target.sut.external` supplies an executable, a prebuilt executable
JAR, worker capacity, and startup/cancellation/stop budgets. It accepts no
shell string, extra argv, environment overrides, attach mode, or configured
database credential. The fixture descriptor supplies the application account
and password only in the private `start` frame after provisioning. Schema,
seed, scenario, worker parameters, and SQL assertions remain config data.
The Go-native entrypoint registry and its variant list remain independent.

Before provisioning, the CLI resolves the Java executable and JAR relative to
the config file (except a bare Java command resolved from `PATH`), checks that
the JAR is readable, self-contained, and has `Main-Class` in the manifest's
main section, checks capacity against the selected scenario's worker count,
validates wire names, and derives
the sorted unique command list and ordered point list from that scenario. The
external adapter checks the peer's `ready` registration against those lists
before admitting workers. A command that exists only in the remote app cannot
be proved at static preflight; missing or mismatched remote registration fails
startup. Each adapter factory copies the JAR into a private per-session snapshot
and checks its digest before construction. The JVM launches that snapshot, so a
replacement of the configured path after construction cannot change the
executed bytes. A changed JAR during copying fails the run rather than
publishing the earlier preflight digest as its provenance. One wire run ID is
shared by all schedule sessions in an operation; each session has a fresh ID.
The selected scenario's start payload is checked against the wire frame limit
before provisioning, with space reserved for the fixture's later database
descriptor. The manifest adds `sut_sha256` for external runs; the
field is absent for Go-native runs. This is an additive v2/v3 artifact field,
so it does not change `artifact_version`.

The existing `arrive_timeout_ms` still sets block inference to 1× and step
coordination to 20×. For external runs, the run context is
`startup_timeout_ms + 60 × arrive_timeout_ms`; this single deadline includes
fixture reset, JVM startup, command execution, and Oracle evaluation. Startup
is independently bounded by the configured startup budget and that remaining
run context. The adapter factory receives the run context so snapshot copying
stops when its deadline expires. Stop has a detached context with
`stop_timeout_ms`, which matches the external adapter's total stop budget; the
adapter reserves the latter half
for termination and process reaping, and sends the remaining first-half budget
in the wire `stop` frame. The cancellation budget must fit within stop. Budget
arithmetic is checked before provisioning. These are failure bounds, never a
means of coordinating worker arrivals.

External execution uses the same adapter-independent saved-run and portable
schedule lookup; it has no embedded schedule registry. Wire cancellation and
session faults remain run errors, and uncertain cleanup quarantines the fixture
under the existing engine rule. The mini JVM CLI integration test establishes
configuration-only launch and replay composition. It is not Spring/JDBC
acceptance; the live paired MySQL evidence is tracked by
[#111](https://github.com/weavegate/weavegate/issues/111).
