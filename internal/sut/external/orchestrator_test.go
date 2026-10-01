package external

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
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

type quarantineProbe struct{ cause error }

type finishProbe struct {
	syncpoint.Runtime
	finishes atomic.Int32
}

type waitArriveProbe struct {
	syncpoint.Runtime
	held, resume chan struct{}
}

func (r *waitArriveProbe) WaitArrive(ctx context.Context, workerID, point string, timeout time.Duration) (syncpoint.ArriveStatus, error) {
	status, err := r.Runtime.WaitArrive(ctx, workerID, point, timeout)
	if status == syncpoint.ArriveStatusArrived && err == nil {
		close(r.held)
		<-r.resume
	}
	return status, err
}

func (r *finishProbe) Finish(workerID string, workerErr error) error {
	r.finishes.Add(1)
	return r.Runtime.Finish(workerID, workerErr)
}

func (f *quarantineProbe) Ready(*fixture.DB) error        { return f.check() }
func (f *quarantineProbe) Reset(context.Context) error    { return f.check() }
func (f *quarantineProbe) Quarantine(err error)           { f.cause = err }
func (f *quarantineProbe) Teardown(context.Context) error { return nil }
func (f *quarantineProbe) check() error {
	if f.cause != nil {
		return errors.Join(fixture.ErrQuarantined, f.cause)
	}
	return nil
}

func TestOrchestratorLateFaultAndFingerprint(t *testing.T) {
	var healthyFingerprint string
	for _, mode := range []string{"healthy", "healthy_repeat", "wire_fatal", "process_death", "context_cancel"} {
		t.Run(mode, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.a.id = randomID
				fixtureProbe := &quarantineProbe{}
				evaluating, returnEvaluation := make(chan context.Context, 1), make(chan struct{})
				eval := oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
					provisional, err := oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
					if err != nil {
						return oracle.Evaluation{}, err
					}
					evaluating <- ctx
					<-returnEvaluation
					return provisional, nil
				})
				o, err := orchestrator.New(orchestrator.Config{Fixture: fixtureProbe, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
					NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
				select {
				case <-done:
					t.Fatal("evaluation returned before its release barrier")
				default:
				}
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
				if mode == "wire_fatal" || mode == "process_death" {
					if fixtureProbe.cause == nil || !errors.Is(fixtureProbe.Reset(context.Background()), fixture.ErrQuarantined) || !errors.Is(fixtureProbe.cause, p.a.Faults().Err()) {
						t.Fatal("fault did not quarantine the fixture and reject Reset")
					}
					reportEvaluationVector(t, mode, r, p)
				} else if fixtureProbe.cause != nil {
					t.Fatal("healthy cleanup quarantined fixture")
				}
				if len(r.result.Workers) != 1 || r.result.Workers[0].Err != nil {
					t.Fatal("committed worker fact lost", r.err)
				}
				if mode == "healthy" || mode == "healthy_repeat" {
					if r.err != nil || r.result.Fingerprint == "" {
						t.Fatal("normal run failed", r.err)
					}
					if err := fixtureProbe.Reset(context.Background()); err != nil {
						t.Fatal("successful Stop did not permit fixture Reset", err)
					}
					if mode == "healthy" {
						reportCheck(t, "requirement/go-success-lifecycle", "observe/evidence", "internal/sut/external/orchestrator_test.go:TestOrchestratorLateFaultAndFingerprint")
						reportCheck(t, "case/success", "step/15/expect/1/reset_allowed", "internal/sut/external/orchestrator_test.go:TestOrchestratorLateFaultAndFingerprint")
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
					if mode == "context_cancel" {
						reportCheck(t, "case/commit_wins_cancel", "step/14/expect/0/operation_context_error", "internal/sut/external/orchestrator_test.go:TestOrchestratorLateFaultAndFingerprint")
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

func reportEvaluationVector(t *testing.T, mode string, r struct {
	result orchestrator.RunResult
	err    error
}, p *peer) {
	t.Helper()
	row, faultLabel, faultEvent, retain := "case/fatal_after_terminals", "latch_adapter_fault", "receive/fatal", "retain_adapter_fault"
	if mode == "process_death" {
		row, faultLabel, faultEvent, retain = "case/death_after_terminals", "latch_transport_fault", "local/child_exit", "retain_transport_fault"
	}
	if r.err == nil || r.result.Fingerprint != "" || len(r.result.Evaluation.Results) != 0 || len(r.result.Workers) != 1 || r.result.Workers[0].Err != nil || p.a.Faults().Err() == nil {
		t.Fatal("late fault did not discard the provisional result while preserving the terminal")
	}
	if mode == "process_death" && !errors.Is(r.err, errTransport) {
		t.Fatal("transport failure was not retained")
	}
	for _, phase := range []struct {
		index  int
		event  string
		labels []string
	}{
		{12, "local/begin_evaluation", []string{"evaluation_started", "fault_supervision_active"}},
		{13, "local/provisional_evaluation", []string{"hold_evaluation_return", "no_run_success"}},
		{14, faultEvent, []string{faultLabel, "invalidate_evaluation", "abort_run", "quarantine_fixture"}},
		{15, "local/complete_evaluation", []string{"discard_provisional_evaluation", retain, "no_run_success"}},
		{16, "local/check_operation_result", []string{"operation_error", "no_run_success"}},
	} {
		reportCheck(t, row, fmt.Sprintf("step/%d/%s", phase.index, phase.event), "internal/sut/external/orchestrator_test.go:reportEvaluationVector")
		for i, label := range phase.labels {
			reportCheck(t, row, fmt.Sprintf("step/%d/expect/%d/%s", phase.index, i, label), "internal/sut/external/orchestrator_test.go:reportEvaluationVector")
		}
	}
	if mode == "wire_fatal" {
		reportCheck(t, "requirement/late-fatal-cleanup", "observe/evidence", "internal/sut/external/orchestrator_test.go:reportEvaluationVector")
	}
}

func TestOrchestratorQuarantineVectors(t *testing.T) {
	cases := []struct {
		row, mode string
		checks    []string
	}{
		{"process_death", "active_death", []string{"step/8/expect/4/quarantine_fixture", "step/11/expect/3/reset_rejected"}},
		{"process_death", "active_death", []string{"step/8/expect/3/abort_run"}},
		{"readiness_mismatch", "mismatch", []string{"step/6/expect/2/quarantine_fixture", "step/6/expect/3/reset_rejected"}},
		{"unsolicited_startup_stopped", "unsolicited_stopped", []string{"step/3/expect/2/reset_rejected"}},
		{"startup_deadline", "startup_deadline", []string{"step/3/expect/2/quarantine_fixture", "step/3/expect/3/reset_rejected"}},
		{"suppressed_cleanup_failure", "active_fatal", []string{"step/11/expect/3/quarantine_fixture"}},
		{"suppressed_cleanup_failure", "active_fatal", []string{"step/11/expect/2/abort_run"}},
		{"unknown_commit_outcome", "active_fatal", []string{"step/11/expect/3/quarantine_fixture"}},
		{"unknown_commit_outcome", "active_fatal", []string{"step/11/expect/2/abort_run"}},
		{"cancel_cleanup_deadline", "active_fatal", []string{"step/14/expect/3/quarantine_fixture", "step/16/expect/2/quarantine_fixture", "step/16/expect/3/reset_rejected"}},
		{"cancel_cleanup_deadline", "active_fatal", []string{"step/14/expect/2/abort_run"}},
		{"duplicate_stop_call", "stop_timeout", []string{"step/16/expect/1/quarantine_fixture"}},
		{"java_receives_fatal_active", "protocol_fatal", []string{"step/15/expect/4/quarantine_fixture", "step/15/expect/5/reset_rejected"}},
		{"java_receives_fatal_active", "protocol_fatal", []string{"step/8/expect/3/abort_run"}},
		{"java_fatal_cleanup_watchdog_expires", "protocol_fatal", []string{"step/14/expect/4/quarantine_fixture", "step/14/expect/5/reset_rejected"}},
		{"java_fatal_cleanup_watchdog_expires", "protocol_fatal", []string{"step/9/expect/3/abort_run"}},
		{"java_startup_watchdog_expires", "startup_fatal", []string{"step/6/expect/3/quarantine_fixture", "step/6/expect/4/reset_rejected"}},
		{"java_stop_watchdog_expires", "stop_fatal", []string{"step/17/expect/2/quarantine_fixture", "step/18/expect/3/quarantine_fixture", "step/18/expect/4/reset_rejected"}},
		{"active_stop_cancel_watchdog_expires", "active_fatal", []string{"step/15/expect/3/quarantine_fixture", "step/16/expect/3/quarantine_fixture", "step/16/expect/4/reset_rejected"}},
		{"active_stop_cancel_watchdog_expires", "active_fatal", []string{"step/15/expect/2/abort_run"}},
	}
	for _, c := range cases {
		t.Run(c.row, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				probe := &quarantineProbe{}
				o, err := orchestrator.New(orchestrator.Config{
					Fixture: probe, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
					NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
				value := scenario.Scenario{Name: "quarantine", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
				eval := oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
					return oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
				})
				done := make(chan error, 1)
				go func() { _, err := o.Run(context.Background(), value, schedule, eval); done <- err }()
				p.read("start")
				switch c.mode {
				case "unsolicited_stopped":
					p.send("stopped", map[string]any{})
					p.read("fatal")
					p.exit(errors.New("unsolicited stopped"))
				case "mismatch":
					p.send("ready", map[string]any{"commands": []string{"other"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
					p.read("fatal")
					p.exit(errors.New("startup failure"))
				case "startup_deadline":
					<-p.a.Faults().Done()
					p.read("fatal")
					p.exit(errors.New("startup deadline"))
				case "startup_fatal":
					p.send("fatal", map[string]any{"kind": "startup", "message": "startup deadline exceeded"})
					p.exit(errors.New("startup watchdog"))
				default:
					p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
					w := p.read("invoke")
					p.send("accepted", binding(w))
					switch c.mode {
					case "stop_timeout", "stop_fatal":
						p.send("arrive", arrivalBody(w, 1, "after_read"))
						p.read("release")
						p.send("terminal", terminalBody(w, "committed", nil))
						p.read("stop")
						if c.mode == "stop_fatal" {
							p.send("fatal", map[string]any{"kind": "shutdown", "message": "stop deadline exceeded"})
							p.exit(errors.New("stop watchdog"))
						} else {
							<-p.killed
						}
					case "protocol_fatal":
						p.send("arrive", arrivalBody(w, 1, "missing"))
						p.read("fatal")
						p.exit(errors.New("protocol fault"))
					case "active_death":
						p.send("arrive", arrivalBody(w, 1, "after_read"))
						p.exit(errors.New("child died"))
					default:
						p.send("fatal", map[string]any{"kind": "cleanup", "message": "private cleanup failure"})
						p.exit(errors.New("cleanup fault"))
					}
				}
				if err := <-done; err == nil || probe.cause == nil || !errors.Is(probe.Reset(context.Background()), fixture.ErrQuarantined) {
					t.Fatal("failed Run did not quarantine and reject Reset", err)
				}
				if !errors.Is(probe.cause, p.a.Faults().Err()) && c.mode != "stop_timeout" {
					t.Fatal("fixture lost the original session fault")
				}
				for _, check := range c.checks {
					reportCheck(t, "case/"+c.row, check, "internal/sut/external/orchestrator_test.go:TestOrchestratorQuarantineVectors")
				}
			})
		})
	}
}

func TestOrchestratorCancelBeforeAccepted(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		runtime := &finishProbe{Runtime: syncpoint.New()}
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: &quarantineProbe{}, DB: &fixture.DB{}, NewRuntime: func() syncpoint.Runtime { return runtime },
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
		value := scenario.Scenario{Name: "cancel-before-accepted", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		type runResult struct {
			result orchestrator.RunResult
			err    error
		}
		done := make(chan runResult, 1)
		go func() {
			r, e := o.Run(ctx, value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				t.Error("unstarted command reached evaluation")
				return oracle.Evaluation{}, nil
			}))
			done <- runResult{r, e}
		}()
		p.read("start")
		p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
		w := p.read("invoke")
		cancel()
		p.read("cancel")
		p.send("accepted", binding(w))
		b := terminalBody(w, "not_started", wireError("cancelled", "cancelled by context", 0, ""))
		b["connection"] = "not_acquired"
		p.send("terminal", b)
		p.read("stop")
		p.send("stopped", map[string]any{})
		p.exit(nil)
		r := <-done
		if !errors.Is(r.err, context.Canceled) || len(r.result.Unstarted) != 1 || !errors.Is(r.result.Unstarted[0].Err, context.Canceled) || len(r.result.Workers) != 0 || runtime.finishes.Load() != 0 {
			t.Fatal("cancel-before-accepted Run lost its unstarted outcome or called Finish", r.err)
		}
		reportCheck(t, "case/cancel_before_accepted", "step/9/expect/2/no_runtime_finish", "internal/sut/external/orchestrator_test.go:TestOrchestratorCancelBeforeAccepted")
		reportCheck(t, "case/cancel_before_accepted", "step/9/expect/3/operation_context_error", "internal/sut/external/orchestrator_test.go:TestOrchestratorCancelBeforeAccepted")
	})
}

func TestOrchestratorResetAfterActiveStop(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		probe := &quarantineProbe{}
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: probe, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
		value := scenario.Scenario{Name: "active-stop", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		done := make(chan error, 1)
		go func() {
			_, err := o.Run(ctx, value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				t.Error("canceled command reached evaluation")
				return oracle.Evaluation{}, nil
			}))
			done <- err
		}()
		p.read("start")
		p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
		w := p.read("invoke")
		p.send("accepted", binding(w))
		p.send("arrive", arrivalBody(w, 1, "after_read"))
		p.read("release")
		cancel()
		p.read("cancel")
		p.read("stop")
		p.send("terminal", terminalBody(w, "rolled_back", wireError("cancelled", "cancelled by context", 0, "")))
		p.send("stopped", map[string]any{})
		p.exit(nil)
		if err := <-done; !errors.Is(err, context.Canceled) {
			t.Fatal("active cancellation lost", err)
		}
		if probe.cause != nil {
			t.Fatal("clean active Stop quarantined fixture", probe.cause)
		}
		if err := probe.Reset(context.Background()); err != nil {
			t.Fatal("Reset after clean active Stop failed", err)
		}
		reportCheck(t, "case/stop_active_invocation", "step/18/expect/1/reset_allowed", "internal/sut/external/orchestrator_test.go:TestOrchestratorResetAfterActiveStop")
	})
}

func TestOrchestratorResetAfterStopBeforeReady(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		probe := &quarantineProbe{}
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: probe, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
		value := scenario.Scenario{Name: "stop-before-ready", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		done := make(chan error, 1)
		go func() {
			_, err := o.Run(ctx, value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				t.Error("startup cancellation reached evaluation")
				return oracle.Evaluation{}, nil
			}))
			done <- err
		}()
		p.read("start")
		cancel()
		p.read("stop")
		p.send("stopped", map[string]any{})
		p.exit(nil)
		if err := <-done; !errors.Is(err, context.Canceled) {
			t.Fatal("startup cancellation lost", err)
		}
		if probe.cause != nil {
			t.Fatal("clean startup Stop quarantined fixture", probe.cause)
		}
		if err := probe.Reset(context.Background()); err != nil {
			t.Fatal("Reset after clean startup Stop failed", err)
		}
		reportCheck(t, "case/stop_before_ready", "step/4/expect/1/reset_allowed", "internal/sut/external/orchestrator_test.go:TestOrchestratorResetAfterStopBeforeReady")
	})
}

func TestOrchestratorDatabaseBlockingVector(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		runtime := syncpoint.New()
		timedOut := make(chan struct{}, 1)
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: &quarantineProbe{}, DB: &fixture.DB{}, NewRuntime: func() syncpoint.Runtime { return runtime },
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
				p.a.client = client
				return preparedAdapter{p.a}, nil
			},
			BlockInferenceTimeout: 10 * time.Millisecond, StepTimeout: time.Second,
			RunTimeout: 20 * time.Second, StopTimeout: 5 * time.Second,
			OnEvent: func(event orchestrator.Event) error {
				if event.Kind != orchestrator.EventPointTimeout {
					return nil
				}
				if event.Worker != "w2" || event.Point != "after_read" || event.Status != orchestrator.ControlStatusTimeoutInferred {
					return fmt.Errorf("unexpected timeout event: %#v", event)
				}
				snapshot, err := runtime.Snapshot("w2")
				if err != nil || snapshot.State != syncpoint.WorkerStateDBBlocked || p.a.Faults().Err() != nil {
					return fmt.Errorf("timeout did not observe blocked runtime worker: state=%v err=%v fault=%v", snapshot.State, err, p.a.Faults().Err())
				}
				timedOut <- struct{}{}
				return nil
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "after_read"}, {Worker: "w2", Point: "after_read"}})
		if err != nil {
			t.Fatal(err)
		}
		value := scenario.Scenario{Name: "database-blocking", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}, {ID: "w2", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		type runResult struct {
			result orchestrator.RunResult
			err    error
		}
		done := make(chan runResult, 1)
		go func() {
			r, e := o.Run(context.Background(), value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
			}))
			done <- runResult{r, e}
		}()
		p.read("start")
		p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
		w1 := p.read("invoke")
		p.send("accepted", binding(w1))
		p.send("arrive", arrivalBody(w1, 1, "after_read"))
		w2 := p.read("invoke")
		p.send("accepted", binding(w2))
		<-timedOut
		p.read("release")
		p.send("terminal", terminalBody(w1, "committed", nil))
		p.send("arrive", arrivalBody(w2, 1, "after_read"))
		p.read("release")
		p.send("terminal", terminalBody(w2, "committed", nil))
		p.read("stop")
		p.send("stopped", map[string]any{})
		p.exit(nil)
		r := <-done
		if r.err != nil || r.result.Timeouts != 1 || r.result.PendingResolved != 1 || len(r.result.Workers) != 2 {
			t.Fatal("database blocking timeout was not recovered", r.err, r.result.Timeouts, r.result.PendingResolved)
		}
		reportCheck(t, "case/database_blocking", "step/12/local/wait_arrive_timeout", "internal/sut/external/orchestrator_test.go:TestOrchestratorDatabaseBlockingVector")
		reportCheck(t, "case/database_blocking", "step/12/expect/0/runtime_db_blocked", "internal/sut/external/orchestrator_test.go:TestOrchestratorDatabaseBlockingVector")
	})
}

func TestOrchestratorProtocolAbortVectors(t *testing.T) {
	for _, c := range []struct {
		row, check string
	}{
		{"conflicting_duplicate", "step/8/expect/1/abort_run"},
		{"sequence_gap", "step/6/expect/1/abort_run"},
		{"unknown_invocation", "step/6/expect/2/abort_run"},
		{"terminal_while_arrived", "step/8/expect/2/abort_run"},
	} {
		t.Run(c.row, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				probe := &quarantineProbe{}
				var heldRuntime *waitArriveProbe
				newRuntime := func() syncpoint.Runtime {
					if c.row == "terminal_while_arrived" || c.row == "conflicting_duplicate" {
						heldRuntime = &waitArriveProbe{Runtime: syncpoint.New(), held: make(chan struct{}), resume: make(chan struct{})}
						return heldRuntime
					}
					return syncpoint.New()
				}
				o, err := orchestrator.New(orchestrator.Config{
					Fixture: probe, DB: &fixture.DB{}, NewRuntime: newRuntime,
					NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
				value := scenario.Scenario{Name: c.row, Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
				done := make(chan error, 1)
				go func() {
					_, err := o.Run(context.Background(), value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
						t.Error("protocol failure reached evaluation")
						return oracle.Evaluation{}, nil
					}))
					done <- err
				}()
				p.read("start")
				p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
				w := p.read("invoke")
				p.send("accepted", binding(w))
				switch c.row {
				case "conflicting_duplicate":
					arrival := p.send("arrive", arrivalBody(w, 1, "after_read"))
					<-heldRuntime.held
					arrival.Body = arrivalBody(w, 1, "before_write")
					p.sendFrame(arrival)
				case "sequence_gap":
					p.next++
					p.send("arrive", arrivalBody(w, 1, "after_read"))
				case "unknown_invocation":
					body := arrivalBody(w, 1, "after_read")
					body["invocation"] = strings.Repeat("4", 32)
					p.send("arrive", body)
				case "terminal_while_arrived":
					p.send("arrive", arrivalBody(w, 1, "after_read"))
					<-heldRuntime.held
					p.send("terminal", terminalBody(w, "committed", nil))
				}
				p.read("fatal")
				if heldRuntime != nil {
					close(heldRuntime.resume)
				}
				p.exit(errors.New("protocol fault"))
				if err := <-done; err == nil || !errors.Is(err, errProtocol) || probe.cause == nil {
					t.Fatal("protocol fault did not abort Run and quarantine fixture", err)
				}
				reportCheck(t, "case/"+c.row, c.check, "internal/sut/external/orchestrator_test.go:TestOrchestratorProtocolAbortVectors")
			})
		})
	}
}

func TestOrchestratorRetiredTerminalConflictAbortsRun(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		probe := &quarantineProbe{}
		evaluating, releaseEvaluation := make(chan struct{}, 1), make(chan struct{})
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: probe, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
		value := scenario.Scenario{Name: "retired-conflict", Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		done := make(chan error, 1)
		go func() {
			_, err := o.Run(context.Background(), value, schedule, oracle.EvaluatorFunc(func(context.Context, oracle.DB, oracle.RunContext) (oracle.Evaluation, error) {
				evaluating <- struct{}{}
				<-releaseEvaluation
				return oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
			}))
			done <- err
		}()
		p.read("start")
		p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
		w := p.read("invoke")
		p.send("accepted", binding(w))
		p.send("arrive", arrivalBody(w, 1, "after_read"))
		p.read("release")
		p.send("terminal", terminalBody(w, "committed", nil))
		<-evaluating
		p.send("terminal", terminalBody(w, "committed", wireError("application", "conflicting retired outcome", 0, "")))
		p.read("fatal")
		p.exit(errors.New("conflicting retired terminal"))
		close(releaseEvaluation)
		if err := <-done; err == nil || !errors.Is(err, errProtocol) || probe.cause == nil {
			t.Fatal("retired terminal conflict did not abort Run", err)
		}
		reportCheck(t, "case/retired_terminal_conflict", "step/15/expect/2/abort_run", "internal/sut/external/orchestrator_test.go:TestOrchestratorRetiredTerminalConflictAbortsRun")
	})
}

func TestOrchestratorRetainsTerminalPendingBridgeAtFault(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		held, resume := make(chan struct{}), make(chan struct{})
		p.a.beforeRelease = func() { close(held); <-resume }
		o, err := orchestrator.New(orchestrator.Config{
			Fixture: idleFixture{}, DB: &fixture.DB{}, NewRuntime: syncpoint.New,
			NewAdapter: func(client syncpoint.Client) (sut.Adapter, error) {
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
