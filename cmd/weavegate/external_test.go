package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

func externalResolveConfig(t *testing.T) config.Config {
	t.Helper()
	cfg := validResolveConfig()
	jar := filepath.Join(t.TempDir(), "seat.jar")
	file, err := os.Create(jar)
	if err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	manifest, err := archive.Create("META-INF/MANIFEST.MF")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manifest.Write([]byte("Manifest-Version: 1.0\nMain-Class: Seat\n")); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	java, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cfg.Target.SUT = config.SUT{Adapter: config.ExternalAdapter, Variant: "vulnerable", External: &config.ExternalSUT{
		Java: java, JAR: jar, Capacity: 2, StartupTimeoutMS: 30000, CancelTimeoutMS: 5000, StopTimeoutMS: 10000,
	}}
	return cfg
}

func TestExternalPreflightBeforeFixture(t *testing.T) {
	content, err := os.ReadFile(filepath.Join(repoRoot(t), "fixtures/matching-slice/.weavegate/config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	mutated := strings.Replace(string(content), "    adapter: gonative\n    entrypoint: matching-slice\n    variant: vulnerable\n", "    adapter: external\n    variant: vulnerable\n    external:\n      java: java\n      jar: missing.jar\n      capacity: 2\n      startup_timeout_ms: 30000\n      cancel_timeout_ms: 5000\n      stop_timeout_ms: 10000\n", 1)
	if mutated == string(content) {
		t.Fatal("config mutation did not apply")
	}
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(mutated), 0o644); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	factoryCalls := 0
	err = runScenario(context.Background(), &stdout, &stderr, runFlags{config: path, scenario: "concurrent-assign", out: t.TempDir()}, func() fixture.Provisioner {
		factoryCalls++
		return nil
	})
	if exitCodeFromError(err) != ci.ExitInput || factoryCalls != 0 {
		t.Fatalf("preflight exit/calls = %d/%d; err=%v", exitCodeFromError(err), factoryCalls, err)
	}
}

func TestExternalOversizedStartBeforeFixture(t *testing.T) {
	cfg := externalResolveConfig(t)
	content, err := os.ReadFile(filepath.Join(repoRoot(t), "fixtures/matching-slice/.weavegate/config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	mutated := strings.Replace(string(content), "    adapter: gonative\n    entrypoint: matching-slice\n    variant: vulnerable\n",
		fmt.Sprintf("    adapter: external\n    variant: vulnerable\n    external:\n      java: %s\n      jar: %s\n      capacity: 2\n      startup_timeout_ms: 30000\n      cancel_timeout_ms: 5000\n      stop_timeout_ms: 10000\n", cfg.Target.SUT.External.Java, cfg.Target.SUT.External.JAR), 1)
	mutated = strings.ReplaceAll(mutated, `request_id: "42"`, `request_id: "`+strings.Repeat("x", 1<<20)+`"`)
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(mutated), 0o644); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	factoryCalls := 0
	err = runScenario(context.Background(), &stdout, &stderr, runFlags{config: path, scenario: "concurrent-assign", out: t.TempDir()}, func() fixture.Provisioner {
		factoryCalls++
		return nil
	})
	if exitCodeFromError(err) != ci.ExitInput || factoryCalls != 0 || !strings.Contains(stderr.String(), "start frame size limit") {
		t.Fatalf("oversized start exit/calls = %d/%d; err=%v; stderr=%s", exitCodeFromError(err), factoryCalls, err, stderr.String())
	}
}

func TestExternalResolvePreflight(t *testing.T) {
	cfg := externalResolveConfig(t)
	resolved, err := Resolve(cfg, "concurrent-assign", "fixed")
	if err != nil {
		t.Fatalf("resolve external: %v", err)
	}
	if resolved.Scenario.SUTConfig.Variant != "fixed" || resolved.Schedules != nil {
		t.Fatalf("external variant/schedules = %q/%v", resolved.Scenario.SUTConfig.Variant, resolved.Schedules)
	}
	if resolved.Timeouts.Run.Milliseconds() != 45000 || resolved.Timeouts.Stop.Milliseconds() != 10000 || !strings.HasPrefix(resolved.SUTSHA256, "sha256:") {
		t.Fatalf("external budgets/provenance = %+v %q", resolved.Timeouts, resolved.SUTSHA256)
	}
	client := syncpoint.New()
	defer client.Close()
	adapter, err := resolved.NewAdapter(context.Background(), client)
	if err != nil {
		t.Fatalf("construct external: %v", err)
	}
	snapshot := adapter.(*snapshotAdapter).path
	wantSnapshot, err := os.ReadFile(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.SUTSHA256 != fmt.Sprintf("sha256:%x", sha256.Sum256(wantSnapshot)) {
		t.Fatalf("snapshot digest differs from manifest provenance")
	}
	replacement := filepath.Join(filepath.Dir(cfg.Target.SUT.External.JAR), "replacement.jar")
	if err := os.WriteFile(replacement, []byte("changed"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(replacement, cfg.Target.SUT.External.JAR); err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(snapshot); err != nil || !bytes.Equal(got, wantSnapshot) {
		t.Fatalf("snapshot changed after configured JAR replacement: %v", err)
	}
	if _, err := resolved.NewAdapter(context.Background(), client); err == nil {
		t.Fatal("factory accepted changed jar")
	}
	if err := adapter.Stop(context.Background()); err != nil {
		t.Fatalf("stop unused adapter: %v", err)
	}
	if _, err := os.Stat(snapshot); !os.IsNotExist(err) {
		t.Fatalf("snapshot remains after stop: %v", err)
	}

	cases := map[string]func(*config.Config){
		"missing jar":           func(c *config.Config) { c.Target.SUT.External.JAR = filepath.Join(t.TempDir(), "missing.jar") },
		"missing java":          func(c *config.Config) { c.Target.SUT.External.Java = filepath.Join(t.TempDir(), "missing-java") },
		"insufficient capacity": func(c *config.Config) { c.Target.SUT.External.Capacity = 1 },
		"invalid worker": func(c *config.Config) {
			s := c.Scenarios["concurrent-assign"]
			s.Workers[0].Command = "bad\ncommand"
			c.Scenarios["concurrent-assign"] = s
		},
		"invalid variant override": func(c *config.Config) { c.Target.SUT.Variant = "bad\nvariant" },
		"oversized start frame": func(c *config.Config) {
			s := c.Scenarios["concurrent-assign"]
			for i := range s.Workers {
				s.Workers[i].Args = map[string]string{"value": strings.Repeat("\"", 1<<20)}
			}
			c.Scenarios["concurrent-assign"] = s
		},
		"budget overflow": func(c *config.Config) { c.Run.ArriveTimeoutMS = int(^uint(0) >> 1) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			bad := externalResolveConfig(t)
			mutate(&bad)
			_, err := Resolve(bad, "concurrent-assign", "")
			if err == nil || ci.ExitCode(err, ci.Verdict{}) != ci.ExitInput {
				t.Fatalf("want input error, got %v", err)
			}
		})
	}
}

func TestExternalManifestLaunchAttributes(t *testing.T) {
	for _, tc := range []struct {
		name, wantError string
		manifests       []string
	}{
		{"main section", "", []string{"Manifest-Version: 1.0\r\nMain-Class: Seat\r\n\r\n"}},
		{"named section only", "no Main-Class", []string{"Manifest-Version: 1.0\r\n\r\nName: Seat.class\r\nMain-Class: Seat\r\n\r\n"}},
		{"relative class path", "self-contained", []string{"Manifest-Version: 1.0\r\nMain-Class: Seat\r\nClass-Path: dep.jar\r\n\r\n"}},
		{"folded class path", "self-contained", []string{"Manifest-Version: 1.0\r\nMain-Class: Seat\r\nClass-Path: dep.\r\n jar\r\n\r\n"}},
		{"duplicate manifests", "duplicate manifest", []string{"Manifest-Version: 1.0\r\nMain-Class: Seat\r\n\r\n", "Manifest-Version: 1.0\r\nMain-Class: Missing\r\nClass-Path: dep.jar\r\n\r\n"}},
		{"large named section", "", []string{"Manifest-Version: 1.0\r\nMain-Class: Seat\r\n\r\n" + strings.Repeat("Name: filler\r\nSHA-256-Digest: abcdef\r\n\r\n", 2500)}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "app.jar")
			file, err := os.Create(path)
			if err != nil {
				t.Fatal(err)
			}
			archive := zip.NewWriter(file)
			for _, manifest := range tc.manifests {
				entry, err := archive.Create("META-INF/MANIFEST.MF")
				if err != nil {
					t.Fatal(err)
				}
				if _, err := io.WriteString(entry, manifest); err != nil {
					t.Fatal(err)
				}
			}
			if err := archive.Close(); err != nil {
				t.Fatal(err)
			}
			if err := file.Close(); err != nil {
				t.Fatal(err)
			}
			_, err = digestJAR(path)
			if tc.wantError == "" && err != nil || tc.wantError != "" && (err == nil || !strings.Contains(err.Error(), tc.wantError)) {
				t.Fatalf("digestJAR error = %v, want %q", err, tc.wantError)
			}
		})
	}
}

func TestExternalSnapshotDeadlineWhileIOBlocked(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := make(chan struct{})
	release := make(chan struct{})
	defer close(release)
	result := make(chan error, 1)
	go func() {
		_, err := boundedSnapshot(ctx, func() (string, error) {
			close(started)
			<-release
			return "", ctx.Err()
		})
		result <- err
	}()
	<-started
	cancel()
	// The run must return independently of the stalled filesystem operation.
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("blocked snapshot error = %v, want cancellation", err)
		}
	case <-time.After(time.Second):
		t.Fatal("snapshot still waiting for blocked I/O after cancellation")
	}
}

type cancelAfterRead struct {
	cancel context.CancelFunc
}

func (r cancelAfterRead) Read(p []byte) (int, error) {
	p[0] = 'x'
	r.cancel()
	return 1, nil
}

func TestExternalSnapshotCopyObservesCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var dst bytes.Buffer
	_, err := copyWithContext(ctx, &dst, cancelAfterRead{cancel: cancel})
	if !errors.Is(err, context.Canceled) || dst.String() != "x" {
		t.Fatalf("copy result = %q, %v; want first chunk and cancellation", dst.String(), err)
	}
	cfg := externalResolveConfig(t)
	resolved, err := Resolve(cfg, "concurrent-assign", "")
	if err != nil {
		t.Fatal(err)
	}
	client := syncpoint.New()
	defer client.Close()
	if _, err := resolved.NewAdapter(ctx, client); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled adapter factory = %v", err)
	}
}
