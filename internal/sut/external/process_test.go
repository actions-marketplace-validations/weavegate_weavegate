package external

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/sut"
)

// The test binary stands in for the prebuilt JVM, through the exact production
// JVM launch path, including the selected compilation policy. No shell,
// inherited extra FD, or listener is needed. The helper exercises OS pipes
// and Wait, not an in-memory process.
func TestMain(m *testing.M) {
	args := os.Args[1:]
	tuned := len(args) == 3 && args[0] == "-XX:TieredStopAtLevel=1"
	if tuned {
		args = args[1:]
	}
	if len(args) == 2 && args[0] == "-jar" && strings.HasPrefix(args[1], "external-test-") {
		mode := strings.TrimPrefix(args[1], "external-test-")
		if (mode == "default_jvm") == tuned {
			os.Exit(14)
		}
		os.Exit(runChild(mode))
	}
	os.Exit(m.Run())
}

func runChild(mode string) int {
	if mode == "startup_eof" {
		return 0
	}
	if mode == "blocked_start" {
		blockChild()
		return 9
	}
	start, _, err := readFrame(os.Stdin)
	if err != nil || start.Type != "start" {
		return 2
	}
	if mode == "broken_writer" {
		if err := os.Stdin.Close(); err != nil {
			return 13
		}
	}
	seq := 0
	send := func(kind string, body map[string]any) bool {
		seq++
		return writeFrame(os.Stdout, frame{V: 1, Type: kind, Run: start.Run, Session: start.Session, Seq: seq, Body: body}) == nil
	}
	if !send("ready", map[string]any{"commands": start.Body["commands"], "points": start.Body["points"], "capacity": start.Body["capacity"]}) {
		return 3
	}
	if mode == "blocked_writer" || mode == "broken_writer" {
		blockChild()
		return 9
	}
	for {
		f, _, err := readFrame(os.Stdin)
		if err != nil {
			return 4
		}
		switch f.Type {
		case "invoke":
			if mode == "active_eof" {
				return 0
			}
			if !send("accepted", binding(f)) || !send("terminal", terminalBody(f, "committed", nil)) {
				return 5
			}
			if mode == "post_terminal_eof" {
				return 7
			}
		case "cancel":
		case "stop":
			switch mode {
			case "missing_stopped":
				return 0
			case "silent_stop":
				blockChild()
				return 9
			case "nonzero_stop":
				send("stopped", map[string]any{})
				return 6
			}
			if !send("stopped", map[string]any{}) {
				return 8
			}
			return 0
		case "fatal":
			return 10
		default:
			return 11
		}
	}
}

func blockChild() {
	// An OS read on a pipe whose writer remains open models a live child stuck
	// outside the control reader. Only parent termination releases this process.
	r, w, err := os.Pipe()
	if err != nil {
		os.Exit(12)
	}
	defer func() { _ = r.Close() }()
	defer func() { _ = w.Close() }()
	_, _ = io.Copy(io.Discard, r)
}

func TestOwnedChildRealPipes(t *testing.T) {
	// Child race reports are synchronous; omit the race runtime's unrelated
	// one-second exit delay so it does not model application shutdown work.
	t.Setenv("GORACE", "atexit_sleep_ms=0")
	for _, mode := range []string{"healthy", "default_jvm", "startup_eof", "blocked_start", "active_eof", "post_terminal_eof", "missing_stopped", "nonzero_stop", "silent_stop", "blocked_writer", "broken_writer"} {
		t.Run(mode, func(t *testing.T) {
			executable, err := os.Executable()
			if err != nil {
				t.Fatal(err)
			}
			opts := testOptions()
			opts.Java = executable
			opts.JAR = "external-test-" + mode
			opts.DefaultJVM = mode == "default_jvm"
			opts.StartupTimeout = 2 * time.Second
			opts.StopTimeout = 500 * time.Millisecond
			if mode == "blocked_start" {
				opts.StartupTimeout = 100 * time.Millisecond
			}
			if mode == "blocked_writer" {
				opts.Capacity = 1024
			}
			c := &controlledClient{calls: make(chan *arrivalCall, 1)}
			s, err := New(opts, c)
			if err != nil {
				t.Fatal(err)
			}
			a := s.(*adapter)
			t.Cleanup(func() {
				ctx, cancel := context.WithTimeout(context.Background(), time.Second)
				defer cancel()
				_ = a.Stop(ctx)
			})
			body := startBody()
			body["capacity"] = opts.Capacity
			h, err := a.start(context.Background(), body)
			if mode == "startup_eof" || mode == "blocked_start" {
				if err == nil || h != nil {
					t.Fatal("startup fault returned a handle")
				}
				select {
				case <-a.exitDone:
				default:
					t.Fatal("startup failed without reaping")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if mode == "blocked_writer" {
				for i := 0; i < opts.Capacity; i++ {
					if _, err := h.Invoke(context.Background(), strings.Repeat("w", 120)+string(rune(0x100+i)), "assign"); err != nil {
						t.Fatal(err)
					}
				}
			} else {
				ch, err := h.Invoke(context.Background(), "w1", "assign")
				if err != nil {
					t.Fatal(err)
				}
				if mode == "active_eof" || mode == "broken_writer" {
					if _, ok := <-ch; ok {
						t.Fatal("fabricated process-death outcome")
					}
				} else {
					r, ok := <-ch
					// A nonzero exit may be observed before the buffered terminal;
					// neither ordering may manufacture a successful session.
					if mode != "post_terminal_eof" && (!ok || r.Worker == nil || r.Worker.Err != nil) {
						t.Fatal("missing committed outcome")
					}
					if ok {
						if _, more := <-ch; more {
							t.Fatal("extra outcome")
						}
					}
				}
			}
			err = a.Stop(context.Background())
			healthy := mode == "healthy" || mode == "default_jvm"
			if healthy && err != nil {
				t.Fatal(err)
			}
			if !healthy && err == nil {
				t.Fatal("unproven cleanup succeeded")
			}
			select {
			case <-a.exitDone:
			default:
				t.Fatal("Stop returned without reaping")
			}
			select {
			case <-a.writeDone:
			default:
				t.Fatal("pipe writer survived Stop")
			}
			if err2 := a.Stop(context.Background()); (err == nil) != (err2 == nil) {
				t.Fatal("Stop lost latched result")
			}
		})
	}
	t.Log("EXTERNAL_SUT_PROCESS_RESULT launch=owned pipes=real writer=bounded eof=supervised child=reaped stop=idempotent")
	reportCheck(t, "requirement/go-real-pipes", "observe/evidence", "internal/sut/external/process_test.go:TestOwnedChildRealPipes")
}

func TestFreshSessionsAndStableErrors(t *testing.T) {
	var sessions []string
	for i := 0; i < 2; i++ {
		s, err := New(testOptions(), &controlledClient{calls: make(chan *arrivalCall, 1)})
		if err != nil {
			t.Fatal(err)
		}
		a := s.(*adapter)
		_, session, err := a.identities()
		if err != nil {
			t.Fatal(err)
		}
		sessions = append(sessions, session)
	}
	if sessions[0] == sessions[1] {
		t.Fatal("session reused")
	}
	var latch sut.FaultLatch
	latch.Fail(errTransport)
	if !errors.Is(latch.Err(), errTransport) || strings.Contains(latch.Err().Error(), sessions[0]) {
		t.Fatal("volatile fault text")
	}
}
