package external

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
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

// The adapter harness owns wire assertions. These histories also execute the
// same input through Run, which alone owns evaluation and fixture assertions.
// No case-to-generic-fault mapping or copied check indices are used here.
func hasRunVector(id string) bool {
	switch id {
	case "fatal_after_terminals", "death_after_terminals", "suppressed_cleanup_failure", "unknown_commit_outcome", "java_receives_fatal_active", "java_fatal_cleanup_watchdog_expires":
		return true
	}
	return false
}

type vectorRunResult struct {
	result orchestrator.RunResult
	err    error
}

// Preserve real registration, arrival, scheduling and Finish. The bridge's
// return is controlled at the declared runtime_arrive_returns event, including
// an injected runtime error on a valid live arrival.
type vectorRunRuntime struct {
	syncpoint.Runtime
	returned chan error
	resume   chan error
}

func (r *vectorRunRuntime) Arrive(ctx context.Context, worker, point string) error {
	err := r.Runtime.Arrive(ctx, worker, point)
	r.returned <- err
	return <-r.resume
}

func evaluationStepError(s vectorStep) error {
	if s.Peer != "go" {
		return errors.New("evaluation suffix requires Go ownership")
	}
	var args map[string]any
	if s.Action == "local" {
		if err := json.Unmarshal(s.Args, &args); err != nil {
			return err
		}
	}
	var labels string
	switch {
	case s.Action == "local" && s.Event == "begin_evaluation":
		if !fields(args, "requires_decision barrier") || args["requires_decision"] != "G2" || args["barrier"] != "before_return" {
			return errors.New("invalid begin_evaluation arguments")
		}
		labels = "evaluation_started fault_supervision_active"
	case s.Action == "local" && s.Event == "provisional_evaluation":
		if !fields(args, "verdict") || args["verdict"] != "pass" {
			return errors.New("invalid provisional_evaluation arguments")
		}
		labels = "hold_evaluation_return no_run_success"
	case s.Action == "receive":
		f, err := decodeFrame(s.Frame)
		if err != nil {
			return err
		}
		if f.Type != "fatal" || f.Body["kind"] != "cleanup" || s.Delivery != "input" {
			return errors.New("unsupported evaluation frame")
		}
		labels = "latch_adapter_fault invalidate_evaluation abort_run quarantine_fixture"
	case s.Action == "local" && s.Event == "child_exit":
		if !fields(args, "stdout_eof exit_code") || args["stdout_eof"] != true || args["exit_code"] != float64(0) {
			return errors.New("invalid evaluation child exit")
		}
		labels = "latch_transport_fault invalidate_evaluation abort_run quarantine_fixture"
	case s.Action == "local" && s.Event == "complete_evaluation":
		if !fields(args, "") {
			return errors.New("invalid complete_evaluation arguments")
		}
		labels = "discard_provisional_evaluation retain_transport_fault retain_adapter_fault no_run_success"
	case s.Action == "local" && s.Event == "check_operation_result":
		if !fields(args, "scope expected_error requires_decision") || args["scope"] != "run" || args["requires_decision"] != "G2" || (args["expected_error"] != "transport_failure" && args["expected_error"] != "adapter_failure") {
			return errors.New("invalid evaluation result arguments")
		}
		labels = "operation_error no_run_success"
	default:
		return fmt.Errorf("unhandled evaluation event: %s/%s", s.Action, s.Event)
	}
	for _, label := range s.Expect {
		if !contains(strings.Fields(labels), label) {
			return fmt.Errorf("unhandled evaluation assertion %s for %s", label, s.Event)
		}
	}
	return nil
}

func runOrchestratorVector(t *testing.T, id string, steps []vectorStep) {
	t.Helper()
	// Validate the entire suffix before starting the run or injecting any input.
	suffix := len(steps)
	for i, s := range steps {
		if s.Event == "begin_evaluation" {
			suffix = i
			break
		}
	}
	for _, s := range steps[suffix:] {
		if err := evaluationStepError(s); err != nil {
			t.Fatal(err)
		}
	}
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		n := 1
		p.a.id = func() (string, error) { n++; return strings.Repeat(fmt.Sprint(n), 32), nil }
		// Separate termination from reaping for the late-fatal composition witness.
		p.deferKillExit = suffix < len(steps) && id == "fatal_after_terminals"
		probe := &quarantineProbe{}
		runtime := &vectorRunRuntime{Runtime: syncpoint.New(), returned: make(chan error, 1), resume: make(chan error, 1)}
		evaluating := make(chan context.Context, 1)
		finishEvaluation := make(chan struct{})
		eval := oracle.EvaluatorFunc(func(ctx context.Context, _ oracle.DB, _ oracle.RunContext) (oracle.Evaluation, error) {
			provisional, err := oracle.NewEvaluation(oracle.OracleResult{OracleID: "synthetic-pass"})
			evaluating <- ctx
			<-finishEvaluation
			return provisional, err
		})
		o, err := orchestrator.New(orchestrator.Config{Fixture: probe, DB: &fixture.DB{}, NewRuntime: func() syncpoint.Runtime { return runtime },
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
		value := scenario.Scenario{Name: id, Workers: []scenario.Worker{{ID: "w1", Command: "assign"}}, SyncPoints: []string{"after_read"}}
		outputs := make(chan frame, 32)
		go func() {
			for {
				f, _, err := readFrame(p.input)
				if err != nil {
					return
				}
				outputs <- f
			}
		}()
		done := make(chan vectorRunResult, 1)
		go func() { r, err := o.Run(context.Background(), value, schedule, eval); done <- vectorRunResult{r, err} }()
		var evalCtx context.Context
		var result *vectorRunResult
		var stopDone chan error
		var cleanupStarted time.Time
		var clockElapsed time.Duration
		checks := []string{}
		assertPending := func() {
			select {
			case r := <-done:
				t.Fatalf("Run completed before declared boundary: %v", r.err)
			default:
			}
		}
		collect := func() {
			if result == nil {
				r := <-done
				result = &r
			}
		}
		for i, s := range steps {
			if s.Peer == "java" {
				if s.Delivery == "exchange" {
					want, err := decodeFrame(s.Frame)
					if err != nil {
						t.Fatal(err)
					}
					assertFrame(t, <-outputs, want)
				} else if s.Event == "advance_fatal_cleanup_clock" {
					var args map[string]any
					if err := json.Unmarshal(s.Args, &args); err != nil {
						t.Fatal(err)
					}
					elapsed := time.Duration(args["elapsed_ms"].(float64)) * time.Millisecond
					if elapsed < clockElapsed {
						t.Fatal("cleanup clock moved backwards")
					}
					<-time.After(elapsed - clockElapsed)
					clockElapsed = elapsed
					assertPending()
					select {
					case <-p.a.exitDone:
						t.Fatal("child exited before declared watchdog exit")
					default:
					}
				}
				continue // Java-only application events are scripted inputs, not Go evidence.
			}
			var args map[string]any
			if s.Action == "local" {
				if err := json.Unmarshal(s.Args, &args); err != nil {
					t.Fatal(err)
				}
			}
			event := "local/" + s.Event
			switch s.Action {
			case "receive":
				f, err := decodeFrame(s.Frame)
				if err != nil {
					t.Fatal(err)
				}
				p.sendFrame(f)
				event = "receive/" + f.Type
				if f.Type == "fatal" {
					<-p.a.Faults().Done()
					if !strings.Contains(p.a.Faults().Err().Error(), str(f.Body["kind"])) {
						t.Fatal("declared fatal kind lost", p.a.Faults().Err())
					}
					if evalCtx != nil {
						<-evalCtx.Done()
					}
				}
			case "local":
				switch s.Event {
				case "invoke_call":
					if !fields(args, "invocation worker command context") || args["context"] != "fresh" || args["worker"] != "w1" || args["command"] != "assign" || args["invocation"] != strings.Repeat("3", 32) {
						t.Fatal("unsupported run invocation")
					}
					// Run owns Invoke; the following exchange compares its exact wire output.
				case "runtime_arrive_returns":
					ident, ok := args["identity"].(map[string]any)
					if !fields(args, "identity result") || !ok || !fields(ident, "invocation worker arrival point") || ident["invocation"] != strings.Repeat("3", 32) || ident["worker"] != "w1" || ident["arrival"] != "1" || ident["point"] != "after_read" {
						t.Fatal("unsupported run arrival identity")
					}
					if err := <-runtime.returned; err != nil {
						t.Fatal("valid runtime arrival failed before injection", err)
					}
					switch args["result"] {
					case "nil":
						runtime.resume <- nil
					case "protocol_error":
						runtime.resume <- errProtocol
					default:
						t.Fatal("unsupported runtime result")
					}
				case "begin_evaluation":
					if evalCtx != nil {
						t.Fatal("duplicate evaluation begin")
					}
					evalCtx = <-evaluating
					if evalCtx.Err() != nil || p.a.Faults().Err() != nil {
						t.Fatal("evaluation did not begin under healthy supervision")
					}
					assertPending()
				case "provisional_evaluation":
					if evalCtx == nil {
						t.Fatal("provisional evaluation before begin")
					}
					assertPending()
				case "complete_evaluation":
					if evalCtx == nil || evalCtx.Err() == nil || p.a.Faults().Err() == nil {
						t.Fatal("evaluation was not invalidated")
					}
					cleanupStarted = time.Now()
					close(finishEvaluation)
					if p.deferKillExit {
						<-p.killed
						synctest.Wait()
						if elapsed := time.Since(cleanupStarted); elapsed <= 0 || elapsed > 5*time.Second {
							t.Fatal("late-fatal termination escaped Stop budget", elapsed)
						}
						assertPending()
						select {
						case <-p.a.exitDone:
							t.Fatal("termination was mistaken for reaping")
						default:
						}
						p.exit(errors.New("late fatal terminated child"))
					}
					collect()
				case "check_operation_result":
					collect()
					if args["expected_error"] == "transport_failure" && !errors.Is(result.err, errTransport) {
						t.Fatal("transport error lost", result.err)
					}
				case "stop_call":
					if !fields(args, "call_id budget_ms") || args["call_id"] != "fatal-stop" || args["budget_ms"] != float64(5000) {
						t.Fatal("unsupported fatal Stop")
					}
					stopDone = make(chan error, 1)
					go func() { stopDone <- p.a.Stop(context.Background()) }()
					synctest.Wait()
					assertPending()
				case "child_exit":
					if !fields(args, "stdout_eof exit_code") || args["stdout_eof"] != true {
						t.Fatal("invalid run child exit")
					}
					switch args["exit_code"] {
					case float64(0):
						p.exit(nil)
					case float64(1):
						p.exit(errors.New("declared nonzero child exit"))
					default:
						t.Fatal("unsupported run exit code")
					}
					<-p.a.exitDone
					<-p.a.Faults().Done()
					if evalCtx != nil {
						<-evalCtx.Done()
					}
				default:
					t.Fatalf("unhandled run event %s", s.Event)
				}
			default:
				t.Fatalf("unhandled run action %s", s.Action)
			}
			if i >= suffix {
				checks = append(checks, fmt.Sprintf("step/%d/%s", i, event))
			}
			for j, label := range s.Expect {
				if i >= suffix || label == "abort_run" || label == "quarantine_fixture" || label == "reset_rejected" {
					checks = append(checks, fmt.Sprintf("step/%d/expect/%d/%s", i, j, label))
				}
			}
		}
		// Released fatal histories end at fault receipt; cleanup is still performed
		// by Run. The fake child exits only when bounded Stop actually kills it.
		collect()
		fault := p.a.Faults().Err()
		if fault == nil || !errors.Is(result.err, fault) || !errors.Is(probe.cause, fault) || !errors.Is(probe.Reset(context.Background()), fixture.ErrQuarantined) {
			t.Fatal("Run lost its fault, quarantine, or Reset rejection", result.err)
		}
		if len(result.result.Evaluation.Results) != 0 || result.result.Fingerprint != "" {
			t.Fatal("fault retained provisional success")
		}
		if suffix < len(steps) {
			if len(result.result.Workers) != 1 || result.result.Workers[0].Err != nil {
				t.Fatal("late fault lost committed worker")
			}
		} else if len(result.result.Workers) != 0 {
			t.Fatal("fault fabricated worker result")
		}
		if stopDone != nil {
			if err := <-stopDone; !errors.Is(err, fault) {
				t.Fatal("Stop lost runtime fault", err)
			}
		}
		select {
		case <-p.a.exitDone:
		default:
			t.Fatal("Run returned before child reap")
		}
		for _, check := range checks {
			if (strings.HasSuffix(check, "/retain_transport_fault") || strings.HasSuffix(check, "/latch_transport_fault")) && !errors.Is(result.err, errTransport) {
				t.Fatal("declared transport fault not retained", result.err)
			}
			reportCheck(t, "case/"+id, check, "internal/sut/external/run_vectors_test.go:runOrchestratorVector")
		}
		if p.deferKillExit {
			if time.Since(cleanupStarted) > 5*time.Second {
				t.Fatal("late-fatal cleanup exceeded Stop budget")
			}
			reportCheck(t, "requirement/late-fatal-cleanup", "observe/evidence", "internal/sut/external/run_vectors_test.go:runOrchestratorVector")
		}
	})
}

func TestEvaluationDispatchRejectsMutations(t *testing.T) {
	v := loadVectors(t)
	for _, c := range v.Cases {
		if c.ID != "fatal_after_terminals" && c.ID != "death_after_terminals" {
			continue
		}
		for _, s := range c.Steps {
			if err := evaluationStepError(s); err != nil {
				t.Fatal(err)
			}
			if s.Action == "local" {
				changed := s
				var args map[string]any
				if err := json.Unmarshal(s.Args, &args); err != nil {
					t.Fatal(err)
				}
				args["unsupported"] = true
				changed.Args, _ = json.Marshal(args)
				if evaluationStepError(changed) == nil {
					t.Fatalf("accepted extra argument for %s", s.Event)
				}
				changed = s
				changed.Event = "unsupported"
				if evaluationStepError(changed) == nil {
					t.Fatal("accepted unknown event")
				}
			}
			changed := s
			changed.Expect = []string{"unsupported"}
			if evaluationStepError(changed) == nil {
				t.Fatal("accepted unknown assertion")
			}
		}
	}
}
