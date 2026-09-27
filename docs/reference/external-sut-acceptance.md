# External SUT acceptance accounting

The accounting tool records what the external SUT implementations still need to
prove. It does not run an adapter, interpret lifecycle events, or certify a
transaction, deadline or process exit. The checked-in Go, Java and paired
manifests contain no execution evidence and cannot pass the implementation gate.
This accounting contract was added under [#121](https://github.com/weavegate/weavegate/issues/121).
The isolated consumers and live paired run report evidence separately.

## Reviewed input and ownership

[The acceptance plan](testdata/external-sut-acceptance.json) pins
[the shared vectors](testdata/external-sut-v1.json) at reviewed commit
`f32cd292246287a22c1a057012dd468f25c41c7d`, including their exact SHA-256 digest.
The checker rejects different bytes. Its unit test also compares those bytes to
the pinned Git revision and verifies that it is an ancestor of the checkout;
CI fetches full history for this check. When a pull request pins a commit on
its own branch, merge it with a merge commit so the pinned object remains in
the merged ancestry. Squash or rebase merging would break this verification.
Both language consumers must use this same input, without private vector forks.

| Target | Implementation owner | Checked-in result manifest |
| --- | --- | --- |
| Go isolated | [#108](https://github.com/weavegate/weavegate/issues/108) | [Go results](testdata/external-sut-results-go.json) |
| Java isolated | [#109](https://github.com/weavegate/weavegate/issues/109) | [Java results](testdata/external-sut-results-java.json) |
| Live paired/MySQL | [#111](https://github.com/weavegate/weavegate/issues/111) | [Paired results](testdata/external-sut-results-paired.json) |

Each `requirement/` row names an acceptance family, its owner and its required
evidence in the plan. The pinned vectors include 12 Java wire matrix histories;
their isolated Java handlers and real Spring checks are exercised by the Java
suite. Go and paired execution have separate applicability. Launch/budget
composition remains
[#110](https://github.com/weavegate/weavegate/issues/110); reset quarantine remains
[#120](https://github.com/weavegate/weavegate/issues/120).

When adding missing shared cases, review the vector change first, then update
the pin, inventories and templates together. Re-run consumers against that
revision. Earlier results cannot satisfy the new inventory. Keep the existing
structural guard; do not implement protocol semantics in the accounting tool.

## Enumerating and recording results

Run from the repository root with Python 3.9 or newer:

```bash
python3 scripts/check-external-sut-acceptance.py --inventory go
python3 scripts/check-external-sut-acceptance.py --inventory java
python3 scripts/check-external-sut-acceptance.py --inventory paired
python3 scripts/check-external-sut-acceptance.py --template go
python3 scripts/test-external-sut-acceptance.py
```

`--inventory` prints every required check ID for each applicable case and
requirement. `--template` prints an incomplete result manifest to stdout; it also
accepts `java` and `paired`. The checked-in manifests are these templates.
Consumers publish separate filled manifests as CI artifacts
alongside their referenced logs. Do not edit the templates to present a local
run as permanent implementation evidence.

The inventory expands prefixes and uses zero-based expanded step indexes:

| Check ID | Consumer obligation |
| --- | --- |
| `step/N/local/EVENT` | Dispatch through a real harness seam; enforce the event's argument shape before injection. |
| `step/N/receive/TYPE` | Inject the explicit receive input into the target. |
| `step/N/expect/K/LABEL` | Observe this target-side assertion occurrence in vector order. Repeated labels remain separate checks. |
| `step/N/output/TYPE` | Compare actual target output with an `exchange` frame addressed to the scripted peer. |
| `input/*`, `control/*`, `observe/*`, `expect/*` | Execute framing input, fresh control decoder, chunk/EOF behavior, decoded comparison and framing assertions. |
| `observe/evidence` | Link the additional acceptance family's executable evidence for review. |

Opposite-peer local steps and assertions are not results for the target under
test. Adversarial `input` is not expected sender output. Go and Java cases run
separately against scripts; paired execution needs its own live-peer histories.
It cannot replay adversarial isolated vectors as compliant paired conversations.

A manifest has exactly `format`, `target`, `vector`, `run` and `results`.
`format` is integer 1; `target` is `go`, `java` or `paired`; `vector` must equal
the plan's pin. `results` must contain every applicable `case/`, `framing/` and
`requirement/` key. Missing, duplicate, extra and other-target cases are rejected.

Every result contains `status`, `reason` and `checks`:

- `incomplete` requires a nonempty reason. Its `checks` may be empty or contain
  observations already obtained; omitted checks never count as passing.
- `fail` requires a nonempty reason and at least one recorded failing check.
- `pass` requires every inventory check, each with status `pass`.

Each recorded check has exactly `status` (`pass` or `fail`), `handler` (a
nonempty source/test reference) and `evidence` (a nonempty list of artifact IDs).
Unknown fields, check IDs and statuses, including `skip` and `not_applicable`,
are rejected. Applicability comes from the pin and plan. Missing real handlers
leave their checks absent and the case incomplete.

`run` is null when nothing has executed. A recorded run has exactly:

- `revision`: the full implementation commit SHA.
- `command`: the exact repeated command that produced the evidence.
- `repetitions`: an integer of at least 20. Go commands must contain matching
  `-count=N`; use `-count=20` for initial acceptance.
- `repetition_method`: how that command repeats tests. The Java recorder requires
  a command with matching `-Dweavegate.repetitions=N`, which generates one JUnit
  execution per repetition; see the [Java peer](external-sut-java.md).
- `versions`: nonempty version strings. Go requires `go`; Java requires `java`,
  `spring`, `transaction_manager`, `jdbc_driver`, `pool` and `build_tool`; paired
  requires those Java entries plus `go` and `mysql`.
- `artifacts`: a map from artifact ID to exactly `path` and `sha256`. Paths are
  relative to the manifest directory, cannot escape it, and must identify files
  whose bytes match their digests. Publish logs without credentials.

The checker verifies accounting and referenced files. It cannot prove a declared
handler executed, a command really repeated the tests, or an artifact proves
the effect claimed. Reviewers must inspect the implementation and evidence.
Consumer tests must reject unknown event arguments and exception phases before
injection. Assertions must observe independently: they cannot manufacture a
completion, reap a child or inject the error they expect. Real writer/deadline
and pinned Spring tests remain required when accounting unit tests pass.

## Validation versus implementation acceptance

These commands validate the current incomplete manifests:

```bash
python3 scripts/check-external-sut-acceptance.py --results docs/reference/testdata/external-sut-results-go.json
python3 scripts/check-external-sut-acceptance.py --results docs/reference/testdata/external-sut-results-java.json
python3 scripts/check-external-sut-acceptance.py --results docs/reference/testdata/external-sut-results-paired.json
```

Captured output:

```text
EXTERNAL_SUT_ACCEPTANCE_RESULT target=go manifest=valid acceptance=incomplete pass=0 fail=0 incomplete=62
EXTERNAL_SUT_ACCEPTANCE_RESULT target=java manifest=valid acceptance=incomplete pass=0 fail=0 incomplete=60
EXTERNAL_SUT_ACCEPTANCE_RESULT target=paired manifest=valid acceptance=incomplete pass=0 fail=0 incomplete=2
```

For implementation acceptance, check its produced manifest with
`--require-complete`. This command currently exits 1 because the checked-in Go
manifest has no runtime evidence:

```bash
python3 scripts/check-external-sut-acceptance.py --results docs/reference/testdata/external-sut-results-go.json --require-complete
```

Without the flag, exit 0 means only that the manifest is structurally valid;
it may report failures or incomplete work. With the flag, any failed or incomplete
row returns 1. Malformed manifests always return 1. `acceptance=complete` means
all required evidence was reported, subject to the review limits above.

Smoke CI tests rejection paths, checks the fixed
`EXTERNAL_SUT_ACCOUNTING_TEST_RESULT` marker with `grep -F`, validates the three
incomplete templates and proves they fail the strict gate. These checks are not
adapter acceptance. The Java smoke job records a separate runtime manifest and
applies `--require-complete`; Go and paired acceptance have their own remaining
gates. Paired evidence must link accepted Go and Java manifests at the same pin.
