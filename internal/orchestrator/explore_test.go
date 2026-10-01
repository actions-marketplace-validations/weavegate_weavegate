package orchestrator

import (
	"context"
	"errors"
	"iter"
	"reflect"
	"strings"
	"testing"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
	"github.com/weavegate/weavegate/internal/trace"
)

func TestExploreStopsAtFirstViolation(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	executor, cleanup := newExploreTestOrchestrator(t, fixtureRunner)
	evaluations := 0

	result, err := executor.Explore(
		context.Background(),
		matchingScenario(),
		scenario.Exhaustive{},
		oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluations++
			return exploreEvaluation(t, evaluations == 2), nil
		}),
	)
	if err != nil {
		t.Fatalf("explore until first violation: %v", err)
	}
	if !result.CandidatesKnown || result.Candidates != 6 {
		t.Fatalf("candidate total = %d/%t, want 6/known", result.Candidates, result.CandidatesKnown)
	}
	if result.Evaluated != 2 || result.ViolatingIndex != 2 || result.Exhausted {
		t.Fatalf(
			"exploration progress evaluated/index/exhausted = %d/%d/%t, want 2/2/false",
			result.Evaluated,
			result.ViolatingIndex,
			result.Exhausted,
		)
	}
	if evaluations != 2 || fixtureRunner.resetCalls != 2 {
		t.Fatalf("evaluations/resets = %d/%d, want 2/2", evaluations, fixtureRunner.resetCalls)
	}
	if len(result.Summaries) != 2 {
		t.Fatalf("candidate summaries = %d, want 2", len(result.Summaries))
	}
	if result.Summaries[0].Violations != 0 || result.Summaries[1].Violations != 1 {
		t.Fatalf("candidate violations = %d/%d, want 0/1", result.Summaries[0].Violations, result.Summaries[1].Violations)
	}
	if result.Violating.ID == "" || result.Violating.ID != result.Summaries[1].ScheduleID {
		t.Fatalf("violating schedule = %q, want summary ID %q", result.Violating.ID, result.Summaries[1].ScheduleID)
	}
	if result.ViolatingRun.ScheduleID != result.Violating.ID || result.ViolatingRun.Fingerprint == "" {
		t.Fatalf("violating run identity = %q/%q", result.ViolatingRun.ScheduleID, result.ViolatingRun.Fingerprint)
	}
	assertExploreCleanup(t, cleanup, 2)

	t.Log(
		"EXPLORE_ENGINE_RESULT candidates=6 total_known=true evaluated=2 violating_index=2 " +
			"stop_at_first=true resets=2 cleanup=ok run_error=propagated partial_summaries=2 " +
			"nil_evaluator=error zero_oracles=error empty_sequence=error",
	)
}

func TestExploreExhaustsAllCandidatesWithoutViolation(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	executor, cleanup := newExploreTestOrchestrator(t, fixtureRunner)

	result, err := executor.Explore(
		context.Background(),
		matchingScenario(),
		scenario.Exhaustive{},
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("exhaust non-violating candidates: %v", err)
	}
	if !result.CandidatesKnown || result.Candidates != 6 || result.Evaluated != 6 {
		t.Fatalf(
			"candidate count known/total/evaluated = %t/%d/%d, want true/6/6",
			result.CandidatesKnown,
			result.Candidates,
			result.Evaluated,
		)
	}
	if !result.Exhausted || result.ViolatingIndex != 0 || result.Violating.ID != "" {
		t.Fatalf(
			"exhausted/violating index/schedule = %t/%d/%q, want true/0/empty",
			result.Exhausted,
			result.ViolatingIndex,
			result.Violating.ID,
		)
	}
	if len(result.Summaries) != 6 || fixtureRunner.resetCalls != 6 {
		t.Fatalf("summaries/resets = %d/%d, want 6/6", len(result.Summaries), fixtureRunner.resetCalls)
	}
	for index, summary := range result.Summaries {
		if summary.Index != index+1 || summary.Violations != 0 || summary.Fingerprint == "" {
			t.Errorf("candidate summary %d = %#v", index+1, summary)
		}
	}
	assertExploreCleanup(t, cleanup, 6)

	t.Log("EXPLORE_EXHAUSTED_RESULT candidates=6 evaluated=6 violating=none exhausted=true cleanup=ok")
}

func TestExploreSupportsStrategyWithUnknownTotal(t *testing.T) {
	schedules := exhaustiveSchedules(t)
	fixtureRunner := &recordingFixture{}
	executor, cleanup := newExploreTestOrchestrator(t, fixtureRunner)
	evaluations := 0
	strategy := staticStrategy{plan: scenario.SchedulePlan{
		Total:      99,
		TotalKnown: false,
		Seq:        scheduleSequence(schedules[:3]),
	}}

	result, err := executor.Explore(
		context.Background(),
		matchingScenario(),
		strategy,
		oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluations++
			return exploreEvaluation(t, evaluations == 3), nil
		}),
	)
	if err != nil {
		t.Fatalf("explore unknown-total strategy: %v", err)
	}
	if result.CandidatesKnown || result.Candidates != 0 {
		t.Fatalf("unknown candidate total = %d/%t, want 0/false", result.Candidates, result.CandidatesKnown)
	}
	if result.Evaluated != 3 || result.ViolatingIndex != 3 || len(result.Summaries) != 3 {
		t.Fatalf(
			"unknown-total progress evaluated/index/summaries = %d/%d/%d, want 3/3/3",
			result.Evaluated,
			result.ViolatingIndex,
			len(result.Summaries),
		)
	}
	assertExploreCleanup(t, cleanup, 3)

	t.Log(
		"EXPLORE_UNKNOWN_TOTAL_RESULT total_known=false candidates=0 evaluated=3 " +
			"violating_index=3 unknown_is_not_zero=true",
	)
}

func TestExploreReturnsPartialProgressForRunAndOracleErrors(t *testing.T) {
	runRoot := errors.New("reset failed")
	failingFixture := &failOnResetFixture{failAt: 3, err: runRoot}
	runExecutor, runCleanup := newExploreTestOrchestrator(t, failingFixture)
	runResult, err := runExecutor.Explore(
		context.Background(),
		matchingScenario(),
		scenario.Exhaustive{},
		stableEvaluator,
	)
	if !errors.Is(err, runRoot) {
		t.Fatalf("run error = %v, want errors.Is(_, runRoot)", err)
	}
	if !strings.Contains(err.Error(), "candidate 3/6") {
		t.Fatalf("run error lacks candidate position: %v", err)
	}
	var fixtureErr *FixtureError
	if !errors.As(err, &fixtureErr) {
		t.Fatalf("run error = %v, want errors.As(_, *FixtureError) so callers can tell this apart from a scenario/adapter/oracle failure", err)
	}
	if runResult.Evaluated != 3 || len(runResult.Summaries) != 2 || failingFixture.resetCalls != 3 {
		t.Fatalf(
			"run-error progress evaluated/summaries/resets = %d/%d/%d, want 3/2/3",
			runResult.Evaluated,
			len(runResult.Summaries),
			failingFixture.resetCalls,
		)
	}
	assertExploreCleanup(t, runCleanup, 2)

	oracleRoot := errors.New("oracle unavailable")
	oracleFixture := &recordingFixture{}
	oracleExecutor, oracleCleanup := newExploreTestOrchestrator(t, oracleFixture)
	evaluations := 0
	oracleResult, err := oracleExecutor.Explore(
		context.Background(),
		matchingScenario(),
		scenario.Exhaustive{},
		oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluations++
			if evaluations == 3 {
				return oracle.Evaluation{}, oracleRoot
			}
			return exploreEvaluation(t, false), nil
		}),
	)
	if !errors.Is(err, oracleRoot) {
		t.Fatalf("Oracle error = %v, want errors.Is(_, oracleRoot)", err)
	}
	var oracleFixtureErr *FixtureError
	if errors.As(err, &oracleFixtureErr) {
		t.Fatalf("Oracle error = %v, want errors.As(_, *FixtureError) to fail: this is not a fixture failure", err)
	}
	if oracleResult.Evaluated != 3 || len(oracleResult.Summaries) != 2 || oracleFixture.resetCalls != 3 {
		t.Fatalf(
			"Oracle-error progress evaluated/summaries/resets = %d/%d/%d, want 3/2/3",
			oracleResult.Evaluated,
			len(oracleResult.Summaries),
			oracleFixture.resetCalls,
		)
	}
	assertExploreCleanup(t, oracleCleanup, 3)
}

func TestExploreRejectsInvalidInputsAndEmptyEvaluations(t *testing.T) {
	t.Run("nil context", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		var nilContext context.Context
		_, err := executor.Explore(nilContext, matchingScenario(), scenario.Exhaustive{}, stableEvaluator)
		if err == nil || !strings.Contains(err.Error(), "context is required") {
			t.Fatalf("nil context error = %v", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("nil context resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("nil strategy", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		var strategy *staticStrategy
		_, err := executor.Explore(context.Background(), matchingScenario(), strategy, stableEvaluator)
		if err == nil || !strings.Contains(err.Error(), "strategy is required") {
			t.Fatalf("nil strategy error = %v", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("nil strategy resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("nil evaluator", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		var evaluator *oracle.Set
		_, err := executor.Explore(context.Background(), matchingScenario(), scenario.Exhaustive{}, evaluator)
		if err == nil || !strings.Contains(err.Error(), "Oracle evaluator is required") {
			t.Fatalf("nil evaluator error = %v", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("nil evaluator resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("canceled context", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		_, err := executor.Explore(ctx, matchingScenario(), scenario.Exhaustive{}, stableEvaluator)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled context error = %v, want context.Canceled", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("canceled context resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("strategy error", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		rootErr := errors.New("strategy unavailable")
		_, err := executor.Explore(
			context.Background(),
			matchingScenario(),
			staticStrategy{err: rootErr},
			stableEvaluator,
		)
		if !errors.Is(err, rootErr) {
			t.Fatalf("strategy error = %v, want errors.Is(_, rootErr)", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("strategy error resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("nil sequence", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		strategy := staticStrategy{plan: scenario.SchedulePlan{Total: 1, TotalKnown: true}}
		_, err := executor.Explore(context.Background(), matchingScenario(), strategy, stableEvaluator)
		if err == nil || !strings.Contains(err.Error(), "candidate sequence is required") {
			t.Fatalf("nil sequence error = %v", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("nil sequence resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("empty sequence", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, _ := newExploreTestOrchestrator(t, fixtureRunner)
		strategy := staticStrategy{plan: scenario.SchedulePlan{
			Total:      1,
			TotalKnown: true,
			Seq: func(func(scenario.Schedule) bool) {
			},
		}}
		_, err := executor.Explore(context.Background(), matchingScenario(), strategy, stableEvaluator)
		if err == nil || !strings.Contains(err.Error(), "yielded no candidates") {
			t.Fatalf("empty sequence error = %v", err)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("empty sequence resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("zero Oracle results", func(t *testing.T) {
		fixtureRunner := &recordingFixture{}
		executor, cleanup := newExploreTestOrchestrator(t, fixtureRunner)
		result, err := executor.Explore(
			context.Background(),
			matchingScenario(),
			staticStrategy{plan: scenario.SchedulePlan{
				Total:      1,
				TotalKnown: true,
				Seq:        scheduleSequence(exhaustiveSchedules(t)[:1]),
			}},
			oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				return oracle.Evaluation{Fingerprint: strings.Repeat("0", 64)}, nil
			}),
		)
		if err == nil || !strings.Contains(err.Error(), "invalid Oracle evaluation") {
			t.Fatalf("zero-result evaluation error = %v", err)
		}
		if result.Exhausted || result.Evaluated != 1 || len(result.Summaries) != 0 {
			t.Fatalf("zero-result exploration reported verdict: %#v", result)
		}
		if fixtureRunner.resetCalls != 1 {
			t.Fatalf("zero-result resets = %d, want 1", fixtureRunner.resetCalls)
		}
		assertExploreCleanup(t, cleanup, 1)
	})
}

func TestExploreResultCloneIsDeep(t *testing.T) {
	schedule := matchingSchedule(t)
	evaluation := exploreEvaluation(t, true)
	original := ExploreResult{
		Summaries: []CandidateSummary{{Index: 1, ScheduleID: schedule.ID}},
		Violating: schedule,
		ViolatingRun: RunResult{
			Workers:    []sut.WorkerResult{{WorkerID: "w1"}},
			Terminals:  trace.Terminals{{Worker: "w1", State: TerminalStateDone}},
			Trace:      trace.Trace{{Seq: 1, Kind: EventWorkerDone, Worker: "w1"}},
			Evaluation: evaluation,
		},
	}
	cloned := cloneExploreResult(original)

	cloned.Summaries[0].ScheduleID = "changed"
	cloned.Violating.Steps[0].Worker = "changed"
	cloned.ViolatingRun.Workers[0].WorkerID = "changed"
	cloned.ViolatingRun.Terminals[0].Worker = "changed"
	cloned.ViolatingRun.Trace[0].Worker = "changed"
	cloned.ViolatingRun.Evaluation.Results[0].Violations[0].Rows[0]["candidate"] = int64(99)

	if original.Summaries[0].ScheduleID != schedule.ID || original.Violating.Steps[0].Worker != "w1" {
		t.Fatal("exploration clone shares summary or schedule storage")
	}
	if original.ViolatingRun.Workers[0].WorkerID != "w1" ||
		original.ViolatingRun.Terminals[0].Worker != "w1" ||
		original.ViolatingRun.Trace[0].Worker != "w1" {
		t.Fatal("exploration clone shares run evidence storage")
	}
	if got := original.ViolatingRun.Evaluation.Results[0].Violations[0].Rows[0]["candidate"]; got != int64(1) {
		t.Fatalf("exploration clone shares Oracle evidence row: %v", got)
	}
	if !reflect.DeepEqual(cloneExploreResult(ExploreResult{}), ExploreResult{}) {
		t.Fatal("cloning zero exploration result changed its zero value")
	}
}

func exploreEvaluation(t *testing.T, violating bool) oracle.Evaluation {
	t.Helper()
	result := oracle.OracleResult{OracleID: "explore-check"}
	if violating {
		result.Violations = []oracle.Violation{{
			OracleID: "explore-check",
			Kind:     oracle.KindAssertion,
			Rows:     []oracle.Row{{"candidate": int64(1)}},
		}}
	}
	evaluation, err := oracle.NewEvaluation(result)
	if err != nil {
		t.Fatalf("create explore evaluation: %v", err)
	}
	return evaluation
}

func exhaustiveSchedules(t *testing.T) []scenario.Schedule {
	t.Helper()
	plan, err := (scenario.Exhaustive{}).Schedules(matchingScenario())
	if err != nil {
		t.Fatalf("build exhaustive schedule plan: %v", err)
	}
	var schedules []scenario.Schedule
	for schedule := range plan.Seq {
		schedules = append(schedules, schedule)
	}
	return schedules
}

func scheduleSequence(schedules []scenario.Schedule) iter.Seq[scenario.Schedule] {
	return func(yield func(scenario.Schedule) bool) {
		for _, schedule := range schedules {
			if !yield(schedule.Clone()) {
				return
			}
		}
	}
}

type staticStrategy struct {
	plan scenario.SchedulePlan
	err  error
}

func (strategy staticStrategy) Schedules(scenario.Scenario) (scenario.SchedulePlan, error) {
	return strategy.plan, strategy.err
}

type failOnResetFixture struct {
	recordingFixture
	failAt int
	err    error
}

func (fixture *failOnResetFixture) Reset(context.Context) error {
	fixture.resetCalls++
	if fixture.resetCalls == fixture.failAt {
		return fixture.err
	}
	return nil
}

type exploreCleanupTracker struct {
	runtimes []*runtimeProbe
	adapters []*eagerAdapter
}

func newExploreTestOrchestrator(
	t *testing.T,
	fixtureRunner fixture.Fixture,
) (*Orchestrator, *exploreCleanupTracker) {
	t.Helper()
	cleanup := &exploreCleanupTracker{}
	return newTestOrchestrator(t, Config{
		Fixture: fixtureRunner,
		DB:      &fixture.DB{},
		NewRuntime: func() syncpoint.Runtime {
			runtime := newRuntimeProbe()
			cleanup.runtimes = append(cleanup.runtimes, runtime)
			return runtime
		},
		NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
			adapter := newEagerAdapter(client)
			cleanup.adapters = append(cleanup.adapters, adapter)
			return adapter, nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	}), cleanup
}

func assertExploreCleanup(t *testing.T, cleanup *exploreCleanupTracker, want int) {
	t.Helper()
	if len(cleanup.runtimes) != want || len(cleanup.adapters) != want {
		t.Fatalf(
			"created runtimes/adapters = %d/%d, want %d/%d",
			len(cleanup.runtimes),
			len(cleanup.adapters),
			want,
			want,
		)
	}
	for index := 0; index < want; index++ {
		if cleanup.runtimes[index].closeCalls.Load() != 1 ||
			cleanup.adapters[index].stopCalls.Load() != 1 ||
			cleanup.adapters[index].active.Load() != 0 {
			t.Errorf(
				"candidate %d cleanup close/stop/active = %d/%d/%d, want 1/1/0",
				index+1,
				cleanup.runtimes[index].closeCalls.Load(),
				cleanup.adapters[index].stopCalls.Load(),
				cleanup.adapters[index].active.Load(),
			)
		}
	}
}
