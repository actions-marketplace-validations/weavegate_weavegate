<!-- markdownlint-disable-file MD033 MD041 -->

<p align="center">
  <img src="assets/logos/logo-horizontal.svg" width="310" alt="weavegate">
</p>

<p align="center">
  Deterministically replay the DB race, gate the deploy.
</p>

<p align="center">
  <a href="https://github.com/weavegate/weavegate/actions/workflows/smoke.yml?query=branch%3Amain"><img alt="smoke" src="https://github.com/weavegate/weavegate/actions/workflows/smoke.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/weavegate/weavegate/releases"><img alt="release" src="https://img.shields.io/github/v/release/weavegate/weavegate?include_prereleases"></a>
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="database" src="https://img.shields.io/badge/DB-MySQL%208%20%2F%20InnoDB-lightgrey">
  <img alt="coverage" src="https://img.shields.io/badge/coverage-88%25-brightgreen">
</p>

<p align="center">
  <a href="#overview">Overview</a> ·
  <a href="#results">Results</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#run-the-cli">Run the CLI</a>
</p>

<a id="overview"></a>

**weavegate** is a deterministic integration-test harness for application-level database races in transactional `read → decide → write` workflows. It explores the interleavings between sync-points you place, runs the workflow against real MySQL, and lets rule-based SQL oracles judge the resulting state. When an invariant breaks, it saves the release schedule, trace, violating rows, report, and CI exit code so the same failure—and the fix—can be replayed.

![Two overlapping requests produce duplicate state; weavegate controls the interleaving and checks it.](assets/readme/problem.svg)

- **Find the order ·** bounded schedule exploration — expose the interleaving ordinary tests may miss.
- **Force the race ·** named sync-points — coordinate workers without timing sleeps.
- **Prove the outcome ·** SQL oracles and artifacts — retain the verdict, state, trace, and replay command.

See [Limitations](docs/limitations.md) for replay and PASS boundaries.

## Results

![Measured replay, control, exploration, Spring Boot replay, and gate run-time results.](assets/readme/results.svg)

| Measurement | Result |
| --- | --- |
| **Replay · 20/20 × 3** | Every saved replay violated ([source](docs/experiments/baseline-comparison.md#measured-result)). |
| **Controls · 0/100 × 3** | Serial and 20/100 ms staggered runs did not ([source](docs/experiments/baseline-comparison.md#measured-result)). |
| **Explore · 6 candidates** | Index 1 yielded `sch_7dcb74b1e506`; `flaky=false`; **25.5 s** ([source](docs/experiments/exploration.md#reproduction)). |
| **Spring · 20/20 + 20/20** | External JVM, same schedule: vulnerable `WG001`, fixed PASS ([source](docs/experiments/spring-replay.md#repeated-paired-test)). |
| **CI gate · 85–129 s** | `v0.2.0` Spring Boot gate job on `ubuntu-latest` ([source](docs/experiments/gate-run-time.md#ci-gate-on-a-github-hosted-runner)); ≈ 2.7–3.8 s per schedule run on a local host ([source](docs/experiments/gate-run-time.md#local-host-and-where-the-time-goes)). |
| **Boundary · two fixtures + example** | Measured hosts and pinned versions only; not a detection rate or benchmark. |

See [Experiments](docs/README.md#measured-results) for methods and reproduction.

## How it works

![Fixture preparation, scheduled coordination, oracle evaluation, and retained evidence.](assets/readme/how-it-works.svg)

1. **Declare ·** fixture data binds the scenario, migration, seed, and SQL assertions.
2. **Orchestrate ·** the schedule engine explores, replays, and repeats sync-point release orders.
3. **Execute ·** the selected Go-native or external JVM adapter runs workers with separate connections against real MySQL.
4. **Judge and retain ·** composable oracles emit the verdict, schedule, trace, rows, report, and exit code.

See [Architecture](docs/architecture.md#execution-flow) for details.

## Run the CLI

![CLI input, controlled MySQL run, and replayable failure evidence.](assets/readme/run-cli.svg)

> **Pre-release:** running requires Docker/MySQL 8.4; building from source or using `go install` requires Go 1.25+. Interfaces may change before 1.0.

```console
$ go build -o weavegate ./cmd/weavegate
$ ./weavegate run --config fixtures/matching-slice/.weavegate/config.yaml --scenario concurrent-assign --variant vulnerable
## weavegate: FAIL (WG001)
```

- **Input ·** fixture — selects scenario, data, adapter, and oracle.
- **Output · exit 2** — `WG001` writes run evidence.
- **Replay ·** saved schedule — test vulnerable or fixed code.

See [Installation](docs/install.md) for the archive, source, and go install
routes, [Quickstart](docs/quickstart.md) for output and replay, the
[GitHub Actions gate](docs/howto/ci-gate.md) to fail a job on a violation while
retaining the report, replay schedule, and optional pull request comment, and
[Spring Boot adoption](docs/howto/adopt-spring-boot.md) to gate your own
application.

## Contributing / License

- **Contributing ·** fixtures are welcome — follow [Contributing](CONTRIBUTING.md).
- **Support ·** choose a route in [Support](SUPPORT.md).
- **License ·** Apache-2.0 — see [LICENSE](LICENSE).

See [changelog](CHANGELOG.md), [milestones](https://github.com/weavegate/weavegate/milestones), and [related work](docs/related-work.md).
