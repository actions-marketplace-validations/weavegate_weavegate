package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/spf13/cobra"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/diagnostic"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/report"
	"github.com/weavegate/weavegate/internal/scenario"
)

type cleanupGuard struct {
	once      sync.Once
	fixture   fixture.Fixture
	operation context.Context
	timeout   time.Duration
	err       error
}

func (g *cleanupGuard) run() error {
	g.once.Do(func() {
		operation := g.operation
		if operation == nil {
			operation = context.Background()
		}
		cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(operation), g.timeout)
		defer cancel()
		g.err = g.fixture.Teardown(cleanupCtx)
	})
	return g.err
}

type runFlags struct {
	config     string
	scenario   string
	replay     string
	replaySet  bool
	repeat     int
	repeatSet  bool
	variant    string
	variantSet bool
	out        string
}

func newRunCommand(stdout, stderr io.Writer) *cobra.Command {
	flags := &runFlags{}
	cmd := &cobra.Command{
		Use:           "run",
		Short:         "Reach a verdict on a configured scenario.",
		SilenceUsage:  true,
		SilenceErrors: true,
		Args:          cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			flags.replaySet = cmd.Flags().Changed("replay")
			flags.repeatSet = cmd.Flags().Changed("repeat")
			flags.variantSet = cmd.Flags().Changed("variant")
			return runScenario(cmd.Context(), stdout, stderr, *flags, fixture.NewMySQLFixture)
		},
	}

	cmd.Flags().StringVar(&flags.config, "config", ".weavegate/config.yaml", "path to the run configuration file")
	cmd.Flags().StringVar(&flags.scenario, "scenario", "", "scenario name to run (required)")
	cmd.Flags().StringVar(&flags.replay, "replay", "", "schedule ID or file to replay instead of exploring")
	cmd.Flags().IntVar(&flags.repeat, "repeat", 0, "override run.repeat from the config")
	cmd.Flags().StringVar(&flags.variant, "variant", "", "override target.sut.variant from the config")
	cmd.Flags().StringVar(&flags.out, "out", ".weavegate", "run directory base")
	_ = cmd.MarkFlagRequired("scenario")

	return cmd
}

// runScenario is the run command's implementation, factored out from Cobra
// wiring so tests can inject a fixture factory and call it in-process
// without spawning the compiled binary (os.Exit is never exercised here).
func runScenario(
	ctx context.Context,
	stdout, stderr io.Writer,
	flags runFlags,
	newFixture func() fixture.Provisioner,
) (finalErr error) {
	return runScenarioWithDiagnosticDeriver(ctx, stdout, stderr, flags, newFixture, diagnostic.Derive)
}

// runScenarioWithDiagnosticDeriver keeps diagnostic production injectable at
// the CLI boundary. Production always supplies diagnostic.Derive; the seam lets
// a CLI test force the post-execution failure that must still preserve evidence.
func runScenarioWithDiagnosticDeriver(
	ctx context.Context,
	stdout, stderr io.Writer,
	flags runFlags,
	newFixture func() fixture.Provisioner,
	deriveDiagnostics func(diagnostic.Input) ([]diagnostic.Diagnostic, error),
) (finalErr error) {
	startedAt := time.Now().UTC()

	plan, err := buildRunPlan(flags)
	if err != nil {
		return reportRunFailure(stderr, err)
	}

	fx := newFixture()
	cleanup := &cleanupGuard{
		fixture: fx, operation: ctx, timeout: plan.CleanupTimeout,
	}
	cleanupDone := false

	// Register cleanup immediately after fixture construction so every later
	// return requests bounded teardown. The explicit success-path call below
	// records cleanup in the manifest; this fallback runs only when execution
	// returned earlier.
	defer func() {
		if cleanupDone {
			return
		}
		cleanupErr := cleanup.run()
		if cleanupErr == nil {
			return
		}
		_, _ = fmt.Fprintf(stderr, "weavegate: warning: cleanup failed: %v\n", cleanupErr)

		var exit *exitError
		if errors.As(finalErr, &exit) {
			exit.verdict.CleanupFailed = true
			return
		}
		finalErr = &exitError{err: ci.FixtureError(cleanupErr)}
	}()

	db, err := fx.Provision(ctx, plan.Fixture)
	if err != nil {
		return reportRunFailure(stderr, ci.FixtureError(fmt.Errorf("run: provision fixture: %w", err)))
	}

	runID, err := newRunID(startedAt)
	if err != nil {
		return reportRunFailure(stderr, ci.OutputError(fmt.Errorf("run: %w", err)))
	}

	manifest, err := collectManifest(
		ctx, db, plan.Fixture,
		plan.Config.Target.SUT.Adapter, plan.Resolved.Scenario.SUTConfig.Variant,
		runID, startedAt,
	)
	if err != nil {
		return reportRunFailure(stderr, ci.FixtureError(fmt.Errorf("run: collect manifest: %w", err)))
	}
	manifest.SUTSHA256 = plan.Resolved.SUTSHA256

	executor, err := orchestrator.New(orchestrator.Config{
		Fixture:               fx,
		DB:                    db,
		NewRuntime:            plan.Resolved.NewRuntime,
		NewAdapter:            plan.Resolved.NewAdapter,
		BlockInferenceTimeout: plan.Resolved.Timeouts.BlockInference,
		StepTimeout:           plan.Resolved.Timeouts.Step,
		RunTimeout:            plan.Resolved.Timeouts.Run,
		StopTimeout:           plan.Resolved.Timeouts.Stop,
	})
	if err != nil {
		return reportRunFailure(stderr, ci.InputError(fmt.Errorf("run: create orchestrator: %w", err)))
	}

	outcome, err := executeScenario(ctx, executor, plan)
	if err != nil {
		return reportRunFailure(stderr, err)
	}

	// Tear down before writing and printing so cleanup failure is present in
	// the saved manifest. cleanupGuard guarantees this is the only call.
	cleanupErr := cleanup.run()
	cleanupDone = true
	if cleanupErr != nil {
		_, _ = fmt.Fprintf(stderr, "weavegate: warning: cleanup failed: %v\n", cleanupErr)
		manifest.CleanupFailed = true
	}
	semanticVerdict := ci.Verdict{
		Violations:    outcome.Verdict.ViolationRuns,
		Flaky:         outcome.Verdict.Flaky,
		CleanupFailed: manifest.CleanupFailed,
	}

	replayCommand := buildReplayCommand(flags, plan.Resolved.Scenario.SUTConfig.Variant, plan.Repeat, outcome.ViolatingSchedule)
	diagnostics, diagnosticErr := deriveDiagnostics(diagnostic.Input{
		Table:                plan.Resolved.Diagnostics,
		Violations:           diagnosticViolations(violationEvidenceRuns(outcome)),
		OracleOrder:          oracleOrder(plan.Config.Oracle.Assertions),
		TraceOracles:         traceOracleIDs(outcome),
		Flaky:                outcome.Verdict.Flaky,
		Fingerprints:         outcome.Replay.Fingerprints,
		DiscoveryFingerprint: discoveryFingerprint(outcome),
		ScheduleRef:          violatingScheduleID(outcome),
	})
	if diagnosticErr != nil {
		// A diagnostic is a presentation of an already-decided verdict. Drop
		// any partial result, preserve the execution evidence without
		// diagnostics, and report the production failure after publication.
		diagnostics = nil
		diagnosticErr = ci.OutputError(fmt.Errorf("run: derive diagnostics: %w", diagnosticErr))
	}

	run := report.Run{
		Manifest: manifest,
		Scenario: report.Scenario{
			Name:       plan.Resolved.Scenario.Name,
			Workers:    report.NewWorkers(plan.Resolved.Scenario.Workers),
			SyncPoints: plan.Resolved.Scenario.SyncPoints,
			Params:     plan.Resolved.Scenario.SUTConfig.Params,
			Schedule:   report.NewSchedule(outcome.ViolatingSchedule),
		},
		Observation: report.Observation{
			Mode:                 string(outcome.Mode),
			SchedulesExplored:    outcome.SchedulesExplored,
			ExplorePasses:        outcome.PassesExecuted,
			AssertionViolations:  assertionViolations(violationEvidenceRuns(outcome)),
			Diagnostics:          reportDiagnostics(diagnostics),
			Oracles:              oracleDeclarations(plan.Config.Oracle.Assertions),
			Repeat:               plan.Repeat,
			Timeouts:             effectiveTimeouts(plan.Config, plan.Resolved),
			ViolationRuns:        outcome.Verdict.ViolationRuns,
			Flaky:                outcome.Verdict.Flaky,
			Fingerprints:         outcome.Replay.Fingerprints,
			DiscoveryFingerprint: discoveryFingerprint(outcome),
		},
		Trace:         runTrace(outcome),
		Pass:          !outcome.Verdict.Flaky && outcome.Verdict.ViolationRuns == 0,
		Flaky:         outcome.Verdict.Flaky,
		ReplayCommand: replayCommand,
	}
	if diagnosticErr != nil {
		run.DiagnosticDerivationFailed = true
	}

	dir, err := report.WriteRun(plan.Out, run)
	if err != nil {
		return reportRunFailure(stderr, err)
	}

	content, err := os.ReadFile(filepath.Join(dir, report.MarkdownFile))
	if err != nil {
		return reportRunFailure(stderr, ci.OutputError(fmt.Errorf("run: read %s: %w", report.MarkdownFile, err)))
	}
	if err := writeAll(stdout, content); err != nil {
		return reportRunFailure(stderr, ci.OutputError(fmt.Errorf("run: write report to stdout: %w", err)))
	}
	if err := writeAll(stdout, []byte(dir+"\n")); err != nil {
		return reportRunFailure(stderr, ci.OutputError(fmt.Errorf("run: write run directory to stdout: %w", err)))
	}
	if diagnosticErr != nil {
		return reportRunFailure(stderr, diagnosticErr)
	}

	return &exitError{verdict: semanticVerdict}
}

func discoveryFingerprint(outcome runOutcome) string {
	if outcome.Mode != ModeExplore || outcome.DiscoveryRun == nil {
		return ""
	}
	return outcome.DiscoveryRun.Fingerprint
}

// runTrace selects the run whose trace.json should be saved, in priority
// order:
//
//  1. A replay run with an assertion violation: a flaky replay's first run
//     can pass while a later run violates, and observation.json reports
//     that assertion regardless of which run found it, so the saved trace
//     must be able to support the reported observation rather than always
//     coming from a passing run.
//  2. Exploration's own discovery run (the 0/repeat flaky case: exploration
//     found a violation, but every replay run passed) — otherwise a FLAKY
//     verdict would have nothing to show for it at all.
//  3. A replay run whose fingerprint diverged from the others (direct
//     --replay can be flaky when Oracle results, trace events, or terminal
//     state diverge, including with zero assertion violations anywhere; an
//     arbitrary run shows nothing of that divergence, but a mismatching one
//     can provide a lead).
//  4. The first replay run, when nothing above found anything to prefer.
func runTrace(outcome runOutcome) report.Trace {
	selected, ok := selectTraceRun(outcome)
	if !ok {
		return report.Trace{}
	}
	return report.NewTrace(selected.ScheduleID, selected.Trace, selected.Terminals)
}

func selectTraceRun(outcome runOutcome) (orchestrator.RunResult, bool) {
	for _, run := range outcome.Replay.Runs {
		if runHasViolation(run) {
			return run, true
		}
	}
	if outcome.DiscoveryRun != nil {
		return *outcome.DiscoveryRun, true
	}
	if run, ok := firstMismatchRun(outcome.Replay); ok {
		return run, true
	}
	if len(outcome.Replay.Runs) > 0 {
		return outcome.Replay.Runs[0], true
	}
	return orchestrator.RunResult{}, false
}

// traceOracleIDs reports exactly which assertions the run selected for
// trace.json violated. It deliberately delegates selection to selectTraceRun
// so diagnostic evidence cannot drift from the trace writer's choice.
func traceOracleIDs(outcome runOutcome) []string {
	selected, ok := selectTraceRun(outcome)
	if !ok {
		return nil
	}
	var ids []string
	for _, result := range selected.Evaluation.Results {
		if len(result.Violations) > 0 {
			ids = append(ids, result.OracleID)
		}
	}
	return ids
}

// firstMismatchRun returns the first replay run whose fingerprint diverged
// from the replay's baseline (its first run), using orchestrator.Replay's
// own 1-based MismatchRuns indices into Runs.
func firstMismatchRun(replay orchestrator.ReplayResult) (orchestrator.RunResult, bool) {
	if len(replay.MismatchRuns) == 0 {
		return orchestrator.RunResult{}, false
	}
	index := replay.MismatchRuns[0] - 1
	if index < 0 || index >= len(replay.Runs) {
		return orchestrator.RunResult{}, false
	}
	return replay.Runs[index], true
}

func runHasViolation(run orchestrator.RunResult) bool {
	for _, result := range run.Evaluation.Results {
		if len(result.Violations) > 0 {
			return true
		}
	}
	return false
}

// violationEvidenceRuns returns every run that should be scanned for
// violated assertion IDs: all replay runs, plus the exploration's discovery
// run when replay never reproduced its violation. Without the discovery
// run, a 0/repeat flaky verdict would report assertion_violations: [] even
// though exploration found a specific assertion violated.
func violationEvidenceRuns(outcome runOutcome) []orchestrator.RunResult {
	runs := make([]orchestrator.RunResult, 0, len(outcome.Replay.Runs)+1)
	runs = append(runs, outcome.Replay.Runs...)
	if outcome.DiscoveryRun != nil {
		runs = append(runs, *outcome.DiscoveryRun)
	}
	return runs
}

// assertionViolations collects every distinct assertion violation across
// runs, preserving each violation's evidence rows (oracle.Violation.Rows) so
// a saved verdict can be audited against the rows that produced it. Runs are
// scanned in violationEvidenceRuns' order (replay runs, then the discovery
// run), and within a run in oracle declaration order, so the result order is
// deterministic for identical inputs. Two violations sharing an OracleID are
// only collapsed into one entry when their evidence rows are also identical
// — a later run reproducing the same assertion with different rows is kept
// as a separate, individually auditable entry.
func assertionViolations(runs []orchestrator.RunResult) []report.AssertionViolation {
	seen := make(map[string]struct{})
	var violations []report.AssertionViolation
	for _, run := range runs {
		for _, result := range run.Evaluation.Results {
			for _, violation := range result.Violations {
				rows := convertRows(violation.Rows)
				key, err := violationKey(result.OracleID, rows)
				if err != nil {
					continue
				}
				if _, exists := seen[key]; exists {
					continue
				}
				seen[key] = struct{}{}
				violations = append(violations, report.AssertionViolation{
					OracleID: result.OracleID,
					Rows:     rows,
				})
			}
		}
	}
	return violations
}

// diagnosticViolations preserves oracle.Kind until the diagnostic layer has
// classified it, while applying the same evidence de-duplication used by the
// report DTO. Repeated replays of identical evidence must not multiply either
// the artifact entries or a diagnostic's evidence-set count.
func diagnosticViolations(runs []orchestrator.RunResult) []oracle.Violation {
	seen := make(map[string]struct{})
	var violations []oracle.Violation
	for _, run := range runs {
		for _, result := range run.Evaluation.Results {
			for _, violation := range result.Violations {
				encoded, err := json.Marshal(violation)
				if err != nil {
					continue
				}
				key := string(encoded)
				if _, exists := seen[key]; exists {
					continue
				}
				seen[key] = struct{}{}
				violations = append(violations, violation)
			}
		}
	}
	return violations
}

func oracleOrder(assertions []config.Assertion) []string {
	order := make([]string, 0, len(assertions))
	for _, assertion := range assertions {
		order = append(order, assertion.ID)
	}
	return order
}

func violatingScheduleID(outcome runOutcome) string {
	if outcome.ViolatingSchedule == nil {
		return ""
	}
	return outcome.ViolatingSchedule.ID
}

func reportDiagnostics(values []diagnostic.Diagnostic) []report.Diagnostic {
	converted := make([]report.Diagnostic, 0, len(values))
	for _, value := range values {
		converted = append(converted, report.Diagnostic{
			Code: string(value.Code), Severity: string(value.Severity), Title: value.Title,
			Observed: value.Observed, Assertion: value.Assertion, Invariant: value.Invariant,
			Reason: value.Reason, Help: append([]string(nil), value.Help...),
			Evidence: report.DiagnosticEvidence{
				ScheduleRef:  value.Evidence.ScheduleRef,
				Rows:         value.Evidence.Rows,
				EvidenceSets: value.Evidence.EvidenceSets,
				Trace:        value.Evidence.Trace,
				Observation:  value.Evidence.Observation,
			},
		})
	}
	return converted
}

func convertRows(rows []oracle.Row) []report.Row {
	converted := make([]report.Row, 0, len(rows))
	for _, row := range rows {
		converted = append(converted, report.Row(row))
	}
	return converted
}

func violationKey(oracleID string, rows []report.Row) (string, error) {
	encoded, err := json.Marshal(struct {
		OracleID string       `json:"oracle_id"`
		Rows     []report.Row `json:"rows"`
	}{OracleID: oracleID, Rows: rows})
	if err != nil {
		return "", err
	}
	return string(encoded), nil
}

// oracleDeclarations converts the configured assertions to the shape saved
// with the run's evidence, so a saved verdict stays auditable against the
// query that produced it even after the referenced config changes.
func oracleDeclarations(assertions []config.Assertion) []report.OracleDeclaration {
	declarations := make([]report.OracleDeclaration, 0, len(assertions))
	for _, assertion := range assertions {
		declarations = append(declarations, report.OracleDeclaration{
			ID:         assertion.ID,
			SQL:        assertion.SQL,
			ExpectRows: assertion.ExpectRows,
		})
	}
	return declarations
}

// effectiveTimeouts converts this run's effective arrive timeout and its
// four derived orchestrator deadlines (resolved.Timeouts; see resolve.go's
// Resolve) to the shape saved with the run's evidence. These deadlines
// affect terminal timing classifications and normalized fingerprints, and
// therefore potentially the flaky verdict, so a saved run stays auditable
// against the timing policy that produced it even after the referenced
// config changes.
func effectiveTimeouts(cfg config.Config, resolved Resolved) report.Timeouts {
	return report.Timeouts{
		ArriveMS:         int64(cfg.Run.ArriveTimeoutMS),
		BlockInferenceMS: resolved.Timeouts.BlockInference.Milliseconds(),
		StepMS:           resolved.Timeouts.Step.Milliseconds(),
		RunMS:            resolved.Timeouts.Run.Milliseconds(),
		StopMS:           resolved.Timeouts.Stop.Milliseconds(),
	}
}

// buildReplayCommand renders the runnable replay line (A-5). --out is
// deliberately omitted: it is not part of the deterministic file contract
// (docs/adr/0005-volatile-run-metadata-boundary.md), so a config-as-given
// value here would make report.md vary with where a run happened to write
// its evidence rather than with what was actually run. Pasting the line from
// the same directory uses the default --out: stage ① finds evidence written
// there; see cli.md for the complete three-stage resolution order used for
// other locations.
func buildReplayCommand(flags runFlags, variant string, repeat int, schedule *scenario.Schedule) string {
	if schedule == nil {
		return ""
	}
	return fmt.Sprintf(
		"weavegate run --config %s --scenario %s --variant %s --replay %s --repeat %d",
		shellQuote(flags.config),
		shellQuote(flags.scenario),
		shellQuote(variant),
		shellQuote(schedule.ID),
		repeat,
	)
}

func shellQuote(value string) string {
	if value != "" {
		safe := true
		for _, char := range value {
			if !strings.ContainsRune("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-", char) {
				safe = false
				break
			}
		}
		if safe {
			return value
		}
	}
	return "'" + strings.ReplaceAll(value, "'", `'"'"'`) + "'"
}

// reportRunFailure prints err to stderr and wraps it as the process's
// terminal outcome. It never masks the underlying classification: err is
// expected to already be a ci.FixtureError, ci.InputError, or
// ci.OutputError from the call site.
func reportRunFailure(stderr io.Writer, err error) error {
	if errors.Is(err, context.Canceled) {
		err = ci.InterruptedError(err)
	}
	_, _ = fmt.Fprintf(stderr, "weavegate: %v\n", err)
	return &exitError{err: err}
}

// writeAll reports both a write error and a short write (a writer that
// consumed fewer bytes than given without itself returning an error) as a
// failure, so a caller printing an already-decided verdict to a writer that
// closes early — for example because the consuming process closed its end
// of a pipe — cannot silently report success.
func writeAll(w io.Writer, content []byte) error {
	written, err := w.Write(content)
	if err != nil {
		return err
	}
	if written != len(content) {
		return io.ErrShortWrite
	}
	return nil
}
