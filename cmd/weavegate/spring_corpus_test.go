package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/report"
	"github.com/weavegate/weavegate/internal/scenario"
)

// TestSpringRegressionCorpus uses only the existing CLI, adapter and Oracle.
// The test observer captures committed rows and checks cleanup at every reset;
// it never supplies verdicts or coordinates application workers.
func TestSpringRegressionCorpus(t *testing.T) {
	if testing.Short() || os.Getenv("WEAVEGATE_SPRING_CORPUS") != "1" {
		t.Skip("set WEAVEGATE_SPRING_CORPUS=1 after building the two corpus JARs")
	}
	if runtime.GOOS != "linux" {
		t.Skip("child JVM observation reads /proc")
	}
	requireDocker(t)
	root := repoRoot(t)
	out := t.TempDir()
	if base := os.Getenv("WEAVEGATE_SPRING_CORPUS_OUT"); base != "" {
		if err := os.MkdirAll(base, 0o700); err != nil {
			t.Fatal(err)
		}
		var err error
		out, err = os.MkdirTemp(base, "corpus-")
		if err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("SPRING_CORPUS_ARTIFACTS %s", out)
	snapshots := t.TempDir()
	t.Setenv("TMPDIR", snapshots)

	for _, fixtureCase := range []struct {
		name, scenario, assertion, query string
		seed, serial, overlap, fixed     map[string]string
	}{
		{
			name: "spring-quota", scenario: "concurrent-reserve", assertion: "quota-is-conserved",
			query: `SELECT a.capacity, a.remaining, COUNT(r.id) AS receipts,
COALESCE(SUM(r.accepted), 0) AS accepted,
COALESCE(GROUP_CONCAT(r.observed_remaining ORDER BY r.id), '') AS snapshots
FROM quota_account a LEFT JOIN quota_receipt r ON r.account_id = a.id
WHERE a.id = 42 GROUP BY a.capacity, a.remaining`,
			seed:    map[string]string{"capacity": "1", "remaining": "1", "receipts": "0", "accepted": "0", "snapshots": ""},
			serial:  map[string]string{"capacity": "1", "remaining": "0", "receipts": "2", "accepted": "1", "snapshots": "1,0"},
			overlap: map[string]string{"capacity": "1", "remaining": "0", "receipts": "2", "accepted": "2", "snapshots": "1,1"},
			fixed:   map[string]string{"capacity": "1", "remaining": "0", "receipts": "2", "accepted": "1", "snapshots": "1,1"},
		},
		{
			name: "spring-job-claim", scenario: "complete-and-reassign", assertion: "completion-requires-current-lease",
			query: `SELECT j.epoch, j.status, COALESCE(CAST(j.completed_epoch AS CHAR), 'NULL') AS completed_epoch,
COUNT(r.id) AS receipts,
COALESCE(GROUP_CONCAT(CONCAT(r.operation, ':', r.observed_epoch, ':', r.observed_status, ':', r.changed_rows)
ORDER BY r.id), '') AS snapshots
FROM job j LEFT JOIN job_receipt r ON r.job_id = j.id
WHERE j.id = 42 GROUP BY j.epoch, j.status, j.completed_epoch`,
			seed:    map[string]string{"epoch": "1", "status": "RUNNING", "completed_epoch": "NULL", "receipts": "0", "snapshots": ""},
			serial:  map[string]string{"epoch": "1", "status": "DONE", "completed_epoch": "1", "receipts": "2", "snapshots": "complete:1:RUNNING:1,reassign:1:DONE:0"},
			overlap: map[string]string{"epoch": "2", "status": "DONE", "completed_epoch": "1", "receipts": "2", "snapshots": "reassign:1:RUNNING:1,complete:1:RUNNING:1"},
			fixed:   map[string]string{"epoch": "2", "status": "RUNNING", "completed_epoch": "NULL", "receipts": "2", "snapshots": "reassign:1:RUNNING:1,complete:1:RUNNING:0"},
		},
	} {
		t.Run(fixtureCase.name, func(t *testing.T) {
			base := filepath.Join(root, "fixtures", fixtureCase.name)
			jar := filepath.Join(base, "app", "target", fixtureCase.name+".jar")
			env := springEnvironment(t, jar)
			var provenance *report.Manifest
			for _, order := range []string{"serial", "overlap"} {
				for _, variant := range []string{"vulnerable", "fixed"} {
					t.Run(order+"/"+variant, func(t *testing.T) {
						savedPath := filepath.Join(base, "schedules", order+".json")
						saved, err := scenario.LoadScheduleFile(savedPath)
						if err != nil {
							t.Fatal(err)
						}
						wantState := fixtureCase.serial
						wantExit, violations := 0, 0
						if order == "overlap" {
							wantState = fixtureCase.fixed
							if variant == "vulnerable" {
								wantState = fixtureCase.overlap
								wantExit, violations = 2, springRepeat
							}
						}
						dest := filepath.Join(out, fixtureCase.name, order, variant)
						obs := &corpusObserver{Provisioner: fixture.NewMySQLFixture(), snapshots: snapshots,
							query: fixtureCase.query, seed: fixtureCase.seed}
						var stdout, stderr bytes.Buffer
						err = runScenario(context.Background(), &stdout, &stderr, runFlags{
							config: filepath.Join(base, ".weavegate", "config.yaml"), scenario: fixtureCase.scenario,
							variant: variant, variantSet: true, replay: savedPath, replaySet: true,
							repeat: springRepeat, repeatSet: true, out: dest,
						}, func() fixture.Provisioner { return obs })
						// Preserve observer data even when a later assertion fails.
						if mkdirErr := os.MkdirAll(dest, 0o700); mkdirErr != nil {
							t.Fatal(mkdirErr)
						}
						writeCorpusJSON(t, filepath.Join(dest, "database-observations.json"), obs.States)
						if code := exitCodeFromError(err); code != wantExit || obs.failure != nil {
							t.Fatalf("exit=%d want=%d err=%v observer=%v\nstdout=%s\nstderr=%s",
								code, wantExit, err, obs.failure, stdout.String(), stderr.String())
						}
						if obs.resets != springRepeat || obs.teardowns != 1 || len(obs.States) != springRepeat {
							t.Fatalf("lifecycle resets=%d teardowns=%d states=%d", obs.resets, obs.teardowns, len(obs.States))
						}
						for i, state := range obs.States {
							if !reflect.DeepEqual(state, wantState) {
								t.Fatalf("run %d state=%v want=%v", i+1, state, wantState)
							}
						}
						dir := latestRunDir(t, dest)
						observation := readObservation(t, dir)
						if observation.Repeat != springRepeat || observation.Flaky || observation.ViolationRuns != violations || len(observation.Fingerprints) != 1 {
							t.Fatalf("unexpected observation: %+v", observation)
						}
						for _, count := range observation.Fingerprints {
							if count != springRepeat {
								t.Fatalf("fingerprint count=%d", count)
							}
						}
						if violations != 0 {
							if len(observation.Diagnostics) != 1 || observation.Diagnostics[0].Code != "WG001" ||
								len(observation.AssertionViolations) != 1 || observation.AssertionViolations[0].OracleID != fixtureCase.assertion ||
								len(observation.AssertionViolations[0].Rows) != 1 {
								t.Fatalf("violation evidence=%+v", observation)
							}
						} else if len(observation.Diagnostics) != 0 || len(observation.AssertionViolations) != 0 {
							t.Fatalf("unexpected violation evidence=%+v", observation)
						}
						if readFile(t, filepath.Join(dir, report.ScheduleFile)) != readFile(t, savedPath) {
							t.Fatal("saved schedule differs from committed bytes")
						}
						var trace report.Trace
						readCorpusJSON(t, filepath.Join(dir, report.TraceFile), &trace)
						var realized []scenario.CoordinationStep
						for _, event := range trace.Events {
							if event.Kind == "point_timeout" || event.Kind == "step_terminal_skipped" || event.Kind == "worker_failed" {
								t.Fatalf("unexpected trace event=%+v", event)
							}
							if event.Kind == "point_released" {
								realized = append(realized, scenario.CoordinationStep{Worker: event.Worker, Point: event.Point})
							}
						}
						if trace.ScheduleRef != saved.ID || !reflect.DeepEqual(realized, saved.Steps) || len(trace.Terminals) != 2 {
							t.Fatalf("trace mismatch: %+v", trace)
						}
						for _, terminal := range trace.Terminals {
							if terminal.State != "done" || terminal.FailureClass != "none" {
								t.Fatalf("terminal=%+v", terminal)
							}
						}
						var manifest report.Manifest
						readCorpusJSON(t, filepath.Join(dir, report.ManifestFile), &manifest)
						if manifest.CleanupFailed || manifest.Adapter != "external" || manifest.IsolationLevel != "REPEATABLE-READ" || manifest.SUTSHA256 == "" {
							t.Fatalf("manifest=%+v", manifest)
						}
						if provenance != nil && (manifest.SUTSHA256 != provenance.SUTSHA256 || manifest.SchemaVersion != provenance.SchemaVersion || manifest.SeedData != provenance.SeedData) {
							t.Fatal("schedule comparison changed the JAR, schema or seed")
						}
						provenance = &manifest
						t.Logf("SPRING_CORPUS_RESULT fixture=%s order=%s variant=%s repeat=%d exit=%d violation_runs=%d fingerprints=1 requested=realized timeouts=0 resets=checked jvm=reaped connections=closed",
							fixtureCase.name, order, variant, springRepeat, wantExit, violations)
						t.Logf("SPRING_CORPUS_STATE schedule=%s state=%v", saved.ID, wantState)
					})
				}
			}
			t.Logf("SPRING_CORPUS_ENVIRONMENT fixture=%s java=%s libraries=%v", fixtureCase.name, env.java, env.libs)
		})
	}
	if entries, err := filepath.Glob(filepath.Join(snapshots, "weavegate-jar-*")); err != nil || len(entries) != 0 {
		t.Fatalf("JAR snapshots left behind: %v %v", entries, err)
	}
}

type corpusObserver struct {
	fixture.Provisioner
	db                *fixture.DB
	snapshots, query  string
	seed              map[string]string
	States            []map[string]string
	resets, teardowns int
	failure           error
}

func (o *corpusObserver) Provision(ctx context.Context, prepared fixture.Prepared) (*fixture.DB, error) {
	db, err := o.Provisioner.Provision(ctx, prepared)
	o.db = db
	return db, err
}

func (o *corpusObserver) Reset(ctx context.Context) error {
	if err := o.beforeReset(ctx); err != nil {
		o.failure = errors.Join(o.failure, err)
		return err
	}
	if err := o.Provisioner.Reset(ctx); err != nil {
		return err
	}
	state, err := corpusState(ctx, o.db.SQL, o.query)
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(state, o.seed) {
		return fmt.Errorf("reset seed=%v want=%v", state, o.seed)
	}
	o.resets++
	return nil
}

func (o *corpusObserver) Teardown(ctx context.Context) error {
	o.teardowns++
	var observed error
	if o.db != nil {
		observed = o.beforeReset(ctx)
	}
	o.failure = errors.Join(o.failure, observed)
	return errors.Join(observed, o.Provisioner.Teardown(ctx))
}

func (o *corpusObserver) beforeReset(ctx context.Context) error {
	if pids := snapshotJVMs(o.snapshots); len(pids) != 0 {
		return fmt.Errorf("child JVM still alive: %v", pids)
	}
	// Poll only for server retirement of already-closed sessions. This observer
	// never releases a sync point and does not determine workflow ordering.
	ctx, cancel := context.WithTimeout(ctx, sessionCloseBound)
	defer cancel()
	poll := time.NewTicker(20 * time.Millisecond)
	defer poll.Stop()
	for {
		var sessions int
		err := o.db.SQL.QueryRowContext(ctx, `SELECT COUNT(DISTINCT PROCESSLIST_ID)
FROM performance_schema.session_account_connect_attrs
WHERE ATTR_NAME = '_client_name' AND ATTR_VALUE = ?`, connectorJClient).Scan(&sessions)
		if err != nil {
			return err
		}
		if sessions == 0 {
			break
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("Connector/J sessions remain: %d: %w", sessions, ctx.Err())
		case <-poll.C:
		}
	}
	if o.resets > 0 {
		state, err := corpusState(ctx, o.db.SQL, o.query)
		if err != nil {
			return err
		}
		o.States = append(o.States, state)
	}
	return nil
}

func corpusState(ctx context.Context, db *sql.DB, query string) (map[string]string, error) {
	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	if !rows.Next() {
		return nil, errors.Join(errors.New("missing fixture state"), rows.Err())
	}
	values := make([]string, len(columns))
	dest := make([]any, len(columns))
	for i := range values {
		dest[i] = &values[i]
	}
	if err := rows.Scan(dest...); err != nil {
		return nil, err
	}
	state := make(map[string]string, len(columns))
	for i, name := range columns {
		state[name] = values[i]
	}
	if rows.Next() {
		return nil, errors.New("multiple fixture states")
	}
	return state, rows.Err()
}

func readCorpusJSON(t *testing.T, path string, value any) {
	t.Helper()
	if err := json.Unmarshal([]byte(readFile(t, path)), value); err != nil {
		t.Fatal(err)
	}
}

func writeCorpusJSON(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(data, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
}
