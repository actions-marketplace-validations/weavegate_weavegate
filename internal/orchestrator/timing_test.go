package orchestrator

import (
	"context"
	"errors"
	"reflect"
	"testing"

	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/timing"
)

func TestTimingPreservesReplayEvidence(t *testing.T) {
	evaluator := oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
		return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	})
	executor := newReplayTestOrchestrator(t, &recordingFixture{}, nil)
	schedule := matchingSchedule(t)
	baseline, err := executor.Replay(context.Background(), matchingScenario(), schedule, 20, evaluator)
	if err != nil {
		t.Fatal(err)
	}
	ctx, recorder := timing.New(context.Background())
	measured, err := executor.Replay(ctx, matchingScenario(), schedule, 20, evaluator)
	if err != nil {
		t.Fatal(err)
	}
	if measured.Flaky || !reflect.DeepEqual(baseline.Fingerprints, measured.Fingerprints) {
		t.Fatalf("timing changed determinism: baseline=%+v measured=%+v", baseline, measured)
	}
	for i, run := range measured.Runs {
		before := baseline.Runs[i]
		if run.ScheduleID != before.ScheduleID || !reflect.DeepEqual(run.Trace, before.Trace) ||
			!reflect.DeepEqual(run.Terminals, before.Terminals) || !reflect.DeepEqual(run.Evaluation, before.Evaluation) {
			t.Fatalf("timing changed evidence at replay %d", i+1)
		}
	}
	counts := map[string]int{}
	for _, span := range recorder.Snapshot() {
		counts[span.Name]++
		if span.Name == "schedule_run" && span.ScheduleID != schedule.ID {
			t.Fatalf("timing schedule = %q", span.ScheduleID)
		}
	}
	for _, name := range []string{"schedule_run", "fixture_reset", "adapter_create", "adapter_start", "schedule_execute", "oracle_evaluate", "adapter_stop"} {
		if counts[name] != 20 {
			t.Fatalf("%s count = %d, want 20", name, counts[name])
		}
	}
	t.Log("RUN_TIMING_RESULT repeat=20 fingerprints=unchanged evidence=unchanged stages=complete")
}

func TestTimingDoesNotHideExecutionFailure(t *testing.T) {
	want := errors.New("oracle failure")
	evaluator := oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
		return oracle.Evaluation{}, want
	})
	ctx, recorder := timing.New(context.Background())
	executor := newReplayTestOrchestrator(t, &recordingFixture{}, nil)
	run, err := executor.Run(ctx, matchingScenario(), matchingSchedule(t), evaluator)
	if !errors.Is(err, want) || run.Fingerprint != "" || len(run.Evaluation.Results) != 0 {
		t.Fatalf("execution failure became evidence: run=%+v err=%v", run, err)
	}
	spans := recorder.Snapshot()
	if spans[0].Name != "schedule_run" || spans[0].DurationNS <= 0 || spans[len(spans)-1].Name != "adapter_stop" {
		t.Fatalf("failure cleanup timing incomplete: %+v", spans)
	}
}
