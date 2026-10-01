package orchestrator

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

func TestReplayStableFingerprint(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	orchestrator := newReplayTestOrchestrator(t, fixtureRunner, nil)
	evaluations := 0
	terminalBeforeEvaluation := true
	evaluator := oracle.EvaluatorFunc(func(
		_ context.Context,
		_ oracle.DB,
		run oracle.RunContext,
	) (oracle.Evaluation, error) {
		evaluations++
		terminalBeforeEvaluation = terminalBeforeEvaluation &&
			len(run.Terminals) == 2 && len(run.Trace) > 0 &&
			run.Trace[len(run.Trace)-1].Kind == EventScheduleComplete
		return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	})

	result, err := orchestrator.Replay(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		20,
		evaluator,
	)
	if err != nil {
		t.Fatalf("replay stable schedule: %v", err)
	}
	if result.ScheduleID != "sch_ba00582f9632" || result.Repeat != 20 {
		t.Fatalf("replay identity = %q/%d, want sch_ba00582f9632/20", result.ScheduleID, result.Repeat)
	}
	if len(result.Runs) != 20 {
		t.Fatalf("replay runs = %d, want 20", len(result.Runs))
	}
	if len(result.Fingerprints) != 1 {
		t.Fatalf("unique fingerprints = %d, want 1", len(result.Fingerprints))
	}
	if result.Flaky {
		t.Fatal("stable replay flaky = true, want false")
	}
	if len(result.MismatchRuns) != 0 {
		t.Fatalf("stable replay mismatch runs = %v, want none", result.MismatchRuns)
	}
	if fixtureRunner.resetCalls != 20 {
		t.Fatalf("fixture resets = %d, want 20", fixtureRunner.resetCalls)
	}
	for index, run := range result.Runs {
		if run.Fingerprint == "" {
			t.Fatalf("run %d fingerprint is empty", index+1)
		}
		if len(run.Evaluation.Results) != 1 || run.Evaluation.Results[0].OracleID != "stable-pass" {
			t.Fatalf("run %d evaluation = %#v, want one stable-pass result", index+1, run.Evaluation)
		}
	}
	if evaluations != 20 || !terminalBeforeEvaluation {
		t.Fatalf("Oracle evaluation count/order = %d/%t, want 20/true", evaluations, terminalBeforeEvaluation)
	}

	t.Log(
		"ORCHESTRATOR_REPLAY_CORE_RESULT schedule=sch_ba00582f9632 " +
			"repeat=20 fingerprints=1 resets=20 flaky=false",
	)
	t.Log(
		"ORCHESTRATOR_ORACLE_RESULT schedule=sch_ba00582f9632 evaluations=20 " +
			"oracles_per_evaluation=1 terminal_before_evaluation=true " +
			"fingerprints=1 cleanup=ok flaky=false",
	)
}

func TestReplayReportsLogicalMismatch(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	orchestrator := newReplayTestOrchestrator(t, fixtureRunner, nil)
	evaluatorCalls := 0

	result, err := orchestrator.Replay(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		10,
		oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluatorCalls++
			if evaluatorCalls == 7 {
				return oracle.NewEvaluation(oracle.OracleResult{
					OracleID: "stable-pass",
					Violations: []oracle.Violation{
						{OracleID: "stable-pass", Kind: oracle.KindAssertion, Rows: []oracle.Row{{"different": true}}},
					},
				})
			}
			return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
		}),
	)
	if err != nil {
		t.Fatalf("replay mismatched schedule: %v", err)
	}
	if !result.Flaky {
		t.Fatal("mismatched replay flaky = false, want true")
	}
	if len(result.Fingerprints) != 2 {
		t.Fatalf("unique fingerprints = %d, want 2", len(result.Fingerprints))
	}
	if !reflect.DeepEqual(result.MismatchRuns, []int{7}) {
		t.Fatalf("mismatch runs = %v, want [7]", result.MismatchRuns)
	}
	if fixtureRunner.resetCalls != 10 {
		t.Fatalf("fixture resets = %d, want 10", fixtureRunner.resetCalls)
	}
}

func TestReplayKeepsInfrastructureErrorDistinct(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	orchestrator := newReplayTestOrchestrator(t, fixtureRunner, nil)
	evaluatorErr := errors.New("evaluator unavailable")
	evaluatorCalls := 0

	result, err := orchestrator.Replay(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		20,
		oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluatorCalls++
			if evaluatorCalls == 3 {
				return oracle.Evaluation{}, evaluatorErr
			}
			return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
		}),
	)
	if !errors.Is(err, evaluatorErr) {
		t.Fatalf("replay error = %v, want errors.Is(_, evaluatorErr)", err)
	}
	if result.Flaky {
		t.Fatal("infrastructure error reported flaky = true, want false")
	}
	if len(result.Runs) != 2 {
		t.Fatalf("successful runs before error = %d, want 2", len(result.Runs))
	}
	if fixtureRunner.resetCalls != 3 {
		t.Fatalf("fixture resets = %d, want 3", fixtureRunner.resetCalls)
	}

	invalidFixture := &recordingFixture{}
	invalid := newReplayTestOrchestrator(t, invalidFixture, nil)
	invalidResult, invalidErr := invalid.Replay(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		0,
		stableEvaluator,
	)
	if invalidErr == nil {
		t.Fatal("repeat=0 error = nil, want configuration error")
	}
	if invalidResult.Flaky {
		t.Fatal("repeat=0 flaky = true, want false")
	}
	if invalidFixture.resetCalls != 0 {
		t.Fatalf("repeat=0 fixture resets = %d, want 0", invalidFixture.resetCalls)
	}
}

func TestReplayFingerprintExcludesNondeterministicValues(t *testing.T) {
	evaluation, err := oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	if err != nil {
		t.Fatalf("create evaluation: %v", err)
	}
	first := RunResult{
		Evaluation: evaluation,
		Workers: []sut.WorkerResult{
			{WorkerID: "w1", Err: errors.New("first raw error"), Duration: time.Second},
		},
		Terminals: []WorkerTerminal{
			{Worker: "w1", State: TerminalStateFailed, FailureClass: WorkerFailureError},
		},
		Trace: []Event{
			{
				Seq:          1,
				Kind:         EventWorkerFailed,
				Step:         1,
				Worker:       "w1",
				Status:       ControlStatusNone,
				FailureClass: WorkerFailureError,
			},
		},
		Elapsed: time.Second,
	}
	second := first
	second.Workers = append([]sut.WorkerResult(nil), first.Workers...)
	second.Terminals = first.Terminals.Clone()
	second.Trace = first.Trace.Clone()
	second.Workers[0].Err = errors.New("different raw error")
	second.Workers[0].Duration = 99 * time.Second
	second.Elapsed = 100 * time.Second

	firstFingerprint, err := normalizedFingerprint(first)
	if err != nil {
		t.Fatalf("fingerprint first run: %v", err)
	}
	secondFingerprint, err := normalizedFingerprint(second)
	if err != nil {
		t.Fatalf("fingerprint second run: %v", err)
	}
	if firstFingerprint != secondFingerprint {
		t.Fatalf("nondeterministic values changed fingerprint: %q != %q", firstFingerprint, secondFingerprint)
	}
	// A-22's one-time payload switch is complete. Keep the canonical struct shape
	// explicit here; do not replace this calculation with the observed hash.
	canonical := fmt.Sprintf(
		`{"oracle":%q,"terminals":[{"worker":"w1","state":"failed","failure_class":"worker_error"}],"trace":[{"seq":1,"kind":"worker_failed","step":1,"worker":"w1","point":"","status":"none","failure_class":"worker_error"}]}`,
		evaluation.Fingerprint,
	)
	wantFingerprint := fmt.Sprintf("%x", sha256.Sum256([]byte(canonical)))
	if firstFingerprint != wantFingerprint {
		t.Fatalf("normalized fingerprint = %q, want pre-extraction hash %q", firstFingerprint, wantFingerprint)
	}

	second.Terminals[0].FailureClass = WorkerFailureMySQLDeadlock
	changedFingerprint, err := normalizedFingerprint(second)
	if err != nil {
		t.Fatalf("fingerprint changed terminal: %v", err)
	}
	if changedFingerprint == firstFingerprint {
		t.Fatal("failure class did not change fingerprint")
	}
}

func TestReplayFingerprintNormalizesEmptyEvidence(t *testing.T) {
	evaluation, err := oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	if err != nil {
		t.Fatalf("create empty-evidence evaluation: %v", err)
	}
	fingerprint, err := normalizedFingerprint(RunResult{Evaluation: evaluation})
	if err != nil {
		t.Fatalf("fingerprint empty evidence: %v", err)
	}

	canonical := fmt.Sprintf(`{"oracle":%q,"terminals":[],"trace":[]}`, evaluation.Fingerprint)
	want := fmt.Sprintf("%x", sha256.Sum256([]byte(canonical)))
	if fingerprint != want {
		t.Fatalf("empty evidence fingerprint = %q, want canonical [] payload hash %q", fingerprint, want)
	}
}

func newReplayTestOrchestrator(
	t *testing.T,
	fixtureRunner *recordingFixture,
	onEvent EventObserver,
) *Orchestrator {
	t.Helper()

	return newTestOrchestrator(t, Config{
		Fixture:    fixtureRunner,
		DB:         &fixture.DB{},
		NewRuntime: syncpoint.New,
		NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
			return newEagerAdapter(client), nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
		OnEvent:               onEvent,
	})
}

type eagerAdapter struct {
	faults sut.FaultLatch
	client syncpoint.Client

	mu     sync.Mutex
	ctx    context.Context
	cancel context.CancelFunc
	wait   sync.WaitGroup
	errors map[string]error

	active    atomic.Int32
	stopCalls atomic.Int32
}

func newEagerAdapter(client syncpoint.Client) *eagerAdapter {
	return &eagerAdapter{client: client, errors: make(map[string]error)}
}

func (a *eagerAdapter) Start(
	ctx context.Context,
	_ sut.SUTConfig,
	_ *fixture.DB,
) (sut.Handle, error) {
	a.mu.Lock()
	a.ctx, a.cancel = context.WithCancel(ctx)
	a.mu.Unlock()
	return a, nil
}

func (a *eagerAdapter) Invoke(
	_ context.Context,
	workerID string,
	_ string,
) (<-chan sut.InvocationOutcome, error) {
	a.mu.Lock()
	ctx := a.ctx
	workerErr := a.errors[workerID]
	a.mu.Unlock()

	results := make(chan sut.InvocationOutcome, 1)
	a.wait.Add(1)
	a.active.Add(1)
	go func() {
		defer a.wait.Done()
		defer a.active.Add(-1)
		defer close(results)

		err := a.client.Arrive(ctx, workerID, "after_read_request")
		if err == nil {
			err = a.client.Arrive(ctx, workerID, "before_insert_assignment")
		}
		if err == nil {
			err = workerErr
		}
		results <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: workerID, Err: err}}
	}()
	return results, nil
}

func (a *eagerAdapter) Stop(ctx context.Context) error {
	a.stopCalls.Add(1)
	a.mu.Lock()
	cancel := a.cancel
	a.mu.Unlock()
	if cancel != nil {
		cancel()
	}

	done := make(chan struct{})
	go func() {
		a.wait.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (a *eagerAdapter) Faults() sut.SessionFaults { return &a.faults }
