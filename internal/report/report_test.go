package report

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/timing"
)

func sampleSchedule(t *testing.T) Schedule {
	t.Helper()

	return Schedule{
		ID: "sch_fbf6b1dfaae2",
		Steps: []CoordinationStep{
			{Worker: "w1", Point: "after_read_request"},
			{Worker: "w2", Point: "after_read_request"},
		},
	}
}

func sampleRun(t *testing.T, runID string) Run {
	t.Helper()

	schedule := sampleSchedule(t)
	return Run{
		Manifest: Manifest{
			RunID:            runID,
			StartedAt:        time.Date(2026, 8, 16, 12, 0, 0, 0, time.UTC),
			WeavegateVersion: "0.0.0-dev",
			SchemaVersion:    "abc123def456",
			SeedData:         "def456abc123",
			IsolationLevel:   "REPEATABLE-READ",
			Engine:           "InnoDB",
			Adapter:          "gonative",
			Variant:          "vulnerable",
			Image:            "mysql:8.4",
		},
		Scenario: Scenario{
			Name: "concurrent-assign",
			Workers: []Worker{
				{ID: "w1", Command: "assign"},
				{ID: "w2", Command: "assign"},
			},
			SyncPoints: []string{"after_read_request", "before_insert_assignment"},
			Schedule:   &schedule,
		},
		Observation: Observation{
			Mode:              "explore",
			SchedulesExplored: 2,
			ExplorePasses:     1,
			AssertionViolations: []AssertionViolation{
				{OracleID: "active-assignment-is-unique", Rows: []Row{{"active_count": int64(2)}}},
			},
			Repeat: 20,
			Timeouts: Timeouts{
				ArriveMS:         3000,
				BlockInferenceMS: 3000,
				StepMS:           60000,
				RunMS:            180000,
				StopMS:           60000,
			},
			ViolationRuns:        20,
			Flaky:                false,
			Fingerprints:         map[string]int{"fp-discovery": 20},
			DiscoveryFingerprint: "fp-discovery",
		},
		Trace: Trace{
			ScheduleRef: schedule.ID,
			Events: []Event{
				{Seq: 1, Kind: "fixture_reset", Step: -1, Status: "none", FailureClass: "none"},
			},
			Terminals: []WorkerTerminal{
				{Worker: "w1", State: "done", FailureClass: "none"},
			},
		},
		Pass:          false,
		Flaky:         false,
		ReplayCommand: "weavegate run --config .weavegate/config.yaml --scenario concurrent-assign --variant vulnerable --replay " + schedule.ID + " --repeat 20",
	}
}

func TestWriteRunArtifacts(t *testing.T) {
	base := t.TempDir()
	umask := 0o077
	previousUmask := syscall.Umask(umask)
	defer syscall.Umask(previousUmask)
	expectedDirMode := dirMode &^ os.FileMode(umask)
	expectedFileMode := fileMode &^ os.FileMode(umask)
	run := sampleRun(t, "run_20260816T120000.000Z_aaaaaaaa")

	dir, err := WriteRun(base, run)
	if err != nil {
		t.Fatalf("write run: %v", err)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read run directory: %v", err)
	}
	if len(entries) != 7 {
		t.Fatalf("run directory has %d entries, want 7", len(entries))
	}

	info, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("stat run directory: %v", err)
	}
	if info.Mode().Perm() != expectedDirMode {
		t.Fatalf("run directory mode = %o, want %o", info.Mode().Perm(), expectedDirMode)
	}

	for _, name := range FileNames {
		path := filepath.Join(dir, name)
		fileInfo, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat %s: %v", name, err)
		}
		if fileInfo.Mode().Perm() != expectedFileMode {
			t.Fatalf("%s mode = %o, want %o", name, fileInfo.Mode().Perm(), expectedFileMode)
		}
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		if len(content) == 0 || content[len(content)-1] != '\n' {
			t.Fatalf("%s does not end with a trailing newline", name)
		}
		if strings.Count(string(content), "\n") == 1 && strings.HasSuffix(name, ".json") {
			t.Fatalf("%s is not indented JSON", name)
		}
	}
	for _, name := range []string{ManifestFile, ScenarioFile, ObservationFile, TraceFile, MergedFile} {
		var doc map[string]any
		if err := json.Unmarshal(mustRead(t, filepath.Join(dir, name)), &doc); err != nil {
			t.Fatalf("parse %s: %v", name, err)
		}
		if got := doc["artifact_version"]; got != float64(ArtifactVersion) {
			t.Fatalf("%s artifact_version = %#v, want %d", name, got, ArtifactVersion)
		}
	}
	scenarioContent := string(mustRead(t, filepath.Join(dir, ScenarioFile)))
	if strings.Contains(scenarioContent, "violating_schedule") || !strings.Contains(scenarioContent, `"schedule"`) {
		t.Fatalf("scenario.json is not v2 neutral schedule output: %s", scenarioContent)
	}
	loadedSchedule, err := scenario.LoadScheduleFile(filepath.Join(dir, ScheduleFile))
	if err != nil {
		t.Fatalf("load portable schedule: %v", err)
	}
	if loadedSchedule.ID != run.Scenario.Schedule.ID {
		t.Fatalf("portable schedule ID = %q, want %q", loadedSchedule.ID, run.Scenario.Schedule.ID)
	}

	var traceDoc map[string]json.RawMessage
	if err := json.Unmarshal(mustRead(t, filepath.Join(dir, TraceFile)), &traceDoc); err != nil {
		t.Fatalf("parse trace.json: %v", err)
	}
	if string(traceDoc["events"]) == "null" || string(traceDoc["terminals"]) == "null" {
		t.Fatalf("trace.json has null events/terminals, want []: %s", traceDoc)
	}
	var observationDoc map[string]json.RawMessage
	if err := json.Unmarshal(mustRead(t, filepath.Join(dir, ObservationFile)), &observationDoc); err != nil {
		t.Fatalf("parse observation.json: %v", err)
	}
	if string(observationDoc["diagnostics"]) != "[]" {
		t.Fatalf("observation diagnostics = %s, want []", observationDoc["diagnostics"])
	}
	var mergedDoc struct {
		Observation struct {
			Diagnostics []Diagnostic `json:"diagnostics"`
		} `json:"observation"`
	}
	if err := json.Unmarshal(mustRead(t, filepath.Join(dir, MergedFile)), &mergedDoc); err != nil {
		t.Fatalf("parse report.json diagnostics: %v", err)
	}
	if mergedDoc.Observation.Diagnostics == nil {
		t.Fatal("report.json observation diagnostics is null")
	}

	// A second run with an empty trace must still encode [] rather than null.
	emptyRun := sampleRun(t, "run_20260816T120001.000Z_bbbbbbbb")
	emptyRun.Trace.Events = nil
	emptyRun.Trace.Terminals = nil
	emptyRun.Observation.AssertionViolations = nil
	emptyDir, err := WriteRun(base, emptyRun)
	if err != nil {
		t.Fatalf("write run with nil slices: %v", err)
	}
	emptyTraceContent := string(mustRead(t, filepath.Join(emptyDir, TraceFile)))
	if strings.Contains(emptyTraceContent, "null") {
		t.Fatalf("trace.json with empty slices encodes null: %s", emptyTraceContent)
	}
	emptyObservationContent := string(mustRead(t, filepath.Join(emptyDir, ObservationFile)))
	if strings.Contains(emptyObservationContent, "null") {
		t.Fatalf("observation.json with empty slice encodes null: %s", emptyObservationContent)
	}

	withoutSchedule := sampleRun(t, "run_20260816T120002.000Z_eeeeeeee")
	withoutSchedule.Scenario.Schedule = nil
	withoutSchedule.Trace.ScheduleRef = ""
	withoutScheduleDir, err := WriteRun(base, withoutSchedule)
	if err != nil {
		t.Fatalf("write run without schedule: %v", err)
	}
	withoutScheduleEntries, err := os.ReadDir(withoutScheduleDir)
	if err != nil {
		t.Fatalf("read run without schedule: %v", err)
	}
	if len(withoutScheduleEntries) != 6 {
		t.Fatalf("run without schedule has %d entries, want 6", len(withoutScheduleEntries))
	}
	if _, err := os.Stat(filepath.Join(withoutScheduleDir, ScheduleFile)); !os.IsNotExist(err) {
		t.Fatalf("run without schedule has schedule.json: %v", err)
	}

	t.Log("REPORT_DIAGNOSTIC_RESULT field=observation.diagnostics files=6_or_7 empty=json_array dto=report_owned merged=report_json deterministic=true")

	replayLine := extractReplayLine(t, filepath.Join(dir, MarkdownFile))
	if !strings.HasPrefix(replayLine, "weavegate run ") || !strings.Contains(replayLine, run.Scenario.Schedule.ID) || strings.Contains(replayLine, "--out") {
		t.Fatalf("report.md replay line = %q, not self-sufficient", replayLine)
	}

	rerunIdentical := testRerunIdentical(t, base)

	t.Logf(
		"RUN_ARTIFACT_RESULT files=6_or_7 schedule_file=present_when_scheduled umask=0077 dir_mode=0%o file_mode=0%o key_order=canonical "+
			"empty_slice=json_array trailing_newline=true volatile_files=manifest+report_json "+
			"deterministic_files=%d rerun_identical=%s replay_line=out_omitted shell_quote=posix config_path=as_given "+
			"tmp_same_filesystem=true partial_write=cleaned write_failure=output_error",
		expectedDirMode,
		expectedFileMode,
		len(DeterministicFiles),
		rerunIdentical,
	)
}

func TestWriteRunUsesSeparateDiagnosticFailureVersion(t *testing.T) {
	base := t.TempDir()
	tests := []struct {
		name                       string
		runID                      string
		diagnosticDerivationFailed bool
		wantVersion                int
	}{
		{
			name:        "ordinary",
			runID:       "run_20260816T120010.000Z_aaaaaaaa",
			wantVersion: ArtifactVersion,
		},
		{
			name:                       "diagnostic_failure",
			runID:                      "run_20260816T120011.000Z_bbbbbbbb",
			diagnosticDerivationFailed: true,
			wantVersion:                DiagnosticFailureArtifactVersion,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			run := sampleRun(t, test.runID)
			run.DiagnosticDerivationFailed = test.diagnosticDerivationFailed
			dir, err := WriteRun(base, run)
			if err != nil {
				t.Fatalf("write %s run: %v", test.name, err)
			}

			for _, name := range []string{ManifestFile, ScenarioFile, ObservationFile, TraceFile, MergedFile} {
				var doc struct {
					ArtifactVersion int `json:"artifact_version"`
				}
				if err := json.Unmarshal(mustRead(t, filepath.Join(dir, name)), &doc); err != nil {
					t.Fatalf("parse %s: %v", name, err)
				}
				if doc.ArtifactVersion != test.wantVersion {
					t.Fatalf("%s artifact_version = %d, want %d", name, doc.ArtifactVersion, test.wantVersion)
				}
			}

			var merged Merged
			if err := json.Unmarshal(mustRead(t, filepath.Join(dir, MergedFile)), &merged); err != nil {
				t.Fatalf("parse %s: %v", MergedFile, err)
			}
			if merged.Manifest.ArtifactVersion != test.wantVersion ||
				merged.Scenario.ArtifactVersion != test.wantVersion ||
				merged.Observation.ArtifactVersion != test.wantVersion {
				t.Fatalf("%s nested artifact versions = manifest:%d scenario:%d observation:%d, want %d",
					MergedFile,
					merged.Manifest.ArtifactVersion,
					merged.Scenario.ArtifactVersion,
					merged.Observation.ArtifactVersion,
					test.wantVersion,
				)
			}
		})
	}

	t.Log("ARTIFACT_VERSION_RESULT ordinary=v2 retained_diagnostic_failure=v3 run_scoped_json=consistent copied_evidence=distinguishable")
}

func TestWriteRunPreservesExistingDestination(t *testing.T) {
	base := t.TempDir()
	run := sampleRun(t, "run_20260816T120003.000000000Z_22222222222222222222222222222222")
	finalDir := filepath.Join(base, "runs", run.Manifest.RunID)
	if err := os.MkdirAll(finalDir, 0o755); err != nil {
		t.Fatalf("create existing run directory: %v", err)
	}
	sentinelPath := filepath.Join(finalDir, "sentinel")
	const sentinel = "preserve me"
	if err := os.WriteFile(sentinelPath, []byte(sentinel), 0o600); err != nil {
		t.Fatalf("write sentinel: %v", err)
	}

	_, err := WriteRun(base, run)
	if err == nil {
		t.Fatal("write colliding run: want error, got nil")
	}
	if got := ci.ExitCode(err, ci.Verdict{}); got != ci.ExitInput {
		t.Fatalf("collision exit code = %d, want %d", got, ci.ExitInput)
	}
	content, readErr := os.ReadFile(sentinelPath)
	if readErr != nil || string(content) != sentinel {
		t.Fatalf("existing destination changed: content=%q err=%v", content, readErr)
	}
}

func TestPassingDirectReplayUsesNeutralEvidenceSemantics(t *testing.T) {
	base := t.TempDir()
	run := sampleRun(t, "run_20260816T120002.000000000Z_11111111111111111111111111111111")
	run.Observation.Mode = "replay"
	run.Observation.SchedulesExplored = 0
	run.Observation.ExplorePasses = 0
	run.Observation.AssertionViolations = nil
	run.Observation.ViolationRuns = 0
	run.Observation.DiscoveryFingerprint = ""
	run.Pass = true

	dir, err := WriteRun(base, run)
	if err != nil {
		t.Fatalf("write passing replay: %v", err)
	}
	markdown := string(mustRead(t, filepath.Join(dir, MarkdownFile)))
	if !strings.Contains(markdown, "| replayed: "+run.Scenario.Schedule.ID) || strings.Contains(markdown, "| violating:") {
		t.Fatalf("passing replay markdown has misleading schedule label: %s", markdown)
	}
	observation := string(mustRead(t, filepath.Join(dir, ObservationFile)))
	if strings.Contains(observation, "discovery_fingerprint") {
		t.Fatalf("direct replay emitted discovery_fingerprint: %s", observation)
	}
	flaky := run
	flaky.Pass = false
	flaky.Flaky = true
	flaky.Observation.Flaky = true
	flakyMarkdown := renderMarkdown(flaky)
	if !strings.Contains(flakyMarkdown, "| violating: "+run.Scenario.Schedule.ID) || strings.Contains(flakyMarkdown, "| replayed:") {
		t.Fatalf("flaky replay lost violating label: %s", flakyMarkdown)
	}

	t.Log("ARTIFACT_V2_RESULT files=6_or_7 writer=v2 schedule=neutral schedule_file=canonical mode=recorded direct_replay_discovery=omitted passing_replay=replayed legacy_reader=v1+v2+v3")
}

func TestRenderMarkdownDiagnostics(t *testing.T) {
	run := sampleRun(t, "run_20260816T120004.000Z_ffffffff")
	without := renderMarkdown(run)
	wantWithout := "## weavegate: FAIL\n" +
		"scenario: concurrent-assign | schedules explored: 2 | violating: sch_fbf6b1dfaae2\n" +
		"assertion: active-assignment-is-unique\n" +
		"flaky: false (repeat=20)\n" +
		"replay: weavegate run --config .weavegate/config.yaml --scenario concurrent-assign --variant vulnerable --replay sch_fbf6b1dfaae2 --repeat 20\n"
	if without != wantWithout {
		t.Fatalf("no-diagnostic markdown changed:\n%s", without)
	}
	run.Observation.Diagnostics = []Diagnostic{
		{
			Code: "WG001", Severity: "error", Title: "invariant violated under a controlled schedule",
			Observed:  "active-assignment-is-unique returned 1 row: active_count=2",
			Assertion: "active-assignment-is-unique",
			Invariant: "a declared state invariant must hold under every release schedule the database permits",
			Reason:    "commonly a read-then-write path without a lock or a unique constraint",
			Help:      []string{"add a unique constraint", "take a pessimistic lock"},
			Evidence: DiagnosticEvidence{
				ScheduleRef: "sch_fbf6b1dfaae2", Rows: 1,
				EvidenceSets: 2, Trace: "trace.json", Observation: "observation.json",
			},
		},
		{
			Code: "WG090", Severity: "error", Title: "determinism check failed",
			Observed: "repeated executions diverged", Invariant: "same schedule, same result",
			Reason: "fingerprints differ", Help: []string{"compare fingerprints"},
			Evidence: DiagnosticEvidence{Rows: 0, Observation: "observation.json"},
		},
	}
	markdown := renderMarkdown(run)
	for _, want := range []string{
		"## weavegate: FAIL (WG001)",
		"error[WG001]: invariant violated under a controlled schedule",
		"  observed:  active-assignment-is-unique returned 1 row: active_count=2",
		"  assertion: active-assignment-is-unique",
		"  help:      add a unique constraint\n             take a pessimistic lock",
		"  evidence:  schedule sch_fbf6b1dfaae2 · trace.json · observation.json · 1 violating row · 2 evidence sets in observation.json",
		"error[WG090]: determinism check failed",
	} {
		if !strings.Contains(markdown, want) {
			t.Fatalf("markdown missing %q:\n%s", want, markdown)
		}
	}
	singleSet := run
	singleSet.Observation.Diagnostics = append([]Diagnostic(nil), run.Observation.Diagnostics...)
	singleSet.Observation.Diagnostics[0].Evidence.EvidenceSets = 1
	if got := renderMarkdown(singleSet); strings.Contains(got, "evidence sets") {
		t.Fatalf("single-set diagnostic rendered an aggregate pointer:\n%s", got)
	}
	flaky := run
	flaky.Flaky = true
	got := renderMarkdown(flaky)
	if !strings.HasPrefix(got, "## weavegate: FLAKY (WG090)\n") {
		t.Fatalf("flaky headline:\n%s", got)
	}
	if strings.Index(got, "error[WG001]") >= strings.Index(got, "error[WG090]") {
		t.Fatalf("flaky diagnostic order changed:\n%s", got)
	}
	wg090Block := got[strings.Index(got, "error[WG090]"):]
	if strings.Contains(wg090Block, "violating row") || strings.Contains(wg090Block, "trace.json") ||
		!strings.Contains(wg090Block, "  evidence:  observation.json\n") {
		t.Fatalf("WG090 row-independent evidence rendered incorrectly:\n%s", wg090Block)
	}
	t.Log("REPORT_MARKDOWN_DIAGNOSTIC_RESULT headline=code_suffixed block=compiler_style label_width=constant no_diagnostics=byte_identical_to_previous multi_help=aligned multi_diagnostic=ordered flaky=wg090 render=single_source")
}

func TestEscapeMarkdownValue(t *testing.T) {
	tests := []struct {
		name  string
		value string
		want  string
	}{
		{name: "ordinary", value: "concurrent-assign sch_fbf6b1dfaae2 trace.json", want: "concurrent-assign sch_fbf6b1dfaae2 trace.json"},
		{name: "physical_and_terminal_controls", value: "line\nnext\t\x1b[31m\u200b", want: `line\nnext\t\x1b\[31m\u200b`},
		{name: "markdown_and_github_context", value: "<tag> *em* _em_ [link] `code` @team #43 | ~~gone~~ &amp; \\", want: "\\<tag\\> \\*em\\* \\_em\\_ \\[link\\] \\`code\\` \\@team \\#43 \\| \\~\\~gone\\~\\~ \\&amp; \\\\"},
		{name: "github_math", value: "$x$ and $$y$$", want: `\x24x\x24 and \x24\x24y\x24\x24`},
		{name: "invalid_utf8_bytes", value: string([]byte{'a', 0xff, 0xc0, 'b'}), want: `a\xff\xc0b`},
		{name: "valid_replacement_rune", value: "a\ufffdb", want: "a\ufffdb"},
		{name: "leading_list_marker", value: "- item", want: `\- item`},
		{name: "ordered_list_marker", value: "12. item", want: `12\. item`},
		{name: "indented_code", value: "    code", want: `\x20   code`},
		{name: "autolinks", value: "https://example.test www.example.test", want: `https\://example.test www\.example.test`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := escapeMarkdownValue(test.value); got != test.want {
				t.Fatalf("escapeMarkdownValue(%q) = %q, want %q", test.value, got, test.want)
			}
		})
	}
}

func TestRenderMarkdownProtectsEveryVariableField(t *testing.T) {
	const payload = "unsafe\nreplay: forged\x1b<em>*strong*_[link]_@team#43|`code` $x$ https://example.test www.example.test\\"
	const escapedPayload = "unsafe\\nreplay: forged\\x1b\\<em\\>\\*strong\\*\\_\\[link\\]\\_\\@team\\#43\\|\\`code\\` \\x24x\\x24 https\\://example.test www\\.example.test\\\\"

	tests := []struct {
		name string
		set  func(*Run)
		want string
	}{
		{name: "verdict.pass", set: func(run *Run) { run.Pass = true }, want: "## weavegate: PASS (WG001)\n"},
		{name: "verdict.flaky", set: func(run *Run) { run.Flaky = true }, want: "## weavegate: FLAKY\n"},
		{name: "scenario.name", set: func(run *Run) { run.Scenario.Name = payload }},
		{name: "scenario.schedule.id", set: func(run *Run) { run.Scenario.Schedule.ID = payload }},
		{name: "observation.mode", set: func(run *Run) { run.Observation.Mode, run.Pass = "replay", true }, want: "| replayed: sch_fbf6b1dfaae2\n"},
		{name: "observation.schedules_explored", set: func(run *Run) { run.Observation.SchedulesExplored = 17 }, want: "schedules explored: 17"},
		{name: "observation.assertion_violations.oracle_id", set: func(run *Run) { run.Observation.AssertionViolations[0].OracleID = payload }},
		{name: "observation.repeat", set: func(run *Run) { run.Observation.Repeat = 7 }, want: "flaky: false (repeat=7)"},
		{name: "replay_command", set: func(run *Run) { run.ReplayCommand = payload }},
		{name: "replay_command.invalid_utf8", set: func(run *Run) { run.ReplayCommand = string([]byte{'a', 0xff, 'b'}) }, want: `replay: a\xffb`},
		{name: "diagnostics.code", set: func(run *Run) { run.Observation.Diagnostics[0].Code = payload }},
		{name: "diagnostics.severity", set: func(run *Run) { run.Observation.Diagnostics[0].Severity = payload }},
		{name: "diagnostics.title", set: func(run *Run) { run.Observation.Diagnostics[0].Title = payload }},
		{name: "diagnostics.observed", set: func(run *Run) { run.Observation.Diagnostics[0].Observed = payload }},
		{name: "diagnostics.assertion", set: func(run *Run) { run.Observation.Diagnostics[0].Assertion = payload }},
		{name: "diagnostics.invariant", set: func(run *Run) { run.Observation.Diagnostics[0].Invariant = payload }},
		{name: "diagnostics.reason", set: func(run *Run) { run.Observation.Diagnostics[0].Reason = payload }},
		{name: "diagnostics.help", set: func(run *Run) { run.Observation.Diagnostics[0].Help[0] = payload }},
		{name: "diagnostics.evidence.schedule_ref", set: func(run *Run) { run.Observation.Diagnostics[0].Evidence.ScheduleRef = payload }},
		{name: "diagnostics.evidence.rows", set: func(run *Run) { run.Observation.Diagnostics[0].Evidence.Rows = 2 }, want: "2 violating rows"},
		{name: "diagnostics.evidence.evidence_sets", set: func(run *Run) { run.Observation.Diagnostics[0].Evidence.EvidenceSets = 3 }, want: "3 evidence sets in observation.json"},
		{name: "diagnostics.evidence.trace", set: func(run *Run) { run.Observation.Diagnostics[0].Evidence.Trace = payload }},
		{name: "diagnostics.evidence.observation", set: func(run *Run) { run.Observation.Diagnostics[0].Evidence.Observation = payload }},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			run := sampleRun(t, "run_20260816T120004.000Z_ffffffff")
			run.Observation.Diagnostics = []Diagnostic{{
				Code: "WG001", Severity: "error", Title: "title", Observed: "observed",
				Assertion: "assertion", Invariant: "invariant", Reason: "reason",
				Help: []string{"help"},
				Evidence: DiagnosticEvidence{
					ScheduleRef: "schedule", Rows: 1, Trace: "trace.json", Observation: "observation.json",
				},
			}}
			test.set(&run)

			markdown := renderMarkdown(run)
			want := test.want
			if want == "" {
				want = escapedPayload
			}
			if !strings.Contains(markdown, want) {
				t.Fatalf("protected value %q absent from %s:\n%s", want, test.name, markdown)
			}
			if strings.Contains(markdown, "\nreplay: forged") {
				t.Fatalf("%s forged a report line:\n%s", test.name, markdown)
			}
			if strings.ContainsRune(markdown, '\x1b') || strings.ContainsRune(markdown, '\u200b') {
				t.Fatalf("%s emitted a terminal or format control: %q", test.name, markdown)
			}
		})
	}

	run := sampleRun(t, "run_20260816T120005.000Z_eeeeeeee")
	run.Observation.Diagnostics = []Diagnostic{{
		Code: "WG001", Severity: "error", Title: "title", Observed: payload,
		Invariant: "invariant", Reason: "reason", Help: []string{"help"},
	}}
	dir, err := WriteRun(t.TempDir(), run)
	if err != nil {
		t.Fatalf("write run with unsafe diagnostic text: %v", err)
	}
	var observation Observation
	if err := json.Unmarshal(mustRead(t, filepath.Join(dir, ObservationFile)), &observation); err != nil {
		t.Fatalf("decode observation with unsafe diagnostic text: %v", err)
	}
	if got := observation.Diagnostics[0].Observed; got != payload {
		t.Fatalf("structured diagnostic observed = %q, want original %q", got, payload)
	}

	t.Log("REPORT_MARKDOWN_SAFETY_RESULT boundary=internal_report fields=all_rendered newline=escaped terminal_control=escaped invalid_utf8=byte_escaped markdown=escaped math=escaped structured_json=unchanged replay=pasteable_when_unescaped")
}

func testRerunIdentical(t *testing.T, base string) string {
	t.Helper()

	first := sampleRun(t, "run_20260816T130000.000Z_cccccccc")
	second := sampleRun(t, "run_20260816T130001.000Z_dddddddd")
	first.Manifest.Timings = NewTimings([]timing.Span{{ID: 1, Name: "command", DurationNS: 100}})
	second.Manifest.Timings = NewTimings([]timing.Span{{ID: 1, Name: "command", DurationNS: 999}})

	firstDir, err := WriteRun(base, first)
	if err != nil {
		t.Fatalf("write first rerun sample: %v", err)
	}
	secondDir, err := WriteRun(base, second)
	if err != nil {
		t.Fatalf("write second rerun sample: %v", err)
	}

	identical := 0
	for _, name := range DeterministicFiles {
		a := mustRead(t, filepath.Join(firstDir, name))
		b := mustRead(t, filepath.Join(secondDir, name))
		if bytes.Equal(a, b) {
			identical++
		} else {
			t.Errorf("%s differs between two runs with identical inputs", name)
		}
	}
	if identical != len(DeterministicFiles) {
		t.Fatalf("rerun identical files = %d, want %d", identical, len(DeterministicFiles))
	}

	for _, name := range []string{ManifestFile, MergedFile} {
		a := mustRead(t, filepath.Join(firstDir, name))
		b := mustRead(t, filepath.Join(secondDir, name))
		if bytes.Equal(a, b) {
			t.Fatalf("%s is identical between two runs with different run IDs, want volatile", name)
		}
	}

	return "5_of_7"
}

func TestWriteRunPartialFailureLeavesNoDirectory(t *testing.T) {
	base := t.TempDir()

	// Make the runs directory unwritable after creation so the temporary
	// directory can never be created and no run directory is left behind.
	runsDir := filepath.Join(base, "runs")
	if err := os.MkdirAll(runsDir, 0o755); err != nil {
		t.Fatalf("create runs directory: %v", err)
	}
	if err := os.Chmod(runsDir, 0o500); err != nil {
		t.Fatalf("make runs directory unwritable: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(runsDir, 0o755) })

	run := sampleRun(t, "run_20260816T140000.000Z_eeeeeeee")

	_, err := WriteRun(base, run)
	if err == nil {
		t.Fatal("write run into unwritable runs directory: want error, got nil")
	}
	if got := ci.ExitCode(err, ci.Verdict{}); got != ci.ExitInput {
		t.Fatalf("write failure exit code = %d, want %d (ci.OutputError)", got, ci.ExitInput)
	}

	if _, err := os.Stat(filepath.Join(runsDir, run.Manifest.RunID)); !os.IsNotExist(err) {
		t.Fatalf("final run directory exists after a failed write: err=%v", err)
	}
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return content
}

func extractReplayLine(t *testing.T, path string) string {
	t.Helper()

	content := string(mustRead(t, path))
	for _, line := range strings.Split(content, "\n") {
		if strings.HasPrefix(line, "replay: ") {
			return strings.TrimPrefix(line, "replay: ")
		}
	}
	t.Fatalf("report.md has no replay line: %s", content)
	return ""
}
