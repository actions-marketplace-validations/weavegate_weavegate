# Second local tuning registration

The first campaign (`local-tuning-20261018`, registration commit
`530233d9bba03e7f0d7c42199a61e00b8861c08c`) stopped after its first pair.
Both CLI invocations exited 5 before provisioning: the pinned
`mysql@sha256:…` reference violates the existing `target.db` requirement that
the value start with `mysql:`. No schedule, JVM, or database execution was
observed. The two command receipts, logs, cleanup observations, original plan,
and all 32 planned pairs remain in the stopped evidence bundle. No timing
estimate is made from these preflight failures.

The new campaign `local-tuning-20261018-r2` uses
`mysql:8.4@sha256:8dbcf531a03aade657e181b9cf2f1d1803ce621a1d55610cb44cb531ab7d7db6`.
The resolved image is identical. Only this input syntax changes; the workload,
32-pair budget, fixed order, product settings, collector/analyzer, and limits
remain the same. The first campaign is not pooled into the new campaign and
its failures are not replaced in its ledger. `registration-r2/` and this note
must be published before the second campaign begins. The original
`registration/` remains unchanged. This is a local tuning correction, not an
amendment to the registered E1 protocol or its pilot/confirmation sample.

```bash
python3 -B experiments/gate-performance/local-pilot/freeze.py \
  /tmp/weavegate-a1 experiments/gate-performance/local-pilot/registration-r2 \
  local-tuning-20261018-r2
```
