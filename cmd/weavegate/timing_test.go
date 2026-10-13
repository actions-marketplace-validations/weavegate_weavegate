package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/weavegate/weavegate/internal/report"
)

// requireRunTimings checks the published CLI artifact against execution counts,
// including all passing candidates in exhausted exploration.
func requireRunTimings(t *testing.T, dir string) {
	t.Helper()
	manifest := readManifest(t, dir)
	observation := readObservation(t, dir)
	scenario := readScenario(t, dir)
	timings := manifest.Timings
	if timings == nil || timings.Scope != "before_artifact_write" || len(timings.Spans) == 0 {
		t.Fatalf("missing timing evidence: %+v", timings)
	}
	counts := map[string]int{}
	children := map[int]map[string]int{}
	for index, span := range timings.Spans {
		counts[span.Name]++
		if span.ID != index+1 || span.StartNS < 0 || span.DurationNS < 0 || span.ParentID < 0 || span.ParentID >= span.ID {
			t.Fatalf("invalid timing interval: %+v", span)
		}
		if index == 0 {
			if span.Name != "command" || span.ParentID != 0 || span.DurationNS <= 0 {
				t.Fatalf("invalid timing root: %+v", span)
			}
			continue
		}
		if span.ParentID == 0 {
			t.Fatalf("interval is outside command: %+v", span)
		}
		parent := timings.Spans[span.ParentID-1]
		if span.StartNS < parent.StartNS || span.StartNS+span.DurationNS > parent.StartNS+parent.DurationNS {
			t.Fatalf("interval outside parent: child=%+v parent=%+v", span, parent)
		}
		if children[span.ParentID] == nil {
			children[span.ParentID] = map[string]int{}
		}
		children[span.ParentID][span.Name]++
		if span.Name == "schedule_run" && (span.ScheduleID == "" ||
			(observation.Mode == "replay" && span.ScheduleID != scenario.Schedule.ID)) {
			t.Fatalf("missing/wrong execution identity: %+v", span)
		}
	}
	for _, name := range []string{"preflight", "fixture_provision", "fixture_teardown"} {
		if counts[name] != 1 {
			t.Fatalf("%s count = %d, want 1", name, counts[name])
		}
	}
	executions := observation.SchedulesExplored
	if scenario.Schedule != nil {
		executions += observation.Repeat
	}
	if counts["schedule_run"] != executions || counts["explore_pass"] != observation.ExplorePasses {
		t.Fatalf("timing lost execution/pass evidence: counts=%v observation=%+v", counts, observation)
	}
	for _, span := range timings.Spans {
		if span.Name != "schedule_run" {
			continue
		}
		for _, name := range []string{"fixture_reset", "adapter_create", "adapter_start", "schedule_execute", "oracle_evaluate", "adapter_stop"} {
			if children[span.ID][name] != 1 {
				t.Fatalf("execution %d missing/duplicate %s: %v", span.ID, name, children[span.ID])
			}
		}
	}
	if manifest.Adapter == "external" && (counts["jar_snapshot"] != executions || counts["process_start_to_ready"] != executions) {
		t.Fatalf("external startup timing incomplete: %v", counts)
	}
	content, err := os.ReadFile(filepath.Join(dir, report.MergedFile))
	if err != nil {
		t.Fatal(err)
	}
	var merged report.Merged
	if err := json.Unmarshal(content, &merged); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(merged.Manifest.Timings, timings) {
		t.Fatal("manifest and merged report timing disagree")
	}
	t.Log("CLI_TIMING_RESULT scope=before_artifact_write executions=complete hierarchy=valid")
}
