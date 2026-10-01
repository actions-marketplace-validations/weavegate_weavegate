package external

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/sut"
)

// Every Go-applicable shared history runs against the adapter and a scripted
// child. Evaluation and fixture observations are owned by orchestrator tests.
func TestSharedLifecycleGo(t *testing.T) {
	v := loadVectors(t)
	cases := 0
	for _, c := range v.Cases {
		if !contains(c.Targets, "go") {
			continue
		}
		cases++
		t.Run(c.ID, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.a.id = func() (string, error) { return strings.Repeat("2", 32), nil }
				h := &vectorHarness{p: p, outputs: make(chan frame, 128), streams: map[string]<-chan sut.InvocationOutcome{}, outcomes: map[string]sut.InvocationOutcome{}, contexts: map[string]context.Context{}, cancels: map[string]context.CancelFunc{}, calls: map[string]*arrivalCall{}, stopCalls: map[string]<-chan error{}, stopErrors: map[string]error{}}
				if c.ID == "startup_submillisecond_budget" {
					h.startHeld, h.startResume = make(chan struct{}), make(chan struct{})
					p.a.beforeWrite = func(kind string) {
						if kind == "start" {
							close(h.startHeld)
							<-h.startResume
						}
					}
				}
				go func() {
					for {
						f, _, err := readFrame(p.input)
						if err != nil {
							return
						}
						h.outputs <- f
					}
				}()
				h.start = p.startAsync(context.Background())
				steps := append(expandPrefix(t, v, c.Prefix), c.Steps...)
				for i, s := range steps {
					if s.Event == "begin_evaluation" {
						break // The orchestrator witness owns evaluation and fixture checks.
					}
					h.step(t, "case/"+c.ID, i, s)
				}
				if p.a.Faults().Err() != nil {
					p.exit(nil)
				}
			})
		})
	}
	if cases != 53 {
		t.Fatalf("reviewed Go lifecycle inventory changed: %d cases", cases)
	}
	t.Log("EXTERNAL_SUT_VECTOR_GO_RESULT cases=53 dispatch=closed assertions=observed")
	reportCheck(t, "requirement/go-dispatch", "observe/evidence", "internal/sut/external/vectors_test.go:TestSharedLifecycleGo")
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func expandPrefix(t *testing.T, v vectors, name string) []vectorStep {
	t.Helper()
	steps, ok := v.Prefixes[name]
	if !ok {
		t.Fatal("unknown vector prefix")
	}
	var result []vectorStep
	for _, s := range steps {
		if s.Prefix != "" {
			result = append(result, expandPrefix(t, v, s.Prefix)...)
		} else {
			result = append(result, s)
		}
	}
	return result
}

type vectorHarness struct {
	p             *peer
	start         <-chan error
	outputs       chan frame
	pending       []frame
	streams       map[string]<-chan sut.InvocationOutcome
	outcomes      map[string]sut.InvocationOutcome
	contexts      map[string]context.Context
	cancels       map[string]context.CancelFunc
	calls         map[string]*arrivalCall
	callCount     int
	stopCalls     map[string]<-chan error
	stopErrors    map[string]error
	stopDeadline  time.Time
	stopCause     string
	child         *child
	blockedWorker string
	releaseHeld   chan struct{}
	releaseResume chan struct{}
	stderrDigest  string
	stderrSize    int
	originalFault error
	startHeld     chan struct{}
	startResume   chan struct{}
	lastReject    error
}

func reportCheck(t *testing.T, row, check, handler string) {
	t.Helper()
	raw, _ := json.Marshal(map[string]string{"row": row, "check": check, "handler": handler})
	t.Logf("EXTERNAL_SUT_CHECK %s", raw)
}

func (h *vectorHarness) step(t *testing.T, row string, index int, s vectorStep) {
	t.Helper()
	// Reject an unknown assertion before a receive or local event can change the
	// target. An observer must only inspect effects produced by the step itself.
	for _, label := range s.Expect {
		if !knownGoVectorAssertion(label) && s.Peer == "go" {
			t.Fatalf("unhandled assertion %s", label)
		}
	}
	base := fmt.Sprintf("step/%d", index)
	if s.Peer == "java" {
		if s.Delivery != "exchange" {
			return
		}
		want, err := decodeFrame(s.Frame)
		if err != nil {
			t.Fatal(err)
		}
		var got frame
		if len(h.pending) > 0 {
			got = h.pending[0]
			h.pending = h.pending[1:]
		} else {
			got = <-h.outputs
		}
		assertFrame(t, got, want)
		reportCheck(t, row, base+"/output/"+want.Type, "internal/sut/external/vectors_test.go:vectorHarness.step")
		return
	}
	h.p.a.mu.Lock()
	beforeSeq := h.p.a.received
	h.p.a.mu.Unlock()
	beforeCalls := h.callCount
	var f frame
	var id string
	switch s.Action {
	case "receive":
		var err error
		f, err = decodeFrame(s.Frame)
		if err != nil {
			t.Fatal(err)
		}
		id = str(f.Body["invocation"])
		h.p.sendFrame(f)
		reportCheck(t, row, base+"/receive/"+f.Type, "internal/sut/external/vectors_test.go:vectorHarness.step")
	case "local":
		args := map[string]any{}
		if err := json.Unmarshal(s.Args, &args); err != nil {
			t.Fatal(err)
		}
		switch s.Event {
		case "invoke_call":
			if !fields(args, "invocation worker command context") || args["context"] != "fresh" || !identity(args["invocation"]) || !name(args["worker"]) || !name(args["command"]) {
				t.Fatal("invalid invoke_call arguments")
			}
			id = str(args["invocation"])
			h.p.a.mu.Lock()
			h.p.a.id = func() (string, error) { return id, nil }
			h.p.a.mu.Unlock()
			ctx, cancel := context.WithCancel(context.Background())
			h.contexts[id], h.cancels[id] = ctx, cancel
			ch, err := h.p.a.Invoke(ctx, str(args["worker"]), str(args["command"]))
			if err != nil {
				t.Fatal(err)
			}
			h.streams[id] = ch
		case "invoke_rejected":
			if !fields(args, "worker command reason") || !name(args["worker"]) || !name(args["command"]) || (args["reason"] != "unknown_command" && args["reason"] != "capacity") {
				t.Fatal("invalid rejected invocation arguments")
			}
			ch, err := h.p.a.Invoke(context.Background(), str(args["worker"]), str(args["command"]))
			if err == nil || ch != nil {
				t.Fatal("rejected invocation acquired a worker")
			}
			h.lastReject = err
		case "exhaust_outbound_sequence":
			if !fields(args, "limit") || args["limit"] != float64(maxSequence) {
				t.Fatal("invalid outbound sequence limit")
			}
			h.p.a.mu.Lock()
			h.p.a.seq = maxSequence
			h.p.a.mu.Unlock()
		case "exhaust_arrivals":
			if !fields(args, "limit invocation") || args["limit"] != float64(maxSequence) || !identity(args["invocation"]) {
				t.Fatal("invalid arrival sequence limit")
			}
			id = str(args["invocation"])
			h.p.a.mu.Lock()
			active := h.p.a.invocations[id]
			if active == nil || !active.accepted {
				h.p.a.mu.Unlock()
				t.Fatal("arrival exhaustion without accepted invocation")
			}
			active.arrival = maxSequence
			h.p.a.mu.Unlock()
		case "runtime_arrive_returns":
			if !fields(args, "identity result") {
				t.Fatal("invalid runtime return arguments")
			}
			ident, ok := args["identity"].(map[string]any)
			if !ok || !fields(ident, "invocation worker arrival point") {
				t.Fatal("invalid arrival identity")
			}
			id = str(ident["invocation"])
			call := h.calls[id+"/"+str(ident["arrival"])]
			if call == nil || call.worker != ident["worker"] || call.point != ident["point"] {
				t.Fatal("return without matching runtime call")
			}
			switch args["result"] {
			case "nil":
				call.result <- nil
			case "cancelled":
				if call.ctx.Err() == nil {
					t.Fatal("runtime context not canceled")
				}
			case "protocol_error":
				call.result <- errProtocol
			default:
				t.Fatal("unknown runtime return result")
			}
			<-call.returned
		case "cancel_context":
			if !fields(args, "invocation") && (!fields(args, "invocation scope") || args["scope"] != "invocation") {
				t.Fatal("unsupported cancellation scope")
			}
			id = str(args["invocation"])
			cancel := h.cancels[id]
			if cancel == nil {
				t.Fatal("unknown context")
			}
			cancel()
		case "stop_call":
			if !fields(args, "budget_ms") && !fields(args, "call_id budget_ms") {
				t.Fatal("invalid stop_call arguments")
			}
			if args["budget_ms"] != float64(5000) {
				t.Fatal("unsupported stop budget")
			}
			callID := str(args["call_id"])
			if callID == "" {
				callID = "default"
			}
			if h.stopCalls[callID] != nil {
				t.Fatal("duplicate stop call ID")
			}
			h.p.a.mu.Lock()
			if len(h.p.a.workers) == 1 {
				for _, active := range h.p.a.workers {
					id = active.id
				}
			}
			h.p.a.mu.Unlock()
			result := make(chan error, 1)
			h.stopCalls[callID] = result
			go func() { result <- h.p.a.Stop(context.Background()) }()
		case "wait_arrive_timeout":
			if !fields(args, "worker point") || !name(str(args["worker"])) || !name(str(args["point"])) {
				t.Fatal("invalid wait_arrive_timeout arguments")
			}
			h.blockedWorker = str(args["worker"])
			if len(h.p.client.calls) != 0 {
				t.Fatal("unexpected arrival before blocked transaction released")
			}
		case "hold_release_enqueue", "resume_release_enqueue":
			if !fields(args, "identity") {
				t.Fatal("invalid release barrier arguments")
			}
			ident, ok := args["identity"].(map[string]any)
			if !ok || !fields(ident, "invocation worker arrival point") {
				t.Fatal("invalid release barrier identity")
			}
			id = str(ident["invocation"])
			if h.calls[id+"/"+str(ident["arrival"])] == nil {
				t.Fatal("release barrier without runtime arrival")
			}
			if s.Event == "hold_release_enqueue" {
				if h.releaseHeld != nil {
					t.Fatal("release barrier already active")
				}
				h.releaseHeld, h.releaseResume = make(chan struct{}), make(chan struct{})
				h.p.a.beforeRelease = func() { close(h.releaseHeld); <-h.releaseResume }
			} else {
				if h.releaseHeld == nil {
					t.Fatal("release barrier was not armed")
				}
				close(h.releaseResume)
			}
		case "child_exit":
			if !fields(args, "stdout_eof exit_code") || args["stdout_eof"] != true {
				t.Fatal("unsupported child exit arguments")
			}
			h.p.a.mu.Lock()
			if len(h.p.a.workers) == 1 {
				for _, active := range h.p.a.workers {
					id = active.id
				}
			}
			h.p.a.mu.Unlock()
			switch args["exit_code"] {
			case float64(0):
				h.p.exit(nil)
			case float64(1), float64(137):
				h.p.exit(errors.New("child exited nonzero"))
			default:
				t.Fatal("unknown child exit code")
			}
		case "check_stop_results":
			if !fields(args, "call_ids expected_error") {
				t.Fatal("unsupported stop result arguments")
			}
			if args["expected_error"] != nil && args["expected_error"] != "transport_failure" && args["expected_error"] != "stop_timeout" {
				t.Fatal("unknown stop error class")
			}
			h.stopCause = str(args["expected_error"])
			ids, ok := args["call_ids"].([]any)
			if !ok || len(ids) == 0 {
				t.Fatal("missing stop call IDs")
			}
			for _, item := range ids {
				callID := str(item)
				call := h.stopCalls[callID]
				if call == nil {
					t.Fatal("unknown stop call ID")
				}
				if _, seen := h.stopErrors[callID]; !seen {
					h.stopErrors[callID] = <-call
				}
			}
		case "check_operation_result":
			if !fields(args, "scope expected_error requires_decision") || args["scope"] != "run" || args["expected_error"] != "context_cancelled" || args["requires_decision"] != "G6" {
				t.Fatal("unsupported operation result arguments")
			}
			for invocationID, ctx := range h.contexts {
				if errors.Is(ctx.Err(), context.Canceled) {
					id = invocationID
					break
				}
			}
			if id == "" {
				t.Fatal("operation context was not canceled")
			}
		case "stderr_bytes":
			if !fields(args, "byte_count segments retained_bytes retained_sha256") || args["byte_count"] != float64(1048577) || args["retained_bytes"] != float64(1048576) {
				t.Fatal("invalid stderr vector size")
			}
			segments, ok := args["segments"].([]any)
			if !ok || len(segments) != 2 {
				t.Fatal("invalid stderr segments")
			}
			var payload []byte
			for _, segment := range segments {
				item, ok := segment.(map[string]any)
				if !ok || !fields(item, "hex repeat") {
					t.Fatal("invalid stderr segment")
				}
				part, err := hex.DecodeString(str(item["hex"]))
				if err != nil || len(part) != 1 || item["repeat"].(float64) < 1 {
					t.Fatal("invalid stderr segment bytes")
				}
				payload = append(payload, bytes.Repeat(part, int(item["repeat"].(float64)))...)
			}
			if len(payload) != int(args["byte_count"].(float64)) {
				t.Fatal("stderr byte count mismatch")
			}
			done := make(chan error, 1)
			go func() { _, err := h.p.stderr.Write(payload); done <- err }()
			if err := <-done; err != nil {
				t.Fatal(err)
			}
			h.stderrDigest = str(args["retained_sha256"])
			h.stderrSize = int(args["retained_bytes"].(float64))
		case "stop_half_deadline":
			if !fields(args, "") {
				t.Fatal("invalid stop deadline arguments")
			}
			<-h.p.killed
		case "stop_deadline":
			if !fields(args, "") {
				t.Fatal("invalid stop deadline arguments")
			}
			<-h.p.a.stopDone
		case "startup_deadline":
			if !fields(args, "") {
				t.Fatal("invalid startup deadline arguments")
			}
			<-h.p.a.Faults().Done()
			synctest.Wait() // Let detached cleanup publish its cutoff after the fault.
			h.p.a.mu.Lock()
			cutoff := h.p.a.graceDeadline
			h.p.a.mu.Unlock()
			if cutoff.IsZero() {
				t.Fatal("detached cleanup did not set a cutoff")
			}
			time.Sleep(time.Until(cutoff)) // Advance only the fake supervision clock.
		case "launch_child":
			if !fields(args, "") || h.startHeld == nil {
				t.Fatal("invalid launch event")
			}
			<-h.startHeld
		case "startup_before_write":
			if !fields(args, "remaining_us") || args["remaining_us"] != float64(999) || h.startResume == nil {
				t.Fatal("invalid startup write budget")
			}
			h.p.a.mu.Lock()
			remaining := time.Duration(args["remaining_us"].(float64)) * time.Microsecond
			now := h.p.a.startDeadline.Add(-remaining)
			h.p.a.now = func() time.Time { return now }
			h.p.a.mu.Unlock()
			close(h.startResume)
		default:
			t.Fatalf("unhandled local event: %s", s.Event)
		}
		reportCheck(t, row, base+"/local/"+s.Event, "internal/sut/external/vectors_test.go:vectorHarness.step")
	default:
		t.Fatal("unknown target action")
	}
	synctest.Wait()
	for len(h.outputs) > 0 {
		h.pending = append(h.pending, <-h.outputs)
	}
	for len(h.p.client.calls) > 0 {
		call := <-h.p.client.calls
		if f.Type != "arrive" {
			t.Fatal("unexpected runtime call outside arrival receipt")
		}
		key := id + "/" + str(f.Body["arrival"])
		if h.calls[key] != nil {
			t.Fatal("duplicate runtime call")
		}
		h.calls[key] = call
		h.callCount++
	}
	for i, label := range s.Expect {
		h.observe(t, label, id, f, beforeSeq, beforeCalls)
		if label != "quarantine_fixture" && label != "reset_rejected" && label != "reset_allowed" && label != "no_runtime_finish" && label != "operation_context_error" && label != "abort_run" {
			reportCheck(t, row, fmt.Sprintf("%s/expect/%d/%s", base, i, label), "internal/sut/external/vectors_test.go:vectorHarness.observe")
		}
	}
}

func knownGoVectorAssertion(label string) bool {
	switch label {
	case "start_returns_handle", "reserve_invocation", "result_channel_created",
		"send_invoke", "send_release", "send_cancel", "mark_accepted",
		"client_arrive_once", "no_release_before_runtime_return",
		"worker_result_nil", "worker_result_error", "worker_result_cancelled",
		"failure_class_error", "failure_class_mysql_deadlock",
		"close_result_channel", "retire_invocation", "cancel_bridge",
		"supplied_invocation_context_cancelled", "consume_cancelled_arrival",
		"no_client_arrive", "no_release", "no_reply", "drop_foreign_session",
		"sequence_unchanged", "ignore_duplicate", "consume_retired_invocation",
		"no_worker_result", "no_new_worker_effect", "no_fatal", "set_single_deadline",
		"send_stop", "await_eof_and_exit", "stop_ok", "reset_allowed",
		"unstarted_outcome", "no_runtime_finish", "operation_context_error",
		"fatal_protocol", "abort_run", "close_admission", "call_pending",
		"stop_still_pending", "all_calls_return_success", "runtime_db_blocked",
		"no_transport_fault", "pending_arrival_resolves", "worker_outcome_preserved", "release_barrier_armed",
		"release_candidate_ready", "enqueue_held", "cancel_latched_atomically",
		"release_enqueued_atomically", "retained_tail_matches_digest",
		"no_protocol_effect", "no_worker_block", "latch_adapter_fault",
		"quarantine_fixture", "latch_transport_fault", "no_bridge_tasks",
		"close_pipes", "child_reaped", "retain_transport_fault",
		"no_stopped_required", "all_calls_return_same_failure",
		"reset_rejected", "await_reaping", "stop_error", "retain_adapter_fault",
		"send_fatal", "best_effort_stop", "no_stop_success",
		"startup_error", "no_invoke", "latch_startup_fault",
		"retain_startup_fault", "startup_error_preserved",
		"start_returns_no_handle", "kill_child",
		"reuse_deadline", "no_second_child", "latch_stop_timeout_failure",
		"no_nil_stop_result", "return_latched_failure", "no_new_deadline",
		"latch_startup_error", "begin_detached_cleanup", "no_start_return",
		"owned_child_waiting_start", "no_start_frame", "no_stop_frame", "no_handle",
		"admission_rejected", "no_wire_output", "outbound_sequence_at_limit",
		"arrival_sequence_at_limit", "fatal_without_wire":
		return true
	default:
		return false
	}
}

func (h *vectorHarness) observe(t *testing.T, label, id string, f frame, beforeSeq, beforeCalls int) {
	t.Helper()
	a := h.p.a
	a.mu.Lock()
	defer a.mu.Unlock()
	w := a.invocations[id]
	need := func(ok bool) {
		t.Helper()
		if !ok {
			t.Fatalf("unmet vector assertion %s", label)
		}
	}
	result := func() sut.InvocationOutcome {
		if r, ok := h.outcomes[id]; ok {
			return r
		}
		select {
		case r, ok := <-h.streams[id]:
			need(ok)
			h.outcomes[id] = r
			return r
		default:
			t.Fatalf("result unavailable for %s", label)
			return sut.InvocationOutcome{}
		}
	}
	switch label {
	case "start_returns_handle":
		need(a.ready && !a.stopping)
		h.child = a.proc
		select {
		case err := <-h.start:
			need(err == nil)
		default:
			t.Fatal("Start still pending")
		}
	case "reserve_invocation":
		need(w != nil && !w.retired && a.workers[w.worker] == w)
	case "result_channel_created":
		need(h.streams[id] != nil)
	case "send_invoke", "send_release", "send_cancel":
		found := false
		for _, output := range h.pending {
			if output.Type == strings.TrimPrefix(label, "send_") && output.Body["invocation"] == id {
				found = true
				break
			}
		}
		need(found)
	case "mark_accepted":
		need(w != nil && w.accepted && w.sending)
		select {
		case <-w.delivered:
		default:
			t.Fatal("accepted before write completion")
		}
	case "client_arrive_once":
		need(h.callCount == beforeCalls+1 && w != nil && w.bridges == 1)
	case "no_release_before_runtime_return":
		need(w != nil && w.outstanding && len(h.pending) == 0)
	case "worker_result_nil":
		r := result()
		need(r.Worker != nil && r.Unstarted == nil && r.Worker.Err == nil)
	case "worker_result_error", "worker_result_cancelled":
		r := result()
		need(r.Worker != nil && r.Worker.Err != nil)
		if label == "worker_result_cancelled" {
			need(errors.Is(r.Worker.Err, context.Canceled))
		}
	case "failure_class_error":
		need(orchestrator.ClassifyWorkerFailure(result().Worker.Err) == orchestrator.WorkerFailureError)
	case "failure_class_mysql_deadlock":
		need(orchestrator.ClassifyWorkerFailure(result().Worker.Err) == orchestrator.WorkerFailureMySQLDeadlock)
	case "close_result_channel":
		result()
		select {
		case _, ok := <-h.streams[id]:
			need(!ok)
		default:
			t.Fatal("result channel still open")
		}
	case "retire_invocation":
		need(w != nil && w.retired && w.bridges == 0 && a.workers[w.worker] != w)
	case "cancel_bridge":
		cancelled := w != nil && w.ctx.Err() != nil
		for key, call := range h.calls {
			if strings.HasPrefix(key, id+"/") && call.ctx.Err() != nil {
				cancelled = true
			}
		}
		need(cancelled && (a.faults.Err() != nil || h.contexts[id] != nil && h.contexts[id].Err() != nil || w != nil && (w.reason == "context" || w.reason == "stop")))
	case "supplied_invocation_context_cancelled":
		need(h.contexts[id] != nil && h.contexts[id].Err() != nil)
	case "consume_cancelled_arrival":
		need(w != nil && w.reason != "" && !w.outstanding && a.received == f.Seq)
	case "no_client_arrive":
		need(h.callCount == beforeCalls)
	case "no_release":
		for _, output := range h.pending {
			need(output.Type != "release")
		}
	case "no_reply":
		need(len(h.pending) == 0)
	case "drop_foreign_session":
		need(a.stale > 0 && f.Session != a.session)
	case "sequence_unchanged", "ignore_duplicate":
		need(a.received == beforeSeq)
	case "consume_retired_invocation":
		need(w != nil && w.retired && a.received == f.Seq)
	case "no_worker_result":
		for _, ch := range h.streams {
			need(len(ch) == 0)
		}
	case "no_new_worker_effect":
		need(w != nil && w.retired)
		next := a.workers[w.worker]
		need(next != nil && next != w && next.accepted && !next.retired && next.reason == "" && len(next.results) == 0)
	case "no_fatal":
		need(a.faults.Err() == nil)
	case "set_single_deadline":
		need(a.stopping && !a.stopDeadline.IsZero() && !a.graceDeadline.IsZero())
		if h.stopDeadline.IsZero() {
			h.stopDeadline = a.stopDeadline
		} else {
			need(a.stopDeadline.Equal(h.stopDeadline))
		}
	case "send_stop":
		found := false
		for _, output := range h.pending {
			found = found || output.Type == "stop"
		}
		need(found)
	case "await_eof_and_exit":
		need(a.stopped && !a.eof)
		select {
		case <-a.stopDone:
			t.Fatal("Stop returned before EOF and process exit")
		default:
		}
	case "stop_ok":
		select {
		case <-a.stopDone:
			need(a.stopErr == nil && a.eof && a.exitErr == nil)
		default:
			t.Fatal("Stop did not complete after child exit")
		}
	case "reset_allowed":
		need(a.stopErr == nil && a.faults.Err() == nil && a.stopped && a.eof)
	case "unstarted_outcome":
		r := result()
		need(r.Unstarted != nil && r.Worker == nil && errors.Is(r.Unstarted.Err, context.Canceled))
	case "no_runtime_finish":
		// Only the orchestrator's runtime can witness Finish.
	case "operation_context_error":
		// A canceled invocation context cannot prove the Run error.
	case "fatal_protocol":
		need(errors.Is(a.faults.Err(), errProtocol))
	case "abort_run":
		need(a.faults.Err() != nil) // The Run error is checked separately.
	case "latch_adapter_fault":
		need(a.faults.Err() != nil)
		if f.Type == "fatal" {
			need(strings.Contains(a.faults.Err().Error(), str(f.Body["kind"])))
		} else {
			need(errors.Is(a.faults.Err(), errProtocol))
		}
		h.originalFault = a.faults.Err()
	case "quarantine_fixture":
		// This harness does not own a fixture. The orchestrator witness reports
		// this check after observing a real Quarantine and rejected Reset.
		need(a.faults.Err() != nil)
	case "close_admission":
		need(a.stopping)
	case "call_pending", "stop_still_pending":
		for _, call := range h.stopCalls {
			select {
			case <-call:
				t.Fatal("Stop returned before cleanup completion")
			default:
			}
		}
	case "all_calls_return_success":
		need(len(h.stopErrors) > 0)
		for _, err := range h.stopErrors {
			need(err == nil)
		}
	case "all_calls_return_same_failure":
		need(len(h.stopErrors) > 0)
		for _, err := range h.stopErrors {
			switch h.stopCause {
			case "transport_failure":
				need(errors.Is(err, errTransport))
			case "stop_timeout":
				need(errors.Is(err, context.DeadlineExceeded))
			default:
				t.Fatal("unknown stop failure class")
			}
		}
	case "latch_transport_fault", "retain_transport_fault":
		need(errors.Is(a.faults.Err(), errTransport))
	case "no_bridge_tasks":
		for _, active := range a.invocations {
			need(active.bridges == 0)
		}
	case "close_pipes":
		select {
		case <-a.readDone:
		default:
			t.Fatal("reader not stopped")
		}
		select {
		case <-a.writeDone:
		default:
			t.Fatal("writer not stopped")
		}
	case "child_reaped":
		select {
		case <-a.exitDone:
		default:
			t.Fatal("child not reaped")
		}
	case "no_stopped_required":
		need(!a.stopped && a.faults.Err() != nil)
	case "await_reaping":
		need(a.stopping && a.faults.Err() != nil)
	case "stop_error":
		select {
		case <-a.stopDone:
			need(a.stopErr != nil)
		default:
			t.Fatal("Stop did not finish with an error")
		}
	case "retain_adapter_fault":
		need(h.originalFault != nil && a.faults.Err() == h.originalFault)
		if a.stopErr != nil {
			need(errors.Is(a.stopErr, h.originalFault))
		}
	case "send_fatal":
		found := false
		for _, output := range h.pending {
			found = found || output.Type == "fatal"
		}
		need(found)
	case "best_effort_stop":
		need(a.stopping)
	case "no_stop_success":
		need(a.stopping && a.faults.Err() != nil)
	case "startup_error":
		need(a.faults.Err() != nil && !a.ready)
		if h.originalFault == nil {
			h.originalFault = a.faults.Err()
		}
	case "latch_startup_error":
		need(a.faults.Err() != nil && !a.ready)
		h.originalFault = a.faults.Err()
	case "begin_detached_cleanup":
		need(a.stopping && a.faults.Err() != nil)
	case "no_start_return":
		need(!a.ready && a.faults.Err() != nil)
	case "owned_child_waiting_start":
		need(a.proc != nil && !a.startSent && h.startHeld != nil)
	case "no_start_frame":
		need(!a.startSent && len(h.pending) == 0)
	case "no_stop_frame":
		need(!a.stopSent)
	case "no_handle":
		need(!a.ready)
		select {
		case err := <-h.start:
			need(err != nil)
		default:
			t.Fatal("Start has not returned its failure")
		}
	case "admission_rejected":
		need(h.lastReject != nil && a.faults.Err() == nil)
	case "no_wire_output":
		need(len(h.pending) == 0 && len(h.outputs) == 0)
	case "outbound_sequence_at_limit":
		need(a.seq == maxSequence && a.faults.Err() == nil)
	case "arrival_sequence_at_limit":
		need(w != nil && w.arrival == maxSequence && w.bridges == 0)
	case "fatal_without_wire":
		need(errors.Is(a.faults.Err(), errProtocol) && len(h.pending) == 0)
	case "no_invoke":
		need(len(a.invocations) == 0 && len(h.streams) == 0)
	case "latch_startup_fault":
		need(a.faults.Err() != nil && !a.ready)
		h.originalFault = a.faults.Err()
	case "retain_startup_fault", "startup_error_preserved":
		need(h.originalFault != nil && a.faults.Err() == h.originalFault)
	case "start_returns_no_handle":
		need(!a.ready)
		select {
		case err := <-h.start:
			need(err != nil)
		default:
			t.Fatal("Start has not returned its failure")
		}
	case "kill_child":
		select {
		case <-h.p.killed:
		default:
			t.Fatal("child was not killed")
		}
	case "reuse_deadline", "no_new_deadline":
		need(!h.stopDeadline.IsZero() && a.stopDeadline.Equal(h.stopDeadline))
	case "no_second_child":
		need(h.child != nil && a.proc == h.child)
	case "latch_stop_timeout_failure":
		need(errors.Is(a.stopErr, context.DeadlineExceeded) && a.faults.Err() != nil)
	case "no_nil_stop_result":
		need(len(h.stopErrors) > 0)
		for _, err := range h.stopErrors {
			need(err != nil)
		}
	case "return_latched_failure":
		select {
		case <-a.stopDone:
			need(a.stopErr != nil)
		default:
			t.Fatal("later Stop caller did not inherit completed cleanup")
		}
	case "reset_rejected":
		need(a.stopErr != nil && a.faults.Err() != nil)
	case "runtime_db_blocked":
		need(h.blockedWorker != "" && len(h.p.client.calls) == 0)
		for _, call := range h.calls {
			need(call.worker != h.blockedWorker)
		}
	case "no_transport_fault":
		need(a.faults.Err() == nil)
	case "pending_arrival_resolves":
		need(h.blockedWorker != "" && w != nil && w.worker == h.blockedWorker && w.bridges == 1)
	case "worker_outcome_preserved":
		r, ok := h.outcomes[id]
		need(ok && r.Worker != nil && r.Worker.Err == nil && r.Unstarted == nil)
	case "release_barrier_armed":
		need(h.releaseHeld != nil && h.releaseResume != nil && w != nil && w.bridges == 1)
	case "release_candidate_ready", "enqueue_held":
		need(h.releaseHeld != nil && w != nil && w.bridges == 1)
		select {
		case <-h.releaseHeld:
		default:
			t.Fatal("release candidate did not reach barrier")
		}
		for _, output := range h.pending {
			need(output.Type != "release")
		}
	case "cancel_latched_atomically":
		need(w != nil && w.reason == "context" && w.ctx.Err() != nil)
	case "release_enqueued_atomically":
		found := false
		for _, output := range h.pending {
			found = found || output.Type == "release" && output.Body["invocation"] == id
		}
		need(found && w != nil && !w.outstanding)
	case "retained_tail_matches_digest":
		a.logs.mu.Lock()
		retained := bytes.Clone(a.logs.bytes)
		a.logs.mu.Unlock()
		digest := sha256.Sum256(retained)
		need(len(retained) == h.stderrSize && hex.EncodeToString(digest[:]) == h.stderrDigest)
	case "no_protocol_effect":
		need(a.faults.Err() == nil && a.ready && !a.stopping)
	case "no_worker_block":
		need(len(a.workers) == 1 && len(h.streams) == 1 && len(h.pending) == 0)
	default:
		t.Fatalf("unhandled assertion %s", label)
	}
}
