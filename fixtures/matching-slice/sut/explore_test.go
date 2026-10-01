package matchingsut

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"slices"
	"testing"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/scenario"
	internalsut "github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/sut/gonative"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

const (
	matchingExploreCandidates   = 6
	matchingExploreRepeat       = 20
	matchingExploreCensusRepeat = 3
	matchingExploreFixedRepeat  = 5
	matchingExploreReplayRepeat = 20
)

func TestExploreConcurrentAssign(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), matchingReplayTestTimeout)
	defer cancel()

	runner := fixture.NewMySQLFixture()
	t.Cleanup(func() {
		if err := runner.Teardown(context.Background()); err != nil {
			t.Errorf("cleanup matching exploration fixture: %v", err)
		}
	})
	prepared, err := fixture.Prepare(fixture.FixtureSpec{
		Image:      "mysql:8.4",
		Migrations: filepath.Join("..", "db", "migration"),
		Seed:       filepath.Join("..", "db", "seed.sql"),
	})
	if err != nil {
		t.Fatalf("prepare matching exploration fixture: %v", err)
	}
	db, err := runner.Provision(ctx, prepared)
	if err != nil {
		t.Fatalf("provision matching exploration fixture: %v", err)
	}

	executor, err := newMatchingExploreOrchestrator(runner, db)
	if err != nil {
		t.Fatalf("create matching exploration orchestrator: %v", err)
	}
	evaluator, err := newMatchingInvariantOracleSet()
	if err != nil {
		t.Fatalf("create matching invariant Oracle set: %v", err)
	}

	vulnerableScenario := newMatchingExploreScenario(string(variantVulnerable))
	vulnerableSchedules := matchingExploreSchedules(t, vulnerableScenario)
	discovery := exploreMatchingVulnerable(
		t,
		ctx,
		executor,
		vulnerableScenario,
		evaluator,
	)
	replay := saveAndReplayMatchingDiscovery(
		t,
		ctx,
		executor,
		vulnerableScenario,
		evaluator,
		discovery.schedule,
		discovery.fingerprint,
	)
	censusViolations := censusMatchingVulnerable(
		t,
		ctx,
		executor,
		vulnerableScenario,
		evaluator,
		vulnerableSchedules,
	)

	fixedScenario := newMatchingExploreScenario(string(variantFixed))
	fixedSchedules := matchingExploreSchedules(t, fixedScenario)
	for index := range vulnerableSchedules {
		if fixedSchedules[index].ID != vulnerableSchedules[index].ID ||
			!slices.Equal(fixedSchedules[index].Steps, vulnerableSchedules[index].Steps) {
			t.Fatalf(
				"fixed matching candidate %d = %+v, want vulnerable candidate %+v",
				index+1,
				fixedSchedules[index],
				vulnerableSchedules[index],
			)
		}
	}
	fixed := exploreMatchingFixed(t, ctx, executor, fixedScenario, evaluator)

	t.Logf(
		"MATCHING_EXPLORE_RESULT variant=vulnerable candidates=6 repeats=20 "+
			"evaluated=%d distinct_schedules=1 distinct_indices=1 distinct_fingerprints=1 "+
			"violating_index=%d schedule=%s saved=reloaded replay_repeat=20 "+
			"violation_runs=%d fingerprint_match=true worker_errors=0 deadlocks=0 flaky=false",
		discovery.evaluated,
		discovery.index,
		discovery.schedule.ID,
		replay.violationRuns,
	)
	t.Logf(
		"MATCHING_EXPLORE_CENSUS variant=vulnerable candidates=6 repeats=3 "+
			"evaluated=18 violating_candidates=%d stable=true",
		censusViolations,
	)
	t.Logf(
		"MATCHING_EXPLORE_RESULT variant=fixed candidates=6 repeats=5 evaluated=%d "+
			"violating=none exhausted=true pending_resolved_runs=%d "+
			"terminal_skipped_runs=%d worker_errors=0 deadlocks=0",
		fixed.evaluated,
		fixed.pendingResolvedRuns,
		fixed.terminalSkippedRuns,
	)
}

type matchingDiscoveryEvidence struct {
	schedule    scenario.Schedule
	index       int
	fingerprint string
	evaluated   int
}

func exploreMatchingVulnerable(
	t *testing.T,
	ctx context.Context,
	executor *orchestrator.Orchestrator,
	value scenario.Scenario,
	evaluator oracle.Evaluator,
) matchingDiscoveryEvidence {
	t.Helper()

	scheduleIDs := make(map[string]struct{})
	indices := make(map[int]struct{})
	fingerprints := make(map[string]struct{})
	var discovered scenario.Schedule
	workerErrors := 0
	deadlocks := 0
	evaluated := 0
	baselineIndex := 0
	baselineScheduleID := ""
	baselineFingerprint := ""

	for repeat := 1; repeat <= matchingExploreRepeat; repeat++ {
		result, err := executor.Explore(ctx, value, scenario.Exhaustive{}, evaluator)
		if err != nil {
			logMatchingExploreDiagnostic(t, string(variantVulnerable), repeat, result)
			t.Fatalf("explore vulnerable matching repeat %d: %v", repeat, err)
		}
		if !result.CandidatesKnown || result.Candidates != matchingExploreCandidates ||
			result.Exhausted || result.ViolatingIndex < 1 ||
			result.ViolatingIndex > matchingExploreCandidates ||
			result.Evaluated != result.ViolatingIndex ||
			len(result.Summaries) != result.Evaluated {
			logMatchingExploreDiagnostic(t, string(variantVulnerable), repeat, result)
			t.Fatalf("vulnerable matching exploration repeat %d result = %+v", repeat, result)
		}
		if err := validateMatchingInvariantViolation(result.ViolatingRun); err != nil {
			logMatchingExploreDiagnostic(t, string(variantVulnerable), repeat, result)
			t.Fatalf("validate vulnerable matching repeat %d: %v", repeat, err)
		}
		if repeat == 1 {
			baselineIndex = result.ViolatingIndex
			baselineScheduleID = result.Violating.ID
			baselineFingerprint = result.ViolatingRun.Fingerprint
		} else if result.ViolatingIndex != baselineIndex ||
			result.Violating.ID != baselineScheduleID ||
			result.ViolatingRun.Fingerprint != baselineFingerprint {
			logMatchingExploreDiagnostic(t, string(variantVulnerable), repeat, result)
			t.Fatalf(
				"vulnerable matching exploration repeat %d discovered index=%d schedule=%s fingerprint=%s, want %d/%s/%s",
				repeat,
				result.ViolatingIndex,
				result.Violating.ID,
				result.ViolatingRun.Fingerprint,
				baselineIndex,
				baselineScheduleID,
				baselineFingerprint,
			)
		}

		runWorkerErrors, runDeadlocks := matchingTerminalFailures(result.ViolatingRun.Terminals)
		workerErrors += runWorkerErrors
		deadlocks += runDeadlocks
		scheduleIDs[result.Violating.ID] = struct{}{}
		indices[result.ViolatingIndex] = struct{}{}
		fingerprints[result.ViolatingRun.Fingerprint] = struct{}{}
		evaluated += result.Evaluated
		discovered = result.Violating.Clone()
	}

	if len(scheduleIDs) != 1 || len(indices) != 1 || len(fingerprints) != 1 {
		t.Fatalf(
			"vulnerable matching exploration is unstable: schedules=%v indices=%v fingerprints=%v",
			scheduleIDs,
			indices,
			fingerprints,
		)
	}
	if workerErrors != 0 || deadlocks != 0 {
		t.Fatalf(
			"vulnerable matching exploration terminal failures: worker_errors=%d deadlocks=%d",
			workerErrors,
			deadlocks,
		)
	}

	return matchingDiscoveryEvidence{
		schedule:    discovered,
		index:       onlyMatchingMapKey(indices),
		fingerprint: onlyMatchingMapKey(fingerprints),
		evaluated:   evaluated,
	}
}

type matchingReplayDiscoveryEvidence struct {
	violationRuns int
}

func saveAndReplayMatchingDiscovery(
	t *testing.T,
	ctx context.Context,
	executor *orchestrator.Orchestrator,
	value scenario.Scenario,
	evaluator oracle.Evaluator,
	discovered scenario.Schedule,
	discoveryFingerprint string,
) matchingReplayDiscoveryEvidence {
	t.Helper()

	path := filepath.Join(t.TempDir(), "discovered-schedule.json")
	if err := scenario.WriteScheduleFile(path, discovered); err != nil {
		t.Fatalf("save discovered matching schedule: %v", err)
	}
	reloaded, err := scenario.LoadScheduleFile(path)
	if err != nil {
		t.Fatalf("reload discovered matching schedule: %v", err)
	}
	if reloaded.ID != discovered.ID || !slices.Equal(reloaded.Steps, discovered.Steps) {
		t.Fatalf("reloaded matching schedule = %+v, want %+v", reloaded, discovered)
	}

	replay, err := executor.Replay(
		ctx,
		value,
		reloaded,
		matchingExploreReplayRepeat,
		evaluator,
	)
	if err != nil {
		failMatchingReplay(t, string(variantVulnerable), replay, "replay discovered matching schedule: %v", err)
	}
	if replay.Flaky || len(replay.Fingerprints) != 1 || len(replay.Runs) != matchingExploreReplayRepeat {
		failMatchingReplay(
			t,
			string(variantVulnerable),
			replay,
			"discovered matching replay is unstable: runs=%d fingerprints=%v flaky=%t",
			len(replay.Runs),
			replay.Fingerprints,
			replay.Flaky,
		)
	}
	if replay.Fingerprints[discoveryFingerprint] != matchingExploreReplayRepeat {
		failMatchingReplay(
			t,
			string(variantVulnerable),
			replay,
			"discovered matching replay fingerprints=%v, want discovery fingerprint %q in every run",
			replay.Fingerprints,
			discoveryFingerprint,
		)
	}

	violationRuns := 0
	workerErrors := 0
	deadlocks := 0
	for index, run := range replay.Runs {
		if err := validateMatchingInvariantViolation(run); err != nil {
			failMatchingRun(
				t,
				string(variantVulnerable),
				index+1,
				run,
				"validate discovered matching replay run %d: %v",
				index+1,
				err,
			)
		}
		violationRuns++
		runWorkerErrors, runDeadlocks := matchingTerminalFailures(run.Terminals)
		workerErrors += runWorkerErrors
		deadlocks += runDeadlocks
	}
	if workerErrors != 0 || deadlocks != 0 {
		failMatchingReplay(
			t,
			string(variantVulnerable),
			replay,
			"discovered matching replay terminal failures: worker_errors=%d deadlocks=%d",
			workerErrors,
			deadlocks,
		)
	}

	return matchingReplayDiscoveryEvidence{violationRuns: violationRuns}
}

func censusMatchingVulnerable(
	t *testing.T,
	ctx context.Context,
	executor *orchestrator.Orchestrator,
	value scenario.Scenario,
	evaluator oracle.Evaluator,
	schedules []scenario.Schedule,
) int {
	t.Helper()

	var baseline []int
	for repeat := 1; repeat <= matchingExploreCensusRepeat; repeat++ {
		violating := make([]int, 0, len(schedules))
		runs := make([]orchestrator.RunResult, 0, len(schedules))
		for index, schedule := range schedules {
			run, err := executor.Run(ctx, value, schedule, evaluator)
			if err != nil {
				logMatchingExploreRunDiagnostic(t, string(variantVulnerable), repeat, index+1, run, 0)
				t.Fatalf(
					"run vulnerable matching census repeat %d candidate %d/%d (%s): %v",
					repeat,
					index+1,
					len(schedules),
					schedule.ID,
					err,
				)
			}
			runs = append(runs, run)
			violations, err := matchingInvariantViolationCount(run)
			if err != nil {
				logMatchingExploreRunDiagnostic(t, string(variantVulnerable), repeat, index+1, run, 0)
				t.Fatalf("validate vulnerable matching census repeat %d candidate %d: %v", repeat, index+1, err)
			}
			workerErrors, deadlocks := matchingTerminalFailures(run.Terminals)
			if workerErrors != 0 || deadlocks != 0 {
				logMatchingExploreRunDiagnostic(t, string(variantVulnerable), repeat, index+1, run, violations)
				t.Fatalf(
					"vulnerable matching census repeat %d candidate %d terminal failures: worker_errors=%d deadlocks=%d",
					repeat,
					index+1,
					workerErrors,
					deadlocks,
				)
			}
			if violations > 0 {
				violating = append(violating, index+1)
			}
		}

		if repeat == 1 {
			baseline = append([]int(nil), violating...)
			continue
		}
		if !slices.Equal(violating, baseline) {
			for index, run := range runs {
				violations, _ := matchingInvariantViolationCount(run)
				logMatchingExploreRunDiagnostic(t, string(variantVulnerable), repeat, index+1, run, violations)
			}
			t.Fatalf(
				"vulnerable matching census repeat %d violating candidates=%v, baseline=%v",
				repeat,
				violating,
				baseline,
			)
		}
	}
	return len(baseline)
}

type matchingFixedExploreEvidence struct {
	evaluated           int
	pendingResolvedRuns int
	terminalSkippedRuns int
}

func exploreMatchingFixed(
	t *testing.T,
	ctx context.Context,
	executor *orchestrator.Orchestrator,
	value scenario.Scenario,
	evaluator oracle.Evaluator,
) matchingFixedExploreEvidence {
	t.Helper()

	metrics := &matchingExploreRunMetrics{}
	recordingEvaluator := oracle.EvaluatorFunc(func(
		evaluateCtx context.Context,
		db oracle.DB,
		run oracle.RunContext,
	) (oracle.Evaluation, error) {
		evaluation, err := evaluator.Evaluate(evaluateCtx, db, run)
		if err != nil {
			return oracle.Evaluation{}, err
		}
		metrics.observe(run)
		return evaluation, nil
	})

	evaluated := 0
	for repeat := 1; repeat <= matchingExploreFixedRepeat; repeat++ {
		result, err := executor.Explore(ctx, value, scenario.Exhaustive{}, recordingEvaluator)
		if err != nil {
			logMatchingExploreDiagnostic(t, string(variantFixed), repeat, result)
			t.Fatalf("explore fixed matching repeat %d: %v", repeat, err)
		}
		if !result.CandidatesKnown || result.Candidates != matchingExploreCandidates ||
			!result.Exhausted || result.ViolatingIndex != 0 ||
			result.Evaluated != matchingExploreCandidates ||
			len(result.Summaries) != matchingExploreCandidates {
			logMatchingExploreDiagnostic(t, string(variantFixed), repeat, result)
			t.Fatalf("fixed matching exploration repeat %d result = %+v", repeat, result)
		}
		for _, summary := range result.Summaries {
			if summary.Violations != 0 {
				logMatchingExploreDiagnostic(t, string(variantFixed), repeat, result)
				t.Fatalf("fixed matching repeat %d candidate %d violations=%d, want 0", repeat, summary.Index, summary.Violations)
			}
		}
		evaluated += result.Evaluated
	}

	if evaluated != matchingExploreCandidates*matchingExploreFixedRepeat ||
		metrics.runs != evaluated {
		t.Fatalf("fixed matching evaluated=%d recorded_runs=%d, want %d", evaluated, metrics.runs, matchingExploreCandidates*matchingExploreFixedRepeat)
	}
	if metrics.workerErrors != 0 || metrics.deadlocks != 0 {
		t.Fatalf(
			"fixed matching terminal failures: worker_errors=%d deadlocks=%d",
			metrics.workerErrors,
			metrics.deadlocks,
		)
	}

	return matchingFixedExploreEvidence{
		evaluated:           evaluated,
		pendingResolvedRuns: metrics.pendingResolvedRuns,
		terminalSkippedRuns: metrics.terminalSkippedRuns,
	}
}

type matchingExploreRunMetrics struct {
	runs                int
	pendingResolvedRuns int
	terminalSkippedRuns int
	workerErrors        int
	deadlocks           int
}

func (metrics *matchingExploreRunMetrics) observe(run oracle.RunContext) {
	metrics.runs++
	workerErrors, deadlocks := matchingTerminalFailures(run.Terminals)
	metrics.workerErrors += workerErrors
	metrics.deadlocks += deadlocks

	timedOut := make(map[int]bool)
	pendingResolved := false
	terminalSkipped := false
	for _, event := range run.Trace {
		switch event.Kind {
		case orchestrator.EventPointTimeout:
			timedOut[event.Step] = true
		case orchestrator.EventPointReleased:
			if timedOut[event.Step] {
				pendingResolved = true
			}
		case orchestrator.EventStepTerminalSkipped:
			terminalSkipped = true
		}
	}
	if pendingResolved {
		metrics.pendingResolvedRuns++
	}
	if terminalSkipped {
		metrics.terminalSkippedRuns++
	}
}

func matchingExploreSchedules(t *testing.T, value scenario.Scenario) []scenario.Schedule {
	t.Helper()

	plan, err := (scenario.Exhaustive{}).Schedules(value)
	if err != nil {
		t.Fatalf("build matching exhaustive plan: %v", err)
	}
	if !plan.TotalKnown || plan.Total != matchingExploreCandidates {
		t.Fatalf("matching candidate plan total=%d known=%t, want 6/true", plan.Total, plan.TotalKnown)
	}
	schedules := make([]scenario.Schedule, 0, plan.Total)
	for schedule := range plan.Seq {
		if err := scenario.Validate(value, schedule); err != nil {
			t.Fatalf("validate matching candidate %d: %v", len(schedules)+1, err)
		}
		schedules = append(schedules, schedule)
	}
	if len(schedules) != matchingExploreCandidates {
		t.Fatalf("matching candidates=%d, want %d", len(schedules), matchingExploreCandidates)
	}
	return schedules
}

func newMatchingExploreOrchestrator(
	runner fixture.Fixture,
	db *fixture.DB,
) (*orchestrator.Orchestrator, error) {
	return orchestrator.New(orchestrator.Config{
		Fixture:    runner,
		DB:         db,
		NewRuntime: syncpoint.New,
		NewAdapter: func(client syncpoint.Client) (internalsut.Adapter, error) {
			return gonative.New(NewRegistry(client)), nil
		},
		BlockInferenceTimeout: replayLockInferenceTimeout,
		StepTimeout:           replayStepTimeout,
		RunTimeout:            replayRunTimeout,
		StopTimeout:           replayStopTimeout,
	})
}

func newMatchingExploreScenario(selected string) scenario.Scenario {
	return scenario.Scenario{
		Name: "matching-concurrent-assign",
		Workers: []scenario.Worker{
			{ID: "w1", Command: CommandAssign},
			{ID: "w2", Command: CommandAssign},
		},
		SyncPoints: []string{AfterReadRequest, BeforeInsertAssignment},
		SUTConfig: internalsut.SUTConfig{
			Variant: selected,
			Params:  map[string]string{"request_id": fmt.Sprint(seededRequestID)},
		},
	}
}

func validateMatchingInvariantViolation(run orchestrator.RunResult) error {
	violations, err := matchingInvariantViolationCount(run)
	if err != nil {
		return err
	}
	if violations != 1 {
		return fmt.Errorf("matching invariant violations=%d, want 1", violations)
	}

	violation := run.Evaluation.Results[0].Violations[0]
	if violation.OracleID != matchingOracleID || violation.Kind != oracle.KindAssertion ||
		len(violation.Rows) != 1 {
		return fmt.Errorf("matching invariant violation=%+v, want one assertion evidence row", violation)
	}
	evidenceJSON, err := json.Marshal(violation.Rows[0])
	if err != nil || string(evidenceJSON) != matchingViolationEvidence {
		return fmt.Errorf("matching invariant evidence=%s (error=%v), want %s", evidenceJSON, err, matchingViolationEvidence)
	}
	return nil
}

func matchingInvariantViolationCount(run orchestrator.RunResult) (int, error) {
	if len(run.Evaluation.Results) != 1 {
		return 0, fmt.Errorf("matching invariant results=%d, want 1", len(run.Evaluation.Results))
	}
	result := run.Evaluation.Results[0]
	if result.OracleID != matchingOracleID {
		return 0, fmt.Errorf("matching invariant Oracle ID=%q, want %q", result.OracleID, matchingOracleID)
	}
	return len(result.Violations), nil
}

func matchingTerminalFailures(terminals []orchestrator.WorkerTerminal) (workerErrors int, deadlocks int) {
	for _, terminal := range terminals {
		switch terminal.FailureClass {
		case orchestrator.WorkerFailureNone:
		case orchestrator.WorkerFailureMySQLDeadlock:
			deadlocks++
		default:
			workerErrors++
		}
	}
	return workerErrors, deadlocks
}

func onlyMatchingMapKey[K comparable](values map[K]struct{}) K {
	for value := range values {
		return value
	}
	var zero K
	return zero
}

func logMatchingExploreDiagnostic(
	t *testing.T,
	selected string,
	repeat int,
	result orchestrator.ExploreResult,
) {
	t.Helper()

	for _, summary := range result.Summaries {
		run := orchestrator.RunResult{
			ScheduleID:  summary.ScheduleID,
			Fingerprint: summary.Fingerprint,
		}
		if summary.Index == result.ViolatingIndex {
			run = result.ViolatingRun
		}
		logMatchingExploreRunDiagnostic(
			t,
			selected,
			repeat,
			summary.Index,
			run,
			summary.Violations,
		)
	}
	if result.Evaluated > len(result.Summaries) {
		t.Logf(
			"EXPLORE_DIAGNOSTIC variant=%s repeat=%d candidate=%d schedule=unknown "+
				"violations=unknown fingerprint=unknown terminal_class=unavailable trace=[]",
			selected,
			repeat,
			result.Evaluated,
		)
	}
}

func logMatchingExploreRunDiagnostic(
	t *testing.T,
	selected string,
	repeat int,
	candidate int,
	run orchestrator.RunResult,
	violations int,
) {
	t.Helper()

	terminalClasses := make([]orchestrator.WorkerFailureClass, 0, len(run.Terminals))
	for _, terminal := range run.Terminals {
		terminalClasses = append(terminalClasses, terminal.FailureClass)
	}
	terminalJSON, terminalErr := json.Marshal(terminalClasses)
	traceJSON, traceErr := json.Marshal(run.Trace)
	t.Logf(
		"EXPLORE_DIAGNOSTIC variant=%s repeat=%d candidate=%d schedule=%s "+
			"violations=%d fingerprint=%s evaluation_fingerprint=%s "+
			"terminal_class=%s trace=%s marshal_errors=%v/%v",
		selected,
		repeat,
		candidate,
		run.ScheduleID,
		violations,
		run.Fingerprint,
		run.Evaluation.Fingerprint,
		terminalJSON,
		traceJSON,
		terminalErr,
		traceErr,
	)
}
