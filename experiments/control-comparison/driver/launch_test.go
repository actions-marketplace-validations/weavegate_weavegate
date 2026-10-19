package main

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Process readiness is an event, never a delay. Deadlines bound cleanup only.
func TestReapHelper(t *testing.T) {
	mode := os.Getenv("E5_REAP_HELPER")
	if mode == "" {
		return
	}
	if mode == "ignore-term" {
		signal.Ignore(syscall.SIGTERM)
	}
	_, _ = io.WriteString(os.Stdout, "ready\n")
	_, _ = io.Copy(io.Discard, os.Stdin)
	os.Exit(0)
}

func childForReap(t *testing.T, mode string) (*exec.Cmd, io.WriteCloser) {
	t.Helper()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(binary, "-test.run=^TestReapHelper$")
	cmd.Env = append(os.Environ(), "E5_REAP_HELPER="+mode)
	input, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	output, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = input.Close(); _ = cmd.Process.Kill() })
	line, err := bufio.NewReader(output).ReadString('\n')
	if err != nil || line != "ready\n" {
		t.Fatalf("readiness: %q %v", line, err)
	}
	return cmd, input
}

func TestReapNormalExit(t *testing.T) {
	cmd, input := childForReap(t, "normal")
	if err := input.Close(); err != nil {
		t.Fatal(err)
	}
	reaped, err := reap(cmd, time.Now().Add(5*time.Second))
	if !reaped || err != nil {
		t.Fatalf("reaped=%t err=%v", reaped, err)
	}
}

func TestReapExpiredDeadlineKillsOwnedChild(t *testing.T) {
	cmd, _ := childForReap(t, "ignore-term")
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	reaped, err := reap(cmd, time.Now().Add(-time.Second))
	if !reaped || err == nil || !strings.Contains(err.Error(), "child stop deadline exceeded") {
		t.Fatalf("reaped=%t err=%v", reaped, err)
	}
	var exit *exec.ExitError
	if !errors.As(err, &exit) || !exit.Sys().(syscall.WaitStatus).Signaled() {
		t.Fatalf("missing original process exit evidence: %v", err)
	}
	t.Log("E5_REAP_RESULT deadline=shared kill=bounded exit=retained")
}

func TestEventCancellationAndMissingEvent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := event(ctx, make(chan map[string]any), "ready", time.Hour); !errors.Is(err, context.Canceled) {
		t.Fatalf("lost cancellation: %v", err)
	}
	closed := make(chan map[string]any)
	close(closed)
	if _, err := event(context.Background(), closed, "ready", time.Hour); err == nil {
		t.Fatal("EOF accepted as readiness")
	}
}
