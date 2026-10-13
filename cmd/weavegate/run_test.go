package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/diagnostic"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/report"
	"github.com/weavegate/weavegate/internal/scenario"
)

func TestReportDiagnosticsPreservesEvidenceArtifact(t *testing.T) {
	got := reportDiagnostics([]diagnostic.Diagnostic{
		{Evidence: diagnostic.Evidence{EvidenceSets: 2, Trace: "trace.json", Observation: "observation.json"}},
		{Evidence: diagnostic.Evidence{Observation: "observation.json"}},
	})
	if len(got) != 2 || got[0].Evidence.EvidenceSets != 2 || got[0].Evidence.Trace != "trace.json" ||
		got[0].Evidence.Observation != "observation.json" || got[1].Evidence.Trace != "" ||
		got[1].Evidence.Observation != "observation.json" {
		t.Fatalf("diagnostic evidence conversion = %#v", got)
	}
}

func requireDocker(t *testing.T) {
	t.Helper()
	if testing.Short() {
		t.Skip("skipping Docker-backed test in -short mode")
	}
	if err := exec.Command("docker", "info").Run(); err != nil {
		t.Skipf("skipping Docker-backed test: docker unavailable: %v", err)
	}
}

func repoRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("get working directory: %v", err)
	}
	return filepath.Clean(filepath.Join(wd, "..", ".."))
}

func chdir(t *testing.T, dir string) {
	t.Helper()
	original, err := os.Getwd()
	if err != nil {
		t.Fatalf("get working directory: %v", err)
	}
	if err := os.Chdir(dir); err != nil {
		t.Fatalf("change directory to %q: %v", dir, err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(original); err != nil {
			t.Fatalf("restore working directory to %q: %v", original, err)
		}
	})
}

func latestRunDir(t *testing.T, outDir string) string {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(outDir, "runs"))
	if err != nil {
		t.Fatalf("read runs directory under %q: %v", outDir, err)
	}
	if len(entries) != 1 {
		t.Fatalf("runs directory under %q has %d entries, want 1", outDir, len(entries))
	}
	return filepath.Join(outDir, "runs", entries[0].Name())
}

func listRunFiles(t *testing.T, dir string) []os.DirEntry {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read run directory %q: %v", dir, err)
	}
	return entries
}

func readObservation(t *testing.T, dir string) report.Observation {
	t.Helper()
	var observation report.Observation
	content, err := os.ReadFile(filepath.Join(dir, report.ObservationFile))
	if err != nil {
		t.Fatalf("read %s: %v", report.ObservationFile, err)
	}
	if err := json.Unmarshal(content, &observation); err != nil {
		t.Fatalf("parse %s: %v", report.ObservationFile, err)
	}
	return observation
}

func readManifest(t *testing.T, dir string) report.Manifest {
	t.Helper()
	var manifest report.Manifest
	content, err := os.ReadFile(filepath.Join(dir, report.ManifestFile))
	if err != nil {
		t.Fatalf("read %s: %v", report.ManifestFile, err)
	}
	if err := json.Unmarshal(content, &manifest); err != nil {
		t.Fatalf("parse %s: %v", report.ManifestFile, err)
	}
	return manifest
}

func readScenario(t *testing.T, dir string) report.Scenario {
	t.Helper()
	var doc report.Scenario
	content, err := os.ReadFile(filepath.Join(dir, report.ScenarioFile))
	if err != nil {
		t.Fatalf("read %s: %v", report.ScenarioFile, err)
	}
	if err := json.Unmarshal(content, &doc); err != nil {
		t.Fatalf("parse %s: %v", report.ScenarioFile, err)
	}
	return doc
}

func extractReplayLine(t *testing.T, dir string) string {
	t.Helper()
	content, err := os.ReadFile(filepath.Join(dir, report.MarkdownFile))
	if err != nil {
		t.Fatalf("read %s: %v", report.MarkdownFile, err)
	}
	for _, line := range strings.Split(string(content), "\n") {
		if strings.HasPrefix(line, "replay: ") {
			return strings.TrimPrefix(line, "replay: ")
		}
	}
	t.Fatalf("report.md has no replay line: %s", content)
	return ""
}

type teardownFailingFixture struct {
	fixture.Provisioner
}

func (f teardownFailingFixture) Teardown(ctx context.Context) error {
	_ = f.Provisioner.Teardown(ctx)
	return errors.New("simulated cleanup failure")
}

// resetFailingFixture simulates a database container dying (or Reset
// otherwise failing) partway through exploration or replay: Provision and
// Teardown behave normally, but every Reset call fails.
type resetFailingFixture struct {
	fixture.Provisioner
}

func (f resetFailingFixture) Reset(context.Context) error {
	return errors.New("simulated fixture reset failure")
}

// failingWriter simulates a broken output pipe: every Write call fails,
// as if the process consuming stdout had already closed its end.
type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) {
	return 0, errors.New("simulated write failure")
}

func TestReplayCommandPreservesShellArguments(t *testing.T) {
	attackMarker := filepath.Join(t.TempDir(), "must-not-exist")
	flags := runFlags{
		config:   "config dir/it's $(touch " + attackMarker + ");*.yaml",
		scenario: "scenario name;echo nope",
		variant:  "fixed'$HOME",
		out:      "ignored out;path",
	}
	scheduleValue := &scenario.Schedule{ID: "sch_ba00582f9632"}
	line := buildReplayCommand(flags, flags.variant, 7, scheduleValue)
	if strings.Contains(line, "--out") {
		t.Fatalf("replay command contains --out: %q", line)
	}
	got := parseShellReplayArgs(t, line)
	want := []string{
		"weavegate", "run",
		"--config", flags.config,
		"--scenario", flags.scenario,
		"--variant", flags.variant,
		"--replay", scheduleValue.ID,
		"--repeat", "7",
	}
	if len(got) != len(want) {
		t.Fatalf("parsed replay argv = %#v, want %#v", got, want)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("parsed replay argv[%d] = %q, want %q; argv=%#v", index, got[index], want[index], got)
		}
	}
	if _, err := os.Stat(attackMarker); !os.IsNotExist(err) {
		t.Fatalf("shell metacharacter executed unexpectedly: %v", err)
	}

	t.Log("CLI_REPLAY_QUOTE_RESULT argv=preserved whitespace=true single_quote=true metachar=true out=omitted")
}

func parseShellReplayArgs(t *testing.T, line string) []string {
	t.Helper()
	script := "weavegate() { printf '%s\\037' \"$@\"; }\n" + line
	output, err := exec.Command("sh", "-c", script).Output()
	if err != nil {
		t.Fatalf("parse replay command with POSIX shell: %v", err)
	}
	parts := bytes.Split(output, []byte{0x1f})
	if len(parts) > 0 && len(parts[len(parts)-1]) == 0 {
		parts = parts[:len(parts)-1]
	}
	args := make([]string, 0, len(parts)+1)
	args = append(args, "weavegate")
	for _, part := range parts {
		args = append(args, string(part))
	}
	return args
}

func TestRun(t *testing.T) {
	requireDocker(t)

	observed := map[string]string{}
	// Computed before any chdir below: repoRoot derives the module root from
	// the current working directory, which only holds while it is still the
	// go test package directory.
	root := repoRoot(t)
	configPath := filepath.Join(root, "fixtures", "matching-slice", ".weavegate", "config.yaml")

	// report.md's replay line omits --out (it is not part of the
	// deterministic file contract; see buildReplayCommand), so a pasted
	// replay resolves stage ① of --replay lookup (A-5) against the default
	// ".weavegate" relative to the *current* working directory. Running the
	// whole test from one fixed directory makes that resolution work the
	// same way a real paste-and-run would.
	workDir := t.TempDir()
	chdir(t, workDir)
	vulnerableOutDir := filepath.Join(workDir, ".weavegate")
	var vulnerableDir string

	t.Run("explore_vulnerable", func(t *testing.T) {
		stdout, stderr, exit := run(
			"run",
			"--config", configPath,
			"--scenario", "concurrent-assign",
			"--variant", "vulnerable",
		)
		if exit != ci.ExitViolation {
			t.Fatalf("vulnerable explore exit = %d, want %d; stdout=%s stderr=%s", exit, ci.ExitViolation, stdout, stderr)
		}
		dir := latestRunDir(t, vulnerableOutDir)
		entries := listRunFiles(t, dir)
		if len(entries) != 7 {
			t.Fatalf("run directory has %d files, want 7", len(entries))
		}
		requireRunTimings(t, dir)
		obs := readObservation(t, dir)
		if obs.Repeat != 20 || obs.ViolationRuns != 20 || obs.Flaky {
			t.Fatalf("vulnerable observation = %+v, want repeat=20 violation_runs=20 flaky=false", obs)
		}
		doc := readScenario(t, dir)
		if doc.Schedule == nil || !strings.HasPrefix(doc.Schedule.ID, "sch_") {
			t.Fatalf("vulnerable scenario.json violating schedule missing/invalid: %+v", doc)
		}
		if obs.Mode != string(ModeExplore) || obs.DiscoveryFingerprint == "" {
			t.Fatalf("vulnerable observation mode/discovery = %q/%q", obs.Mode, obs.DiscoveryFingerprint)
		}
		if len(obs.Diagnostics) != 1 || obs.Diagnostics[0].Code != "WG001" {
			t.Fatalf("vulnerable diagnostics = %+v, want WG001", obs.Diagnostics)
		}
		if !strings.Contains(stdout, "## weavegate: FAIL (WG001)") || !strings.Contains(stdout, "error[WG001]:") {
			t.Fatalf("vulnerable stdout = %q, want the WG001 report", stdout)
		}

		vulnerableDir = dir

		t.Logf(
			"CLI_RUN_RESULT mode=explore variant=vulnerable passes=%d evaluated=%d schedule=%s repeat=%d violation_runs=%d flaky=%t diagnostic=WG001 artifacts=%d exit=%d",
			obs.ExplorePasses, obs.SchedulesExplored, doc.Schedule.ID, obs.Repeat, obs.ViolationRuns, obs.Flaky, len(entries), exit,
		)
	})

	t.Run("explore_fixed", func(t *testing.T) {
		outDir := t.TempDir()
		stdout, stderr, exit := run(
			"run",
			"--config", configPath,
			"--scenario", "concurrent-assign",
			"--variant", "fixed",
			"--out", outDir,
		)
		if exit != ci.ExitOK {
			t.Fatalf("fixed explore exit = %d, want %d; stdout=%s stderr=%s", exit, ci.ExitOK, stdout, stderr)
		}
		dir := latestRunDir(t, outDir)
		entries := listRunFiles(t, dir)
		if len(entries) != 6 {
			t.Fatalf("run directory has %d files, want 6", len(entries))
		}
		requireRunTimings(t, dir)
		obs := readObservation(t, dir)
		if obs.SchedulesExplored != 18 || obs.ExplorePasses != 3 || obs.Flaky || obs.ViolationRuns != 0 {
			t.Fatalf("fixed observation = %+v, want evaluated=18 passes=3 flaky=false violation_runs=0", obs)
		}
		doc := readScenario(t, dir)
		if doc.Schedule != nil {
			t.Fatalf("fixed scenario.json has a schedule, want none: %+v", doc.Schedule)
		}
		if obs.Mode != string(ModeExplore) || obs.DiscoveryFingerprint != "" {
			t.Fatalf("fixed observation mode/discovery = %q/%q", obs.Mode, obs.DiscoveryFingerprint)
		}
		if len(obs.Diagnostics) != 0 {
			t.Fatalf("fixed diagnostics = %+v, want none", obs.Diagnostics)
		}

		observed["artifacts_written_on_pass"] = "true"

		t.Logf(
			"CLI_RUN_RESULT mode=explore variant=fixed passes=%d evaluated=%d violating=none exhausted=true flaky=%t diagnostic=none artifacts=%d exit=%d",
			obs.ExplorePasses, obs.SchedulesExplored, obs.Flaky, len(entries), exit,
		)
	})

	t.Run("replay_known_schedule", func(t *testing.T) {
		outDir := t.TempDir()
		stdout, stderr, exit := run(
			"run",
			"--config", configPath,
			"--scenario", "concurrent-assign",
			"--variant", "vulnerable",
			"--replay", "sch_ba00582f9632",
			"--repeat", "20",
			"--out", outDir,
		)
		if exit != ci.ExitViolation {
			t.Fatalf("replay known schedule exit = %d, want %d; stdout=%s stderr=%s", exit, ci.ExitViolation, stdout, stderr)
		}
		dir := latestRunDir(t, outDir)
		entries := listRunFiles(t, dir)
		requireRunTimings(t, dir)
		obs := readObservation(t, dir)
		doc := readScenario(t, dir)
		if doc.Schedule == nil || doc.Schedule.ID != "sch_ba00582f9632" {
			t.Fatalf("replay schedule = %+v, want sch_ba00582f9632", doc.Schedule)
		}
		if obs.Mode != string(ModeReplay) || obs.DiscoveryFingerprint != "" {
			t.Fatalf("replay observation mode/discovery = %q/%q", obs.Mode, obs.DiscoveryFingerprint)
		}
		if len(obs.Diagnostics) != 1 || obs.Diagnostics[0].Code != "WG001" {
			t.Fatalf("replay diagnostics = %+v, want WG001", obs.Diagnostics)
		}

		t.Logf(
			"CLI_RUN_RESULT mode=replay variant=vulnerable schedule=%s repeat=%d violation_runs=%d flaky=%t diagnostic=WG001 artifacts=%d exit=%d",
			doc.Schedule.ID, obs.Repeat, obs.ViolationRuns, obs.Flaky, len(entries), exit,
		)
	})

	t.Run("replay_command_roundtrip", func(t *testing.T) {
		if vulnerableDir == "" {
			t.Fatal("vulnerable run directory not captured; explore_vulnerable must run first")
		}
		replayLine := extractReplayLine(t, vulnerableDir)
		fields := parseShellReplayArgs(t, replayLine)
		if len(fields) < 2 || fields[0] != "weavegate" || fields[1] != "run" {
			t.Fatalf("replay line = %q, does not start with \"weavegate run\"", replayLine)
		}

		stdout, stderr, exit := run(fields[1:]...)
		if exit != ci.ExitViolation {
			t.Fatalf("replay command roundtrip exit = %d, want %d; stdout=%s stderr=%s", exit, ci.ExitViolation, stdout, stderr)
		}

		if strings.Contains(replayLine, "--out") {
			t.Fatalf("replay line unexpectedly contains --out: %q", replayLine)
		}
		t.Logf("CLI_REPLAY_COMMAND_RESULT source=report_md executed=verbatim argv=preserved out=omitted resolved_from=run_directory verdict=identical exit=%d", exit)
	})

	t.Run("bad_config", func(t *testing.T) {
		_, _, exit := run("run", "--config", filepath.Join(t.TempDir(), "does-not-exist.yaml"), "--scenario", "concurrent-assign")
		if exit != ci.ExitInput {
			t.Fatalf("bad config exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["bad_config"] = "5"
	})

	t.Run("missing_scenario", func(t *testing.T) {
		_, _, exit := run("run", "--config", configPath)
		if exit != ci.ExitInput {
			t.Fatalf("missing --scenario exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["missing_scenario"] = "5"
	})

	t.Run("unknown_scenario", func(t *testing.T) {
		_, _, exit := run("run", "--config", configPath, "--scenario", "does-not-exist")
		if exit != ci.ExitInput {
			t.Fatalf("unknown scenario exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["unknown_scenario"] = "5"
	})

	t.Run("nonpositive_repeat_override", func(t *testing.T) {
		// Neither Docker nor a valid --scenario is reachable before this
		// rejection: it must fire from flag validation alone, before
		// provisioning starts.
		_, _, exit := run("run", "--config", configPath, "--scenario", "concurrent-assign", "--repeat", "0")
		if exit != ci.ExitInput {
			t.Fatalf("--repeat 0 exit = %d, want %d", exit, ci.ExitInput)
		}
		_, _, exit = run("run", "--config", configPath, "--scenario", "concurrent-assign", "--repeat", "-1")
		if exit != ci.ExitInput {
			t.Fatalf("--repeat -1 exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["nonpositive_repeat_override"] = "5"
	})

	t.Run("unknown_schedule", func(t *testing.T) {
		_, _, exit := run(
			"run", "--config", configPath, "--scenario", "concurrent-assign",
			"--replay", "sch_000000000000", "--out", t.TempDir(),
		)
		if exit != ci.ExitInput {
			t.Fatalf("unknown schedule exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["unknown_schedule"] = "5"
	})

	t.Run("unverifiable_run_evidence", func(t *testing.T) {
		// resolveReplaySchedule is a pure filesystem lookup; exercise it
		// directly rather than through a full Docker-backed run.
		outDir := t.TempDir()
		runDir := filepath.Join(outDir, "runs", "run_20260823T000000.000000001Z_cccccccccccccccccccccccccccccccc")
		if err := os.MkdirAll(runDir, 0o755); err != nil {
			t.Fatalf("create run directory: %v", err)
		}

		scheduleValue, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "w1", Point: "p1"}})
		if err != nil {
			t.Fatalf("build schedule: %v", err)
		}
		scheduleValue.Steps = append(scheduleValue.Steps,
			scenario.CoordinationStep{Worker: "tampered", Point: "unverifiable"})
		writeScenarioDoc(t, filepath.Join(runDir, "scenario.json"), scheduleValue)

		_, err = resolveReplaySchedule(scheduleValue.ID, outDir, nil)
		if err == nil {
			t.Fatal("resolve unverifiable run evidence: want error, got nil")
		}
		if got := ci.ExitCode(err, ci.Verdict{}); got != ci.ExitInput {
			t.Fatalf("unverifiable run evidence exit = %d, want %d", got, ci.ExitInput)
		}
		observed["unverifiable_run_evidence"] = "5"
	})

	t.Run("missing_fixture_source", func(t *testing.T) {
		badConfig := writeConfigWithBadMigrations(t, configPath)
		_, _, exit := run("run", "--config", badConfig, "--scenario", "concurrent-assign", "--out", t.TempDir())
		if exit != ci.ExitInput {
			t.Fatalf("missing fixture source exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["missing_fixture_source"] = "5"
	})

	t.Run("unwritable_out", func(t *testing.T) {
		blocked := filepath.Join(t.TempDir(), "blocked")
		if err := os.WriteFile(blocked, []byte("not a directory"), 0o644); err != nil {
			t.Fatalf("create blocking file: %v", err)
		}
		_, _, exit := run(
			"run", "--config", configPath, "--scenario", "concurrent-assign",
			"--variant", "fixed", "--out", filepath.Join(blocked, "out"),
		)
		if exit != ci.ExitInput {
			t.Fatalf("unwritable --out exit = %d, want %d", exit, ci.ExitInput)
		}
		observed["unwritable_out"] = "5"
	})

	t.Run("cleanup_failure_on_pass", func(t *testing.T) {
		var stdout, stderr bytes.Buffer
		outDir := t.TempDir()
		flags := runFlags{
			config:   configPath,
			scenario: "concurrent-assign",
			variant:  "fixed",
			out:      outDir,
		}
		err := runScenario(context.Background(), &stdout, &stderr, flags, func() fixture.Provisioner {
			return teardownFailingFixture{Provisioner: fixture.NewMySQLFixture()}
		})
		exit := exitCodeFromError(err)
		if exit != ci.ExitFixture {
			t.Fatalf("cleanup failure on pass exit = %d, want %d; stderr=%s", exit, ci.ExitFixture, stderr.String())
		}
		if !strings.Contains(stderr.String(), "cleanup failed") {
			t.Fatalf("cleanup failure stderr = %q, want a cleanup warning", stderr.String())
		}
		if !strings.Contains(stdout.String(), "## weavegate: PASS") {
			t.Fatalf("cleanup failure stdout = %q, want the scenario's own PASS headline unaffected by cleanup", stdout.String())
		}
		manifest := readManifest(t, latestRunDir(t, outDir))
		if !manifest.CleanupFailed {
			t.Fatalf("saved manifest.json cleanup_failed = false, want true so the run directory itself records the leaked container")
		}
		observed["cleanup_failure_on_pass"] = "4"
	})

	t.Run("fixture_failure_during_replay", func(t *testing.T) {
		var stdout, stderr bytes.Buffer
		flags := runFlags{
			config:   configPath,
			scenario: "concurrent-assign",
			variant:  "vulnerable",
			out:      t.TempDir(),
		}
		err := runScenario(context.Background(), &stdout, &stderr, flags, func() fixture.Provisioner {
			return resetFailingFixture{Provisioner: fixture.NewMySQLFixture()}
		})
		exit := exitCodeFromError(err)
		if exit != ci.ExitFixture {
			t.Fatalf("fixture failure during replay exit = %d, want %d; stderr=%s", exit, ci.ExitFixture, stderr.String())
		}
		observed["fixture_failure_during_replay"] = "4"
	})

	t.Run("diagnostic_derivation_failure", func(t *testing.T) {
		var stdout, stderr bytes.Buffer
		outDir := t.TempDir()
		flags := runFlags{
			config:    configPath,
			scenario:  "concurrent-assign",
			replay:    "sch_ba00582f9632",
			replaySet: true,
			repeat:    20,
			repeatSet: true,
			out:       outDir,
		}
		deriveCalls := 0
		err := runScenarioWithDiagnosticDeriver(
			context.Background(), &stdout, &stderr, flags, fixture.NewMySQLFixture,
			func(input diagnostic.Input) ([]diagnostic.Diagnostic, error) {
				deriveCalls++
				if len(input.Violations) == 0 {
					t.Fatal("diagnostic derivation was reached without the completed Oracle violation")
				}
				return nil, errors.New("simulated diagnostic invariant failure")
			},
		)
		exit := exitCodeFromError(err)
		if exit != ci.ExitInput {
			t.Fatalf("diagnostic derivation failure exit = %d, want %d; stderr=%s", exit, ci.ExitInput, stderr.String())
		}
		if deriveCalls != 1 {
			t.Fatalf("diagnostic derivation calls = %d, want 1", deriveCalls)
		}
		if !strings.Contains(stderr.String(), "run: derive diagnostics: simulated diagnostic invariant failure") {
			t.Fatalf("diagnostic derivation stderr = %q, want classified failure", stderr.String())
		}

		dir := latestRunDir(t, outDir)
		entries := listRunFiles(t, dir)
		if len(entries) != 7 {
			t.Fatalf("diagnostic derivation failure artifacts = %d, want 7", len(entries))
		}
		for _, name := range []string{
			report.ManifestFile,
			report.ScenarioFile,
			report.ObservationFile,
			report.TraceFile,
			report.MergedFile,
		} {
			var doc struct {
				ArtifactVersion int `json:"artifact_version"`
			}
			content, err := os.ReadFile(filepath.Join(dir, name))
			if err != nil {
				t.Fatalf("read diagnostic derivation failure %s: %v", name, err)
			}
			if err := json.Unmarshal(content, &doc); err != nil {
				t.Fatalf("parse diagnostic derivation failure %s: %v", name, err)
			}
			if doc.ArtifactVersion != report.DiagnosticFailureArtifactVersion {
				t.Fatalf("diagnostic derivation failure %s artifact_version = %d, want %d",
					name, doc.ArtifactVersion, report.DiagnosticFailureArtifactVersion)
			}
		}
		observation := readObservation(t, dir)
		if observation.Repeat != 20 || observation.ViolationRuns != 20 || len(observation.AssertionViolations) == 0 {
			t.Fatalf("diagnostic derivation failure observation = %+v, want 20/20 preserved Oracle violations", observation)
		}
		if len(observation.Diagnostics) != 0 {
			t.Fatalf("diagnostic derivation failure diagnostics = %+v, want none", observation.Diagnostics)
		}
		scenarioDoc := readScenario(t, dir)
		if scenarioDoc.Schedule == nil || scenarioDoc.Schedule.ID != "sch_ba00582f9632" {
			t.Fatalf("diagnostic derivation failure schedule = %+v, want preserved replay schedule", scenarioDoc.Schedule)
		}
		if !strings.Contains(stdout.String(), "## weavegate: FAIL\n") || strings.Contains(stdout.String(), "WG001") {
			t.Fatalf("diagnostic derivation failure stdout = %q, want diagnostic-free FAIL report", stdout.String())
		}
		if !strings.HasSuffix(stdout.String(), dir+"\n") {
			t.Fatalf("diagnostic derivation failure stdout = %q, want final run directory %q", stdout.String(), dir)
		}

		observed["diagnostic_derivation_failure"] = "5"
		observed["diagnostic_failure_artifacts"] = "7"
		observed["diagnostic_failure_version"] = "3"
	})

	t.Run("stdout_write_failure", func(t *testing.T) {
		var stderr bytes.Buffer
		flags := runFlags{
			config:   configPath,
			scenario: "concurrent-assign",
			variant:  "fixed",
			out:      t.TempDir(),
		}
		err := runScenario(context.Background(), failingWriter{}, &stderr, flags, fixture.NewMySQLFixture)
		exit := exitCodeFromError(err)
		if exit != ci.ExitInput {
			t.Fatalf("stdout write failure exit = %d, want %d; stderr=%s", exit, ci.ExitInput, stderr.String())
		}
		observed["stdout_write_failure"] = "5"
	})

	order := []string{
		"bad_config", "missing_scenario", "unknown_scenario", "nonpositive_repeat_override", "unknown_schedule",
		"unverifiable_run_evidence", "unwritable_out", "missing_fixture_source",
		"artifacts_written_on_pass", "cleanup_failure_on_pass", "fixture_failure_during_replay",
		"diagnostic_derivation_failure", "diagnostic_failure_artifacts", "diagnostic_failure_version", "stdout_write_failure",
	}
	parts := make([]string, 0, len(order))
	for _, key := range order {
		value, ok := observed[key]
		if !ok {
			t.Fatalf("marker attribute %q was not observed by any subtest", key)
		}
		parts = append(parts, key+"="+value)
	}
	t.Logf("CLI_RUN_FAILURE_RESULT %s", strings.Join(parts, " "))
}

func writeScenarioDoc(t *testing.T, path string, schedule scenario.Schedule) {
	t.Helper()
	doc := struct {
		ViolatingSchedule scenario.Schedule `json:"violating_schedule"`
	}{ViolatingSchedule: schedule}
	content, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("marshal scenario document: %v", err)
	}
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatalf("write scenario document %q: %v", path, err)
	}
}

func writeConfigWithBadMigrations(t *testing.T, configPath string) string {
	t.Helper()
	content, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("read config %q: %v", configPath, err)
	}
	mutated := strings.Replace(string(content), "migrations: ../db/migration", "migrations: ../db/does-not-exist", 1)
	if mutated == string(content) {
		t.Fatal("write bad-migrations config: expected substring not found in source config")
	}
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(mutated), 0o644); err != nil {
		t.Fatalf("write bad-migrations config: %v", err)
	}
	return path
}

// TestRunEvidenceSelection covers the 0/repeat flaky case (A-18-adjacent):
// exploration finds a violation, but every replay run passes. Without a
// discovery-run fallback, runTrace and assertionViolations would report
// nothing to substantiate the resulting FLAKY verdict. No Docker needed:
// runOutcome is built directly, the same way verdict_test.go builds
// VerdictInput.
func TestRunEvidenceSelection(t *testing.T) {
	violating := violatingEvaluation(t)
	passing := passingEvaluation(t)
	discoveryRun := orchestrator.RunResult{
		ScheduleID:  "sch_discovery000",
		Evaluation:  violating,
		Fingerprint: "fp-violation",
		Trace:       nil,
	}

	t.Run("discovery_fallback_when_replay_never_reproduces", func(t *testing.T) {
		outcome := runOutcome{
			Mode:         ModeExplore,
			DiscoveryRun: &discoveryRun,
			Replay: orchestrator.ReplayResult{
				Runs: repeatRuns(passing, "fp-pass", 20),
			},
		}

		trace := runTrace(outcome)
		if trace.ScheduleRef != discoveryRun.ScheduleID {
			t.Fatalf("trace.schedule_ref = %q, want discovery run %q", trace.ScheduleRef, discoveryRun.ScheduleID)
		}

		violations := assertionViolations(violationEvidenceRuns(outcome))
		if len(violations) != 1 || violations[0].OracleID != "active-assignment-is-unique" {
			t.Fatalf("assertion_violations = %+v, want [active-assignment-is-unique] from the discovery run", violations)
		}
		if len(violations[0].Rows) == 0 {
			t.Fatalf("assertion_violations[0].Rows is empty, want the discovery run's evidence rows")
		}
		if got := traceOracleIDs(outcome); len(got) != 1 || got[0] != "active-assignment-is-unique" {
			t.Fatalf("trace oracle IDs = %v, want the discovery run's violated assertion", got)
		}
	})

	t.Run("violating_replay_run_preferred_over_discovery", func(t *testing.T) {
		outcome := runOutcome{
			Mode:         ModeExplore,
			DiscoveryRun: &discoveryRun,
			Replay: orchestrator.ReplayResult{
				Runs: append(repeatRuns(passing, "fp-pass", 19), orchestrator.RunResult{
					ScheduleID:  "sch_replayrun0001",
					Evaluation:  violating,
					Fingerprint: "fp-violation",
				}),
			},
		}

		trace := runTrace(outcome)
		if trace.ScheduleRef != "sch_replayrun0001" {
			t.Fatalf("trace.schedule_ref = %q, want the violating replay run, not the discovery run", trace.ScheduleRef)
		}
		if got := traceOracleIDs(outcome); len(got) != 1 || got[0] != "active-assignment-is-unique" {
			t.Fatalf("trace oracle IDs = %v, want the selected replay run's violated assertion", got)
		}
	})

	t.Run("mismatch_run_preferred_when_no_violation_anywhere", func(t *testing.T) {
		// Direct --replay can be flaky purely from fingerprint divergence
		// (differing terminal states or timing classification) with zero
		// assertion violations anywhere -- no discovery run exists in this
		// mode, so the mismatching run is the only informative choice.
		baseline := orchestrator.RunResult{ScheduleID: "sch_baseline0001", Evaluation: passing, Fingerprint: "fp-a"}
		mismatch := orchestrator.RunResult{ScheduleID: "sch_mismatch0001", Evaluation: passing, Fingerprint: "fp-b"}
		outcome := runOutcome{
			Mode: ModeReplay,
			Replay: orchestrator.ReplayResult{
				Fingerprints: map[string]int{"fp-a": 1, "fp-b": 1},
				MismatchRuns: []int{2},
				Runs:         []orchestrator.RunResult{baseline, mismatch},
			},
		}

		trace := runTrace(outcome)
		if trace.ScheduleRef != mismatch.ScheduleID {
			t.Fatalf("trace.schedule_ref = %q, want the mismatching run %q, not the baseline", trace.ScheduleRef, mismatch.ScheduleID)
		}
	})

	t.Run("no_discovery_falls_back_to_first_replay_run", func(t *testing.T) {
		outcome := runOutcome{
			Mode: ModeExplore,
			Replay: orchestrator.ReplayResult{
				Runs: repeatRuns(passing, "fp-pass", 3),
			},
		}

		trace := runTrace(outcome)
		if trace.ScheduleRef == discoveryRun.ScheduleID {
			t.Fatalf("trace.schedule_ref = %q, want a replay run (no discovery run set)", trace.ScheduleRef)
		}
		if len(assertionViolations(violationEvidenceRuns(outcome))) != 0 {
			t.Fatalf("assertion_violations should be empty when nothing violated")
		}
	})

	t.Run("no_runs_at_all", func(t *testing.T) {
		trace := runTrace(runOutcome{})
		if trace.ScheduleRef != "" || trace.Events != nil || trace.Terminals != nil {
			t.Fatalf("trace = %+v, want zero value when there is nothing to report", trace)
		}
		if got := traceOracleIDs(runOutcome{}); len(got) != 0 {
			t.Fatalf("trace oracle IDs = %v, want none when no trace is selected", got)
		}
	})
}
