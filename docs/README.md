# weavegate documentation

This map covers the documentation shipped in this repository; choose the section that matches the question you need to answer.

This repository-relative map is bundled in release archives, but targets outside `docs/`—including `fixtures/`, `internal/`, `cmd/`, and `CONTRIBUTING.md`—remain in the [weavegate repository](https://github.com/weavegate/weavegate).

## Start here

Use these guides when you are new to weavegate or ready to create and instrument a fixture.

- [Quickstart](quickstart.md) — Install weavegate and reproduce the bundled matching-slice failure.
- [Instrument a workflow](howto/instrument.md) — Place sync-points around a contested decision without changing its semantics.
- [Write a fixture](howto/write-a-fixture.md) — Package a schema, seed data, adapter, schedules, and oracle as a reusable fixture.

## Reference (the contract)

Use these pages when you need the exact CLI, configuration, artifact, or diagnostic contract.

- [CLI](reference/cli.md) — Look up commands, flags, output, and replay behavior.
- [Configuration](reference/config.md) — Look up configuration keys, their defaults, and the documented behavior of each.
- [Exit codes](reference/exit-codes.md) — Interpret process status across pass, fail, flaky, and usage outcomes.
- [Fixture application connection descriptor](reference/fixture-connection.md) — Consume the prepared database's application endpoint without leaking its ephemeral credential.
- [Report schema](reference/report-schema.md) — Consume run artifacts without guessing their fields or stability guarantees.
- [Adapter outcomes](reference/adapter-outcomes.md) — Invocation streams, session faults, cancellation boundaries, and reproducible Go evidence.
- [External SUT wire v1 (proposed)](reference/external-sut-v1.md) — Review the child-process protocol; external CLI execution is not enabled.
- [Go external SUT adapter (in development)](reference/external-sut-go.md) — Inspect the Go peer, executable evidence, and remaining acceptance before enablement.
- [Java Spring external SUT peer (in development)](reference/external-sut-java.md) — Opt into Spring instrumentation and inspect the Java peer's evidence and remaining acceptance.
- [External SUT conformance (proposed)](reference/external-sut-conformance.md) — Share protocol vectors, lifecycle sequences, and implementation acceptance checks.
- [External SUT acceptance accounting](reference/external-sut-acceptance.md) — Pin shared vectors and account for per-target handlers, results, and missing runtime evidence.
- [WG001](reference/diagnostics/WG001.md) — Understand evidence for an invariant violation under a controlled schedule.
- [WG090](reference/diagnostics/WG090.md) — Diagnose a determinism check that produced inconsistent normalized evidence.

## Explanation (why it works)

Use these pages to understand the model, boundaries, tradeoffs, and relationship to other tools.

- [Concepts](concepts.md) — Build the vocabulary for schedules, sync-points, traces, and oracles.
- [Architecture](architecture.md) — Follow execution through the CLI, fixture, orchestrator, adapter, and oracle boundaries.
- [Why the fix works](why-the-fix-works.md) — See why locking the contested rows restores the fixture invariant.
- [Frequently asked questions](faq.md) — Resolve common questions about scope, determinism, and intended use.
- [Related work and attribution](related-work.md) — Compare weavegate with adjacent concurrency-testing approaches and trace its influences.
- [Limitations](limitations.md) — Check what a saved schedule proves and where that evidence stops.

## Decision records

Use these records when you need the rationale behind a durable design boundary.

- [ADR 0001: Worker-owned database connections](adr/0001-worker-owned-connections.md) — Learn why each worker owns the connection that carries its transaction.
- [ADR 0002: Sync-point runtime state machine](adr/0002-syncpoint-runtime-state-machine.md) — See how arrival, release, blocking, and terminal states are represented.
- [ADR 0003: Oracle evaluation boundary](adr/0003-oracle-evaluation-boundary.md) — Understand why verdict logic belongs exclusively to oracles.
- [ADR 0004: Schedule exploration boundary](adr/0004-schedule-exploration-boundary.md) — Find where candidate enumeration ends and execution begins.
- [ADR 0005: Volatile run metadata boundary](adr/0005-volatile-run-metadata-boundary.md) — Distinguish normalized evidence from run-specific metadata.
- [ADR 0006: Diagnostic mapping key](adr/0006-diagnostic-mapping-key.md) — See how verdicts map to stable diagnostic identities.
- [ADR 0007: Artifact version policy](adr/0007-artifact-version-policy.md) — Check compatibility rules for persisted run artifacts.
- [ADR 0008: Diagnostic evidence model](adr/0008-diagnostic-evidence-model.md) — Understand how structured evidence supports a diagnostic verdict.
- [ADR 0009: Portable schedule artifact and lookup](adr/0009-schedule-portability.md) — Learn how schedules remain portable and are resolved for replay.
- [ADR 0010: External SUT protocol (proposed)](adr/0010-external-sut-protocol.md) — Review the planned Spring transaction boundary and required engine extension decisions.
- [ADR 0011: Report Markdown rendering boundary](adr/0011-report-markdown-rendering-boundary.md) — See how runtime values remain one-line and inert in Markdown reports.
- [ADR 0012: Preserve evidence when diagnostic derivation fails](adr/0012-diagnostic-derivation-failure-evidence.md) — Learn why a completed run retains evidence and exits 5 when diagnostic production fails.
- [ADR 0013: Scope a clean-run differential Oracle](adr/0013-clean-run-differential-oracle-scope.md) — See which second invariant justifies a richer Oracle and how v0.2.0 bounds it.
- [ADR 0014: Adapter outcome boundaries](adr/0014-adapter-outcome-boundaries.md) — Session faults, unstarted invocations, cancellation, and error aggregation.
- [ADR 0015: Java application execution boundary](adr/0015-java-execution-boundary.md) — Define trusted fixture SQL, JDBC ownership, and Spring proxy requirements.

## Measured results

Use these experiments for reproduced observations; their measured values are tied to the recorded hosts and are not universal performance claims.

- [Replay determinism](experiments/determinism.md) — Inspect repeated evidence from the saved matching-slice schedule.
- [Schedule exploration](experiments/exploration.md) — Review which candidates reproduce the invariant violation and how they were enumerated.
- [Baseline comparison](experiments/baseline-comparison.md) — Compare uncontrolled baseline detection with controlled schedule replay.

## Maintainer notes

Use these notes when maintaining repository-facing documentation and visuals.

- [README editorial and visual maintenance](maintainers/readme-maintenance.md) — Follow the content, evidence, and visual contracts for focused README updates.
