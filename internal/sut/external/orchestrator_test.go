package external

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

// Only provisioning is stubbed: the real orchestrator owns runtime Finish,
// evaluation invalidation, cancellation, result collection and final Stop.
type preparedAdapter struct{ *adapter }

func (a preparedAdapter) Start(ctx context.Context, _ sut.SUTConfig, _ *fixture.DB) (sut.Handle, error) {
	return a.start(ctx, startBody())
}

type idleFixture struct{}

func (idleFixture) Reset(context.Context) error    { return nil }
func (idleFixture) Ready(*fixture.DB) error        { return nil }
func (idleFixture) Quarantine(error)               {}
func (idleFixture) Teardown(context.Context) error { return nil }

func TestOrchestratorLateFaultAndFingerprint(t *testing.T) {
	var healthyFingerprint string
	for _, mode := range []string{"healthy", "healthy_repeat", "wire_fatal", "process_death", "context_cancel"} {
		t.Run(mode, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.a.id = randomID
				evaluating, returnEvaluation := make(chan context.Context, 1), make(chan struct{})
				eval := oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
					evaluating <- ctx
					<-returnEvaluation
					return oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
				})
				o, err := orchestrator.New(orchestrator.Config{Fixture: idleFixture{}, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
					NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
						p.a.client = client
						return preparedAdapter{p.a}, nil
					},
					BlockInferenceTimeout: time.Second, StepTimeout: time.Second, RunTimeout: 20 * time.Second, StopTimeout: 5 * time.Second})
				if err != nil {
					t.Fatal(err)
				}
				schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "after_read"}})
				if err != nil {
					t.Fatal(err)
				}
				value := scenario.Scenario{Name: "external-lifecycle", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				type runResult struct {
					result orchestrator.RunResult
					err    error
				}
				done := make(chan runResult, 1)
				go func() { r, e := o.Run(ctx, value, schedule, eval); done <- runResult{r, e} }()
				p.read("start")
				p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
				w := p.read("invoke")
				p.send("accepted", binding(w))
				p.send("arrive", arrivalBody(w, 1, "after_read"))
				p.read("release")
				p.send("terminal", terminalBody(w, "committed", nil))
				evalCtx := <-evaluating
				switch mode {
				case "wire_fatal":
					p.send("fatal", map[string]any{"kind": "transaction", "message": "private SQL and credentials"})
					<-p.a.Faults().Done()
					<-evalCtx.Done()
					p.exit(errors.New("fatal exit"))
				case "process_death":
					p.exit(errors.New("child died"))
					<-p.a.Faults().Done()
					<-evalCtx.Done()
				case "context_cancel":
					cancel()
					<-evalCtx.Done()
				}
				close(returnEvaluation)
				if mode == "healthy" || mode == "healthy_repeat" || mode == "context_cancel" {
					p.read("stop")
					p.send("stopped", map[string]any{})
					p.exit(nil)
				}
				r := <-done
				if len(r.result.Workers) != 1 || r.result.Workers[0].Err != nil {
					t.Fatal("committed worker fact lost", r.err)
				}
				if mode == "healthy" || mode == "healthy_repeat" {
					if r.err != nil || r.result.Fingerprint == "" {
						t.Fatal("normal run failed", r.err)
					}
					if healthyFingerprint == "" {
						healthyFingerprint = r.result.Fingerprint
					} else if r.result.Fingerprint != healthyFingerprint {
						t.Fatal("volatile wire identity changed fingerprint")
					}
				} else {
					if r.err == nil || r.result.Fingerprint != "" || len(r.result.Evaluation.Results) != 0 {
						t.Fatal("late failure left provisional success", r.err)
					}
					if mode == "context_cancel" && !errors.Is(r.err, context.Canceled) {
						t.Fatal("operation cancellation lost")
					}
					if mode == "process_death" && !errors.Is(r.err, errTransport) {
						t.Fatal("transport cause lost")
					}
				}
				select {
				case <-p.a.exitDone:
				default:
					t.Fatal("Run returned before reaping")
				}
			})
		})
	}
	t.Log("EXTERNAL_SUT_RUN_RESULT late_fatal=invalidates late_death=invalidates committed_result=preserved cancellation=run_error fingerprints=stable")
}

func TestOrchestratorRetainsTerminalPendingBridgeAtFault(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		held, resume := make(chan struct{}), make(chan struct{})
		p.a.beforeRelease = func() { close(held); <-resume }
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: idleFixture{}, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
			NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
				p.a.client = client
				return preparedAdapter{p.a}, nil
			},
			BlockInferenceTimeout: time.Second, StepTimeout: time.Second,
			RunTimeout: 20 * time.Second, StopTimeout: 5 * time.Second,
		})
		if err != nil {
			t.Fatal(err)
		}
		schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "after_read"}})
		if err != nil {
			t.Fatal(err)
		}
		value := scenario.Scenario{Name: "pending-terminal", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		evaluated := make(chan struct{}, 1)
		eval := oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
			evaluated <- struct{}{}
			return oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
		})
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		type runResult struct {
			result orchestrator.RunResult
			err    error
		}
		done := make(chan runResult, 1)
		go func() { result, err := o.Run(ctx, value, schedule, eval); done <- runResult{result, err} }()
		p.read("start")
		p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
		w := p.read("invoke")
		p.send("accepted", binding(w))
		p.send("arrive", arrivalBody(w, 1, "after_read"))
		<-held
		cancel()
		p.read("cancel")
		p.read("stop")
		p.send("terminal", terminalBody(w, "committed", nil))
		synctest.Wait()
		p.a.mu.Lock()
		inv := p.a.invocations[str(w.Body["invocation"])]
		pending := inv.terminal != nil && inv.bridges == 1 && !inv.retired
		p.a.mu.Unlock()
		if !pending {
			t.Fatal("terminal was not validated while bridge held")
		}
		p.exit(errors.New("child died before bridge unwound"))
		<-p.a.Faults().Done()
		close(resume)
		r := <-done
		if len(r.result.Workers) != 1 || r.result.Workers[0].WorkerID != "w1" || r.result.Workers[0].Err != nil {
			t.Fatal("run lost the validated committed result", r.err)
		}
		if !errors.Is(r.err, errTransport) || !errors.Is(r.err, context.Canceled) {
			t.Fatal("known result cleared transport or operation failure", r.err)
		}
		if r.result.Fingerprint != "" || len(r.result.Evaluation.Results) != 0 {
			t.Fatal("failed run retained a successful evaluation")
		}
		select {
		case <-evaluated:
			t.Fatal("oracle ran during failed cleanup")
		default:
		}
	})
}
