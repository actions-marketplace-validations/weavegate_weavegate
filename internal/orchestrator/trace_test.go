package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"testing"

	mysqldriver "github.com/go-sql-driver/mysql"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
	"github.com/weavegate/weavegate/internal/trace"
)

func TestTraceRecordsSavedAndRealizedOrder(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	runtime := newRuntimeProbe()
	adapter := newScriptedAdapter(runtime)
	var observed trace.Trace
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
		OnEvent: func(event Event) error {
			observed = append(observed, event)
			return nil
		},
	})

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run traced schedule: %v", err)
	}
	if !reflect.DeepEqual(observed, result.Trace) {
		t.Fatalf("observed trace differs from run trace:\nobserved=%#v\nrun=%#v", observed, result.Trace)
	}
	if len(result.Trace) != 16 {
		t.Fatalf("trace events = %d, want 16: %#v", len(result.Trace), result.Trace)
	}
	for index, event := range result.Trace {
		if event.Seq != index+1 {
			t.Fatalf("trace[%d] sequence = %d, want %d", index, event.Seq, index+1)
		}
	}

	wantKinds := []EventKind{
		EventFixtureReset,
		EventWorkerRegistered,
		EventWorkerRegistered,
		EventWorkerInvoked,
		EventPointArrived,
		EventWorkerInvoked,
		EventPointTimeout,
		EventPointReleased,
		EventPointArrived,
		EventPointReleased,
		EventPointArrived,
		EventPointReleased,
		EventWorkerDone,
		EventStepTerminalSkipped,
		EventWorkerDone,
		EventScheduleComplete,
	}
	for index, want := range wantKinds {
		if result.Trace[index].Kind != want {
			t.Fatalf("trace[%d] kind = %q, want %q", index, result.Trace[index].Kind, want)
		}
	}

	timeout := result.Trace[6]
	if timeout.Step != 1 || timeout.Worker != "w2" ||
		timeout.Status != ControlStatusTimeoutInferred ||
		timeout.FailureClass != WorkerFailureNone {
		t.Fatalf("timeout event = %#v, want step 1 timeout_inferred without failure", timeout)
	}

	var released []int
	for _, event := range result.Trace {
		if event.Kind == EventPointReleased {
			released = append(released, event.Step)
		}
	}
	if !reflect.DeepEqual(released, []int{0, 2, 1}) {
		t.Fatalf("realized release steps = %v, want [0 2 1]", released)
	}
	skipped := result.Trace[13]
	if skipped.Step != 3 || skipped.Worker != "w2" ||
		skipped.Status != ControlStatusTerminalSkipped {
		t.Fatalf("terminal skipped event = %#v, want w2 step 3", skipped)
	}
	if len(result.Terminals) != 2 {
		t.Fatalf("terminals = %d, want 2", len(result.Terminals))
	}
	for _, terminal := range result.Terminals {
		if terminal.State != TerminalStateDone || terminal.FailureClass != WorkerFailureNone {
			t.Fatalf("terminal = %#v, want done/none", terminal)
		}
	}
}

func TestTraceObserverErrorTriggersCleanup(t *testing.T) {
	observerErr := errors.New("trace observer rejected event")
	fixtureRunner := &recordingFixture{}
	runtime := newRuntimeProbe()
	adapter := newScriptedAdapter(runtime)
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
		OnEvent: func(event Event) error {
			if event.Kind == EventPointReleased {
				return observerErr
			}
			return nil
		},
	})

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		stableEvaluator,
	)
	if !errors.Is(err, observerErr) {
		t.Fatalf("trace observer run error = %v, want errors.Is(_, observerErr)", err)
	}
	if adapter.stopCalls.Load() != 1 {
		t.Fatalf("adapter stops = %d, want 1", adapter.stopCalls.Load())
	}
	if adapter.active.Load() != 0 {
		t.Fatalf("active workers = %d, want 0", adapter.active.Load())
	}
	if runtime.closeCalls.Load() != 1 {
		t.Fatalf("runtime closes = %d, want 1", runtime.closeCalls.Load())
	}
	if len(result.Trace) == 0 || result.Trace[len(result.Trace)-1].Kind != EventPointReleased {
		t.Fatalf("partial trace = %#v, want rejected point release appended", result.Trace)
	}
}

func TestTracePreservesClassifiedWorkerCause(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	deadlock := &mysqldriver.MySQLError{Number: 1213, Message: "deadlock found"}
	wrapped := fmt.Errorf("assign transaction: %w", deadlock)
	orchestrator := newTestOrchestrator(t, Config{
		Fixture:    fixtureRunner,
		DB:         &fixture.DB{},
		NewRuntime: syncpoint.New,
		NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
			adapter := newEagerAdapter(client)
			adapter.errors["w1"] = wrapped
			return adapter, nil
		},
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})

	result, err := orchestrator.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		stableEvaluator,
	)
	if err != nil {
		t.Fatalf("run deadlock-classified schedule: %v", err)
	}
	var gotMySQL *mysqldriver.MySQLError
	if !errors.As(result.Workers[0].Err, &gotMySQL) || gotMySQL.Number != 1213 {
		t.Fatalf("worker cause = %v, want wrapped MySQL 1213", result.Workers[0].Err)
	}
	if result.Terminals[0].State != TerminalStateFailed ||
		result.Terminals[0].FailureClass != WorkerFailureMySQLDeadlock {
		t.Fatalf("w1 terminal = %#v, want failed/mysql_deadlock_1213", result.Terminals[0])
	}

	found := false
	for _, event := range result.Trace {
		if event.Kind == EventWorkerFailed && event.Worker == "w1" {
			found = true
			if event.Status != ControlStatusNone ||
				event.FailureClass != WorkerFailureMySQLDeadlock {
				t.Fatalf("worker failed event = %#v", event)
			}
		}
	}
	if !found {
		t.Fatal("trace lacks w1 worker_failed event")
	}
}
