package external

import (
	"context"
	"errors"
	"fmt"
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
		{"readiness_mismatch", "mismatch", []string{"step/6/expect/2/quarantine_fixture", "step/6/expect/3/reset_rejected"}},
		{"unsolicited_startup_stopped", "unsolicited_stopped", []string{"step/3/expect/2/reset_rejected"}},
		{"startup_deadline", "startup_deadline", []string{"step/3/expect/2/quarantine_fixture", "step/3/expect/3/reset_rejected"}},
		{"suppressed_cleanup_failure", "active_fatal", []string{"step/11/expect/3/quarantine_fixture"}},
		{"unknown_commit_outcome", "active_fatal", []string{"step/11/expect/3/quarantine_fixture"}},
		{"cancel_cleanup_deadline", "active_fatal", []string{"step/14/expect/3/quarantine_fixture", "step/16/expect/2/quarantine_fixture", "step/16/expect/3/reset_rejected"}},
		{"duplicate_stop_call", "stop_timeout", []string{"step/16/expect/1/quarantine_fixture"}},
		{"java_receives_fatal_active", "protocol_fatal", []string{"step/15/expect/4/quarantine_fixture", "step/15/expect/5/reset_rejected"}},
		{"java_fatal_cleanup_watchdog_expires", "protocol_fatal", []string{"step/14/expect/4/quarantine_fixture", "step/14/expect/5/reset_rejected"}},
		{"java_startup_watchdog_expires", "startup_fatal", []string{"step/6/expect/3/quarantine_fixture", "step/6/expect/4/reset_rejected"}},
		{"java_stop_watchdog_expires", "stop_fatal", []string{"step/17/expect/2/quarantine_fixture", "step/18/expect/3/quarantine_fixture", "step/18/expect/4/reset_rejected"}},
		{"active_stop_cancel_watchdog_expires", "active_fatal", []string{"step/15/expect/3/quarantine_fixture", "step/16/expect/3/quarantine_fixture", "step/16/expect/4/reset_rejected"}},
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
