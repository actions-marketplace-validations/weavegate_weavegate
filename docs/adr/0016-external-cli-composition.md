# ADR 0016: External CLI composition and budgets

- Status: Implemented for external wire v1 CLI selection
- Scope: [issue #110](https://github.com/weavegate/weavegate/issues/110)

The CLI accepts `target.sut.adapter: external` with one owned `java -jar`
launch. `target.sut.external` supplies an executable, a prebuilt executable
JAR, worker capacity, a closed JVM tuning choice, and startup/cancellation/stop
budgets. It accepts no shell string, arbitrary extra argv, environment overrides, attach mode, or configured
database credential. The fixture descriptor supplies the application account
and password only in the private `start` frame after provisioning. Schema,
seed, scenario, worker parameters, and SQL assertions remain config data.
The Go-native entrypoint registry and its variant list remain independent.

For [#172](https://github.com/weavegate/weavegate/issues/172), omitted or
`startup` tuning adds the fixed HotSpot `-XX:TieredStopAtLevel=1` option before
`-jar`. Each schedule's short-lived JVM avoids higher-tier compilation work;
application initialization and readiness checks still finish before admission.
`none` keeps the original launch for unsupported VMs or workloads that benefit
from higher compilation tiers. This is a startup/throughput tradeoff, not a
timeout change or an assertion that every application is faster. There is no
automatic fallback that could execute application initialization twice.
The self-contained JAR snapshot, fresh process, protocol and cleanup rules
are unchanged. No extra build step or persistent cache is introduced. Local
attribution and whole-gate evidence are separate from E1 confirmation; see
[startup tuning](../../experiments/gate-performance/startup/README.md).
`manifest.sut_jvm_tuning` retains the selected policy as additive volatile
metadata under [ADR 0007](0007-artifact-version-policy.md).

Before provisioning, the CLI resolves the Java executable and JAR relative to
the config file (except a bare Java command resolved from `PATH`), checks that
the JAR is readable and self-contained, requires one manifest with a unique
`Main-Class` in its main section, checks capacity against the selected
scenario's worker count, validates wire names, and derives the sorted unique
command list and ordered point list from that scenario. The external adapter
checks the peer's `ready` registration against those lists before admitting
workers. A command that exists only in the remote app cannot
be proved at static preflight; missing or mismatched remote registration fails
startup. Preflight hashes the configured JAR while copying it into a private
image, validates that image's manifest, then discards it. Each external adapter
copies a per-session snapshot during `Start` and checks its digest against
preflight before launching. The JVM launches that snapshot, so a
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
run context. The adapter's `Start` receives the run context and stops waiting
for snapshot copying when its deadline expires; the engine factory signature
remains adapter-independent. If filesystem I/O remains blocked, the private
copy finishes cleanup asynchronously once that I/O returns. Stop
has a detached context with `stop_timeout_ms`, which matches the external
adapter's total stop budget; the adapter reserves the latter half for
termination and process reaping, and sends the remaining first-half budget
in the wire `stop` frame. The cancellation budget must fit within stop. Budget
arithmetic is checked before provisioning. These are failure bounds, never a
means of coordinating worker arrivals.

External execution uses the same adapter-independent saved-run and portable
schedule lookup; it has no embedded schedule registry. Wire cancellation and
session faults remain run errors, and uncertain cleanup quarantines the fixture
under the existing engine rule. The mini JVM CLI integration test establishes
configuration-only launch and replay composition. It is not Spring/JDBC
acceptance; the live paired MySQL evidence is recorded by
[#111](https://github.com/weavegate/weavegate/issues/111) in
[Spring paired replay](../experiments/spring-replay.md).
