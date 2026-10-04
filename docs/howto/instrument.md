# Instrument a Workflow with Sync-Points

Sync-points expose selected boundaries in a concurrent workflow so a test
orchestrator can release workers in a chosen order. They are coordination
seams, not sleeps, locks, or verdict logic.

The CLI ships one built-in Go-native entrypoint and can launch an explicitly
instrumented external JVM from [configuration](../reference/config.md#external-jvm).
This guide explains the Go-native reference fixture's instrumentation pattern.
For a Spring application, use the [Java peer's opt-in boundary](../reference/external-sut-java.md#opting-in)
and the external config keys. The
[`spring-matching` fixture](../../fixtures/spring-matching/README.md) applies
the same two points to a Spring Boot application.

## Start from the contested decision

Look for a workflow whose correctness depends on state that can change between
an observation and its write:

```text
read -> decide -> write
```

Place points at stable semantic boundaries around that decision, not at line
numbers or elapsed-time guesses:

1. after the transaction has read the state that informs the decision;
2. immediately before the write that commits the decision.

The matching-slice fixture names those boundaries `after_read_request` and
`before_insert_assignment`. The first lets both vulnerable workers observe an
active request. The second lets both workers finish their "already assigned?"
decision before either assignment is written. The
[fixture walkthrough](../../fixtures/matching-slice/README.md#assignment-workflow)
explains the resulting schedule.

Do not add a point inside a database driver call or use a sleep as a substitute.
A point says which application boundary was reached; it does not prove why a
worker has not reached another boundary.

## Add the smallest consumer-side seam

Keep the workflow dependent on a tiny interface and pass the worker identity
through the request or command boundary. The following constructed diff shows
the pattern; adapt the names to the workflow's domain:

```diff
+type SyncPoint interface {
+    Arrive(context.Context, string, string) error
+}
+
+type NoopSyncPoint struct{}
+
+func (NoopSyncPoint) Arrive(context.Context, string, string) error {
+    return nil
+}

 func (s *Service) Assign(ctx context.Context, workerID string) error {
     request, err := s.repository.ReadRequest(ctx)
     if err != nil {
         return err
     }
+    if err := s.syncPoint.Arrive(ctx, workerID, "after_read_request"); err != nil {
+        return err
+    }
     if s.repository.HasAssignment(ctx, request.ID) {
         return nil
     }
+    if err := s.syncPoint.Arrive(ctx, workerID, "before_insert_assignment"); err != nil {
+        return err
+    }
     return s.repository.InsertAssignment(ctx, request.ID)
 }
```

Propagate an arrival error so cancellation or a failed coordinator rolls back
the transaction. The reference implementation does this in
[`service.go`](../../fixtures/matching-slice/sut/service.go).

## Keep production execution a no-op

Production construction must select a no-op implementation. In the reference
fixture, `NewRegistry(nil)` installs `NoopSyncPoint`, whose `Arrive` method
returns immediately. Only the replay and exploration harnesses inject the
coordinating runtime. See
[`registry.go`](../../fixtures/matching-slice/sut/registry.go) and the
worker-facing [`syncpoint.Client`](../../internal/syncpoint/runtime.go).

This split keeps the application workflow identical in test and normal
execution while ensuring normal execution is never waiting for a test
orchestrator. Test the default explicitly: the reference fixture asserts that
a nil dependency selects the no-op and records the point sequence separately.

## Declare the same names in the scenario

The scenario's `sync_points` list is the ordered vocabulary every worker uses.
Its names must match the strings passed to `Arrive`:

```yaml
sync_points:
  - after_read_request
  - before_insert_assignment
```

Once the seam and scenario agree, schedules can refer to a worker and point by
name. The orchestrator controls releases; the oracle still owns the verdict.
