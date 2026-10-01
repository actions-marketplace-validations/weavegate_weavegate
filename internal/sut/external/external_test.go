package external

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/sut"
)

type arrivalCall struct {
	worker, point string
	ctx           context.Context
	result        chan error
	returned      chan struct{}
}
type controlledClient struct{ calls chan *arrivalCall }

func (c *controlledClient) Arrive(ctx context.Context, worker, point string) error {
	call := &arrivalCall{worker: worker, point: point, ctx: ctx, result: make(chan error, 1), returned: make(chan struct{})}
	c.calls <- call
	defer close(call.returned)
	select {
	case err := <-call.result:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

type peer struct {
	t              *testing.T
	a              *adapter
	client         *controlledClient
	input          *io.PipeReader
	output, stderr *io.PipeWriter
	exitCh         chan error
	exitOnce       sync.Once
	next           int
	killed         chan struct{}
	killOnce       sync.Once
	deferKillExit  bool
}

func testOptions() Options {
	return Options{Java: "java", JAR: "test.jar", RunID: strings.Repeat("1", 32), Commands: []string{"assign"}, Points: []string{"after_read", "before_write"}, Capacity: 2,
		StartupTimeout: 10 * time.Second, CancelTimeout: time.Second, StopTimeout: 5 * time.Second}
}

func newPeer(t *testing.T) *peer {
	t.Helper()
	c := &controlledClient{calls: make(chan *arrivalCall, 1024)}
	s, err := New(testOptions(), c)
	if err != nil {
		t.Fatal(err)
	}
	a := s.(*adapter)
	n := 2
	a.id = func() (string, error) { id := fmt.Sprintf("%032x", n); n++; return id, nil }
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	errR, errW := io.Pipe()
	p := &peer{t: t, a: a, client: c, input: inR, output: outW, stderr: errW, exitCh: make(chan error, 1), killed: make(chan struct{})}
	a.launch = func(string, string) (*child, error) {
		return &child{stdin: inW, stdout: outR, stderr: errR, wait: func() error { return <-p.exitCh }, kill: func() error {
			p.killOnce.Do(func() { close(p.killed) })
			if !p.deferKillExit {
				p.exit(errors.New("killed"))
			}
			return nil
		}}, nil
	}
	t.Cleanup(func() {
		p.exit(nil)
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = a.Stop(ctx)
	})
	return p
}

func (p *peer) exit(err error) {
	p.exitOnce.Do(func() { _ = p.output.Close(); _ = p.stderr.Close(); _ = p.input.Close(); p.exitCh <- err })
}

func startBody() map[string]any {
	return map[string]any{"variant": "fixed", "params": map[string]string{"request_id": "1"}, "commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2, "startup_ms": 10000, "cancel_ms": 1000,
		"database": map[string]any{"driver": "mysql", "host": "127.0.0.1", "port": 33060, "name": "weavegate", "username": "synthetic", "password": "synthetic-only"}}
}

func (p *peer) startAsync(ctx context.Context) <-chan error {
	result := make(chan error, 1)
	go func() {
		h, err := p.a.start(ctx, startBody())
		if err == nil && h == nil {
			err = errors.New("missing handle")
		}
		result <- err
	}()
	return result
}

func (p *peer) start() {
	p.t.Helper()
	result := p.startAsync(context.Background())
	f := p.read("start")
	if number(f.Body["startup_ms"]) != 10000 {
		p.t.Fatal("startup deadline changed")
	}
	p.send("ready", map[string]any{"commands": []string{"assign"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
	if err := <-result; err != nil {
		p.t.Fatal(err)
	}
}

func (p *peer) read(kind string) frame {
	p.t.Helper()
	f, _, err := readFrame(p.input)
	if err != nil {
		p.t.Fatalf("read %s: %v", kind, err)
	}
	if f.Type != kind {
		p.t.Fatalf("expected %s, got %s", kind, f.Type)
	}
	return f
}
func (p *peer) send(kind string, body map[string]any) frame {
	p.t.Helper()
	p.next++
	f := frame{V: 1, Type: kind, Run: p.a.run, Session: p.a.session, Seq: p.next, Body: body}
	p.sendFrame(f)
	return f
}
func (p *peer) sendFrame(f frame) {
	p.t.Helper()
	if err := writeFrame(p.output, f); err != nil {
		p.t.Fatal(err)
	}
}
func (p *peer) invoke(ctx context.Context, worker string) (frame, <-chan sut.InvocationOutcome) {
	p.t.Helper()
	ch, err := p.a.Invoke(ctx, worker, "assign")
	if err != nil {
		p.t.Fatal(err)
	}
	f := p.read("invoke")
	p.send("accepted", binding(f))
	return f, ch
}
func binding(f frame) map[string]any {
	return map[string]any{"invocation": f.Body["invocation"], "worker": f.Body["worker"]}
}
func arrivalBody(f frame, n int, point string) map[string]any {
	b := binding(f)
	b["arrival"] = fmt.Sprint(n)
	b["point"] = point
	return b
}
func terminalBody(f frame, tx string, wireErr any) map[string]any {
	b := binding(f)
	b["transaction"] = tx
	b["connection"] = "returned"
	b["error"] = wireErr
	return b
}
func wireError(kind, message string, code int, state string) map[string]any {
	return map[string]any{"kind": kind, "message": message, "mysql_code": code, "sql_state": state}
}
func outcome(t *testing.T, ch <-chan sut.InvocationOutcome) sut.InvocationOutcome {
	t.Helper()
	r, ok := <-ch
	if !ok {
		t.Fatal("closed without outcome")
	}
	if _, ok := <-ch; ok {
		t.Fatal("more than one outcome")
	}
	return r
}
func (p *peer) stop() {
	p.t.Helper()
	result := make(chan error, 1)
	go func() { result <- p.a.Stop(context.Background()) }()
	f := p.read("stop")
	if number(f.Body["budget_ms"]) != 2500 {
		p.t.Fatal("incorrect graceful budget")
	}
	p.send("stopped", map[string]any{})
	p.exit(nil)
	if err := <-result; err != nil {
		p.t.Fatal(err)
	}
	if err := p.a.Stop(context.Background()); err != nil {
		p.t.Fatal("idempotent stop", err)
	}
}

func TestTargetedBridgeAndWorkerReuse(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		p.start()
		w1, ch1 := p.invoke(context.Background(), "w1")
		p.send("arrive", arrivalBody(w1, 1, "after_read"))
		c1 := <-p.client.calls
		// A blocked bridge cannot prevent another accepted/terminal frame.
		w2, ch2 := p.invoke(context.Background(), "w2")
		p.send("arrive", arrivalBody(w2, 1, "after_read"))
		c2 := <-p.client.calls
		c1.result <- nil
		release := p.read("release")
		if release.Body["invocation"] != w1.Body["invocation"] {
			t.Fatal("release targeted wrong worker")
		}
		p.send("arrive", arrivalBody(w1, 2, "before_write"))
		c3 := <-p.client.calls
		c3.result <- nil
		p.read("release")
		p.send("terminal", terminalBody(w1, "committed", nil))
		if r := outcome(t, ch1); r.Worker == nil || r.Worker.Err != nil {
			t.Fatal("committed outcome", r)
		}
		// Retired arrivals and identical terminal bodies cannot affect reused w1.
		w3, ch3 := p.invoke(context.Background(), "w1")
		p.send("arrive", arrivalBody(w1, 3, "after_read"))
		p.send("terminal", terminalBody(w1, "committed", nil))
		p.send("terminal", terminalBody(w3, "committed", nil))
		outcome(t, ch3)
		c2.result <- nil
		p.read("release")
		p.send("terminal", terminalBody(w2, "rolled_back", wireError("application", "synthetic-only private SQL", 0, "")))
		if r := outcome(t, ch2); r.Worker == nil || r.Worker.Err == nil || strings.Contains(r.Worker.Err.Error(), "synthetic-only") {
			t.Fatal("rollback error/redaction", r)
		}
		if len(p.client.calls) != 0 {
			t.Fatal("retired invocation called runtime")
		}
		p.stop()
		t.Log("EXTERNAL_SUT_BRIDGE_RESULT targeted=true reader=live incremented=true worker_reuse=isolated retired_terminal=no_effect rollback=error")
	})
}

func TestCancellationOrderings(t *testing.T) {
	for _, cancelFirst := range []bool{true, false} {
		t.Run(fmt.Sprint(cancelFirst), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				held, resume := make(chan struct{}), make(chan struct{})
				p.a.beforeRelease = func() { close(held); <-resume }
				p.start()
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				w, ch := p.invoke(ctx, "w1")
				p.send("arrive", arrivalBody(w, 1, "after_read"))
				call := <-p.client.calls
				call.result <- nil
				<-held
				if cancelFirst {
					cancel()
					f := p.read("cancel")
					if f.Body["reason"] != "context" {
						t.Fatal("wrong origin")
					}
					close(resume)
				} else {
					close(resume)
					p.read("release")
					cancel()
					p.read("cancel")
				}
				p.send("terminal", terminalBody(w, "rolled_back", wireError("cancelled", "cancelled by context", 0, "")))
				r := outcome(t, ch)
				if r.Worker == nil || !errors.Is(r.Worker.Err, context.Canceled) {
					t.Fatal("cancellation identity lost")
				}
				// The next frame must be invoke, never a late release from the retired call.
				w2, ch2 := p.invoke(context.Background(), "w1")
				p.send("terminal", terminalBody(w2, "committed", nil))
				outcome(t, ch2)
				p.stop()
			})
		})
	}
	t.Log("EXTERNAL_SUT_CANCEL_RESULT cancel_wins=no_release release_wins=release_then_cancel reuse=fresh")
}

func TestUnstartedAndCommitWinsCancellation(t *testing.T) {
	for _, tx := range []string{"not_started", "committed"} {
		t.Run(tx, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.start()
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				ch, err := p.a.Invoke(ctx, "w1", "assign")
				if err != nil {
					t.Fatal(err)
				}
				w := p.read("invoke")
				cancel()
				p.read("cancel")
				p.send("accepted", binding(w))
				b := terminalBody(w, tx, nil)
				if tx == "not_started" {
					b["connection"] = "not_acquired"
					b["error"] = wireError("cancelled", "cancelled by context", 0, "")
				}
				p.send("terminal", b)
				r := outcome(t, ch)
				if tx == "not_started" {
					if r.Worker != nil || r.Unstarted == nil || !errors.Is(r.Unstarted.Err, context.Canceled) {
						t.Fatal("fabricated started result")
					}
				} else if r.Worker == nil || r.Worker.Err != nil {
					t.Fatal("committed fact rewritten")
				}
				p.stop()
			})
		})
	}
}

func TestMySQLClassification(t *testing.T) {
	for _, code := range []int{1213, 1205} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.start()
				w, ch := p.invoke(context.Background(), "w1")
				p.send("terminal", terminalBody(w, "rolled_back", wireError("mysql", "jdbc:mysql://private?password=synthetic-only", code, "40001")))
				r := outcome(t, ch)
				var sqlErr *mysql.MySQLError
				if !errors.As(r.Worker.Err, &sqlErr) || int(sqlErr.Number) != code || string(sqlErr.SQLState[:]) != "40001" {
					t.Fatal("vendor metadata lost")
				}
				if strings.Contains(r.Worker.Err.Error(), "synthetic-only") {
					t.Fatal("raw message leaked")
				}
				want := orchestrator.WorkerFailureError
				if code == 1213 {
					want = orchestrator.WorkerFailureMySQLDeadlock
				}
				if got := orchestrator.ClassifyWorkerFailure(r.Worker.Err); got != want {
					t.Fatalf("failure classification: %v, want %v", got, want)
				}
				p.stop()
			})
		})
	}
}

func TestProtocolStateRejections(t *testing.T) {
	for _, name := range []string{"unknown_invocation", "worker_binding", "accepted_binding", "terminal_binding", "unknown_point", "sequence_gap", "sequence_exhaustion", "arrival_exhaustion", "conflicting_duplicate", "terminal_while_arrived", "retired_terminal_conflict", "wrong_direction", "duplicate_start", "duplicate_invoke", "duplicate_accepted", "arrival_gap", "concurrent_arrival", "released_arrival_duplicate"} {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.start()
				w, ch := p.invoke(context.Background(), "w1")
				switch name {
				case "unknown_invocation":
					b := binding(w)
					b["invocation"] = strings.Repeat("f", 32)
					p.send("accepted", b)
				case "worker_binding":
					b := binding(w)
					b["worker"] = "w2"
					p.send("arrive", withArrival(b))
				case "accepted_binding":
					b := binding(w)
					b["worker"] = "w2"
					p.send("accepted", b)
				case "terminal_binding":
					b := terminalBody(w, "committed", nil)
					b["worker"] = "w2"
					p.send("terminal", b)
				case "unknown_point":
					p.send("arrive", arrivalBody(w, 1, "missing"))
				case "sequence_gap":
					p.next++
					p.send("arrive", arrivalBody(w, 1, "after_read"))
				case "sequence_exhaustion":
					p.next = maxSequence - 1
					p.send("arrive", arrivalBody(w, 1, "after_read"))
				case "arrival_exhaustion":
					p.send("arrive", arrivalBody(w, maxSequence, "after_read"))
				case "conflicting_duplicate":
					p.next--
					p.send("arrive", arrivalBody(w, 1, "after_read"))
				case "terminal_while_arrived":
					p.send("arrive", arrivalBody(w, 1, "after_read"))
					<-p.client.calls
					p.send("terminal", terminalBody(w, "committed", nil))
				case "retired_terminal_conflict":
					p.send("terminal", terminalBody(w, "committed", nil))
					outcome(t, ch)
					_, ch = p.invoke(context.Background(), "w1")
					p.send("terminal", terminalBody(w, "rolled_back", wireError("application", "rollback", 0, "")))
				case "wrong_direction":
					p.send("release", arrivalBody(w, 1, "after_read"))
				case "duplicate_start":
					p.send("start", startBody())
				case "duplicate_invoke":
					b := binding(w)
					b["command"] = "assign"
					p.send("invoke", b)
				case "duplicate_accepted":
					p.send("accepted", binding(w))
				case "arrival_gap":
					p.send("arrive", arrivalBody(w, 2, "after_read"))
				case "concurrent_arrival":
					p.send("arrive", arrivalBody(w, 1, "after_read"))
					<-p.client.calls
					p.send("arrive", arrivalBody(w, 2, "before_write"))
				case "released_arrival_duplicate":
					p.send("arrive", arrivalBody(w, 1, "after_read"))
					call := <-p.client.calls
					call.result <- nil
					p.read("release")
					p.send("arrive", arrivalBody(w, 1, "after_read"))
				}
				<-p.a.Faults().Done()
				p.read("fatal")
				if _, ok := <-ch; ok {
					t.Fatal("fault fabricated outcome")
				}
				p.exit(errors.New("protocol exit"))
				<-p.a.stopDone
				if !errors.Is(p.a.Stop(context.Background()), errProtocol) {
					t.Fatal("original protocol cause lost")
				}
			})
		})
	}
	reportCheck(t, "requirement/go-wire-matrix", "observe/evidence", "internal/sut/external/external_test.go:TestProtocolStateRejections")
}
func withArrival(b map[string]any) map[string]any {
	b["arrival"] = "1"
	b["point"] = "after_read"
	return b
}

func TestDuplicateAndForeignSessionIsolation(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		p.start()
		w, ch := p.invoke(context.Background(), "w1")
		f := p.send("arrive", arrivalBody(w, 1, "after_read"))
		call := <-p.client.calls
		p.sendFrame(f)
		foreign := f
		foreign.Session = strings.Repeat("f", 32)
		foreign.Seq = 100000
		p.sendFrame(foreign)
		call.result <- nil
		p.read("release")
		p.send("terminal", terminalBody(w, "committed", nil))
		outcome(t, ch)
		if len(p.client.calls) != 0 {
			t.Fatal("duplicate called runtime")
		}
		p.a.mu.Lock()
		stale := p.a.stale
		p.a.mu.Unlock()
		if stale != 1 {
			t.Fatal("stale counter")
		}
		p.stop()
	})
}

func TestStopActiveAndFirstCancellationOrigin(t *testing.T) {
	for _, cancelFirst := range []bool{true, false} {
		t.Run(fmt.Sprint(cancelFirst), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				p.start()
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				w, ch := p.invoke(ctx, "w1")
				p.send("arrive", arrivalBody(w, 1, "after_read"))
				call := <-p.client.calls
				if cancelFirst {
					cancel()
				}
				stops := make(chan error, 2)
				go func() { stops <- p.a.Stop(context.Background()) }()
				go func() { stops <- p.a.Stop(context.Background()) }()
				f := p.read("cancel")
				reason := "stop"
				if cancelFirst {
					reason = "context"
				}
				if f.Body["reason"] != reason {
					t.Fatal("cancel origin replaced")
				}
				p.read("stop")
				<-call.returned
				synctest.Wait()
				select {
				case err := <-stops:
					t.Fatalf("premature Stop: %v", err)
				default:
				}
				p.send("terminal", terminalBody(w, "rolled_back", wireError("cancelled", "cancelled by "+reason, 0, "")))
				outcome(t, ch)
				p.send("stopped", map[string]any{})
				synctest.Wait()
				select {
				case err := <-stops:
					t.Fatalf("Stop before EOF/exit: %v", err)
				default:
				}
				p.exit(nil)
				for i := 0; i < 2; i++ {
					if err := <-stops; err != nil {
						t.Fatal(err)
					}
				}
			})
		})
	}
	reportCheck(t, "requirement/go-cancel-origin", "observe/evidence", "internal/sut/external/external_test.go:TestStopActiveAndFirstCancellationOrigin")
}

func TestProcessDeathUnwindsAndReaps(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		p.start()
		w, ch := p.invoke(context.Background(), "w1")
		p.send("arrive", arrivalBody(w, 1, "after_read"))
		call := <-p.client.calls
		p.exit(errors.New("process died"))
		<-p.a.Faults().Done()
		<-call.returned
		if _, ok := <-ch; ok {
			t.Fatal("invented outcome after process death")
		}
		if err := p.a.Stop(context.Background()); !errors.Is(err, errTransport) {
			t.Fatal("transport cause lost", err)
		}
		select {
		case <-p.a.exitDone:
		default:
			t.Fatal("not reaped")
		}
		p.a.mu.Lock()
		bridges := p.a.invocations[str(w.Body["invocation"])].bridges
		p.a.mu.Unlock()
		if bridges != 0 {
			t.Fatal("bridge survived Stop")
		}
		t.Log("EXTERNAL_SUT_DEATH_RESULT fault=transport outcome=absent bridge=unwound child=reaped stop=error")
	})
}

func TestSilentStopUsesOneDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		p.start()
		start := time.Now()
		done := make(chan error, 2)
		go func() { done <- p.a.Stop(context.Background()) }()
		p.read("stop")
		go func() { done <- p.a.Stop(context.Background()) }()
		// Only deadline timers advance virtual time; no sleep coordinates a peer.
		for i := 0; i < 2; i++ {
			if err := <-done; err == nil {
				t.Fatal("silent peer stop succeeded")
			}
		}
		if time.Since(start) != 2500*time.Millisecond {
			t.Fatalf("wrong forced cutoff: %v", time.Since(start))
		}
		select {
		case <-p.killed:
		default:
			t.Fatal("child not killed")
		}
		if err := p.a.Stop(context.Background()); err == nil {
			t.Fatal("failure cleared")
		}
	})
}

func TestStartupMismatchAndNoReady(t *testing.T) {
	for _, mode := range []string{"mismatch", "unsolicited_stopped", "no_ready", "stop_before_ready", "submillisecond"} {
		t.Run(mode, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				p := newPeer(t)
				if mode == "submillisecond" {
					p.a.beforeWrite = func(kind string) {
						if kind == "start" {
							p.a.mu.Lock()
							p.a.now = func() time.Time { return p.a.startDeadline.Add(-999 * time.Microsecond) }
							p.a.mu.Unlock()
						}
					}
				}
				result := p.startAsync(context.Background())
				if mode == "submillisecond" {
					if err := <-result; err == nil {
						t.Fatal("submillisecond startup succeeded")
					}
					return
				}
				p.read("start")
				switch mode {
				case "mismatch":
					p.send("ready", map[string]any{"commands": []string{"wrong"}, "points": []string{"after_read", "before_write"}, "capacity": 2})
					p.read("fatal")
					p.exit(errors.New("fatal"))
				case "unsolicited_stopped":
					p.send("stopped", map[string]any{})
					p.read("fatal")
					p.exit(errors.New("fatal"))
				case "stop_before_ready":
					stop := make(chan error, 1)
					go func() { stop <- p.a.Stop(context.Background()) }()
					p.read("stop")
					p.send("stopped", map[string]any{})
					p.exit(nil)
					if err := <-stop; err != nil {
						t.Fatal(err)
					}
				case "no_ready": // Leave both pipes live and the writer blocked until its governing cutoff.
				}
				if err := <-result; err == nil {
					t.Fatal("startup returned a handle")
				}
				select {
				case <-p.a.exitDone:
				default:
					t.Fatal("startup returned before child reaped")
				}
			})
		})
	}
}

func TestAdmissionValidationAndTailBound(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		p := newPeer(t)
		if _, err := p.a.Start(context.Background(), sut.SUTConfig{}, &fixture.DB{}); err == nil {
			t.Fatal("missing descriptor accepted")
		}
		p.start()
		_, _ = p.invoke(context.Background(), "w1")
		_, _ = p.invoke(context.Background(), "w2")
		for _, worker := range []string{"w1", "w3"} {
			if _, err := p.a.Invoke(context.Background(), worker, "assign"); err == nil {
				t.Fatal("reserved worker/capacity accepted")
			}
		}
		if _, err := p.a.Invoke(context.Background(), "other", "missing"); err == nil {
			t.Fatal("unknown command accepted")
		}
		logs := strings.Repeat("x", maxFrame) + "tail"
		if _, err := io.WriteString(p.stderr, logs); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		p.a.logs.mu.Lock()
		defer p.a.logs.mu.Unlock()
		if len(p.a.logs.bytes) != maxFrame || !strings.HasSuffix(string(p.a.logs.bytes), "tail") {
			t.Fatal("stderr tail was not bounded")
		}
	})
}
