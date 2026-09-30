// Package report writes the six or seven run directory artifacts a weavegate
// run leaves behind, and renders the human-readable summary. Two files are
// volatile per run (manifest.json, report.json); the remaining five are
// deterministic when a schedule is present — the same inputs produce
// byte-identical output. See
// docs/adr/0005-volatile-run-metadata-boundary.md.
package report

import (
	"time"

	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/trace"
)

const (
	// ArtifactVersion is the released contract used by ordinary run evidence.
	ArtifactVersion = 2
	// DiagnosticFailureArtifactVersion preserves ArtifactVersion's released
	// diagnostics semantics while identifying evidence retained after
	// diagnostic derivation failed.
	DiagnosticFailureArtifactVersion = 3
)

// Manifest is volatile per-run metadata: identifiers, timestamps, and
// environment facts collected immediately after fixture provisioning
// (A-7). This package never queries the database itself.
type Manifest struct {
	ArtifactVersion  int       `json:"artifact_version"`
	RunID            string    `json:"run_id"`
	StartedAt        time.Time `json:"started_at"`
	WeavegateVersion string    `json:"weavegate_version"`
	SchemaVersion    string    `json:"schema_version"`
	SeedData         string    `json:"seed_data"`
	IsolationLevel   string    `json:"isolation_level"`
	Engine           string    `json:"engine"`
	Adapter          string    `json:"adapter"`
	Variant          string    `json:"variant"`
	Image            string    `json:"image"`
	// SUTSHA256 identifies the configured external JAR checked at preflight
	// and again before each adapter is constructed. It is absent for gonative.
	SUTSHA256 string `json:"sut_sha256,omitempty"`

	// CleanupFailed is true when fixture teardown failed after this run
	// completed (A-18): a leaked container or database. It belongs here,
	// not in a deterministic file, because whether teardown happens to
	// succeed is a volatile fact about this specific run's environment,
	// independent of the scenario's own (deterministic) outcome — see
	// Pass on Run. It never changes what Pass or the rendered headline
	// say; it only explains, for this saved run, why the process exit
	// code may be higher than the headline alone would suggest.
	CleanupFailed bool `json:"cleanup_failed"`
}

// Worker binds a stable worker ID to an adapter command. It is this
// package's own serialization shape for scenario.Worker: the engine type
// carries no JSON tags of its own, so encoding it directly would leak
// exported Go field names (ID/Command) instead of this package's
// lowercase snake_case contract.
type Worker struct {
	ID      string `json:"id"`
	Command string `json:"command"`
}

// NewWorkers converts scenario workers into this package's own JSON shape.
func NewWorkers(workers []scenario.Worker) []Worker {
	converted := make([]Worker, 0, len(workers))
	for _, worker := range workers {
		converted = append(converted, Worker{ID: worker.ID, Command: worker.Command})
	}
	return converted
}

// CoordinationStep identifies one worker arrival that a replay intends to
// release, mirroring scenario.CoordinationStep as this package's own
// serialization shape.
type CoordinationStep struct {
	Worker string `json:"worker"`
	Point  string `json:"point"`
}

// Schedule is a content-addressed total order of coordination intents,
// mirroring scenario.Schedule as this package's own serialization shape.
type Schedule struct {
	ID    string             `json:"id"`
	Steps []CoordinationStep `json:"steps"`
}

// NewSchedule converts a scenario schedule into this package's own JSON
// shape, or returns nil when schedule is nil (a passing run with nothing to
// report).
func NewSchedule(schedule *scenario.Schedule) *Schedule {
	if schedule == nil {
		return nil
	}
	steps := make([]CoordinationStep, 0, len(schedule.Steps))
	for _, step := range schedule.Steps {
		steps = append(steps, CoordinationStep{Worker: step.Worker, Point: step.Point})
	}
	return &Schedule{ID: schedule.ID, Steps: steps}
}

// Scenario is the configured scenario together with the schedule this run
// reports on — the schedule discovered during exploration, or the schedule
// a replay was given. Params is the effective worker Args shared by every
// worker in the scenario (config validation requires them to be identical),
// so the evidence records which parameters produced this verdict even if
// the referenced config is later edited or deleted.
type Scenario struct {
	ArtifactVersion int               `json:"artifact_version"`
	Name            string            `json:"name"`
	Workers         []Worker          `json:"workers"`
	SyncPoints      []string          `json:"sync_points"`
	Params          map[string]string `json:"params"`
	Schedule        *Schedule         `json:"schedule,omitempty"`
}

// OracleDeclaration is one assertion's effective declaration (config's
// oracle.assertions entry), persisted alongside the run's evidence so a
// saved verdict can still be audited against the query that produced it
// even if the referenced config is later edited or deleted.
type OracleDeclaration struct {
	ID         string `json:"id"`
	SQL        string `json:"sql"`
	ExpectRows int    `json:"expect_rows"`
}

// Row is deterministic, JSON-safe evidence keyed by a stable column name.
// It mirrors oracle.Row's shape as this package's own type, rather than
// exposing the oracle package's type in the report contract.
type Row map[string]any

// AssertionViolation is one Oracle's violation evidence: the rows that
// caused an assertion to fail, mirroring oracle.Violation.Rows. Distinct
// violations of the same OracleID (for example, the discovery run and a
// replay run reproducing it with different rows) are kept as separate
// entries rather than merged, so each is individually auditable.
type AssertionViolation struct {
	OracleID string `json:"oracle_id"`
	Rows     []Row  `json:"rows"`
}

// Diagnostic is the report-owned serialization shape for one named verdict.
// Keeping this DTO here prevents internal diagnostic implementation types
// from becoming the artifact contract by accident.
type Diagnostic struct {
	Code      string             `json:"code"`
	Severity  string             `json:"severity"`
	Title     string             `json:"title"`
	Observed  string             `json:"observed"`
	Assertion string             `json:"assertion,omitempty"`
	Invariant string             `json:"invariant"`
	Reason    string             `json:"reason"`
	Help      []string           `json:"help"`
	Evidence  DiagnosticEvidence `json:"evidence"`
}

type DiagnosticEvidence struct {
	ScheduleRef  string `json:"schedule_ref,omitempty"`
	Rows         int    `json:"rows"`
	EvidenceSets int    `json:"evidence_sets,omitempty"`
	Trace        string `json:"trace"`
	Observation  string `json:"observation,omitempty"`
}

// Timeouts is the effective execution timeout configuration used for this
// run: the configured arrive timeout and the four orchestrator deadlines
// derived from it (A-13). These deadlines affect terminal timing
// classifications, normalized fingerprints, and therefore potentially the
// flaky verdict, so they are snapshotted here alongside the other effective
// run inputs (repeat, oracles, params) — a saved verdict stays auditable
// against the timing policy that produced it even if the referenced config
// is later edited or deleted.
type Timeouts struct {
	ArriveMS         int64 `json:"arrive_timeout_ms"`
	BlockInferenceMS int64 `json:"block_inference_timeout_ms"`
	StepMS           int64 `json:"step_timeout_ms"`
	RunMS            int64 `json:"run_timeout_ms"`
	StopMS           int64 `json:"stop_timeout_ms"`
}

// Observation holds only the fields this run can actually compute (A-8).
// Fields belonging to unimplemented oracles are omitted rather than
// zero-filled, so "not measured" is never presented as "measured zero".
type Observation struct {
	ArtifactVersion     int                  `json:"artifact_version"`
	Mode                string               `json:"mode"`
	SchedulesExplored   int                  `json:"schedules_explored"`
	ExplorePasses       int                  `json:"explore_passes"`
	AssertionViolations []AssertionViolation `json:"assertion_violations"`
	Diagnostics         []Diagnostic         `json:"diagnostics"`
	Oracles             []OracleDeclaration  `json:"oracles"`
	Repeat              int                  `json:"repeat"`
	Timeouts            Timeouts             `json:"timeouts"`
	ViolationRuns       int                  `json:"violation_runs"`
	Flaky               bool                 `json:"flaky"`

	// Fingerprints counts, per distinct normalized execution fingerprint,
	// how many of the repeat replay runs produced it. A run can be flaky when
	// Oracle results, normalized trace events, or terminal state diverge,
	// including with zero assertion violations anywhere, so this is the
	// evidence of *why* such a run is flaky.
	Fingerprints map[string]int `json:"fingerprints"`

	// DiscoveryFingerprint is present only when exploration produced a
	// discovery run. Direct replay never emits it.
	DiscoveryFingerprint string `json:"discovery_fingerprint,omitempty"`
}

// Event is one ordered, wall-clock-free trace observation, mirroring
// trace.Event as this package's own serialization shape.
type Event struct {
	Seq          int    `json:"seq"`
	Kind         string `json:"kind"`
	Step         int    `json:"step"`
	Worker       string `json:"worker"`
	Point        string `json:"point"`
	Status       string `json:"status"`
	FailureClass string `json:"failure_class"`
}

// WorkerTerminal is the normalized terminal data included in replay
// fingerprints, mirroring trace.WorkerTerminal as this package's own
// serialization shape.
type WorkerTerminal struct {
	Worker       string `json:"worker"`
	State        string `json:"state"`
	FailureClass string `json:"failure_class"`
}

// Trace is the run directory's trace.json shape.
type Trace struct {
	ArtifactVersion int              `json:"artifact_version"`
	ScheduleRef     string           `json:"schedule_ref"`
	Events          []Event          `json:"events"`
	Terminals       []WorkerTerminal `json:"terminals"`
}

// NewTrace converts an engine trace into this package's own JSON shape.
func NewTrace(scheduleRef string, events trace.Trace, terminals trace.Terminals) Trace {
	convertedEvents := make([]Event, 0, len(events))
	for _, event := range events {
		convertedEvents = append(convertedEvents, Event{
			Seq:          event.Seq,
			Kind:         string(event.Kind),
			Step:         event.Step,
			Worker:       event.Worker,
			Point:        event.Point,
			Status:       string(event.Status),
			FailureClass: string(event.FailureClass),
		})
	}
	convertedTerminals := make([]WorkerTerminal, 0, len(terminals))
	for _, terminal := range terminals {
		convertedTerminals = append(convertedTerminals, WorkerTerminal{
			Worker:       terminal.Worker,
			State:        string(terminal.State),
			FailureClass: string(terminal.FailureClass),
		})
	}
	return Trace{ScheduleRef: scheduleRef, Events: convertedEvents, Terminals: convertedTerminals}
}

// Merged is the report.json shape: manifest, scenario, and observation in
// one file. It inherits manifest's volatile fields, so it is not part of
// the deterministic file set.
type Merged struct {
	ArtifactVersion int         `json:"artifact_version"`
	Manifest        Manifest    `json:"manifest"`
	Scenario        Scenario    `json:"scenario"`
	Observation     Observation `json:"observation"`
}

// Run is everything needed to write one run directory.
type Run struct {
	// DiagnosticDerivationFailed selects DiagnosticFailureArtifactVersion for
	// every run-scoped JSON artifact. Keeping the semantic input rather than an
	// arbitrary version number makes version 3 exclusive to retained diagnostic
	// derivation failures.
	DiagnosticDerivationFailed bool

	Manifest    Manifest
	Scenario    Scenario
	Observation Observation
	Trace       Trace

	// Pass is true when the run found no violation. Flaky takes priority
	// over Pass in the rendered headline (A-17): a run can be neither a
	// clean pass nor a stable violation.
	Pass  bool
	Flaky bool

	// ReplayCommand is the shell-correct command line for this run's schedule
	// (A-5). The Markdown boundary may escape its display; it is pasteable
	// only when that rendering leaves it unchanged. Empty when the run has no
	// schedule to replay (an exhausted PASS with no discovery).
	ReplayCommand string
}
