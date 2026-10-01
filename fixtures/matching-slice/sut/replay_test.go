package matchingsut

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/oracle/sqlassert"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/scenario"
	internalsut "github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/sut/gonative"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

const (
	matchingOracleID           = "active-assignment-is-unique"
	matchingCountsOracleID     = "matching-workflow-counts"
	matchingViolationEvidence  = `{"active_assignment_count":2,"project_request_id":42}`
	matchingCountDriftEvidence = `{"active_assignment_count":1,"assignment_count":1,"matching_session_count":2}`
	matchingReplayRepeat       = 20
	matchingReplayTestTimeout  = 14 * time.Minute
	replayRunTimeout           = 15 * time.Second
	replayStepTimeout          = 5 * time.Second
	replayLockInferenceTimeout = 250 * time.Millisecond
	replayLockObservationWait  = 2 * time.Second
	replayLockObservationPoll  = 10 * time.Millisecond
	replayStopTimeout          = 5 * time.Second
)

func TestReplayConcurrentAssign(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), matchingReplayTestTimeout)
	defer cancel()

	runner := fixture.NewMySQLFixture()
	t.Cleanup(func() {
		if err := runner.Teardown(context.Background()); err != nil {
			t.Errorf("cleanup matching replay fixture: %v", err)
		}
	})
	prepared, err := fixture.Prepare(fixture.FixtureSpec{
		Image:      "mysql:8.4",
		Migrations: filepath.Join("..", "db", "migration"),
		Seed:       filepath.Join("..", "db", "seed.sql"),
	})
	if err != nil {
		t.Fatalf("prepare matching replay fixture: %v", err)
	}
	db, err := runner.Provision(ctx, prepared)
	if err != nil {
		t.Fatalf("provision matching replay fixture: %v", err)
	}

	saved, err := scenario.LoadScheduleFile(
		filepath.Join("..", "schedules", "concurrent-assign.json"),
	)
	if err != nil {
		t.Fatalf("load matching replay schedule: %v", err)
	}

	startedAt := time.Now()
	vulnerable := replayMatchingVariant(
		t,
		ctx,
		runner,
		db,
		saved,
		string(variantVulnerable),
	)
	fixed := replayMatchingVariant(
		t,
		ctx,
		runner,
		db,
		saved,
		string(variantFixed),
	)
	elapsed := time.Since(startedAt)

	t.Logf(
		"MATCHING_ORACLE_RESULT schedule=sch_ba00582f9632 variant=vulnerable "+
			"oracle=active-assignment-is-unique count_oracle=matching-workflow-counts "+
			"oracles_evaluated=%d repeat=20 "+
			"violation_runs=%d violations=%d evidence_rows=%d evidence_json=%s flaky=%t",
		vulnerable.oraclesEvaluated,
		vulnerable.violationRuns,
		vulnerable.violations,
		vulnerable.evidenceRows,
		vulnerable.evidenceJSON,
		vulnerable.flaky,
	)
	t.Logf(
		"MATCHING_ORACLE_RESULT schedule=sch_ba00582f9632 variant=fixed "+
			"oracle=active-assignment-is-unique count_oracle=matching-workflow-counts "+
			"oracles_evaluated=%d repeat=20 "+
			"pass_runs=%d violations=%d evidence_rows=%d flaky=%t",
		fixed.oraclesEvaluated,
		fixed.passRuns,
		fixed.violations,
		fixed.evidenceRows,
		fixed.flaky,
	)
	t.Logf(
		"MATCHING_REPLAY_RESULT schedule=sch_ba00582f9632 variant=vulnerable repeat=20 "+
			"duplicate_runs=%d blocked_runs=%d sessions=2 assignments=2 active_assignments=2 "+
			"worker_errors=%d deadlocks=%d flaky=%t",
		vulnerable.duplicateRuns,
		vulnerable.blockedRuns,
		vulnerable.workerErrors,
		vulnerable.deadlocks,
		vulnerable.flaky,
	)
	t.Logf(
		"MATCHING_REPLAY_RESULT schedule=sch_ba00582f9632 variant=fixed repeat=20 "+
			"pass_runs=%d blocked_runs=%d lock_wait_runs=%d sessions=1 assignments=1 "+
			"active_assignments=1 worker_errors=%d deadlocks=%d flaky=%t",
		fixed.passRuns,
		fixed.blockedRuns,
		fixed.lockWaitRuns,
		fixed.workerErrors,
		fixed.deadlocks,
		fixed.flaky,
	)
	t.Logf(
		"MATCHING_REPLAY_TIMING schedule=sch_ba00582f9632 runs=40 elapsed=%s average=%s",
		elapsed,
		elapsed/(2*matchingReplayRepeat),
	)
}

type matchingReplayEvidence struct {
	oraclesEvaluated int
	passRuns         int
	violationRuns    int
	violations       int
	evidenceRows     int
	evidenceJSON     string
	duplicateRuns    int
	blockedRuns      int
	lockWaitRuns     int
	workerErrors     int
	deadlocks        int
	flaky            bool
}

const matchingAssertionQuery = `
SELECT
    project_request_id,
    COUNT(*) AS active_assignment_count
FROM assignment
WHERE status = 'ACTIVE'
GROUP BY project_request_id
HAVING COUNT(*) > 1
ORDER BY project_request_id;
`

func newMatchingOracleSet(selected string) (*oracle.Set, error) {
	assertion, err := newMatchingInvariantOracle()
	if err != nil {
		return nil, err
	}

	wantCounts := workflowCounts{sessions: 2, assignments: 2, activeAssignments: 2}
	if selected == string(variantFixed) {
		wantCounts = workflowCounts{sessions: 1, assignments: 1, activeAssignments: 1}
	} else if selected != string(variantVulnerable) {
		return nil, fmt.Errorf("create matching count assertion: unsupported variant %q", selected)
	}
	countQuery := fmt.Sprintf(`
SELECT
    (SELECT COUNT(*) FROM matching_session) AS matching_session_count,
    (SELECT COUNT(*) FROM assignment) AS assignment_count,
    (SELECT COUNT(*) FROM assignment WHERE status = 'ACTIVE') AS active_assignment_count
HAVING matching_session_count <> %d
    OR assignment_count <> %d
    OR active_assignment_count <> %d;
`, wantCounts.sessions, wantCounts.assignments, wantCounts.activeAssignments)
	countAssertion, err := sqlassert.NewZeroRow(matchingCountsOracleID, countQuery)
	if err != nil {
		return nil, fmt.Errorf("create matching count SQL assertion: %w", err)
	}

	set, err := oracle.NewSet(assertion, countAssertion)
	if err != nil {
		return nil, fmt.Errorf("create matching Oracle set: %w", err)
	}
	return set, nil
}

func newMatchingInvariantOracleSet() (*oracle.Set, error) {
	assertion, err := newMatchingInvariantOracle()
	if err != nil {
		return nil, err
	}

	set, err := oracle.NewSet(assertion)
	if err != nil {
		return nil, fmt.Errorf("create matching invariant Oracle set: %w", err)
	}
	return set, nil
}

func newMatchingInvariantOracle() (oracle.Oracle, error) {
	assertion, err := sqlassert.NewZeroRow(matchingOracleID, matchingAssertionQuery)
	if err != nil {
		return nil, fmt.Errorf("create matching SQL assertion: %w", err)
	}
	return assertion, nil
}

func replayMatchingVariant(
	t *testing.T,
	ctx context.Context,
	runner fixture.Fixture,
	db *fixture.DB,
	saved scenario.Schedule,
	selected string,
) matchingReplayEvidence {
	t.Helper()

	evidence := matchingReplayEvidence{}
	onEvent := func(event orchestrator.Event) error {
		if selected != string(variantFixed) || event.Kind != orchestrator.EventPointTimeout {
			return nil
		}
		if event.Status != orchestrator.ControlStatusTimeoutInferred ||
			event.FailureClass != orchestrator.WorkerFailureNone {
			return fmt.Errorf("fixed timeout event has mixed taxonomy: %+v", event)
		}

		waits, err := waitForInnoDBRowLockWaitEvidence(
			ctx,
			replayLockObservationWait,
			replayLockObservationPoll,
			func(sampleCtx context.Context) (int, error) {
				return currentInnoDBRowLockWaits(sampleCtx, db.SQL)
			},
		)
		if err != nil {
			return fmt.Errorf("observe fixed InnoDB row lock wait: %w", err)
		}
		if waits < 1 {
			return fmt.Errorf(
				"observe fixed InnoDB row lock wait at step %d: got %d, want at least 1",
				event.Step,
				waits,
			)
		}
		evidence.lockWaitRuns++
		return nil
	}

	executor, err := orchestrator.New(orchestrator.Config{
		Fixture:    runner,
		DB:         db,
		NewRuntime: syncpoint.New,
		NewAdapter: func(client syncpoint.Client) (internalsut.Adapter, error) {
			return gonative.New(NewRegistry(client)), nil
		},
		BlockInferenceTimeout: replayLockInferenceTimeout,
		StepTimeout:           replayStepTimeout,
		RunTimeout:            replayRunTimeout,
		StopTimeout:           replayStopTimeout,
		OnEvent:               onEvent,
	})
	if err != nil {
		t.Fatalf("create %s replay orchestrator: %v", selected, err)
	}

	value := scenario.Scenario{
		Name: "matching-concurrent-assign",
		Workers: []scenario.Worker{
			{ID: "w1", Command: CommandAssign},
			{ID: "w2", Command: CommandAssign},
		},
		SyncPoints: []string{AfterReadRequest, BeforeInsertAssignment},
		SUTConfig: internalsut.SUTConfig{
			Variant: selected,
			Params:  map[string]string{"request_id": fmt.Sprint(seededRequestID)},
		},
	}
	evaluator, err := newMatchingOracleSet(selected)
	if err != nil {
		t.Fatalf("create %s matching Oracle set: %v", selected, err)
	}

	replay, err := executor.Replay(
		ctx,
		value,
		saved,
		matchingReplayRepeat,
		evaluator,
	)
	if err != nil {
		t.Fatalf("replay matching %s variant: %v", selected, err)
	}
	evidence.flaky = replay.Flaky
	if replay.Flaky {
		failMatchingReplay(
			t,
			selected,
			replay,
			"matching %s replay is flaky: fingerprints=%d mismatch_runs=%v",
			selected,
			len(replay.Fingerprints),
			replay.MismatchRuns,
		)
	}
	if len(replay.Fingerprints) != 1 {
		failMatchingReplay(
			t,
			selected,
			replay,
			"matching %s fingerprints = %d, want 1",
			selected,
			len(replay.Fingerprints),
		)
	}
	if len(replay.Runs) != matchingReplayRepeat {
		t.Fatalf("matching %s runs = %d, want %d", selected, len(replay.Runs), matchingReplayRepeat)
	}

	wantCounts := workflowCounts{sessions: 2, assignments: 2, activeAssignments: 2}
	if selected == string(variantFixed) {
		wantCounts = workflowCounts{sessions: 1, assignments: 1, activeAssignments: 1}
	}

	for index, run := range replay.Runs {
		runNumber := index + 1
		if len(run.Evaluation.Results) != 2 {
			failMatchingRun(
				t,
				selected,
				runNumber,
				run,
				"matching %s run %d results = %d, want 2",
				selected,
				runNumber,
				len(run.Evaluation.Results),
			)
		}
		result := run.Evaluation.Results[0]
		if result.OracleID != matchingOracleID {
			failMatchingRun(
				t,
				selected,
				runNumber,
				run,
				"matching %s run %d Oracle ID = %q, want %q",
				selected,
				runNumber,
				result.OracleID,
				matchingOracleID,
			)
		}
		countResult := run.Evaluation.Results[1]
		if countResult.OracleID != matchingCountsOracleID || len(countResult.Violations) != 0 {
			failMatchingRun(
				t,
				selected,
				runNumber,
				run,
				"matching %s run %d count verdict = %+v, want evaluated PASS from %q",
				selected,
				runNumber,
				countResult,
				matchingCountsOracleID,
			)
		}
		evidence.oraclesEvaluated = len(run.Evaluation.Results)
		if selected == string(variantVulnerable) {
			if len(result.Violations) != 1 ||
				result.Violations[0].OracleID != matchingOracleID ||
				result.Violations[0].Kind != oracle.KindAssertion ||
				len(result.Violations[0].Rows) != 1 {
				failMatchingRun(
					t,
					selected,
					runNumber,
					run,
					"matching vulnerable run %d verdict = %+v, want one assertion violation with one row",
					runNumber,
					result,
				)
			}
			evidenceJSON, marshalErr := json.Marshal(result.Violations[0].Rows[0])
			if marshalErr != nil || string(evidenceJSON) != matchingViolationEvidence {
				failMatchingRun(
					t,
					selected,
					runNumber,
					run,
					"matching vulnerable run %d evidence JSON = %s (error=%v), want %s",
					runNumber,
					evidenceJSON,
					marshalErr,
					matchingViolationEvidence,
				)
			}
			evidence.violationRuns++
			evidence.violations += len(result.Violations)
			evidence.evidenceRows += len(result.Violations[0].Rows)
			evidence.evidenceJSON = string(evidenceJSON)
		} else {
			if len(result.Violations) != 0 {
				failMatchingRun(
					t,
					selected,
					runNumber,
					run,
					"matching fixed run %d violations = %d, want evaluated PASS",
					runNumber,
					len(result.Violations),
				)
			}
			evidence.passRuns++
		}

		timeoutCount := 0
		terminalSkipped := false
		for _, event := range run.Trace {
			switch event.Kind {
			case orchestrator.EventPointTimeout:
				timeoutCount++
			case orchestrator.EventStepTerminalSkipped:
				if event.Step == 3 && event.Worker == "w2" &&
					event.Point == BeforeInsertAssignment {
					terminalSkipped = true
				}
			}
		}
		if timeoutCount > 0 {
			evidence.blockedRuns++
		}

		for workerIndex, terminal := range run.Terminals {
			if terminal.FailureClass == orchestrator.WorkerFailureMySQLDeadlock {
				evidence.deadlocks++
			} else if terminal.FailureClass != orchestrator.WorkerFailureNone {
				evidence.workerErrors++
			}
			if run.Workers[workerIndex].Err != nil {
				t.Fatalf(
					"matching %s run %d worker %q: %v",
					selected,
					runNumber,
					terminal.Worker,
					run.Workers[workerIndex].Err,
				)
			}
		}

		if selected == string(variantVulnerable) {
			if timeoutCount != 0 {
				t.Fatalf("vulnerable run %d timeout events = %d, want 0", runNumber, timeoutCount)
			}
			continue
		}

		if timeoutCount != 1 {
			t.Fatalf("fixed run %d timeout events = %d, want 1", runNumber, timeoutCount)
		}
		if !terminalSkipped {
			t.Fatalf("fixed run %d lacks terminal skip for w2 before insert", runNumber)
		}
	}
	// duplicate_runs is retained only as a compatibility alias for the SQL
	// Oracle's violation_runs; it is not an independent verdict.
	evidence.duplicateRuns = evidence.violationRuns

	if selected == string(variantVulnerable) {
		if evidence.oraclesEvaluated != 2 ||
			evidence.violationRuns != matchingReplayRepeat ||
			evidence.violations != matchingReplayRepeat ||
			evidence.evidenceRows != matchingReplayRepeat ||
			evidence.evidenceJSON != matchingViolationEvidence ||
			evidence.blockedRuns != 0 {
			failMatchingReplay(t, selected, replay, "vulnerable replay evidence = %+v", evidence)
		}
	} else if evidence.oraclesEvaluated != 2 ||
		evidence.passRuns != matchingReplayRepeat ||
		evidence.violations != 0 ||
		evidence.evidenceRows != 0 ||
		evidence.blockedRuns != matchingReplayRepeat ||
		evidence.lockWaitRuns != matchingReplayRepeat {
		failMatchingReplay(t, selected, replay, "fixed replay evidence = %+v", evidence)
	}
	if evidence.workerErrors != 0 || evidence.deadlocks != 0 {
		t.Fatalf("matching %s terminal failures = %+v", selected, evidence)
	}

	counts, err := readWorkflowCountsResult(ctx, db.SQL)
	if err != nil {
		t.Fatalf("read final matching %s counts: %v", selected, err)
	}
	if counts != wantCounts {
		t.Fatalf("final matching %s counts = %+v, want %+v", selected, counts, wantCounts)
	}
	return evidence
}

func TestMatchingCountOracleDetectsDrift(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	runner := fixture.NewMySQLFixture()
	t.Cleanup(func() {
		if err := runner.Teardown(context.Background()); err != nil {
			t.Errorf("cleanup matching count Oracle fixture: %v", err)
		}
	})
	prepared, err := fixture.Prepare(fixture.FixtureSpec{
		Image:      "mysql:8.4",
		Migrations: filepath.Join("..", "db", "migration"),
		Seed:       filepath.Join("..", "db", "seed.sql"),
	})
	if err != nil {
		t.Fatalf("prepare matching count Oracle fixture: %v", err)
	}
	db, err := runner.Provision(ctx, prepared)
	if err != nil {
		t.Fatalf("provision matching count Oracle fixture: %v", err)
	}

	if _, err := db.SQL.ExecContext(
		ctx,
		"INSERT INTO matching_session (status) VALUES ('ACTIVE'), ('INACTIVE')",
	); err != nil {
		t.Fatalf("seed matching count drift sessions: %v", err)
	}
	if _, err := db.SQL.ExecContext(
		ctx,
		`INSERT INTO assignment (project_request_id, matching_session_id, status)
         VALUES (?, 1, 'ACTIVE')`,
		seededRequestID,
	); err != nil {
		t.Fatalf("seed matching count drift assignment: %v", err)
	}

	evaluator, err := newMatchingOracleSet(string(variantFixed))
	if err != nil {
		t.Fatalf("create fixed matching Oracle set: %v", err)
	}
	evaluation, err := evaluator.Evaluate(ctx, db.SQL, oracle.RunContext{})
	if err != nil {
		t.Fatalf("evaluate fixed matching count drift: %v", err)
	}
	if len(evaluation.Results) != 2 {
		t.Fatalf("matching count drift results = %d, want 2", len(evaluation.Results))
	}
	if len(evaluation.Results[0].Violations) != 0 {
		t.Fatalf("matching uniqueness drift result = %+v, want PASS", evaluation.Results[0])
	}
	countResult := evaluation.Results[1]
	if countResult.OracleID != matchingCountsOracleID ||
		len(countResult.Violations) != 1 ||
		len(countResult.Violations[0].Rows) != 1 {
		t.Fatalf("matching count drift result = %+v, want one violation row", countResult)
	}
	evidenceJSON, err := json.Marshal(countResult.Violations[0].Rows[0])
	if err != nil || string(evidenceJSON) != matchingCountDriftEvidence {
		t.Fatalf(
			"matching count drift evidence = %s (error=%v), want %s",
			evidenceJSON,
			err,
			matchingCountDriftEvidence,
		)
	}
}

func TestWaitForInnoDBRowLockWaitEvidenceRetriesUntilVisible(t *testing.T) {
	attempts := 0
	waits, err := waitForInnoDBRowLockWaitEvidence(
		context.Background(),
		time.Second,
		time.Millisecond,
		func(context.Context) (int, error) {
			attempts++
			if attempts < 3 {
				return 0, nil
			}
			return 1, nil
		},
	)
	if err != nil {
		t.Fatalf("wait for delayed row-lock evidence: %v", err)
	}
	if waits != 1 || attempts != 3 {
		t.Fatalf("row-lock evidence waits=%d attempts=%d, want 1/3", waits, attempts)
	}

	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = waitForInnoDBRowLockWaitEvidence(
		canceledCtx,
		time.Second,
		time.Millisecond,
		func(context.Context) (int, error) { return 0, nil },
	)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled row-lock evidence error = %v, want context canceled", err)
	}
}

func waitForInnoDBRowLockWaitEvidence(
	ctx context.Context,
	timeout time.Duration,
	pollInterval time.Duration,
	sample func(context.Context) (int, error),
) (int, error) {
	waitCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()

	last := 0
	for {
		waits, err := sample(waitCtx)
		if err != nil {
			return last, err
		}
		last = waits
		if waits >= 1 {
			return waits, nil
		}

		select {
		case <-waitCtx.Done():
			return last, fmt.Errorf("row-lock wait remained %d: %w", last, waitCtx.Err())
		case <-ticker.C:
		}
	}
}

func failMatchingReplay(
	t *testing.T,
	selected string,
	replay orchestrator.ReplayResult,
	format string,
	args ...any,
) {
	t.Helper()
	for index, run := range replay.Runs {
		logMatchingRunDiagnostic(t, selected, index+1, run)
	}
	t.Fatalf(format, args...)
}

func failMatchingRun(
	t *testing.T,
	selected string,
	runNumber int,
	run orchestrator.RunResult,
	format string,
	args ...any,
) {
	t.Helper()
	logMatchingRunDiagnostic(t, selected, runNumber, run)
	t.Fatalf(format, args...)
}

func logMatchingRunDiagnostic(
	t *testing.T,
	selected string,
	runNumber int,
	run orchestrator.RunResult,
) {
	t.Helper()
	type oracleDiagnostic struct {
		OracleID   string       `json:"oracle_id"`
		Violations int          `json:"violations"`
		Evidence   []oracle.Row `json:"evidence"`
	}
	type terminalDiagnostic struct {
		Worker       string                          `json:"worker"`
		FailureClass orchestrator.WorkerFailureClass `json:"failure_class"`
	}

	oracles := make([]oracleDiagnostic, 0, len(run.Evaluation.Results))
	for _, result := range run.Evaluation.Results {
		evidence := make([]oracle.Row, 0)
		for _, violation := range result.Violations {
			evidence = append(evidence, violation.Rows...)
		}
		oracles = append(oracles, oracleDiagnostic{
			OracleID:   result.OracleID,
			Violations: len(result.Violations),
			Evidence:   evidence,
		})
	}
	terminals := make([]terminalDiagnostic, 0, len(run.Terminals))
	for _, terminal := range run.Terminals {
		terminals = append(terminals, terminalDiagnostic{
			Worker:       terminal.Worker,
			FailureClass: terminal.FailureClass,
		})
	}

	oracleJSON, oracleErr := json.Marshal(oracles)
	terminalJSON, terminalErr := json.Marshal(terminals)
	traceJSON, traceErr := json.Marshal(run.Trace)
	t.Logf(
		"MATCHING_ORACLE_DIAGNOSTIC variant=%s run=%d evaluation_fingerprint=%s "+
			"oracles=%s terminals=%s trace=%s marshal_errors=%v/%v/%v",
		selected,
		runNumber,
		run.Evaluation.Fingerprint,
		oracleJSON,
		terminalJSON,
		traceJSON,
		oracleErr,
		terminalErr,
		traceErr,
	)
}

func readWorkflowCountsResult(ctx context.Context, db *sql.DB) (workflowCounts, error) {
	queries := []string{
		"SELECT COUNT(*) FROM matching_session",
		"SELECT COUNT(*) FROM assignment",
		"SELECT COUNT(*) FROM assignment WHERE status = 'ACTIVE'",
	}
	values := make([]int, len(queries))
	for index, query := range queries {
		if err := db.QueryRowContext(ctx, query).Scan(&values[index]); err != nil {
			return workflowCounts{}, fmt.Errorf("query matching workflow count %d: %w", index, err)
		}
	}
	return workflowCounts{
		sessions:          values[0],
		assignments:       values[1],
		activeAssignments: values[2],
	}, nil
}

func currentInnoDBRowLockWaits(ctx context.Context, db *sql.DB) (int, error) {
	var name string
	var waits int
	if err := db.QueryRowContext(
		ctx,
		"SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_current_waits'",
	).Scan(&name, &waits); err != nil {
		return 0, err
	}
	if name != "Innodb_row_lock_current_waits" {
		return 0, fmt.Errorf("unexpected InnoDB status variable %q", name)
	}
	return waits, nil
}
