package main

import (
	"archive/zip"
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
	if _, err := resolved.NewAdapter(client); err != nil {
		t.Fatalf("construct external: %v", err)
	}
	if err := os.WriteFile(cfg.Target.SUT.External.JAR, []byte("changed"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := resolved.NewAdapter(client); err == nil {
		t.Fatal("factory accepted changed jar")
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
		"budget overflow":          func(c *config.Config) { c.Run.ArriveTimeoutMS = int(^uint(0) >> 1) },
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
