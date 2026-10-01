package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

const (
	testBlockTimeout = 50 * time.Millisecond
	testStepTimeout  = 500 * time.Millisecond
	testRunTimeout   = 3 * time.Second
	testStopTimeout  = 500 * time.Millisecond
)

func TestRunPassesDeadlineToAdapterFactory(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	called := false
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:    &recordingFixture{},
		DB:         &fixture.DB{},
		NewRuntime: syncpoint.New,
		NewAdapter: func(factoryCtx context.Context, _ syncpoint.Client) (sut.Adapter, error) {
			called = true
			if _, ok := factoryCtx.Deadline(); !ok {
				t.Fatal("adapter factory has no run deadline")
			}
			cancel()
			<-factoryCtx.Done()
			return nil, factoryCtx.Err()
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	_, err := orchestrator.Run(ctx, matchingScenario(), matchingSchedule(t), stableEvaluator)
	if !called || !errors.Is(err, context.Canceled) {
		t.Fatalf("factory called/error = %t/%v, want cancellation", called, err)
	}
}

func TestRunSavedScheduleWithOracle(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	runtime := newRuntimeProbe()
	var adapter *scriptedAdapter

	orchestrator := newTestOrchestrator(t, Config{
		Fixture:    fixtureRunner,
		DB:         &fixture.DB{},
		NewRuntime: func() syncpoint.Runtime { return runtime },
		NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
			adapter = newScriptedAdapter(client)
			return adapter, nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	evaluatorCalled := false
	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		oracle.EvaluatorFunc(func(_ context.Context, _ oracle.DB, run oracle.RunContext) (oracle.Evaluation, error) {
			evaluatorCalled = true
			if adapter.stopped.Load() {
				t.Fatal("adapter stopped before Oracle evaluation")
			}
			if len(run.Terminals) != 2 {
				t.Fatalf("Oracle terminals = %d, want 2", len(run.Terminals))
			}
			if adapter.terminalResourcesReleased.Load() != 2 {
				t.Fatalf("released terminal resources = %d, want 2", adapter.terminalResourcesReleased.Load())
			}
			if len(run.Trace) == 0 || run.Trace[len(run.Trace)-1].Kind != EventScheduleComplete {
				t.Fatalf("Oracle trace lacks schedule_complete: %#v", run.Trace)
			}
			run.Terminals[0].Worker = "evaluator_mutation"
			run.Trace[0].Worker = "evaluator_mutation"
			return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
		}),
	)
	if err != nil {
		t.Fatalf("run saved schedule: %v", err)
	}
	if !evaluatorCalled {
		t.Fatal("Oracle evaluator was not called")
	}
	if result.ScheduleID != "sch_ba00582f9632" || result.Steps != 4 {
		t.Fatalf("run identity = %q/%d, want sch_ba00582f9632/4", result.ScheduleID, result.Steps)
	}
	if result.Timeouts != 1 {
		t.Fatalf("run timeouts = %d, want 1", result.Timeouts)
	}
	if result.PendingResolved != 1 {
		t.Fatalf("pending resolved = %d, want 1", result.PendingResolved)
	}
	if len(result.Workers) != 2 {
		t.Fatalf("run workers = %d, want 2", len(result.Workers))
	}
	for index, workerID := range []string{"w1", "w2"} {
		if result.Workers[index].WorkerID != workerID {
			t.Fatalf("worker[%d] = %q, want %q", index, result.Workers[index].WorkerID, workerID)
		}
		if result.Workers[index].Err != nil {
			t.Fatalf("worker[%d] error = %v, want nil", index, result.Workers[index].Err)
		}
	}
	if result.Fingerprint == "" || result.Evaluation.Fingerprint == "" {
		t.Fatalf("run/evaluation fingerprints = %q/%q, want nonempty", result.Fingerprint, result.Evaluation.Fingerprint)
	}
	if len(result.Evaluation.Results) != 1 || result.Evaluation.Results[0].OracleID != "stable-pass" {
		t.Fatalf("run evaluation = %#v, want stable-pass", result.Evaluation)
	}
	if result.Terminals[0].Worker != "w1" || result.Trace[0].Worker != "" {
		t.Fatalf("evaluator mutation changed stored run evidence: terminals=%#v trace=%#v", result.Terminals, result.Trace)
	}
	if result.Elapsed <= 0 {
		t.Fatalf("run elapsed = %s, want positive", result.Elapsed)
	}
	if fixtureRunner.resetCalls != 1 {
		t.Fatalf("fixture resets = %d, want 1", fixtureRunner.resetCalls)
	}
	if adapter.stopCalls.Load() != 1 {
		t.Fatalf("adapter stops = %d, want 1", adapter.stopCalls.Load())
	}
	if adapter.active.Load() != 0 {
		t.Fatalf("active adapter workers = %d, want 0", adapter.active.Load())
	}
	if runtime.closeCalls.Load() != 1 {
		t.Fatalf("runtime closes = %d, want 1", runtime.closeCalls.Load())
	}
	for _, workerID := range []string{"w1", "w2"} {
		snapshot, snapshotErr := runtime.Snapshot(workerID)
		if snapshotErr != nil {
			t.Fatalf("snapshot %s: %v", workerID, snapshotErr)
		}
		if snapshot.State != syncpoint.WorkerStateDone {
			t.Fatalf("worker %s state = %s, want done", workerID, snapshot.State)
		}
	}

	t.Log(
		"ORCHESTRATOR_RUN_RESULT schedule=sch_ba00582f9632 workers=2 steps=4 " +
			"timeouts=1 pending_resolved=1 terminal_done=2 cleanup=ok",
	)
}

func TestRunQuarantinesFailedStopAcrossCalls(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	stopCause := errors.New("shutdown could not prove database cleanup")
	starts := 0
	var oldRuntime syncpoint.Runtime
	newExecutor := func() *Orchestrator {
		return newTestOrchestrator(t, Config{
			Fixture: fixtureRunner, DB: &fixture.DB{},
			NewRuntime: func() syncpoint.Runtime {
				runtime := syncpoint.New()
				if oldRuntime == nil {
					oldRuntime = runtime
				}
				return runtime
			},
			NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
				starts++
				adapter := newScriptedAdapter(client)
				adapter.stopErr = stopCause
				return adapter, nil
			},
			BlockInferenceTimeout: testBlockTimeout, StepTimeout: testStepTimeout,
			RunTimeout: testRunTimeout, StopTimeout: testStopTimeout,
		})
	}
	first := newExecutor()
	value, schedule := matchingScenario(), matchingSchedule(t)
	if _, err := first.Run(context.Background(), value, schedule, stableEvaluator); !errors.Is(err, stopCause) {
		t.Fatalf("first run error = %v, want stop cause", err)
	}
	if fixtureRunner.resetCalls != 1 || starts != 1 {
		t.Fatalf("first run reset/start calls = %d/%d, want 1/1", fixtureRunner.resetCalls, starts)
	}
	if err := oldRuntime.Arrive(context.Background(), "w1", "after_read_request"); !errors.Is(err, syncpoint.ErrClosed) {
		t.Fatalf("old bridge arrival after failed Stop = %v, want closed runtime", err)
	}
	for _, call := range []struct {
		name string
		run  func() error
	}{
		{"direct Run", func() error { _, err := first.Run(context.Background(), value, schedule, stableEvaluator); return err }},
		{"new Orchestrator", func() error {
			_, err := newExecutor().Run(context.Background(), value, schedule, stableEvaluator)
			return err
		}},
		{"Replay", func() error {
			_, err := first.Replay(context.Background(), value, schedule, 2, stableEvaluator)
			return err
		}},
	} {
		err := call.run()
		if !errors.Is(err, fixture.ErrQuarantined) || !errors.Is(err, stopCause) {
			t.Fatalf("%s error = %v, want quarantine and original stop cause", call.name, err)
		}
		if fixtureRunner.resetCalls != 1 || starts != 1 {
			t.Fatalf("%s reached Reset or adapter: resets=%d starts=%d", call.name, fixtureRunner.resetCalls, starts)
		}
	}
	t.Log("FIXTURE_QUARANTINE_RESULT stop=failed direct_run=blocked replay=blocked old_bridge=closed resets=1 adapter_starts=1")
}

func TestRunQuarantinesSessionFaultAfterSuccessfulStop(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	cause := errors.New("unknown transaction outcome")
	starts := 0
	o := newTestOrchestrator(t, Config{
		Fixture: fixtureRunner, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
		NewAdapter: func(context.Context, syncpoint.Client) (sut.Adapter, error) {
			starts++
			a := &outcomeAdapter{}
			a.stop = func(context.Context) error { a.faults.Fail(cause); return nil }
			return a, nil
		},
		BlockInferenceTimeout: testBlockTimeout, StepTimeout: testStepTimeout,
		RunTimeout: testRunTimeout, StopTimeout: testStopTimeout,
	})
	value := scenario.Scenario{Name: "fault", Workers: []scenario.Worker{{ID: "w1", Command: "command"}}, SyncPoints: []string{"point"}}
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "point"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.Run(context.Background(), value, schedule, stableEvaluator); !errors.Is(err, cause) {
		t.Fatalf("first run error = %v, want session cause", err)
	}
	if _, err := o.Run(context.Background(), value, schedule, stableEvaluator); !errors.Is(err, fixture.ErrQuarantined) || !errors.Is(err, cause) {
		t.Fatalf("second run error = %v, want quarantine and session cause", err)
	}
	if fixtureRunner.resetCalls != 1 || starts != 1 {
		t.Fatalf("fault allowed reuse: resets=%d starts=%d", fixtureRunner.resetCalls, starts)
	}
}

func TestRunSuccessfulStopAllowsReuse(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	starts := 0
	o := newTestOrchestrator(t, Config{
		Fixture: fixtureRunner, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
		NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
			starts++
			return newScriptedAdapter(client), nil
		},
		BlockInferenceTimeout: testBlockTimeout, StepTimeout: testStepTimeout,
		RunTimeout: testRunTimeout, StopTimeout: testStopTimeout,
	})
	if _, err := o.Replay(context.Background(), matchingScenario(), matchingSchedule(t), 2, stableEvaluator); err != nil {
		t.Fatalf("replay after successful Stop: %v", err)
	}
	if fixtureRunner.resetCalls != 2 || starts != 2 || fixtureRunner.quarantine != nil {
		t.Fatalf("successful reuse: resets=%d starts=%d quarantine=%v", fixtureRunner.resetCalls, starts, fixtureRunner.quarantine)
	}
}

func TestRunDefersPointBehindPendingWorkerArrival(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	runtime := newRuntimeProbe()
	adapter := newScriptedAdapter(runtime)
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w2", Point: "before_insert_assignment"},
		{Worker: "w1", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("create dependent-point schedule: %v", err)
	}

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		schedule,
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run dependent-point schedule: %v", err)
	}
	if result.Timeouts != 1 || result.PendingResolved != 1 {
		t.Fatalf(
			"dependent-point progress timeouts=%d pending_resolved=%d, want 1/1",
			result.Timeouts,
			result.PendingResolved,
		)
	}

	var released []int
	var skipped []int
	for _, event := range result.Trace {
		switch event.Kind {
		case EventPointReleased:
			released = append(released, event.Step)
		case EventStepTerminalSkipped:
			skipped = append(skipped, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 3, 1}) {
		t.Fatalf("dependent-point releases = %v, want [0 3 1]", released)
	}
	if !reflect.DeepEqual(skipped, []int{2}) {
		t.Fatalf("dependent-point skips = %v, want [2]", skipped)
	}
}

func TestRunResolvesDeferredWorkerPointsInOrder(t *testing.T) {
	baseRuntime := syncpoint.New()
	runtime := &waitForNextPointOnReleaseRuntime{
		Runtime:      baseRuntime,
		workerID:     "w2",
		releasePoint: "after_read_request",
		nextPoint:    "before_insert_assignment",
		timeout:      testStepTimeout,
	}
	adapter := newScriptedAdapter(runtime)
	adapter.w2VisitsBeforeInsert = true
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               &recordingFixture{},
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w1", Point: "before_insert_assignment"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w2", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("create deferred-worker schedule: %v", err)
	}

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		schedule,
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run deferred-worker schedule: %v", err)
	}
	if result.Timeouts != 1 || result.PendingResolved != 2 {
		t.Fatalf(
			"deferred-worker progress timeouts=%d pending_resolved=%d, want 1/2",
			result.Timeouts,
			result.PendingResolved,
		)
	}

	var released []int
	for _, event := range result.Trace {
		if event.Kind == EventPointReleased {
			released = append(released, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 1, 2, 3}) {
		t.Fatalf("deferred-worker releases = %v, want [0 1 2 3]", released)
	}
}

func TestRunPreservesScheduleBarrierWhenDrainingPending(t *testing.T) {
	runtime := syncpoint.New()
	adapter := newScriptedAdapter(runtime)
	adapter.w2VisitsBeforeInsert = true
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               &recordingFixture{},
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	value := scenario.Scenario{
		Name: "schedule-barrier",
		Workers: []scenario.Worker{
			{ID: "w1", Command: "assign"},
			{ID: "w3", Command: "assign"},
			{ID: "w2", Command: "assign"},
		},
		SyncPoints: []string{"after_read_request", "before_insert_assignment"},
		SUTConfig:  sut.SUTConfig{Variant: "fixed"},
	}
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w1", Point: "before_insert_assignment"},
		{Worker: "w3", Point: "after_read_request"},
		{Worker: "w3", Point: "before_insert_assignment"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w2", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("create schedule-barrier schedule: %v", err)
	}

	result, err := orchestrator.Run(context.Background(), value, schedule, stableEvaluator)
	if err != nil {
		t.Fatalf("run schedule-barrier schedule: %v", err)
	}
	if result.Timeouts != 1 || result.PendingResolved != 2 {
		t.Fatalf(
			"schedule-barrier progress timeouts=%d pending_resolved=%d, want 1/2",
			result.Timeouts,
			result.PendingResolved,
		)
	}

	var released []int
	for _, event := range result.Trace {
		if event.Kind == EventPointReleased {
			released = append(released, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 1, 2, 3, 4, 5}) {
		t.Fatalf("schedule-barrier releases = %v, want [0 1 2 3 4 5]", released)
	}
}

func TestRunReleasesLaterStepsBeforeCollectingFinalWorker(t *testing.T) {
	runtime := syncpoint.New()
	adapter := newScriptedAdapter(runtime)
	adapter.w1WaitsForW2Final = true
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               &recordingFixture{},
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w1", Point: "before_insert_assignment"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w2", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("create post-final dependency schedule: %v", err)
	}

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		schedule,
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run post-final dependency schedule: %v", err)
	}
	for _, worker := range result.Workers {
		if worker.Err != nil {
			t.Fatalf("worker %q error = %v, want nil", worker.WorkerID, worker.Err)
		}
	}

	var released []int
	for _, event := range result.Trace {
		if event.Kind == EventPointReleased {
			released = append(released, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 1, 2, 3}) {
		t.Fatalf("post-final dependency releases = %v, want [0 1 2 3]", released)
	}
}

func TestRunContinuesPendingDrainBeforeCollectingFinalWorker(t *testing.T) {
	baseRuntime := syncpoint.New()
	runtime := &timeoutFirstWaitPerWorkerRuntime{
		Runtime: baseRuntime,
		waited:  make(map[string]bool),
	}
	adapter := newScriptedAdapter(runtime)
	adapter.w1WaitsForW2Final = true
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               &recordingFixture{},
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w1", Point: "before_insert_assignment"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w2", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("create pending post-final dependency schedule: %v", err)
	}

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		schedule,
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run pending post-final dependency schedule: %v", err)
	}
	if result.Timeouts != 2 || result.PendingResolved != 4 {
		t.Fatalf(
			"pending post-final progress timeouts=%d pending_resolved=%d, want 2/4",
			result.Timeouts,
			result.PendingResolved,
		)
	}
	for _, worker := range result.Workers {
		if worker.Err != nil {
			t.Fatalf("worker %q error = %v, want nil", worker.WorkerID, worker.Err)
		}
	}

	var released []int
	for _, event := range result.Trace {
		if event.Kind == EventPointReleased {
			released = append(released, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 1, 2, 3}) {
		t.Fatalf("pending post-final releases = %v, want [0 1 2 3]", released)
	}
}

func TestDrainPendingClearsStepWhenPollCollectsTerminal(t *testing.T) {
	runtime := &timeoutWaitRuntime{Runtime: syncpoint.New()}
	t.Cleanup(runtime.Close)
	collected := make(chan collectedResult, 1)
	collected <- collectedResult{result: sut.WorkerResult{WorkerID: "w1"}}
	close(collected)

	result := RunResult{}
	execution := &workerExecution{
		worker:          scenario.Worker{ID: "w1", Command: "assign"},
		collectedResult: collected,
	}
	coordinator := runCoordinator{
		ctx:     context.Background(),
		runtime: runtime,
		value:   scenario.Scenario{SyncPoints: []string{"after_read_request"}},
		schedule: scenario.Schedule{Steps: []scenario.CoordinationStep{
			{Worker: "w1", Point: "after_read_request"},
		}},
		result:     &result,
		trace:      newTraceRecorder(nil),
		executions: map[string]*workerExecution{"w1": execution},
		pending:    map[int]bool{0: true},
	}

	progressed, err := coordinator.drainPending()
	if err != nil {
		t.Fatalf("drain terminal pending step: %v", err)
	}
	if !progressed {
		t.Fatal("drain terminal pending step progressed = false, want true")
	}
	if len(coordinator.pending) != 0 {
		t.Fatalf("pending steps = %v, want none", coordinator.pending)
	}
	if !execution.terminal || execution.terminalState != TerminalStateDone {
		t.Fatalf("execution terminal = %+v, want done", execution)
	}
	if result.Timeouts != 0 {
		t.Fatalf("timeouts = %d, want 0 after terminal poll", result.Timeouts)
	}

	wantKinds := []EventKind{EventWorkerDone, EventStepTerminalSkipped}
	var gotKinds []EventKind
	for _, event := range coordinator.trace.events {
		gotKinds = append(gotKinds, event.Kind)
	}
	if !reflect.DeepEqual(gotKinds, wantKinds) {
		t.Fatalf("terminal pending trace = %v, want %v", gotKinds, wantKinds)
	}
}

func TestRunSerializesSharedFixtureLifecycle(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:    fixtureRunner,
		DB:         &fixture.DB{},
		NewRuntime: syncpoint.New,
		NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
			return newEagerAdapter(client), nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	schedule := matchingSchedule(t)

	firstObserved := make(chan struct{})
	releaseFirst := make(chan struct{})
	firstResult := make(chan error, 1)
	go func() {
		_, err := orchestrator.Run(
			context.Background(),
			matchingScenario(),
			schedule,
			oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				close(firstObserved)
				<-releaseFirst
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "first-pass"})
			}),
		)
		firstResult <- err
	}()

	select {
	case <-firstObserved:
	case <-time.After(testRunTimeout):
		t.Fatal("first run did not reach observer")
	}

	secondCtx, cancelSecond := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancelSecond()
	_, secondErr := orchestrator.Run(
		secondCtx,
		matchingScenario(),
		schedule,
		stableEvaluator,
	)
	if !errors.Is(secondErr, context.DeadlineExceeded) {
		t.Fatalf("overlapping run error = %v, want deadline exceeded", secondErr)
	}
	if fixtureRunner.resetCalls != 1 {
		t.Fatalf("fixture resets while first run active = %d, want 1", fixtureRunner.resetCalls)
	}

	close(releaseFirst)
	select {
	case err := <-firstResult:
		if err != nil {
			t.Fatalf("first serialized run: %v", err)
		}
	case <-time.After(testRunTimeout):
		t.Fatal("first serialized run did not finish")
	}

	if _, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		schedule,
		stableEvaluator,
	); err != nil {
		t.Fatalf("run after gate release: %v", err)
	}
	if fixtureRunner.resetCalls != 2 {
		t.Fatalf("fixture resets after serialized runs = %d, want 2", fixtureRunner.resetCalls)
	}
}

func TestRunCleanup(t *testing.T) {
	rootErr := errors.New("injected run failure")
	cleanupErr := errors.New("injected cleanup failure")

	t.Run("reset", func(t *testing.T) {
		fixtureRunner := &recordingFixture{resetErr: rootErr}
		factoryCalls := 0
		orchestrator := newTestOrchestrator(t, Config{
			Fixture: fixtureRunner,
			DB:      &fixture.DB{},
			NewRuntime: func() syncpoint.Runtime {
				factoryCalls++
				return syncpoint.New()
			},
			NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return newScriptedAdapter(nil), nil },
			BlockInferenceTimeout: testBlockTimeout,
			StepTimeout:           testStepTimeout,
			RunTimeout:            testRunTimeout,
			StopTimeout:           testStopTimeout,
		})

		_, err := orchestrator.Run(
			context.Background(),
			matchingScenario(),
			matchingSchedule(t),
			stableEvaluator,
		)
		if !errors.Is(err, rootErr) {
			t.Fatalf("reset error = %v, want errors.Is(_, rootErr)", err)
		}
		if factoryCalls != 0 {
			t.Fatalf("runtime factory calls after reset failure = %d, want 0", factoryCalls)
		}
	})

	tests := []struct {
		name      string
		configure func(*runtimeProbe, *scriptedAdapter)
		evaluator oracle.Evaluator
	}{
		{
			name: "start",
			configure: func(_ *runtimeProbe, adapter *scriptedAdapter) {
				adapter.startErr = rootErr
			},
		},
		{
			name: "register",
			configure: func(runtime *runtimeProbe, _ *scriptedAdapter) {
				runtime.failRegister = rootErr
			},
		},
		{
			name: "invoke",
			configure: func(_ *runtimeProbe, adapter *scriptedAdapter) {
				adapter.invokeErr = rootErr
			},
		},
		{
			name: "wait",
			configure: func(runtime *runtimeProbe, _ *scriptedAdapter) {
				runtime.failWait = rootErr
			},
		},
		{
			name: "release",
			configure: func(runtime *runtimeProbe, _ *scriptedAdapter) {
				runtime.failRelease = rootErr
			},
		},
		{
			name: "collector",
			configure: func(runtime *runtimeProbe, _ *scriptedAdapter) {
				runtime.failFinish = rootErr
			},
		},
		{
			name:      "evaluator",
			configure: func(_ *runtimeProbe, _ *scriptedAdapter) {},
			evaluator: oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				return oracle.Evaluation{}, rootErr
			}),
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixtureRunner := &recordingFixture{}
			runtime := newRuntimeProbe()
			adapter := newScriptedAdapter(runtime)
			adapter.stopErr = cleanupErr
			test.configure(runtime, adapter)
			evaluator := test.evaluator
			if evaluator == nil {
				evaluator = stableEvaluator
			}

			orchestrator := newTestOrchestrator(t, Config{
				Fixture:               fixtureRunner,
				DB:                    &fixture.DB{},
				NewRuntime:            func() syncpoint.Runtime { return runtime },
				NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
				BlockInferenceTimeout: testBlockTimeout,
				StepTimeout:           testStepTimeout,
				RunTimeout:            testRunTimeout,
				StopTimeout:           testStopTimeout,
			})

			_, err := orchestrator.Run(
				context.Background(),
				matchingScenario(),
				matchingSchedule(t),
				evaluator,
			)
			if !errors.Is(err, rootErr) {
				t.Fatalf("run error = %v, want errors.Is(_, rootErr)", err)
			}
			if !errors.Is(err, cleanupErr) {
				t.Fatalf("run error = %v, want errors.Is(_, cleanupErr)", err)
			}
			if adapter.stopCalls.Load() != 1 {
				t.Fatalf("adapter stops = %d, want 1", adapter.stopCalls.Load())
			}
			if adapter.active.Load() != 0 {
				t.Fatalf("active adapter workers = %d, want 0", adapter.active.Load())
			}
			if runtime.closeCalls.Load() != 1 {
				t.Fatalf("runtime closes = %d, want 1", runtime.closeCalls.Load())
			}
			if fixtureRunner.resetCalls != 1 {
				t.Fatalf("fixture resets = %d, want 1", fixtureRunner.resetCalls)
			}
		})
	}
}

func TestRunPreservesCancellationBeforeFinalizationSetup(t *testing.T) {
	newOrchestrator := func(t *testing.T, fixtureRunner fixture.Fixture) *Orchestrator {
		t.Helper()
		return newTestOrchestrator(t, Config{
			Fixture:               fixtureRunner,
			DB:                    &fixture.DB{},
			NewRuntime:            syncpoint.New,
			NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return newScriptedAdapter(nil), nil },
			BlockInferenceTimeout: testBlockTimeout,
			StepTimeout:           testStepTimeout,
			RunTimeout:            testRunTimeout,
			StopTimeout:           testStopTimeout,
		})
	}

	t.Run("nil evaluator", func(t *testing.T) {
		cause := errors.New("cancel before evaluator validation")
		ctx, cancel := context.WithCancelCause(context.Background())
		cancel(cause)
		fixtureRunner := &recordingFixture{}
		orchestrator := newOrchestrator(t, fixtureRunner)

		result, err := orchestrator.Run(ctx, matchingScenario(), matchingSchedule(t), nil)
		if err == nil || !strings.Contains(err.Error(), "Oracle evaluator is required") ||
			!errors.Is(err, context.Canceled) || !errors.Is(err, cause) {
			t.Fatalf("nil-evaluator cancellation = %v, want validation, context, and custom causes", err)
		}
		if result.Fingerprint != "" || len(result.Evaluation.Results) != 0 {
			t.Fatalf("nil-evaluator cancellation retained provisional result: %#v", result)
		}
		if fixtureRunner.resetCalls != 0 {
			t.Fatalf("nil-evaluator fixture resets = %d, want 0", fixtureRunner.resetCalls)
		}
	})

	t.Run("cancellation after final success observation", func(t *testing.T) {
		cause := errors.New("cancel after final context observation")
		baseCtx, cancel := context.WithCancelCause(context.Background())
		cancel(cause)
		ctx := &activeOnFirstErrContext{Context: baseCtx}
		orchestrator := newOrchestrator(t, &recordingFixture{})

		result, err := orchestrator.Run(ctx, matchingScenario(), matchingSchedule(t), nil)
		if err == nil || !strings.Contains(err.Error(), "Oracle evaluator is required") {
			t.Fatalf("nil-evaluator validation = %v, want validation error", err)
		}
		if errors.Is(err, cause) || errors.Is(err, context.Canceled) {
			t.Fatalf("post-boundary cancellation escaped: %v", err)
		}
		if result.Fingerprint != "" || len(result.Evaluation.Results) != 0 {
			t.Fatalf("post-boundary cancellation retained provisional result: %#v", result)
		}
	})

	t.Run("run gate", func(t *testing.T) {
		cause := errors.New("cancel while waiting for active run")
		orchestrator := newOrchestrator(t, &recordingFixture{})
		<-orchestrator.runGate
		defer func() { orchestrator.runGate <- struct{}{} }()
		ctx, cancel := context.WithCancelCause(context.Background())
		cancel(cause)

		result, err := orchestrator.Run(ctx, matchingScenario(), matchingSchedule(t), stableEvaluator)
		if !errors.Is(err, context.Canceled) || !errors.Is(err, cause) {
			t.Fatalf("run-gate cancellation = %v, want context and custom causes", err)
		}
		if result.Fingerprint != "" || len(result.Evaluation.Results) != 0 {
			t.Fatalf("run-gate cancellation retained provisional result: %#v", result)
		}
	})

	t.Run("fixture reset", func(t *testing.T) {
		cause := errors.New("cancel during fixture reset")
		ctx, cancel := context.WithCancelCause(context.Background())
		fixtureRunner := &cancelingResetFixture{cancel: cancel, cause: cause}
		orchestrator := newOrchestrator(t, fixtureRunner)

		result, err := orchestrator.Run(ctx, matchingScenario(), matchingSchedule(t), stableEvaluator)
		if !errors.Is(err, context.Canceled) || !errors.Is(err, cause) {
			t.Fatalf("fixture-reset cancellation = %v, want context and custom causes", err)
		}
		if result.Fingerprint != "" || len(result.Evaluation.Results) != 0 {
			t.Fatalf("fixture-reset cancellation retained provisional result: %#v", result)
		}
	})

	t.Run("run deadline during fixture reset", func(t *testing.T) {
		resetErr := errors.New("reset failed after run deadline")
		fixtureRunner := &deadlineResetFixture{err: resetErr}
		orchestrator := newTestOrchestrator(t, Config{
			Fixture:               fixtureRunner,
			DB:                    &fixture.DB{},
			NewRuntime:            syncpoint.New,
			NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return newScriptedAdapter(nil), nil },
			BlockInferenceTimeout: testBlockTimeout,
			StepTimeout:           testStepTimeout,
			RunTimeout:            10 * time.Millisecond,
			StopTimeout:           testStopTimeout,
		})

		result, err := orchestrator.Run(
			context.Background(),
			matchingScenario(),
			matchingSchedule(t),
			stableEvaluator,
		)
		if !errors.Is(err, resetErr) || !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("fixture-reset deadline = %v, want reset and run-deadline causes", err)
		}
		if result.Fingerprint != "" || len(result.Evaluation.Results) != 0 {
			t.Fatalf("fixture-reset deadline retained provisional result: %#v", result)
		}
	})
}

func TestRunStopsWorkersBeforeCancelingCollectors(t *testing.T) {
	rootErr := errors.New("injected wait failure")
	runtime := newRuntimeProbe()
	runtime.failWait = rootErr
	adapter := newScriptedAdapter(runtime)
	adapter.unbufferedResults = true
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               &recordingFixture{},
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	_, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		stableEvaluator,
	)
	if !errors.Is(err, rootErr) {
		t.Fatalf("run error = %v, want errors.Is(_, rootErr)", err)
	}
	if errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("run error = %v, want no stop timeout", err)
	}
	if adapter.stopCalls.Load() != 1 {
		t.Fatalf("adapter stops = %d, want 1", adapter.stopCalls.Load())
	}
	if adapter.active.Load() != 0 {
		t.Fatalf("active adapter workers = %d, want 0", adapter.active.Load())
	}
}

func TestRunRejectsInvalidConfig(t *testing.T) {
	base := Config{
		Fixture:    &recordingFixture{},
		DB:         &fixture.DB{},
		NewRuntime: syncpoint.New,
		NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
			return newScriptedAdapter(client), nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	}
	tests := []struct {
		name     string
		mutate   func(*Config)
		wantPart string
	}{
		{name: "fixture", mutate: func(config *Config) { config.Fixture = nil }, wantPart: "fixture"},
		{name: "database", mutate: func(config *Config) { config.DB = nil }, wantPart: "database"},
		{name: "runtime factory", mutate: func(config *Config) { config.NewRuntime = nil }, wantPart: "runtime factory"},
		{name: "adapter factory", mutate: func(config *Config) { config.NewAdapter = nil }, wantPart: "adapter factory"},
		{
			name:     "block timeout",
			mutate:   func(config *Config) { config.BlockInferenceTimeout = 0 },
			wantPart: "block inference timeout",
		},
		{name: "step timeout", mutate: func(config *Config) { config.StepTimeout = 0 }, wantPart: "step timeout"},
		{name: "run timeout", mutate: func(config *Config) { config.RunTimeout = 0 }, wantPart: "run timeout"},
		{name: "stop timeout", mutate: func(config *Config) { config.StopTimeout = 0 }, wantPart: "stop timeout"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			config := base
			test.mutate(&config)
			_, err := New(config)
			if err == nil || !strings.Contains(err.Error(), test.wantPart) {
				t.Fatalf("New error = %v, want containing %q", err, test.wantPart)
			}
		})
	}

	orchestrator := newTestOrchestrator(t, base)
	_, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		nil,
	)
	if err == nil || !strings.Contains(err.Error(), "Oracle evaluator is required") {
		t.Fatalf("nil evaluator error = %v, want Oracle evaluator context", err)
	}
}

// TestRunReportsAdapterCompositionFailure covers the factory boundary an
// out-of-process adapter needs: composing a run's adapter can fail for a
// reason of its own (an unusable launch setting, an unavailable child), and
// that reason must reach the run error instead of collapsing into an
// anonymous factory failure.
func TestRunReportsAdapterCompositionFailure(t *testing.T) {
	composeErr := errors.New("compose adapter for this run")
	fixtureRunner := &recordingFixture{}
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            syncpoint.New,
		NewAdapter:            func(_ context.Context, _ syncpoint.Client) (sut.Adapter, error) { return nil, composeErr },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	_, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		stableEvaluator,
	)
	if err == nil {
		t.Fatal("run with a failing adapter factory: want error, got nil")
	}
	if !errors.Is(err, composeErr) {
		t.Fatalf("run error = %v, want the composition cause", err)
	}
	if !strings.Contains(err.Error(), "create adapter") {
		t.Fatalf("run error = %v, want it to name adapter creation", err)
	}

	t.Log("ORCHESTRATOR_ADAPTER_FACTORY_RESULT cause=preserved start=not_attempted")
}

func newTestOrchestrator(t *testing.T, config Config) *Orchestrator {
	t.Helper()

	orchestrator, err := New(config)
	if err != nil {
		t.Fatalf("new orchestrator: %v", err)
	}
	return orchestrator
}

var stableEvaluator = oracle.EvaluatorFunc(
	func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
		return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	},
)

func matchingScenario() scenario.Scenario {
	return scenario.Scenario{
		Name: "matching-concurrent-assign",
		Workers: []scenario.Worker{
			{ID: "w1", Command: "assign"},
			{ID: "w2", Command: "assign"},
		},
		SyncPoints: []string{"after_read_request", "before_insert_assignment"},
		SUTConfig: sut.SUTConfig{
			Variant: "fixed",
			Params:  map[string]string{"request_id": "42"},
		},
	}
}

func matchingSchedule(t *testing.T) scenario.Schedule {
	t.Helper()

	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "after_read_request"},
		{Worker: "w2", Point: "after_read_request"},
		{Worker: "w1", Point: "before_insert_assignment"},
		{Worker: "w2", Point: "before_insert_assignment"},
	})
	if err != nil {
		t.Fatalf("new matching schedule: %v", err)
	}
	return schedule
}

type recordingFixture struct {
	resetCalls int
	resetErr   error
	quarantine error
}

type cancelingResetFixture struct {
	recordingFixture
	cancel context.CancelCauseFunc
	cause  error
}

type deadlineResetFixture struct {
	recordingFixture
	err error
}

type activeOnFirstErrContext struct {
	context.Context
	observed atomic.Bool
}

func (c *activeOnFirstErrContext) Err() error {
	if c.observed.CompareAndSwap(false, true) {
		return nil
	}
	return c.Context.Err()
}

func (f *cancelingResetFixture) Reset(ctx context.Context) error {
	f.resetCalls++
	f.cancel(f.cause)
	return ctx.Err()
}

func (f *deadlineResetFixture) Reset(ctx context.Context) error {
	f.resetCalls++
	<-ctx.Done()
	return f.err
}

func (*recordingFixture) Provision(context.Context, fixture.FixtureSpec) (*fixture.DB, error) {
	return nil, errors.New("recording fixture does not provision")
}

func (f *recordingFixture) Reset(context.Context) error {
	f.resetCalls++
	if f.quarantine != nil {
		return errors.Join(fixture.ErrQuarantined, f.quarantine)
	}
	return f.resetErr
}

func (f *recordingFixture) Ready(*fixture.DB) error {
	if f.quarantine != nil {
		return errors.Join(fixture.ErrQuarantined, f.quarantine)
	}
	return nil
}

func (f *recordingFixture) Quarantine(err error) {
	if f.quarantine == nil {
		f.quarantine = err
	}
}

func (*recordingFixture) Teardown(context.Context) error {
	return nil
}

type runtimeProbe struct {
	syncpoint.Runtime

	failRegister error
	failWait     error
	failRelease  error
	failFinish   error

	closeCalls atomic.Int32
}

type timeoutWaitRuntime struct {
	syncpoint.Runtime
}

type timeoutFirstWaitPerWorkerRuntime struct {
	syncpoint.Runtime
	mu     sync.Mutex
	waited map[string]bool
}

func (r *timeoutFirstWaitPerWorkerRuntime) WaitArrive(
	ctx context.Context,
	workerID string,
	point string,
	timeout time.Duration,
) (syncpoint.ArriveStatus, error) {
	r.mu.Lock()
	if !r.waited[workerID] {
		r.waited[workerID] = true
		r.mu.Unlock()
		return syncpoint.ArriveStatusTimeout, nil
	}
	r.mu.Unlock()
	return r.Runtime.WaitArrive(ctx, workerID, point, timeout)
}

type waitForNextPointOnReleaseRuntime struct {
	syncpoint.Runtime
	workerID     string
	releasePoint string
	nextPoint    string
	timeout      time.Duration
}

func (r *waitForNextPointOnReleaseRuntime) Release(
	ctx context.Context,
	workerID string,
	point string,
) error {
	if err := r.Runtime.Release(ctx, workerID, point); err != nil {
		return err
	}
	if workerID != r.workerID || point != r.releasePoint {
		return nil
	}
	status, err := r.WaitArrive(ctx, workerID, r.nextPoint, r.timeout)
	if err != nil {
		return err
	}
	if status != syncpoint.ArriveStatusArrived {
		return fmt.Errorf(
			"worker %q next point %q status = %s, want arrived",
			workerID,
			r.nextPoint,
			status,
		)
	}
	return nil
}

func (r *timeoutWaitRuntime) WaitArrive(
	context.Context,
	string,
	string,
	time.Duration,
) (syncpoint.ArriveStatus, error) {
	return syncpoint.ArriveStatusTimeout, nil
}

func newRuntimeProbe() *runtimeProbe {
	return &runtimeProbe{Runtime: syncpoint.New()}
}

func (r *runtimeProbe) Register(workerID string) error {
	if r.failRegister != nil {
		return r.failRegister
	}
	return r.Runtime.Register(workerID)
}

func (r *runtimeProbe) WaitArrive(
	ctx context.Context,
	workerID string,
	point string,
	timeout time.Duration,
) (syncpoint.ArriveStatus, error) {
	if r.failWait != nil {
		return syncpoint.ArriveStatusUnknown, r.failWait
	}
	return r.Runtime.WaitArrive(ctx, workerID, point, timeout)
}

func (r *runtimeProbe) Release(ctx context.Context, workerID, point string) error {
	if r.failRelease != nil {
		return r.failRelease
	}
	return r.Runtime.Release(ctx, workerID, point)
}

func (r *runtimeProbe) Finish(workerID string, workerErr error) error {
	if r.failFinish != nil {
		return r.failFinish
	}
	return r.Runtime.Finish(workerID, workerErr)
}

func (r *runtimeProbe) Close() {
	r.closeCalls.Add(1)
	r.Runtime.Close()
}

type scriptedAdapter struct {
	faults sut.FaultLatch
	client syncpoint.Client

	startErr             error
	invokeErr            error
	stopErr              error
	unbufferedResults    bool
	w1WaitsForW2Final    bool
	w2VisitsBeforeInsert bool

	mu       sync.Mutex
	ctx      context.Context
	cancel   context.CancelFunc
	wait     sync.WaitGroup
	w1Result chan struct{}
	w1Once   sync.Once
	w2Final  chan struct{}
	w2Once   sync.Once

	active    atomic.Int32
	stopCalls atomic.Int32
	stopped   atomic.Bool

	terminalResourcesReleased atomic.Int32
}

func newScriptedAdapter(client syncpoint.Client) *scriptedAdapter {
	return &scriptedAdapter{
		client:   client,
		w1Result: make(chan struct{}),
		w2Final:  make(chan struct{}),
	}
}

func (a *scriptedAdapter) Start(
	ctx context.Context,
	_ sut.SUTConfig,
	_ *fixture.DB,
) (sut.Handle, error) {
	if a.startErr != nil {
		return nil, a.startErr
	}

	a.mu.Lock()
	a.ctx, a.cancel = context.WithCancel(ctx)
	a.mu.Unlock()
	return a, nil
}

func (a *scriptedAdapter) Invoke(
	_ context.Context,
	workerID string,
	_ string,
) (<-chan sut.InvocationOutcome, error) {
	if a.invokeErr != nil {
		return nil, a.invokeErr
	}

	a.mu.Lock()
	ctx := a.ctx
	a.mu.Unlock()
	if ctx == nil {
		return nil, errors.New("scripted adapter was not started")
	}

	resultBuffer := 1
	if a.unbufferedResults {
		resultBuffer = 0
	}
	results := make(chan sut.InvocationOutcome, resultBuffer)
	a.wait.Add(1)
	a.active.Add(1)
	go func() {
		defer a.wait.Done()
		defer a.active.Add(-1)
		defer close(results)

		var workerErr error
		switch workerID {
		case "w1":
			workerErr = a.client.Arrive(ctx, workerID, "after_read_request")
			if workerErr == nil {
				workerErr = a.client.Arrive(ctx, workerID, "before_insert_assignment")
			}
			if workerErr == nil && a.w1WaitsForW2Final {
				select {
				case <-a.w2Final:
				case <-ctx.Done():
					workerErr = ctx.Err()
				}
			}
			a.publishTerminal(results, sut.WorkerResult{WorkerID: workerID, Err: workerErr})
			a.w1Once.Do(func() { close(a.w1Result) })
		case "w2":
			if a.w1WaitsForW2Final {
				workerErr = a.client.Arrive(ctx, workerID, "after_read_request")
			} else {
				select {
				case <-a.w1Result:
					workerErr = a.client.Arrive(ctx, workerID, "after_read_request")
				case <-ctx.Done():
					workerErr = ctx.Err()
				}
			}
			if workerErr == nil && (a.w2VisitsBeforeInsert || a.w1WaitsForW2Final) {
				workerErr = a.client.Arrive(ctx, workerID, "before_insert_assignment")
			}
			if workerErr == nil && a.w1WaitsForW2Final {
				a.w2Once.Do(func() { close(a.w2Final) })
			}
			a.publishTerminal(results, sut.WorkerResult{WorkerID: workerID, Err: workerErr})
		case "w3":
			workerErr = a.client.Arrive(ctx, workerID, "after_read_request")
			if workerErr == nil {
				workerErr = a.client.Arrive(ctx, workerID, "before_insert_assignment")
			}
			a.publishTerminal(results, sut.WorkerResult{WorkerID: workerID, Err: workerErr})
		default:
			a.publishTerminal(results, sut.WorkerResult{
				WorkerID: workerID,
				Err:      fmt.Errorf("unsupported scripted worker %q", workerID),
			})
		}
	}()
	return results, nil
}

func (a *scriptedAdapter) publishTerminal(
	results chan<- sut.InvocationOutcome,
	result sut.WorkerResult,
) {
	// This fake has no database, so the flag models the production adapter's
	// transaction/connection release cut immediately before terminal publication.
	a.terminalResourcesReleased.Add(1)
	results <- sut.InvocationOutcome{Worker: &result}
}

func (a *scriptedAdapter) Stop(ctx context.Context) error {
	a.stopCalls.Add(1)
	a.stopped.Store(true)
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
		return a.stopErr
	case <-ctx.Done():
		return errors.Join(a.stopErr, fmt.Errorf("stop scripted adapter: %w", ctx.Err()))
	}
}

func (a *scriptedAdapter) Faults() sut.SessionFaults { return &a.faults }
