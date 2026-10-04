package main

import (
	"archive/zip"
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	mysqldriver "github.com/go-sql-driver/mysql"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/report"
)

// springPairedEnv opts into the paired Spring/MySQL evidence. The JAR is a
// separate Maven build, so an ordinary `go test ./cmd/...` skips this test.
const springPairedEnv = "WEAVEGATE_SPRING_PAIRED"

const (
	springConfig      = "fixtures/spring-matching/.weavegate/config.yaml"
	springJAR         = "fixtures/spring-matching/app/target/spring-matching.jar"
	springSchedule    = "fixtures/spring-matching/schedules/concurrent-assign.json"
	springScheduleID  = "sch_7dcb74b1e506"
	springRepeat      = 20
	connectorJClient  = "MySQL Connector/J"
	sessionCloseBound = 10 * time.Second
)

// TestSpringMatchingPairedReplay drives the instrumented Spring Boot fixture
// through the CLI against real MySQL. Verdicts come only from the configured
// SQL assertion; this test reads the CLI's exit code and saved artifacts.
// Independently of the engine, a fixture observer checks before every reset
// and before teardown that no child JVM is alive and no Connector/J session
// of the application account remains on the server.
func TestSpringMatchingPairedReplay(t *testing.T) {
	if os.Getenv(springPairedEnv) != "1" {
		t.Skipf("set %s=1 after building %s to record paired Spring evidence", springPairedEnv, springJAR)
	}
	if runtime.GOOS != "linux" {
		t.Skip("child JVM observation reads /proc")
	}
	requireDocker(t)
	root := repoRoot(t)
	if _, err := os.Stat(filepath.Join(root, springJAR)); err != nil {
		t.Fatalf("%s=1 but the fixture JAR is missing: %v", springPairedEnv, err)
	}
	// Every external session snapshots its JAR under the temporary directory.
	// A private root makes a live child JVM attributable to this test.
	snapshots := t.TempDir()
	t.Setenv("TMPDIR", snapshots)
	out := t.TempDir()
	config := filepath.Join(root, springConfig)
	env := springEnvironment(t, filepath.Join(root, springJAR))

	t.Run("explore", func(t *testing.T) {
		obs := newSpringObserver(snapshots)
		var stdout, stderr bytes.Buffer
		err := runScenario(context.Background(), &stdout, &stderr, runFlags{
			config: config, scenario: "concurrent-assign", variant: "vulnerable", variantSet: true,
			repeat: springRepeat, repeatSet: true, out: out,
		}, obs.factory)
		if code := exitCodeFromError(err); code != 2 {
			t.Fatalf("explore exit = %d (%v)\nstdout=%s\nstderr=%s", code, err, stdout.String(), stderr.String())
		}
		obs.require(t)
		env.mysql = obs.mysqlVersion
		dir := latestRunDir(t, out)
		observation := readObservation(t, dir)
		if !strings.Contains(stdout.String(), "FAIL (WG001)") || observation.Flaky || observation.ViolationRuns != springRepeat {
			t.Fatalf("explore verdict: flaky=%v violation_runs=%d\n%s", observation.Flaky, observation.ViolationRuns, stdout.String())
		}
		saved, err := os.ReadFile(filepath.Join(dir, report.ScheduleFile))
		if err != nil {
			t.Fatal(err)
		}
		committed, err := os.ReadFile(filepath.Join(root, springSchedule))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(saved, committed) {
			t.Fatalf("discovered schedule differs from %s:\n%s", springSchedule, saved)
		}
		t.Logf("SPRING_EXPLORE_RESULT variant=vulnerable exit=2 diagnostic=WG001 schedule=%s repeat=%d flaky=false saved=byte_identical",
			springScheduleID, springRepeat)
	})

	// The vulnerable replay resolves the ID from the saved explore run; the
	// fixed replay reads the committed portable file. Both are the same bytes.
	for _, tc := range []struct {
		variant, replay string
		exit            int
	}{
		{"vulnerable", springScheduleID, 2},
		{"fixed", filepath.Join(root, springSchedule), 0},
	} {
		t.Run("replay-"+tc.variant, func(t *testing.T) {
			obs := newSpringObserver(snapshots)
			replayOut := filepath.Join(t.TempDir(), "out")
			if tc.replay == springScheduleID {
				replayOut = out
			}
			var stdout, stderr bytes.Buffer
			started := time.Now()
			err := runScenario(context.Background(), &stdout, &stderr, runFlags{
				config: config, scenario: "concurrent-assign", variant: tc.variant, variantSet: true,
				replay: tc.replay, replaySet: true, repeat: springRepeat, repeatSet: true, out: replayOut,
			}, obs.factory)
			elapsed := time.Since(started)
			if code := exitCodeFromError(err); code != tc.exit {
				t.Fatalf("replay exit = %d (%v)\nstdout=%s\nstderr=%s", code, err, stdout.String(), stderr.String())
			}
			obs.require(t)
			dir := newestRunDir(t, replayOut)
			observation := readObservation(t, dir)
			if observation.Flaky || len(observation.Fingerprints) != 1 || observation.Repeat != springRepeat {
				t.Fatalf("replay determinism: flaky=%v fingerprints=%v repeat=%d", observation.Flaky, observation.Fingerprints, observation.Repeat)
			}
			if got := readScenario(t, dir).Schedule; got == nil || got.ID != springScheduleID {
				t.Fatalf("replayed schedule = %+v", got)
			}
			// One fingerprint over every run means each run produced the same
			// normalized trace, so the representative trace speaks for all 20.
			blocked := strings.Contains(readFile(t, filepath.Join(dir, report.TraceFile)), `"timeout_inferred"`)
			switch tc.variant {
			case "vulnerable":
				if !strings.Contains(stdout.String(), "FAIL (WG001)") || observation.ViolationRuns != springRepeat || blocked {
					t.Fatalf("vulnerable replay: violation_runs=%d blocked=%v\n%s", observation.ViolationRuns, blocked, stdout.String())
				}
				t.Logf("SPRING_REPLAY_RESULT schedule=%s variant=vulnerable repeat=%d exit=2 diagnostic=WG001 violation_runs=%d flaky=false",
					springScheduleID, springRepeat, springRepeat)
			case "fixed":
				if !strings.Contains(stdout.String(), "## weavegate: PASS") || observation.ViolationRuns != 0 || !blocked {
					t.Fatalf("fixed replay: violation_runs=%d blocked=%v\n%s", observation.ViolationRuns, blocked, stdout.String())
				}
				t.Logf("SPRING_REPLAY_RESULT schedule=%s variant=fixed repeat=%d exit=0 verdict=PASS violation_runs=0 blocked_runs=%d flaky=false",
					springScheduleID, springRepeat, springRepeat)
			}
			t.Logf("SPRING_REPLAY_DURATION variant=%s repeat=%d elapsed_ms=%d resets_observed=%d",
				tc.variant, springRepeat, elapsed.Milliseconds(), obs.resets)
		})
	}

	// A failure after the insert rolls back through Spring; the run itself
	// completes, and the observer sees no committed assignment.
	t.Run("rollback", func(t *testing.T) {
		obs := newSpringObserver(snapshots)
		var stdout, stderr bytes.Buffer
		err := runScenario(context.Background(), &stdout, &stderr, runFlags{
			config: config, scenario: "assign-then-fail", repeat: 1, repeatSet: true, out: t.TempDir(),
		}, obs.factory)
		if code := exitCodeFromError(err); code != 0 {
			t.Fatalf("rollback exit = %d (%v)\nstderr=%s", code, err, stderr.String())
		}
		obs.require(t)
		if obs.maxAssignments != 0 {
			t.Fatalf("rolled-back command left %d assignments", obs.maxAssignments)
		}
		t.Log("SPRING_ROLLBACK_RESULT exit=0 assignments=0 jvm=reaped connections=closed")
	})

	// The JVM halts with its transaction open. The engine reports a session
	// fault; the server must roll the orphaned transaction back.
	t.Run("application-death", func(t *testing.T) {
		obs := newSpringObserver(snapshots)
		var stdout, stderr bytes.Buffer
		err := runScenario(context.Background(), &stdout, &stderr, runFlags{
			config: config, scenario: "assign-then-halt", repeat: 1, repeatSet: true, out: t.TempDir(),
		}, obs.factory)
		if code := exitCodeFromError(err); code != 5 || !strings.Contains(stderr.String(), "SUT session fault") {
			t.Fatalf("application death exit = %d (%v)\nstderr=%s", code, err, stderr.String())
		}
		obs.require(t)
		if obs.teardowns != 1 || obs.maxAssignments != 0 {
			t.Fatalf("after death: teardowns=%d assignments=%d", obs.teardowns, obs.maxAssignments)
		}
		t.Log("SPRING_DEATH_RESULT exit=5 fault=session assignments=0 jvm=reaped connections=closed")
	})

	// Cancel the operation while a Connector/J session is executing the
	// locking read, then require the same cleanup as a completed run.
	t.Run("cancellation", func(t *testing.T) {
		obs := newSpringObserver(snapshots)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		type sighting struct {
			state string
			seen  bool
		}
		watched := make(chan sighting, 1)
		go func() {
			state, err := obs.awaitLockingRead(ctx)
			if err == nil {
				cancel()
			}
			watched <- sighting{state, err == nil}
		}()
		var stdout, stderr bytes.Buffer
		err := runScenario(ctx, &stdout, &stderr, runFlags{
			config: config, scenario: "concurrent-assign", variant: "fixed", variantSet: true,
			replay: filepath.Join(root, springSchedule), replaySet: true, repeat: springRepeat, repeatSet: true,
			out: t.TempDir(),
		}, obs.factory)
		cancel()
		sight := <-watched
		if !sight.seen {
			t.Fatalf("no locking read observed before the run ended: %v\nstderr=%s", err, stderr.String())
		}
		if code := exitCodeFromError(err); code != 130 {
			t.Fatalf("canceled run exit = %d (%v)\nstderr=%s", code, err, stderr.String())
		}
		obs.require(t)
		t.Logf("SPRING_CANCEL_STATE state=%q", sight.state)
		t.Log("SPRING_CANCEL_RESULT during=locking_read exit=130 jvm=reaped connections=closed")
	})

	if entries, err := filepath.Glob(filepath.Join(snapshots, "weavegate-jar-*")); err != nil || len(entries) != 0 {
		t.Fatalf("JAR snapshots left behind: %v %v", entries, err)
	}
	t.Logf("SPRING_ENVIRONMENT go=%s java=%s mysql=%s spring_boot=%s spring=%s transaction_manager=%s jdbc_driver=%s pool=%s weavegate_spring=%s",
		runtime.Version(), env.java, env.mysql, env.libs["spring-boot"], env.libs["spring-core"],
		"spring-jdbc-"+env.libs["spring-jdbc"], env.libs["mysql-connector-j"], env.libs["HikariCP"], env.libs["weavegate-spring"])
	t.Log("SPRING_LIFECYCLE_RESULT resets=checked blocked=observed rollback=rolled_back death=rolled_back cancel=cleaned jvm=reaped connections=closed snapshots=removed")
}

type springEnv struct {
	java, mysql string
	libs        map[string]string
}

// springEnvironment records the versions the child JVM actually runs: the
// Java runtime that launches it and the libraries packaged in the JAR.
func springEnvironment(t *testing.T, jar string) springEnv {
	t.Helper()
	output, err := exec.Command("java", "-XshowSettings:properties", "-version").CombinedOutput()
	if err != nil {
		t.Fatalf("java version: %v\n%s", err, output)
	}
	match := regexp.MustCompile(`(?m)^\s*java\.runtime\.version = (\S+)`).FindSubmatch(output)
	if match == nil {
		t.Fatalf("java.runtime.version missing:\n%s", output)
	}
	archive, err := zip.OpenReader(jar)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = archive.Close() }()
	libs := map[string]string{}
	wanted := []string{"spring-boot", "spring-core", "spring-jdbc", "mysql-connector-j", "HikariCP", "weavegate-spring"}
	for _, file := range archive.File {
		name, ok := strings.CutPrefix(file.Name, "BOOT-INF/lib/")
		if !ok {
			continue
		}
		for _, lib := range wanted {
			if version, ok := strings.CutPrefix(name, lib+"-"); ok && version != "" && version[0] >= '0' && version[0] <= '9' {
				libs[lib] = strings.TrimSuffix(version, ".jar")
			}
		}
	}
	for _, lib := range wanted {
		if libs[lib] == "" {
			t.Fatalf("%s is not packaged in %s", lib, jar)
		}
	}
	return springEnv{java: string(match[1]), libs: libs}
}

// springObserver wraps the CLI's MySQL fixture. It never changes fixture
// behavior; it only observes before Reset and Teardown delegate.
type springObserver struct {
	snapshots string

	mu             sync.Mutex
	inner          fixture.Provisioner
	db             *sql.DB
	schema         string
	ready          chan struct{}
	mysqlVersion   string
	resets         int
	teardowns      int
	maxAssignments int
	failures       []string
}

func newSpringObserver(snapshots string) *springObserver {
	return &springObserver{snapshots: snapshots, ready: make(chan struct{})}
}

func (o *springObserver) factory() fixture.Provisioner {
	o.inner = fixture.NewMySQLFixture()
	return observedFixture{Provisioner: o.inner, observer: o}
}

type observedFixture struct {
	fixture.Provisioner
	observer *springObserver
}

func (f observedFixture) Provision(ctx context.Context, prepared fixture.Prepared) (*fixture.DB, error) {
	db, err := f.Provisioner.Provision(ctx, prepared)
	if err == nil {
		f.observer.open(ctx, db.Connection)
	}
	return db, err
}

func (f observedFixture) Reset(ctx context.Context) error {
	f.observer.check(ctx, "reset")
	return f.Provisioner.Reset(ctx)
}

func (f observedFixture) Teardown(ctx context.Context) error {
	f.observer.check(ctx, "teardown")
	f.observer.close()
	return f.Provisioner.Teardown(ctx)
}

// open connects as the application account with the Go driver, so its own
// sessions are distinguishable from the child's Connector/J sessions.
func (o *springObserver) open(ctx context.Context, descriptor fixture.ConnectionDescriptor) {
	password, err := descriptor.Password()
	if err != nil {
		o.fail("observer credential: %v", err)
		return
	}
	cfg := mysqldriver.NewConfig()
	cfg.User, cfg.Passwd, cfg.Net = descriptor.Username, password, "tcp"
	cfg.Addr = net.JoinHostPort(descriptor.Host, strconv.Itoa(descriptor.Port))
	db, err := sql.Open("mysql", cfg.FormatDSN())
	if err != nil {
		o.fail("observer open: %v", err)
		return
	}
	var version string
	if err := db.QueryRowContext(ctx, "SELECT VERSION()").Scan(&version); err != nil {
		_ = db.Close()
		o.fail("observer version: %v", err)
		return
	}
	o.mu.Lock()
	o.db, o.schema, o.mysqlVersion = db, descriptor.Name, version
	o.mu.Unlock()
	close(o.ready)
}

func (o *springObserver) close() {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.db != nil {
		_ = o.db.Close()
		o.db = nil
	}
}

func (o *springObserver) fail(format string, args ...any) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.failures = append(o.failures, fmt.Sprintf(format, args...))
}

// check runs after the engine has stopped the previous adapter. A child JVM
// must already be reaped; the server may take a moment to retire a closed
// client session, so session absence is polled within a fixed bound.
func (o *springObserver) check(ctx context.Context, stage string) {
	if pids := snapshotJVMs(o.snapshots); len(pids) != 0 {
		o.fail("%s: child JVM still running: %v", stage, pids)
	}
	o.mu.Lock()
	db, schema := o.db, o.schema
	if stage == "reset" {
		o.resets++
	} else {
		o.teardowns++
	}
	o.mu.Unlock()
	if db == nil {
		o.fail("%s: observer has no database", stage)
		return
	}
	deadline := time.Now().Add(sessionCloseBound)
	poll := time.NewTicker(20 * time.Millisecond)
	defer poll.Stop()
	for {
		var sessions int
		err := db.QueryRowContext(ctx, `SELECT COUNT(DISTINCT PROCESSLIST_ID)
FROM performance_schema.session_account_connect_attrs
WHERE ATTR_NAME = '_client_name' AND ATTR_VALUE = ?`, connectorJClient).Scan(&sessions)
		if err != nil {
			o.fail("%s: count Connector/J sessions: %v", stage, err)
			return
		}
		if sessions == 0 {
			break
		}
		if time.Now().After(deadline) {
			o.fail("%s: %d Connector/J sessions remain after %s", stage, sessions, sessionCloseBound)
			return
		}
		<-poll.C
	}
	var assignments int
	err := db.QueryRowContext(ctx, "SELECT COUNT(*) FROM `"+schema+"`.assignment").Scan(&assignments)
	var mysqlErr *mysqldriver.MySQLError
	if errors.As(err, &mysqlErr) && mysqlErr.Number == 1146 {
		return // the first reset precedes schema creation
	}
	if err != nil {
		o.fail("%s: count assignments: %v", stage, err)
		return
	}
	o.mu.Lock()
	o.maxAssignments = max(o.maxAssignments, assignments)
	o.mu.Unlock()
}

// awaitLockingRead reports the server state of a Connector/J session while it
// executes the FOR UPDATE request read.
func (o *springObserver) awaitLockingRead(ctx context.Context) (string, error) {
	select {
	case <-o.ready:
	case <-ctx.Done():
		return "", ctx.Err()
	}
	o.mu.Lock()
	db := o.db
	o.mu.Unlock()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		var state sql.NullString
		err := db.QueryRowContext(ctx, `SELECT p.STATE FROM information_schema.PROCESSLIST p
JOIN performance_schema.session_account_connect_attrs a ON a.PROCESSLIST_ID = p.ID
WHERE a.ATTR_NAME = '_client_name' AND a.ATTR_VALUE = ? AND p.COMMAND = 'Query' AND p.INFO LIKE '%FOR UPDATE%'
LIMIT 1`, connectorJClient).Scan(&state)
		switch {
		case err == nil:
			return state.String, nil
		case !errors.Is(err, sql.ErrNoRows):
			return "", err
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

func (o *springObserver) require(t *testing.T) {
	t.Helper()
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.failures) != 0 {
		t.Fatalf("lifecycle observer:\n%s", strings.Join(o.failures, "\n"))
	}
	if o.teardowns != 1 || o.resets == 0 {
		t.Fatalf("lifecycle observer saw resets=%d teardowns=%d", o.resets, o.teardowns)
	}
}

// snapshotJVMs lists processes whose command line names a JAR snapshot under
// root; the CLI launches the child JVM only from such a snapshot.
func snapshotJVMs(root string) []string {
	entries, _ := os.ReadDir("/proc")
	var pids []string
	for _, entry := range entries {
		if _, err := strconv.Atoi(entry.Name()); err != nil {
			continue
		}
		cmdline, err := os.ReadFile(filepath.Join("/proc", entry.Name(), "cmdline"))
		if err == nil && bytes.Contains(cmdline, []byte(root+string(filepath.Separator))) {
			pids = append(pids, entry.Name())
		}
	}
	return pids
}

func newestRunDir(t *testing.T, outDir string) string {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(outDir, "runs"))
	if err != nil || len(entries) == 0 {
		t.Fatalf("read runs under %q: %v", outDir, err)
	}
	// Run IDs begin with a UTC timestamp, so the lexical maximum is newest.
	return filepath.Join(outDir, "runs", entries[len(entries)-1].Name())
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(content)
}
