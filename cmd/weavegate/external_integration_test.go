package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/scenario"
)

func TestExternalCLISeparateJVM(t *testing.T) {
	requireDocker(t)
	java, err := exec.LookPath("java")
	if err != nil {
		t.Skipf("Java unavailable: %v", err)
	}
	javac, err := exec.LookPath("javac")
	if err != nil {
		t.Skipf("javac unavailable: %v", err)
	}
	jarTool, err := exec.LookPath("jar")
	if err != nil {
		t.Skipf("jar unavailable: %v", err)
	}
	dir := t.TempDir()
	classes := filepath.Join(dir, "classes")
	if err := os.Mkdir(classes, 0o755); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join("testdata", "external-mini", "MiniPeer.java")
	if output, err := exec.Command(javac, "-d", classes, source).CombinedOutput(); err != nil {
		t.Fatalf("compile mini peer: %v\n%s", err, output)
	}
	jarPath := filepath.Join(dir, "mini.jar")
	wireLog := filepath.Join(dir, "wire-ids.txt")
	if output, err := exec.Command(jarTool, "--create", "--file", jarPath, "--main-class", "MiniPeer", "-C", classes, ".").CombinedOutput(); err != nil {
		t.Fatalf("package mini peer: %v\n%s", err, output)
	}
	root := repoRoot(t)
	configPath := filepath.Join(dir, "config.yaml")
	content := fmt.Sprintf(`target:
  db: mysql:8.4
  schema:
    migrations: %s
    seed: %s
  sut:
    adapter: external
    variant: test
    external:
      java: %s
      jar: %s
      capacity: 1
      startup_timeout_ms: 10000
      cancel_timeout_ms: 3000
      stop_timeout_ms: 5000
scenarios:
  mini:
    workers:
      - id: w1
        command: ping
        args:
          value: ok
          wire_log: %s
    sync_points: [at]
oracle:
  assertions:
    - id: always-empty
      sql: SELECT 1 WHERE FALSE
      expect_rows: 0
run:
  repeat: 2
  arrive_timeout_ms: 1000
`, filepath.Join(root, "fixtures/matching-slice/db/migration"), filepath.Join(root, "fixtures/matching-slice/db/seed.sql"), java, jarPath, wireLog)
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "out")
	schedule, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "at"}})
	if err != nil {
		t.Fatal(err)
	}
	schedulePath := filepath.Join(dir, "schedule.json")
	if err := scenario.WriteScheduleFile(schedulePath, schedule); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	err = runScenario(context.Background(), &stdout, &stderr, runFlags{config: configPath, scenario: "mini", replay: schedulePath, replaySet: true, out: out}, fixture.NewMySQLFixture)
	if exitCodeFromError(err) != 0 {
		t.Fatalf("external CLI: %v\nstdout=%s\nstderr=%s", err, stdout.String(), stderr.String())
	}
	manifest := readManifest(t, latestRunDir(t, out))
	jarBytes, err := os.ReadFile(jarPath)
	if err != nil {
		t.Fatal(err)
	}
	wantDigest := fmt.Sprintf("sha256:%x", sha256.Sum256(jarBytes))
	if manifest.Adapter != "external" || manifest.SUTSHA256 != wantDigest {
		t.Fatalf("manifest provenance = %+v", manifest)
	}
	if !strings.Contains(stdout.String(), "PASS") {
		t.Fatalf("stdout = %s", stdout.String())
	}
	wireIDs, err := os.ReadFile(wireLog)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(wireIDs)), "\n")
	if len(lines) != 2 {
		t.Fatalf("wire identities = %q, want two sessions", wireIDs)
	}
	first, second := strings.Fields(lines[0]), strings.Fields(lines[1])
	if len(first) != 2 || len(second) != 2 || first[0] != second[0] || first[1] == second[1] {
		t.Fatalf("wire identities = %q, want shared run and distinct sessions", wireIDs)
	}
	t.Log("EXTERNAL_CLI_RESULT separate_jvm=true config_only=true repeat=2 manifest_sha256=true")
}
