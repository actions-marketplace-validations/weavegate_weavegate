package orchestrator

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"time"

	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
	"github.com/weavegate/weavegate/internal/trace"
)

// RunResult contains the stable single-run evidence, Oracle evaluation, and
// normalized fingerprint needed by the replay layer.
type RunResult struct {
	ScheduleID      string
	Steps           int
	Workers         []sut.WorkerResult
	Unstarted       []sut.UnstartedResult
	Terminals       trace.Terminals
	Trace           trace.Trace
	Evaluation      oracle.Evaluation
	Timeouts        int
	PendingResolved int
	Fingerprint     string
	Elapsed         time.Duration
}

type collectedResult struct {
	result    sut.WorkerResult
	unstarted *sut.UnstartedResult
	err       error
}

type workerExecution struct {
	worker          scenario.Worker
	result          sut.WorkerResult
	unstarted       *sut.UnstartedResult
	collectionErr   error
	collected       bool
	terminal        bool
	terminalState   TerminalState
	failureClass    WorkerFailureClass
	collectedResult <-chan collectedResult
}

type runCoordinator struct {
	ctx      context.Context
	cancel   context.CancelCauseFunc
	runtime  syncpoint.Runtime
	handle   sut.Handle
	faults   sut.SessionFaults
	value    scenario.Scenario
	schedule scenario.Schedule
	result   *RunResult
	trace    *traceRecorder

	collectorsContext context.Context
	collectorsWait    *sync.WaitGroup
	blockTimeout      time.Duration
	stepTimeout       time.Duration

	executions  map[string]*workerExecution
	firstSteps  map[string]int
	preObserved map[int]bool
	pending     map[int]bool

	rebuildCollectionErrors bool
}

// Run resets the fixture and executes one saved control schedule. Worker
// failures remain terminal run data; orchestration, Oracle, protocol, and
// cleanup failures are returned as errors.
func (o *Orchestrator) Run(
	ctx context.Context,
	value scenario.Scenario,
	schedule scenario.Schedule,
	evaluator oracle.Evaluator,
) (result RunResult, returnErr error) {
	startedAt := time.Now()
	result = RunResult{
		ScheduleID: schedule.ID,
		Steps:      len(schedule.Steps),
	}
	defer func() {
		result.Elapsed = time.Since(startedAt)
	}()

	if ctx == nil {
		return result, errors.New("run schedule: context is required")
	}
	defer func() {
		// Observe the operation context on every return path, including the run
		// input validation, run gate, and fixture reset before adapter finalization
		// is installed.
		returnErr = joinObservedContextError(returnErr, ctx)
		if returnErr != nil {
			result.Evaluation = oracle.Evaluation{}
			result.Fingerprint = ""
		}
	}()
	if isNilEvaluator(evaluator) {
		return result, errors.New("run schedule: Oracle evaluator is required")
	}
	select {
	case <-ctx.Done():
		return result, fmt.Errorf("run schedule %q: wait for active run: %w", schedule.ID, ctx.Err())
	case <-o.runGate:
	}
	defer func() {
		o.runGate <- struct{}{}
	}()
	if err := scenario.Validate(value, schedule); err != nil {
		return result, fmt.Errorf("run schedule %q: %w", schedule.ID, err)
	}
	if err := o.config.Fixture.Ready(o.config.DB); err != nil {
		return result, fmt.Errorf("run schedule %q: %w", schedule.ID, NewFixtureError(err))
	}
	trace := newTraceRecorder(o.config.OnEvent)
	defer func() {
		result.Trace = trace.clone()
	}()

	runCtx, cancelRun := context.WithTimeout(ctx, o.config.RunTimeout)
	defer cancelRun()
	defer func() {
		// This boundary is installed before fixture reset and executes after the
		// full adapter/runtime cleanup defer when that later boundary exists.
		returnErr = joinObservedContextError(returnErr, runCtx)
	}()
	executionCtx, cancelExecution := context.WithCancelCause(runCtx)
	defer cancelExecution(nil)
	if err := o.config.Fixture.Reset(runCtx); err != nil {
		return result, fmt.Errorf("run schedule %q: reset fixture: %w", schedule.ID, NewFixtureError(err))
	}
	if err := trace.emit(Event{Kind: EventFixtureReset, Step: -1}); err != nil {
		return result, fmt.Errorf("run schedule %q: %w", schedule.ID, err)
	}

	runtime := o.config.NewRuntime()
	if runtime == nil {
		return result, fmt.Errorf("run schedule %q: runtime factory returned nil", schedule.ID)
	}

	var (
		adapter        sut.Adapter
		coordinator    *runCoordinator
		faults         sut.SessionFaults
		stopWatcher    func()
		collectorsWait sync.WaitGroup
	)
	collectorsCtx, cancelCollectors := context.WithCancel(context.WithoutCancel(runCtx))
	defer func() {
		// Abort outstanding commands with the run failure that triggered cleanup,
		// but keep a successful session active through Stop and keep collectors
		// alive in both cases.
		if returnErr != nil {
			cancelExecution(returnErr)
		}
		var stopErr error
		if adapter != nil {
			stopCtx, cancelStop := context.WithTimeout(
				context.WithoutCancel(ctx),
				o.config.StopTimeout,
			)
			stopErr = adapter.Stop(stopCtx)
			cancelStop()
		}
		cancelCollectors()
		collectorsWait.Wait()
		if coordinator != nil {
			collectionErr := coordinator.finalizeEvidence()
			if coordinator.rebuildCollectionErrors && collectionErr != nil {
				returnErr = fmt.Errorf("run schedule %q: %w", schedule.ID, collectionErr)
			} else {
				returnErr = joinRunError(returnErr, collectionErr)
			}
		}
		if stopErr != nil {
			returnErr = joinRunError(returnErr, fmt.Errorf("run schedule %q: stop adapter: %w", schedule.ID, stopErr))
		}
		runtime.Close()
		if stopWatcher != nil {
			stopWatcher()
		}
		// Observe session faults after all cleanup work. The run- and operation-
		// context boundaries run after this defer.
		if faults != nil {
			faultErr := sessionFaultError(faults)
			returnErr = joinRunError(returnErr, faultErr)
			if stopErr != nil || faultErr != nil {
				o.config.Fixture.Quarantine(errors.Join(stopErr, faultErr))
			}
		} else if stopErr != nil {
			o.config.Fixture.Quarantine(stopErr)
		}
	}()

	created, err := o.config.NewAdapter(runCtx, runtime)
	if err != nil {
		return result, fmt.Errorf("run schedule %q: create adapter: %w", schedule.ID, err)
	}
	if created == nil {
		return result, fmt.Errorf("run schedule %q: adapter factory returned nil", schedule.ID)
	}
	adapter = created
	handle, err := adapter.Start(executionCtx, value.Clone().SUTConfig, o.config.DB)
	if err != nil {
		return result, fmt.Errorf("run schedule %q: start adapter: %w", schedule.ID, err)
	}
	if handle == nil {
		return result, fmt.Errorf("run schedule %q: adapter start returned nil handle", schedule.ID)
	}

	candidateFaults := handle.Faults()
	if isNilInterface(candidateFaults) || candidateFaults.Done() == nil {
		return result, fmt.Errorf("run schedule %q: adapter start returned nil fault surface", schedule.ID)
	}
	faults = candidateFaults
	stopWatcher = watchSessionFaults(faults, cancelExecution)
	if fault := sessionFaultError(faults); fault != nil {
		return result, fault
	}

	coordinator = &runCoordinator{
		ctx:               executionCtx,
		cancel:            cancelExecution,
		runtime:           runtime,
		handle:            handle,
		faults:            faults,
		value:             value.Clone(),
		schedule:          schedule.Clone(),
		result:            &result,
		trace:             trace,
		collectorsContext: collectorsCtx,
		collectorsWait:    &collectorsWait,
		blockTimeout:      o.config.BlockInferenceTimeout,
		stepTimeout:       o.config.StepTimeout,
		executions:        make(map[string]*workerExecution, len(value.Workers)),
		firstSteps:        make(map[string]int, len(value.Workers)),
		preObserved:       make(map[int]bool),
		pending:           make(map[int]bool),
	}
	if err := coordinator.execute(); err != nil {
		coordinator.rebuildCollectionErrors = solelyWrapsCollectorFailure(err) ||
			solelyWraps(err, executionCtx.Err())
		return result, fmt.Errorf("run schedule %q: %w", schedule.ID, executionError(executionCtx, err))
	}

	if fault := sessionFaultError(faults); fault != nil {
		return result, fault
	}
	if err := runCtx.Err(); err != nil {
		return result, err
	}
	result.Trace = trace.clone()
	evaluated, err := evaluator.Evaluate(executionCtx, o.config.DB.SQL, oracle.RunContext{
		Trace:     result.Trace.Clone(),
		Terminals: result.Terminals.Clone(),
	})
	if err != nil {
		return result, fmt.Errorf("run schedule %q: evaluate oracles: %w", schedule.ID, executionError(executionCtx, err))
	}
	if fault := sessionFaultError(faults); fault != nil {
		return result, fault
	}
	if err := runCtx.Err(); err != nil {
		return result, err
	}
	validated, err := oracle.ValidateEvaluation(evaluated)
	if err != nil {
		return result, fmt.Errorf("run schedule %q: invalid Oracle evaluation: %w", schedule.ID, err)
	}
	result.Evaluation = validated
	result.Fingerprint, err = normalizedFingerprint(result)
	if err != nil {
		return result, fmt.Errorf("run schedule %q: fingerprint: %w", schedule.ID, err)
	}
	return result, nil
}

// joinObservedContextError treats the Err read as the operation boundary. Cause
// belongs to the same cancellation snapshot only when Err already observed the
// canceled state; cancellation after an active observation belongs to the caller.
func joinObservedContextError(current error, ctx context.Context) error {
	err := ctx.Err()
	if err == nil {
		return current
	}
	current = joinRunError(current, err)
	return joinRunError(current, context.Cause(ctx))
}

func isNilEvaluator(evaluator oracle.Evaluator) bool {
	return isNilInterface(evaluator)
}

func isNilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map,
		reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func (r *runCoordinator) execute() error {
	for index, step := range r.schedule.Steps {
		if _, exists := r.firstSteps[step.Worker]; !exists {
			r.firstSteps[step.Worker] = index
		}
	}

	for _, worker := range r.value.Workers {
		if err := r.runtime.Register(worker.ID); err != nil {
			return fmt.Errorf("register worker %q: %w", worker.ID, err)
		}
		r.executions[worker.ID] = &workerExecution{worker: worker}
		if err := r.trace.emit(Event{
			Kind:   EventWorkerRegistered,
			Step:   -1,
			Worker: worker.ID,
		}); err != nil {
			return err
		}
	}

	for _, worker := range r.value.Workers {
		if err := r.invoke(worker); err != nil {
			return err
		}
		stepIndex := r.firstSteps[worker.ID]
		if err := r.bootstrap(stepIndex); err != nil {
			return err
		}
	}

	// Traverse every saved intent before draining any deferred work. This keeps
	// untraversed steps as ordering barriers while allowing an unavailable intent
	// to remain pending instead of blocking later workers.
	for index := range r.schedule.Steps {
		if err := r.traverse(index); err != nil {
			return err
		}
	}

	// Once the saved frontier is fully traversed, pending intents may realize in
	// a different order. Each pass still scans saved indices in ascending order.
	for len(r.pending) > 0 {
		progressed, err := r.drainPending()
		if err != nil {
			return err
		}
		if !progressed {
			return fmt.Errorf(
				"no progress while %d coordination step(s) remain pending",
				len(r.pending),
			)
		}
	}

	workers := make([]sut.WorkerResult, 0, len(r.value.Workers))
	terminals := make(trace.Terminals, 0, len(r.value.Workers))
	for _, worker := range r.value.Workers {
		execution := r.executions[worker.ID]
		if !execution.collected {
			if err := r.collect(worker.ID, -1); err != nil {
				return err
			}
		}
		workers = append(workers, execution.result)
		terminals = append(terminals, WorkerTerminal{
			Worker:       worker.ID,
			State:        execution.terminalState,
			FailureClass: execution.failureClass,
		})
	}
	r.result.Workers = workers
	r.result.Terminals = terminals
	return r.trace.emit(Event{Kind: EventScheduleComplete, Step: -1})
}

func (r *runCoordinator) invoke(worker scenario.Worker) error {
	results, err := r.handle.Invoke(r.ctx, worker.ID, worker.Command)
	if err != nil {
		return fmt.Errorf("invoke worker %q command %q: %w", worker.ID, worker.Command, err)
	}
	if results == nil {
		return fmt.Errorf("invoke worker %q command %q: nil result channel", worker.ID, worker.Command)
	}

	collected := make(chan collectedResult, 1)
	r.executions[worker.ID].collectedResult = collected
	r.collectorsWait.Add(1)
	go r.collectInvocation(worker.ID, results, collected)
	return r.trace.emit(Event{
		Kind:   EventWorkerInvoked,
		Step:   -1,
		Worker: worker.ID,
	})
}

func (r *runCoordinator) bootstrap(index int) error {
	step := r.schedule.Steps[index]
	status, err := r.runtime.WaitArrive(
		r.ctx,
		step.Worker,
		step.Point,
		r.configuredBlockTimeout(),
	)
	if err != nil {
		return fmt.Errorf("bootstrap worker %q at %q: %w", step.Worker, step.Point, err)
	}
	return r.recordObservation(index, status, true)
}

func (r *runCoordinator) traverse(index int) error {
	step := r.schedule.Steps[index]
	execution := r.executions[step.Worker]
	if execution.terminal {
		return r.emitTerminalSkipped(index)
	}
	if r.pending[index] {
		return nil
	}
	if r.hasPendingPredecessor(index, step.Worker) {
		r.pending[index] = true
		return nil
	}
	if r.preObserved[index] {
		delete(r.preObserved, index)
		return r.release(index)
	}

	status, err := r.runtime.WaitArrive(r.ctx, step.Worker, step.Point, r.configuredStepTimeout())
	if err != nil {
		return fmt.Errorf("wait for step[%d] worker %q at %q: %w", index, step.Worker, step.Point, err)
	}
	return r.recordObservation(index, status, false)
}

func (r *runCoordinator) hasPendingPredecessor(index int, workerID string) bool {
	for pendingIndex := range r.pending {
		if pendingIndex < index && r.schedule.Steps[pendingIndex].Worker == workerID {
			return true
		}
	}
	return false
}

func (r *runCoordinator) recordObservation(index int, status syncpoint.ArriveStatus, bootstrap bool) error {
	step := r.schedule.Steps[index]
	switch status {
	case syncpoint.ArriveStatusArrived:
		if err := r.trace.emit(Event{
			Kind:   EventPointArrived,
			Step:   index,
			Worker: step.Worker,
			Point:  step.Point,
			Status: ControlStatusArrived,
		}); err != nil {
			return err
		}
		if bootstrap {
			r.preObserved[index] = true
			return nil
		}
		return r.release(index)
	case syncpoint.ArriveStatusTimeout:
		if err := r.pollCollector(step.Worker, index); err != nil {
			return err
		}
		if r.executions[step.Worker].terminal {
			if bootstrap {
				return nil
			}
			return r.emitTerminalSkipped(index)
		}
		if err := r.emitTimeout(index); err != nil {
			return err
		}
		r.pending[index] = true
		return nil
	case syncpoint.ArriveStatusDone, syncpoint.ArriveStatusFailed:
		if err := r.collect(step.Worker, index); err != nil {
			return err
		}
		if !bootstrap {
			if err := r.emitTerminalSkipped(index); err != nil {
				return err
			}
		}
		return nil
	case syncpoint.ArriveStatusUnknown:
		return fmt.Errorf(
			"step[%d] worker %q at %q returned unknown arrival status",
			index,
			step.Worker,
			step.Point,
		)
	default:
		return fmt.Errorf(
			"step[%d] worker %q at %q returned unsupported arrival status %d",
			index,
			step.Worker,
			step.Point,
			status,
		)
	}
}

func (r *runCoordinator) release(index int) error {
	step := r.schedule.Steps[index]
	if err := r.runtime.Release(r.ctx, step.Worker, step.Point); err != nil {
		return fmt.Errorf("release step[%d] worker %q at %q: %w", index, step.Worker, step.Point, err)
	}
	if err := r.trace.emit(Event{
		Kind:   EventPointReleased,
		Step:   index,
		Worker: step.Worker,
		Point:  step.Point,
		Status: ControlStatusReleased,
	}); err != nil {
		return err
	}
	return nil
}

func (r *runCoordinator) drainPending() (bool, error) {
	progressed := false
	for index := range r.schedule.Steps {
		if !r.pending[index] {
			continue
		}

		step := r.schedule.Steps[index]
		execution := r.executions[step.Worker]
		if execution.terminal {
			if err := r.emitTerminalSkipped(index); err != nil {
				return progressed, err
			}
			delete(r.pending, index)
			progressed = true
			continue
		}

		status, err := r.runtime.WaitArrive(r.ctx, step.Worker, step.Point, r.configuredStepTimeout())
		if err != nil {
			return progressed, fmt.Errorf(
				"recheck pending step[%d] worker %q at %q: %w",
				index,
				step.Worker,
				step.Point,
				err,
			)
		}
		switch status {
		case syncpoint.ArriveStatusArrived:
			if err := r.trace.emit(Event{
				Kind:   EventPointArrived,
				Step:   index,
				Worker: step.Worker,
				Point:  step.Point,
				Status: ControlStatusArrived,
			}); err != nil {
				return progressed, err
			}
			delete(r.pending, index)
			if err := r.releasePending(index); err != nil {
				return progressed, err
			}
			r.result.PendingResolved++
			progressed = true
		case syncpoint.ArriveStatusDone, syncpoint.ArriveStatusFailed:
			if err := r.collect(step.Worker, index); err != nil {
				return progressed, err
			}
			if err := r.emitTerminalSkipped(index); err != nil {
				return progressed, err
			}
			delete(r.pending, index)
			progressed = true
		case syncpoint.ArriveStatusTimeout:
			if err := r.pollCollector(step.Worker, index); err != nil {
				return progressed, err
			}
			if execution.terminal {
				if err := r.emitTerminalSkipped(index); err != nil {
					return progressed, err
				}
				delete(r.pending, index)
				progressed = true
				continue
			}
			if err := r.emitTimeout(index); err != nil {
				return progressed, err
			}
		case syncpoint.ArriveStatusUnknown:
			return progressed, fmt.Errorf(
				"pending step[%d] worker %q at %q returned unknown arrival status",
				index,
				step.Worker,
				step.Point,
			)
		default:
			return progressed, fmt.Errorf(
				"pending step[%d] worker %q at %q returned unsupported arrival status %d",
				index,
				step.Worker,
				step.Point,
				status,
			)
		}
	}
	return progressed, nil
}

func (r *runCoordinator) releasePending(index int) error {
	step := r.schedule.Steps[index]
	if err := r.runtime.Release(r.ctx, step.Worker, step.Point); err != nil {
		return fmt.Errorf(
			"release pending step[%d] worker %q at %q: %w",
			index,
			step.Worker,
			step.Point,
			err,
		)
	}
	return r.trace.emit(Event{
		Kind:   EventPointReleased,
		Step:   index,
		Worker: step.Worker,
		Point:  step.Point,
		Status: ControlStatusReleased,
	})
}

func (r *runCoordinator) collect(workerID string, step int) error {
	execution := r.executions[workerID]
	if execution.collected {
		return nil
	}
	if execution.collectedResult == nil {
		return fmt.Errorf("collect worker %q before invocation", workerID)
	}

	select {
	case collected, ok := <-execution.collectedResult:
		if !ok {
			return fmt.Errorf("collect worker %q: collector closed without a result", workerID)
		}
		return r.acceptCollected(execution, collected, step)
	case <-r.ctx.Done():
		return fmt.Errorf("collect worker %q: %w", workerID, r.ctx.Err())
	}
}

func (r *runCoordinator) pollCollector(workerID string, step int) error {
	execution := r.executions[workerID]
	if execution.collected || execution.collectedResult == nil {
		return nil
	}

	select {
	case collected, ok := <-execution.collectedResult:
		if !ok {
			return fmt.Errorf("collect worker %q: collector closed without a result", workerID)
		}
		return r.acceptCollected(execution, collected, step)
	default:
		return nil
	}
}

func (r *runCoordinator) emitTerminal(execution *workerExecution, step int) error {
	execution.failureClass = ClassifyWorkerFailure(execution.result.Err)
	event := Event{
		Step:         step,
		Worker:       execution.worker.ID,
		FailureClass: execution.failureClass,
	}
	if execution.result.Err == nil {
		execution.terminalState = TerminalStateDone
		event.Kind = EventWorkerDone
	} else {
		execution.terminalState = TerminalStateFailed
		event.Kind = EventWorkerFailed
	}
	return r.trace.emit(event)
}

func (r *runCoordinator) emitTimeout(index int) error {
	step := r.schedule.Steps[index]
	r.result.Timeouts++
	return r.trace.emit(Event{
		Kind:         EventPointTimeout,
		Step:         index,
		Worker:       step.Worker,
		Point:        step.Point,
		Status:       ControlStatusTimeoutInferred,
		FailureClass: WorkerFailureNone,
	})
}

func (r *runCoordinator) emitTerminalSkipped(index int) error {
	step := r.schedule.Steps[index]
	return r.trace.emit(Event{
		Kind:   EventStepTerminalSkipped,
		Step:   index,
		Worker: step.Worker,
		Point:  step.Point,
		Status: ControlStatusTerminalSkipped,
	})
}

func (r *runCoordinator) configuredBlockTimeout() time.Duration {
	return r.blockTimeout
}

func (r *runCoordinator) configuredStepTimeout() time.Duration {
	return r.stepTimeout
}
