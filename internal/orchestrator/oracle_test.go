package orchestrator

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

func TestRunRejectsInvalidOracleEvaluationsAndCleansUp(t *testing.T) {
	valid, err := oracle.NewEvaluation(oracle.OracleResult{OracleID: "valid-pass"})
	if err != nil {
		t.Fatalf("create valid evaluation: %v", err)
	}
	tests := []struct {
		name       string
		evaluation oracle.Evaluation
	}{
		{name: "empty results", evaluation: oracle.Evaluation{Fingerprint: strings.Repeat("0", 64)}},
		{
			name: "missing Oracle ID",
			evaluation: oracle.Evaluation{
				Results:     []oracle.OracleResult{{OracleID: ""}},
				Fingerprint: strings.Repeat("0", 64),
			},
		},
		{
			name: "mismatched violation ID",
			evaluation: oracle.Evaluation{
				Results: []oracle.OracleResult{
					{
						OracleID: "expected",
						Violations: []oracle.Violation{
							{OracleID: "other", Kind: oracle.KindAssertion, Rows: []oracle.Row{}},
						},
					},
				},
				Fingerprint: strings.Repeat("0", 64),
			},
		},
		{
			name: "invalid evidence value",
			evaluation: oracle.Evaluation{
				Results: []oracle.OracleResult{
					{
						OracleID: "invalid-value",
						Violations: []oracle.Violation{
							{
								OracleID: "invalid-value",
								Kind:     oracle.KindAssertion,
								Rows:     []oracle.Row{{"unsupported": int(1)}},
							},
						},
					},
				},
				Fingerprint: strings.Repeat("0", 64),
			},
		},
		{name: "fingerprint mismatch", evaluation: func() oracle.Evaluation {
			value := valid
			value.Fingerprint = strings.Repeat("0", 64)
			return value
		}()},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			fixtureRunner := &recordingFixture{}
			runtime := newRuntimeProbe()
			adapter := newScriptedAdapter(runtime)
			executor := newTestOrchestrator(t, Config{
				Fixture:               fixtureRunner,
				DB:                    &fixture.DB{},
				NewRuntime:            func() syncpoint.Runtime { return runtime },
				NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
				BlockInferenceTimeout: testBlockTimeout,
				StepTimeout:           testStepTimeout,
				RunTimeout:            testRunTimeout,
				StopTimeout:           testStopTimeout,
			})

			result, err := executor.Run(
				context.Background(),
				matchingScenario(),
				matchingSchedule(t),
				oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
					return test.evaluation, nil
				}),
			)
			if err == nil || !strings.Contains(err.Error(), "invalid Oracle evaluation") {
				t.Fatalf("invalid evaluation error = %v", err)
			}
			if !reflect.DeepEqual(result.Evaluation, oracle.Evaluation{}) || result.Fingerprint != "" {
				t.Fatalf("invalid evaluation stored partial result: %#v/%q", result.Evaluation, result.Fingerprint)
			}
			if fixtureRunner.resetCalls != 1 || adapter.stopCalls.Load() != 1 || runtime.closeCalls.Load() != 1 {
				t.Fatalf(
					"invalid evaluation cleanup reset/stop/close = %d/%d/%d, want 1/1/1",
					fixtureRunner.resetCalls,
					adapter.stopCalls.Load(),
					runtime.closeCalls.Load(),
				)
			}
		})
	}
}

func TestRunRejectsTypedNilOracleBeforeReset(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	executor := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            syncpoint.New,
		NewAdapter:            func(client syncpoint.Client) (sut.Adapter, error) { return newScriptedAdapter(client), nil },
		BlockInferenceTimeout: testBlockTimeout,
		StepTimeout:           testStepTimeout,
		RunTimeout:            testRunTimeout,
		StopTimeout:           testStopTimeout,
	})
	var evaluator *oracle.Set
	_, err := executor.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		evaluator,
	)
	if err == nil || !strings.Contains(err.Error(), "Oracle evaluator is required") {
		t.Fatalf("typed nil evaluator error = %v", err)
	}
	if fixtureRunner.resetCalls != 0 {
		t.Fatalf("typed nil evaluator resets = %d, want 0", fixtureRunner.resetCalls)
	}
}

func TestRunOracleEvaluationUsesRemainingDeadlineAndCleansUp(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	runtime := newRuntimeProbe()
	adapter := newScriptedAdapter(runtime)
	executor := newTestOrchestrator(t, Config{
		Fixture:               fixtureRunner,
		DB:                    &fixture.DB{},
		NewRuntime:            func() syncpoint.Runtime { return runtime },
		NewAdapter:            func(syncpoint.Client) (sut.Adapter, error) { return adapter, nil },
		BlockInferenceTimeout: 5 * time.Millisecond,
		StepTimeout:           20 * time.Millisecond,
		RunTimeout:            100 * time.Millisecond,
		StopTimeout:           testStopTimeout,
	})
	startedAt := time.Now()
	_, err := executor.Run(
		context.Background(),
		matchingScenario(),
		matchingSchedule(t),
		oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
			<-ctx.Done()
			return oracle.Evaluation{}, ctx.Err()
		}),
	)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("deadline evaluator error = %v, want context.DeadlineExceeded", err)
	}
	if elapsed := time.Since(startedAt); elapsed >= time.Second {
		t.Fatalf("deadline evaluator elapsed = %s, want under 1s", elapsed)
	}
	if adapter.stopCalls.Load() != 1 || runtime.closeCalls.Load() != 1 {
		t.Fatalf("deadline cleanup stop/close = %d/%d, want 1/1", adapter.stopCalls.Load(), runtime.closeCalls.Load())
	}
}

func TestReplayWaitsForEvaluationBeforeNextReset(t *testing.T) {
	fixtureRunner := &recordingFixture{}
	executor := newReplayTestOrchestrator(t, fixtureRunner, nil)
	firstEvaluation := make(chan struct{})
	releaseFirst := make(chan struct{})
	var evaluations atomic.Int32
	evaluator := oracle.EvaluatorFunc(func(
		context.Context,
		oracle.DB,
		oracle.RunContext,
	) (oracle.Evaluation, error) {
		if evaluations.Add(1) == 1 {
			close(firstEvaluation)
			<-releaseFirst
		}
		return oracle.NewEvaluation(oracle.OracleResult{OracleID: "stable-pass"})
	})

	type replayResponse struct {
		result ReplayResult
		err    error
	}
	completed := make(chan replayResponse, 1)
	go func() {
		result, err := executor.Replay(
			context.Background(),
			matchingScenario(),
			matchingSchedule(t),
			2,
			evaluator,
		)
		completed <- replayResponse{result: result, err: err}
	}()

	select {
	case <-firstEvaluation:
	case <-time.After(testRunTimeout):
		t.Fatal("first Oracle evaluation did not start")
	}
	if fixtureRunner.resetCalls != 1 {
		t.Fatalf("resets before first evaluation completed = %d, want 1", fixtureRunner.resetCalls)
	}
	close(releaseFirst)
	select {
	case response := <-completed:
		if response.err != nil {
			t.Fatalf("replay after evaluation release: %v", response.err)
		}
		if len(response.result.Runs) != 2 {
			t.Fatalf("replay runs = %d, want 2", len(response.result.Runs))
		}
	case <-time.After(testRunTimeout):
		t.Fatal("replay did not finish after evaluation release")
	}
	if fixtureRunner.resetCalls != 2 || evaluations.Load() != 2 {
		t.Fatalf("final resets/evaluations = %d/%d, want 2/2", fixtureRunner.resetCalls, evaluations.Load())
	}
}
