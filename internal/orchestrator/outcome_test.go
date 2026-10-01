package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

type outcomeAdapter struct {
	faults       sut.FaultLatch
	faultSurface sut.SessionFaults
	startCtx     context.Context
	invoke       func(context.Context, string) (<-chan sut.InvocationOutcome, error)
	stop         func(context.Context) error
}

func (a *outcomeAdapter) Start(ctx context.Context, _ sut.SUTConfig, _ *fixture.DB) (sut.Handle, error) {
	a.startCtx = ctx
	return a, nil
}
func (a *outcomeAdapter) Faults() sut.SessionFaults {
	if a.faultSurface != nil {
		return a.faultSurface
	}
	return &a.faults
}
func (a *outcomeAdapter) Invoke(ctx context.Context, worker, _ string) (<-chan sut.InvocationOutcome, error) {
	if a.invoke != nil {
		return a.invoke(ctx, worker)
	}
	return outcomeStream(sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: worker}}), nil
}
func (a *outcomeAdapter) Stop(ctx context.Context) error {
	if a.stop != nil {
		return a.stop(ctx)
	}
	return nil
}
func outcomeStream(values ...sut.InvocationOutcome) <-chan sut.InvocationOutcome {
	stream := make(chan sut.InvocationOutcome, len(values))
	for _, value := range values {
		stream <- value
	}
	close(stream)
	return stream
}

type outcomeRuntime struct {
	syncpoint.Runtime
	finishes    atomic.Int32
	finishErr   error
	afterFinish func()
	afterClose  func()
}

type notificationWithoutFault struct {
	done chan struct{}
}

func (f *notificationWithoutFault) Done() <-chan struct{} { return f.done }
func (*notificationWithoutFault) Err() *sut.SessionFault  { return nil }

type faultWithoutCause struct {
	done chan struct{}
}

func (f *faultWithoutCause) Done() <-chan struct{} { return f.done }
func (*faultWithoutCause) Err() *sut.SessionFault  { return &sut.SessionFault{} }

type faultBetweenObservations struct {
	done  chan struct{}
	fault *sut.SessionFault
	read  atomic.Bool
}

func (f *faultBetweenObservations) Done() <-chan struct{} { return f.done }
func (f *faultBetweenObservations) Err() *sut.SessionFault {
	if f.read.CompareAndSwap(false, true) {
		close(f.done)
		return nil
	}
	return f.fault
}

func (r *outcomeRuntime) Finish(worker string, err error) error {
	r.finishes.Add(1)
	result := r.Runtime.Finish(worker, err)
	if r.afterFinish != nil {
		r.afterFinish()
	}
	return errors.Join(result, r.finishErr)
}
func (r *outcomeRuntime) Close() {
	r.Runtime.Close()
	if r.afterClose != nil {
		r.afterClose()
	}
}

func runOutcomeTest(t *testing.T, ctx context.Context, adapter *outcomeAdapter, runtime *outcomeRuntime, observer EventObserver, evaluator oracle.Evaluator) (RunResult, error) {
	t.Helper()
	if runtime == nil {
		runtime = &outcomeRuntime{Runtime: syncpoint.New()}
	}
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "point"}})
	if err != nil {
		t.Fatal(err)
	}
	value := scenario.Scenario{Name: "outcomes", Workers: []scenario.Worker{{ID: "w1", Command: "command"}}, SyncPoints: []string{"point"}}
	o := newTestOrchestrator(t, Config{
		Fixture: &recordingFixture{}, DB: &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: time.Second, StepTimeout: time.Second,
		RunTimeout: 3 * time.Second, StopTimeout: time.Second, OnEvent: observer,
	})
	return o.Run(ctx, value, schedule, evaluator)
}

func assertNoProvisionalEvaluation(t *testing.T, result RunResult) {
	t.Helper()
	if len(result.Evaluation.Results) != 0 || result.Evaluation.Fingerprint != "" || result.Fingerprint != "" {
		t.Fatalf("provisional evaluation escaped: %#v", result)
	}
}

func TestReceiveOutcomeBoundsReadyDrainAfterCancellation(t *testing.T) {
	t.Run("observes closure after final outcome", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		stream := make(chan sut.InvocationOutcome, 1)
		stream <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "first"}}
		close(stream)
		readyReadsAfterCancel := 2

		first, ok, err := receiveOutcome(ctx, stream, &readyReadsAfterCancel)
		if err != nil || !ok || first.Worker == nil || first.Worker.WorkerID != "first" {
			t.Fatalf("final ready outcome = %#v, %v, %v", first, ok, err)
		}
		if _, ok, err := receiveOutcome(ctx, stream, &readyReadsAfterCancel); err != nil || ok {
			t.Fatalf("closure after final outcome = ok %v, error %v", ok, err)
		}
	})

	t.Run("bounds continuously ready stream", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		stream := make(chan sut.InvocationOutcome, 3)
		stream <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "first"}}
		stream <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "second"}}
		stream <- sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "third"}}
		readyReadsAfterCancel := 2

		for _, workerID := range []string{"first", "second"} {
			outcome, ok, err := receiveOutcome(ctx, stream, &readyReadsAfterCancel)
			if err != nil || !ok || outcome.Worker == nil || outcome.Worker.WorkerID != workerID {
				t.Fatalf("ready outcome %q = %#v, %v, %v", workerID, outcome, ok, err)
			}
		}
		if _, ok, err := receiveOutcome(ctx, stream, &readyReadsAfterCancel); ok || !errors.Is(err, context.Canceled) {
			t.Fatalf("third ready outcome after cancellation = ok %v, error %v", ok, err)
		}
		if len(stream) != 1 {
			t.Fatalf("ready outcomes remaining = %d, want 1", len(stream))
		}
	})
}

func TestOutcomeAcceptsFaultOwnedEmptyStream(t *testing.T) {
	cause := errors.New("transaction completion unknown")
	a := &outcomeAdapter{}
	a.invoke = func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
		a.faults.Fail(cause)
		return outcomeStream(), nil
	}

	result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
	if !errors.Is(err, cause) {
		t.Fatalf("fault-owned empty stream error = %v, want fault cause", err)
	}
	if strings.Contains(err.Error(), "closed without a result") {
		t.Fatalf("fault-owned empty stream gained protocol error: %v", err)
	}
	assertNoProvisionalEvaluation(t, result)
}

func TestOutcomeRejectsTypedNilFaultSurface(t *testing.T) {
	var faults *notificationWithoutFault
	invoked := false
	a := &outcomeAdapter{
		faultSurface: faults,
		invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
			invoked = true
			return outcomeStream(), nil
		},
	}

	result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
	if err == nil || !strings.Contains(err.Error(), "nil fault surface") {
		t.Fatalf("typed-nil fault surface error = %v", err)
	}
	if invoked {
		t.Fatal("typed-nil fault surface reached invocation")
	}
	assertNoProvisionalEvaluation(t, result)
}

func TestOutcomeRejectsSessionFaultWithoutCause(t *testing.T) {
	faults := &faultWithoutCause{done: make(chan struct{})}
	invoked := false
	a := &outcomeAdapter{
		faultSurface: faults,
		invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
			invoked = true
			return outcomeStream(), nil
		},
	}

	result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
	if err == nil || !strings.Contains(err.Error(), "session fault has nil cause") {
		t.Fatalf("nil-cause session fault error = %v", err)
	}
	if invoked {
		t.Fatal("nil-cause session fault reached invocation")
	}
	assertNoProvisionalEvaluation(t, result)
}

func TestOutcomeSessionFaultBoundaries(t *testing.T) {
	for _, phase := range []string{"execution", "before_evaluation", "during_evaluation", "after_evaluation", "runtime_close"} {
		t.Run(phase, func(t *testing.T) {
			cause := errors.New("session transport failed")
			a := &outcomeAdapter{}
			r := &outcomeRuntime{Runtime: syncpoint.New()}
			var observer EventObserver
			called := false
			if phase == "execution" {
				a.invoke = func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
					a.faults.Fail(cause)
					return outcomeStream(), nil
				}
			}
			if phase == "before_evaluation" {
				observer = func(event Event) error {
					if event.Kind == EventScheduleComplete {
						a.faults.Fail(cause)
					}
					return nil
				}
			}
			if phase == "after_evaluation" {
				a.stop = func(context.Context) error { a.faults.Fail(cause); return nil }
			}
			if phase == "runtime_close" {
				r.afterClose = func() { a.faults.Fail(cause) }
			}
			result, err := runOutcomeTest(t, context.Background(), a, r, observer, oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
				called = true
				if phase == "during_evaluation" {
					a.faults.Fail(cause)
					<-ctx.Done() // The evaluator cannot proceed until the watcher observes the fault.
					if !errors.Is(context.Cause(ctx), cause) {
						t.Errorf("evaluation context cause = %v", context.Cause(ctx))
					}
				}
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "pass"})
			}))
			var fault *sut.SessionFault
			if !errors.Is(err, cause) || !errors.As(err, &fault) || fault != a.faults.Err() {
				t.Fatalf("fault lost: %v", err)
			}
			if called != (phase != "execution" && phase != "before_evaluation") {
				t.Fatalf("evaluation called = %v", called)
			}
			assertNoProvisionalEvaluation(t, result)
			if phase == "execution" && (len(result.Workers) != 0 || r.finishes.Load() != 0) {
				t.Fatal("session fault fabricated a worker terminal")
			}
		})
	}
	t.Log("SUT_SESSION_FAULT_RESULT before=observed during=cancelled after=invalidated cause=latched worker_result=never_fabricated")
}

func TestOutcomeRejectsFaultNotificationWithoutError(t *testing.T) {
	for _, phase := range []string{"after_execution", "during_evaluation"} {
		t.Run(phase, func(t *testing.T) {
			faults := &notificationWithoutFault{done: make(chan struct{})}
			a := &outcomeAdapter{faultSurface: faults}
			var observer EventObserver
			if phase == "after_execution" {
				observer = func(event Event) error {
					if event.Kind == EventScheduleComplete {
						close(faults.done)
					}
					return nil
				}
			}
			result, err := runOutcomeTest(t, context.Background(), a, nil, observer, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				if phase == "during_evaluation" {
					close(faults.done)
				}
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "must-not-finalize"})
			}))
			if err == nil || !strings.Contains(err.Error(), "notification closed without a latched error") {
				t.Fatalf("fault notification error = %v", err)
			}
			assertNoProvisionalEvaluation(t, result)
		})
	}
}

func TestSessionFaultErrorRechecksAfterNotification(t *testing.T) {
	cause := errors.New("fault published between observations")
	fault := &sut.SessionFault{Cause: cause}
	surface := &faultBetweenObservations{done: make(chan struct{}), fault: fault}

	if got := sessionFaultError(surface); got != fault {
		t.Fatalf("session fault = %v, want latched fault %v", got, fault)
	}
}

func TestOutcomeCleanupUsesRunFailureCause(t *testing.T) {
	observerErr := errors.New("reject invoked event")
	stream := make(chan sut.InvocationOutcome, 1)
	var invocationCtx context.Context
	a := &outcomeAdapter{invoke: func(ctx context.Context, _ string) (<-chan sut.InvocationOutcome, error) {
		invocationCtx = ctx
		return stream, nil
	}}
	a.stop = func(context.Context) error {
		<-invocationCtx.Done()
		stream <- sut.InvocationOutcome{Unstarted: &sut.UnstartedResult{
			WorkerID: "w1",
			Err:      context.Cause(invocationCtx),
		}}
		close(stream)
		return nil
	}

	result, err := runOutcomeTest(t, context.Background(), a, nil, func(event Event) error {
		if event.Kind == EventWorkerInvoked {
			return observerErr
		}
		return nil
	}, stableEvaluator)
	if !errors.Is(err, observerErr) {
		t.Fatalf("cleanup error = %v, want observer cause", err)
	}
	if errors.Is(err, context.Canceled) {
		t.Fatalf("cleanup cancellation escaped as caller interruption: %v", err)
	}
	if len(result.Unstarted) != 1 || !errors.Is(result.Unstarted[0].Err, observerErr) {
		t.Fatalf("cleanup unstarted evidence = %#v, want observer cause", result.Unstarted)
	}
}

func TestOutcomeKeepsExecutionContextActiveThroughSuccessfulStop(t *testing.T) {
	a := &outcomeAdapter{}
	a.stop = func(context.Context) error {
		if err := a.startCtx.Err(); err != nil {
			return fmt.Errorf("Start context canceled before successful Stop: %w", err)
		}
		return nil
	}

	result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
	if err != nil {
		t.Fatalf("successful cleanup = %v", err)
	}
	if len(result.Workers) != 1 {
		t.Fatalf("successful result workers = %#v", result.Workers)
	}
}

func TestOutcomeCancellationBoundaries(t *testing.T) {
	for _, phase := range []string{"collection", "evaluation", "stop", "runtime_close"} {
		t.Run(phase, func(t *testing.T) {
			cause := errors.New("caller canceled during " + phase)
			ctx, cancel := context.WithCancelCause(context.Background())
			defer cancel(nil)
			cancelWithCause := func() { cancel(cause) }
			a := &outcomeAdapter{}
			r := &outcomeRuntime{Runtime: syncpoint.New()}
			if phase == "collection" {
				r.afterFinish = cancelWithCause
			}
			if phase == "stop" {
				a.stop = func(context.Context) error { cancelWithCause(); return nil }
			}
			if phase == "runtime_close" {
				r.afterClose = cancelWithCause
			}
			result, err := runOutcomeTest(t, ctx, a, r, nil, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				if phase == "evaluation" {
					cancelWithCause()
				}
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "pass"})
			}))
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("Run error = %v", err)
			}
			if !errors.Is(err, cause) {
				t.Fatalf("Run error = %v, want custom cause %v", err, cause)
			}
			if len(result.Workers) != 1 || result.Workers[0].Err != nil || len(result.Terminals) != 1 || result.Terminals[0].State != TerminalStateDone {
				t.Fatalf("committed worker facts lost: %#v", result)
			}
			assertNoProvisionalEvaluation(t, result)
		})
	}
	t.Log("SUT_CANCEL_BOUNDARY_RESULT collection=observed evaluation=observed cleanup=observed committed_worker_error=nil run_error=context_cancelled custom_cause=preserved")
}

func TestOutcomeUnstartedAfterInvoke(t *testing.T) {
	for _, canceled := range []bool{false, true} {
		t.Run(map[bool]string{false: "initialization_failure", true: "cancel_before_accepted"}[canceled], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			cause := errors.New("initialization failed")
			if canceled {
				cause = context.Canceled
			}
			invoked := make(chan struct{})
			stream := make(chan sut.InvocationOutcome)
			producerDone := make(chan struct{})
			a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) { return stream, nil }}
			a.stop = func(context.Context) error { <-producerDone; return nil }
			go func() {
				defer close(producerDone)
				<-invoked // Invocation has returned and its collector is installed.
				if canceled {
					cancel()
				}
				stream <- sut.InvocationOutcome{Unstarted: &sut.UnstartedResult{WorkerID: "w1", Err: cause}}
				close(stream)
			}()
			r := &outcomeRuntime{Runtime: syncpoint.New()}
			result, err := runOutcomeTest(t, ctx, a, r, func(event Event) error {
				if event.Kind == EventWorkerInvoked {
					close(invoked)
				}
				return nil
			}, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				t.Error("unstarted command reached oracle")
				return oracle.Evaluation{}, nil
			}))
			var unstarted *sut.UnstartedResult
			if !canceled && errors.Is(err, context.Canceled) {
				t.Fatalf("internal wakeup escaped as operation cancellation: %v", err)
			}
			if !errors.Is(err, cause) || !errors.As(err, &unstarted) {
				t.Fatalf("unstarted cause lost: %v", err)
			}
			if len(result.Unstarted) != 1 || len(result.Workers) != 0 || len(result.Terminals) != 0 || r.finishes.Load() != 0 {
				t.Fatalf("fabricated terminal: %#v", result)
			}
		})
	}
	t.Log("SUT_UNSTARTED_RESULT invoke=returned cause=preserved stream=closed collected=true runtime_finish=never rollback=never")
}

func TestOutcomeErrorAggregation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	workerErr, evalErr, sessionErr, stopErr := errors.New("worker"), errors.New("evaluator"), errors.New("session"), errors.New("stop")
	a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
		return outcomeStream(sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "w1", Err: workerErr}}), nil
	}}
	a.stop = func(context.Context) error { a.faults.Fail(sessionErr); cancel(); return stopErr }
	result, err := runOutcomeTest(t, ctx, a, nil, nil, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
		return oracle.Evaluation{}, evalErr
	}))
	for _, cause := range []error{evalErr, sessionErr, stopErr, context.Canceled} {
		if !errors.Is(err, cause) {
			t.Errorf("missing %v from %v", cause, err)
		}
	}
	if len(result.Workers) != 1 || !errors.Is(result.Workers[0].Err, workerErr) || errors.Is(err, workerErr) {
		t.Fatalf("worker error boundary lost: %#v / %v", result, err)
	}
	assertNoProvisionalEvaluation(t, result)
	t.Log("SUT_OUTCOME_ERRORS_RESULT worker=evidence evaluation=joined session=joined cancellation=joined stop=joined provisional=discarded")
}

func TestOutcomeOrdersConcurrentCollectorErrorsByScenario(t *testing.T) {
	streams := map[string]chan sut.InvocationOutcome{
		"w1": make(chan sut.InvocationOutcome),
		"w2": make(chan sut.InvocationOutcome),
	}
	producerDone := make(chan struct{})
	a := &outcomeAdapter{}
	a.invoke = func(ctx context.Context, worker string) (<-chan sut.InvocationOutcome, error) {
		if worker == "w2" {
			go func() {
				defer close(producerDone)
				streams["w2"] <- sut.InvocationOutcome{}
				close(streams["w2"])
				<-ctx.Done()
				streams["w1"] <- sut.InvocationOutcome{}
				close(streams["w1"])
			}()
		}
		return streams[worker], nil
	}
	a.stop = func(context.Context) error {
		<-producerDone
		return nil
	}
	runtime := &collectorOrderingRuntime{Runtime: syncpoint.New()}
	value := scenario.Scenario{
		Name: "collector-order",
		Workers: []scenario.Worker{
			{ID: "w1", Command: "command"},
			{ID: "w2", Command: "command"},
		},
		SyncPoints: []string{"point"},
	}
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "point"},
		{Worker: "w2", Point: "point"},
	})
	if err != nil {
		t.Fatal(err)
	}
	o := newTestOrchestrator(t, Config{
		Fixture: &recordingFixture{}, DB: &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return a, nil },
		BlockInferenceTimeout: time.Second,
		StepTimeout:           time.Second,
		RunTimeout:            3 * time.Second,
		StopTimeout:           time.Second,
	})

	result, err := o.Run(context.Background(), value, schedule, stableEvaluator)
	if err == nil {
		t.Fatal("concurrent collector failures returned nil")
	}
	w1 := strings.Index(err.Error(), `worker "w1" must return exactly one outcome kind`)
	w2 := strings.Index(err.Error(), `worker "w2" must return exactly one outcome kind`)
	if w1 < 0 || w2 < 0 || w1 > w2 {
		t.Fatalf("collector error order = %v, want w1 before w2", err)
	}
	assertNoProvisionalEvaluation(t, result)
}

func TestOutcomeOrdersCollectorErrorsBeforeParentCancellation(t *testing.T) {
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	parentCause := errors.New("parent canceled before collectors finished")
	streams := map[string]chan sut.InvocationOutcome{
		"w1": make(chan sut.InvocationOutcome),
		"w2": make(chan sut.InvocationOutcome),
	}
	runtime := &parentCancellationOrderingRuntime{
		Runtime:         syncpoint.New(),
		w2Waiting:       make(chan struct{}),
		contextObserved: make(chan struct{}),
		collectorsReady: make(chan struct{}),
	}
	producerDone := make(chan struct{})
	a := &outcomeAdapter{}
	a.invoke = func(_ context.Context, worker string) (<-chan sut.InvocationOutcome, error) {
		if worker == "w2" {
			go func() {
				defer close(producerDone)
				<-runtime.w2Waiting
				cancel(parentCause)
				<-runtime.contextObserved
				streams["w2"] <- sut.InvocationOutcome{}
				close(streams["w2"])
				streams["w1"] <- sut.InvocationOutcome{}
				close(streams["w1"])
				close(runtime.collectorsReady)
			}()
		}
		return streams[worker], nil
	}
	a.stop = func(context.Context) error {
		<-producerDone
		return nil
	}
	value := scenario.Scenario{
		Name: "collector-parent-order",
		Workers: []scenario.Worker{
			{ID: "w1", Command: "command"},
			{ID: "w2", Command: "command"},
		},
		SyncPoints: []string{"point"},
	}
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{
		{Worker: "w1", Point: "point"},
		{Worker: "w2", Point: "point"},
	})
	if err != nil {
		t.Fatal(err)
	}
	o := newTestOrchestrator(t, Config{
		Fixture: &recordingFixture{}, DB: &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return a, nil },
		BlockInferenceTimeout: time.Second,
		StepTimeout:           time.Second,
		RunTimeout:            3 * time.Second,
		StopTimeout:           time.Second,
	})

	result, err := o.Run(ctx, value, schedule, stableEvaluator)
	if !errors.Is(err, parentCause) {
		t.Fatalf("parent cancellation cause lost: %v", err)
	}
	w1 := strings.Index(err.Error(), `worker "w1" must return exactly one outcome kind`)
	w2 := strings.Index(err.Error(), `worker "w2" must return exactly one outcome kind`)
	canceled := strings.Index(err.Error(), context.Canceled.Error())
	if w1 < 0 || w2 < 0 || canceled < 0 || w1 > w2 || w2 > canceled {
		t.Fatalf("error order = %v, want w1, w2, then operation cancellation", err)
	}
	assertNoProvisionalEvaluation(t, result)
}

type collectorOrderingRuntime struct {
	syncpoint.Runtime
}

type parentCancellationOrderingRuntime struct {
	syncpoint.Runtime
	w2Waiting       chan struct{}
	contextObserved chan struct{}
	collectorsReady chan struct{}
}

func (r *parentCancellationOrderingRuntime) WaitArrive(
	ctx context.Context,
	worker string,
	_ string,
	_ time.Duration,
) (syncpoint.ArriveStatus, error) {
	if worker == "w1" {
		return syncpoint.ArriveStatusTimeout, nil
	}
	close(r.w2Waiting)
	<-ctx.Done()
	close(r.contextObserved)
	<-r.collectorsReady
	return syncpoint.ArriveStatusUnknown, ctx.Err()
}

func (r *collectorOrderingRuntime) WaitArrive(
	ctx context.Context,
	worker string,
	_ string,
	_ time.Duration,
) (syncpoint.ArriveStatus, error) {
	if worker == "w1" {
		return syncpoint.ArriveStatusTimeout, nil
	}
	<-ctx.Done()
	return syncpoint.ArriveStatusUnknown, ctx.Err()
}

func TestOutcomeStreamValidation(t *testing.T) {
	worker := sut.InvocationOutcome{Worker: &sut.WorkerResult{WorkerID: "w1"}}
	for _, test := range []struct {
		name    string
		values  []sut.InvocationOutcome
		message string
	}{
		{"empty", nil, "closed without a result"},
		{"multiple", []sut.InvocationOutcome{worker, worker}, "more than one result"},
		{"missing_kind", []sut.InvocationOutcome{{}}, "exactly one outcome kind"},
		{"both_kinds", []sut.InvocationOutcome{{Worker: worker.Worker, Unstarted: &sut.UnstartedResult{WorkerID: "w1", Err: context.Canceled}}}, "exactly one outcome kind"},
		{"wrong_worker", []sut.InvocationOutcome{{Worker: &sut.WorkerResult{WorkerID: "wrong"}}}, "returned result for"},
		{"wrong_unstarted_worker", []sut.InvocationOutcome{{Unstarted: &sut.UnstartedResult{WorkerID: "wrong", Err: context.Canceled}}}, "invalid unstarted"},
		{"missing_cause", []sut.InvocationOutcome{{Unstarted: &sut.UnstartedResult{WorkerID: "w1"}}}, "invalid unstarted"},
	} {
		t.Run(test.name, func(t *testing.T) {
			a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
				return outcomeStream(test.values...), nil
			}}
			result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
			if err == nil || !strings.Contains(err.Error(), test.message) {
				t.Fatalf("error=%v, want %s", err, test.message)
			}
			assertNoProvisionalEvaluation(t, result)
		})
	}
	for _, closeStream := range []bool{false, true} {
		t.Run(map[bool]string{false: "unfinished", true: "close_before_evaluation"}[closeStream], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			stream := make(chan sut.InvocationOutcome, 1)
			stream <- worker
			finished := make(chan struct{})
			r := &outcomeRuntime{Runtime: syncpoint.New(), afterFinish: func() { close(finished) }}
			a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) { return stream, nil }}
			var evaluated atomic.Bool
			barrierDone := make(chan struct{})
			go func() {
				defer close(barrierDone)
				<-finished
				if evaluated.Load() {
					t.Error("evaluation before stream closure")
				}
				if closeStream {
					close(stream)
				} else {
					cancel()
				}
			}()
			result, err := runOutcomeTest(t, ctx, a, r, nil, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				evaluated.Store(true)
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "pass"})
			}))
			<-barrierDone
			if closeStream {
				if err != nil || !evaluated.Load() {
					t.Fatalf("closed stream: %v", err)
				}
			} else {
				if err == nil || !strings.Contains(err.Error(), "did not close before cleanup") || evaluated.Load() {
					t.Fatalf("unfinished stream: %v", err)
				}
				assertNoProvisionalEvaluation(t, result)
			}
		})
	}
	t.Run("finish_error_with_multiple_results", func(t *testing.T) {
		finishErr := errors.New("finish failed")
		a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
			return outcomeStream(worker, worker), nil
		}}
		r := &outcomeRuntime{Runtime: syncpoint.New(), finishErr: finishErr}
		result, err := runOutcomeTest(t, context.Background(), a, r, nil, stableEvaluator)
		if !errors.Is(err, finishErr) || !strings.Contains(err.Error(), "more than one result") {
			t.Fatalf("combined Finish/stream error = %v", err)
		}
		assertNoProvisionalEvaluation(t, result)
	})
	t.Run("second_outcome_cancels_before_stop", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		stream := make(chan sut.InvocationOutcome, 2)
		stream <- worker
		stream <- worker
		a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
			return stream, nil
		}}
		a.stop = func(context.Context) error {
			close(stream)
			return nil
		}

		result, err := runOutcomeTest(t, ctx, a, nil, nil, stableEvaluator)
		if err == nil || !strings.Contains(err.Error(), "more than one result") {
			t.Fatalf("multiplicity error = %v", err)
		}
		if errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("multiplicity waited for operation deadline: %v", err)
		}
		assertNoProvisionalEvaluation(t, result)
	})
	for _, test := range []struct {
		name      string
		first     sut.InvocationOutcome
		finishErr error
		want      string
	}{
		{
			name:  "malformed_first_outcome",
			first: sut.InvocationOutcome{},
			want:  "exactly one outcome kind",
		},
		{
			name:      "finish_error",
			first:     worker,
			finishErr: errors.New("finish failed before stream closure"),
			want:      "finish failed before stream closure",
		},
	} {
		t.Run(test.name+"_cancels_before_drain", func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), testStepTimeout)
			defer cancel()
			stop := make(chan struct{})
			producerDone := make(chan struct{})
			a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
				stream := make(chan sut.InvocationOutcome)
				go func() {
					defer close(producerDone)
					stream <- test.first
					<-stop
					close(stream)
				}()
				return stream, nil
			}}
			a.stop = func(context.Context) error {
				close(stop)
				<-producerDone
				return nil
			}
			r := &outcomeRuntime{Runtime: syncpoint.New(), finishErr: test.finishErr}

			result, err := runOutcomeTest(t, ctx, a, r, nil, stableEvaluator)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("first outcome error = %v, want %q", err, test.want)
			}
			if errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("first outcome error waited for run cleanup: %v", err)
			}
			assertNoProvisionalEvaluation(t, result)
		})
	}
	t.Run("malformed_with_multiple_results", func(t *testing.T) {
		producerDone := make(chan struct{})
		a := &outcomeAdapter{invoke: func(context.Context, string) (<-chan sut.InvocationOutcome, error) {
			stream := make(chan sut.InvocationOutcome)
			go func() {
				defer close(producerDone)
				stream <- sut.InvocationOutcome{}
				stream <- worker
				stream <- worker
				close(stream)
			}()
			return stream, nil
		}}
		a.stop = func(ctx context.Context) error {
			select {
			case <-producerDone:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		result, err := runOutcomeTest(t, context.Background(), a, nil, nil, stableEvaluator)
		if !strings.Contains(err.Error(), "exactly one outcome kind") || !strings.Contains(err.Error(), "more than one result") {
			t.Fatalf("combined shape/stream error = %v", err)
		}
		select {
		case <-producerDone:
		default:
			t.Fatal("malformed unbuffered producer remained blocked after Stop")
		}
		assertNoProvisionalEvaluation(t, result)
	})
	t.Log("SUT_OUTCOME_STREAM_RESULT empty=rejected multiple=rejected malformed=rejected unfinished=rejected closure=before_evaluation")
}

func TestOutcomeEvaluationCancellationAggregation(t *testing.T) {
	sessionErr := errors.New("session failed during evaluation")
	evalErr := &evaluationFailure{cause: errors.New("independent evaluator failure")}
	a := &outcomeAdapter{}
	result, err := runOutcomeTest(t, context.Background(), a, nil, nil, oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
		a.faults.Fail(sessionErr)
		<-ctx.Done()
		return oracle.Evaluation{}, errors.Join(ctx.Err(), evalErr)
	}))
	var typed *evaluationFailure
	if !errors.Is(err, sessionErr) || !errors.Is(err, evalErr) || !errors.As(err, &typed) || typed != evalErr {
		t.Fatalf("independent evaluation failure lost: %v", err)
	}
	if errors.Is(err, context.Canceled) {
		t.Fatalf("internal wakeup became caller cancellation: %v", err)
	}
	assertNoProvisionalEvaluation(t, result)
}

type evaluationFailure struct{ cause error }

func (e *evaluationFailure) Error() string { return e.cause.Error() }
func (e *evaluationFailure) Unwrap() error { return e.cause }
