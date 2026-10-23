// The E5 baseline is an experiment, not a product adapter or verdict owner.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"syscall"
	"time"

	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/oracle/sqlassert"
	"github.com/weavegate/weavegate/internal/report"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/timing"
	"gopkg.in/yaml.v3"
)

type options struct {
	config, scenario, arm, variant, out, classpath, application, image, java, jar string
	executions                                                                    int
}

func main() {
	if err := entry(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func entry() error {
	var o options
	flag.StringVar(&o.config, "config", "", "fixture configuration")
	flag.StringVar(&o.scenario, "scenario", "", "scenario")
	flag.StringVar(&o.arm, "arm", "", "B0, B1, B2, or inputs")
	flag.StringVar(&o.variant, "variant", "vulnerable", "variant")
	flag.StringVar(&o.out, "out", "", "new evidence directory")
	flag.StringVar(&o.classpath, "classpath", "", "extracted, unchanged fixture classes and dependencies plus launcher")
	flag.StringVar(&o.application, "application", "", "original application class")
	flag.StringVar(&o.image, "image", "", "pinned image override")
	flag.StringVar(&o.java, "java", "", "Java executable override")
	flag.StringVar(&o.jar, "jar", "", "product JAR override")
	flag.IntVar(&o.executions, "executions", 0, "feasibility override; campaign uses 20/100")
	flag.Parse()
	cfg, err := config.Load(o.config)
	if err != nil {
		return err
	}
	if _, ok := cfg.Scenarios[o.scenario]; !ok {
		return errors.New("unknown scenario")
	}
	if o.variant != "vulnerable" && o.variant != "fixed" {
		return errors.New("unknown variant")
	}
	if o.image != "" {
		cfg.Target.DB = o.image
	}
	if cfg.Target.SUT.External == nil {
		return errors.New("external fixture required")
	}
	if o.java != "" {
		cfg.Target.SUT.External.Java = o.java
	}
	if o.jar != "" {
		cfg.Target.SUT.External.JAR = o.jar
	}
	cfg.Target.SUT.Variant = o.variant
	cfg.Run.Repeat, cfg.Run.ExplorePasses = 20, 3
	if err := cfg.Validate(); err != nil {
		return err
	}
	if o.arm != "inputs" && o.arm != "B0" && o.arm != "B1" && o.arm != "B2" {
		return errors.New("unknown arm")
	}
	e := cfg.Target.SUT.External
	if e.Capacity != 2 || e.StartupTimeoutMS != 30000 || e.CancelTimeoutMS != 5000 || e.StopTimeoutMS != 20000 || cfg.Run.ArriveTimeoutMS != 1000 {
		return errors.New("E5 requires the frozen capacity and timeout settings")
	}
	if o.executions < 0 {
		return errors.New("executions must not be negative")
	}
	if o.out == "" {
		return errors.New("out is required")
	}
	if err := os.Mkdir(o.out, 0o700); err != nil {
		return err
	}
	if o.arm == "inputs" {
		return inputs(o, cfg)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return run(ctx, o, cfg)
}

func inputs(o options, cfg config.Config) error {
	declared := cfg.Scenarios[o.scenario]
	s := scenario.Scenario{Name: o.scenario, SyncPoints: declared.SyncPoints}
	for _, worker := range declared.Workers {
		s.Workers = append(s.Workers, scenario.Worker{ID: worker.ID, Command: worker.Command})
	}
	plan, err := (scenario.Exhaustive{}).Schedules(s)
	if err != nil {
		return err
	}
	candidates := []scenario.Schedule{}
	for candidate := range plan.Seq {
		candidates = append(candidates, candidate)
	}
	if err := save(filepath.Join(o.out, "inputs.json"), map[string]any{"config": cfg, "candidates": candidates}); err != nil {
		return err
	}
	data, err := yaml.Marshal(cfg)
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(o.out, "config.yaml"), data, 0o600)
}

type execution struct {
	Index             int                `json:"index"`
	Seed              any                `json:"seed"`
	State             any                `json:"state"`
	Ready             map[string]any     `json:"ready"`
	Done              map[string]any     `json:"done"`
	Evaluation        *oracle.Evaluation `json:"evaluation"`
	OracleEndNS       int64              `json:"oracle_end_ns"`
	OracleEndsNS      map[string]int64   `json:"oracle_ends_ns"`
	ChildReaped       bool               `json:"child_reaped"`
	ConnectionsClosed bool               `json:"connections_closed"`
	ResetVerified     bool               `json:"reset_verified"`
	CancellationOnly  bool               `json:"cancellation_only"`
	Error             string             `json:"error,omitempty"`
}

func run(ctx context.Context, o options, cfg config.Config) (resultErr error) {
	origin := time.Now()
	ctx, recorder := timing.New(ctx)
	ctx, finish := timing.Start(ctx, "command")
	f := fixture.NewMySQLFixture()
	ledger := map[string]any{"schema": 1, "arm": o.arm, "variant": o.variant,
		"clock": "baseline-process-monotonic", "cleanup_proven": false, "executions": 0}
	defer func() {
		cleanup, end := timing.Start(context.WithoutCancel(ctx), "fixture_teardown")
		bounded, done := context.WithTimeout(cleanup, 30*time.Second)
		err := f.Teardown(bounded)
		done()
		end()
		ledger["cleanup_proven"] = err == nil
		resultErr = errors.Join(resultErr, err)
		if resultErr != nil {
			ledger["error"] = resultErr.Error()
			ledger["cancellation_only"] = ctx.Err() != nil && resultErr.Error() == ctx.Err().Error()
		}
		finish()
		ledger["timings"] = report.NewTimings(recorder.Snapshot())
		ledger["process_elapsed_ns"] = time.Since(origin).Nanoseconds()
		resultErr = errors.Join(resultErr, save(filepath.Join(o.out, "run.json"), ledger))
	}()
	prepared, err := fixture.Prepare(fixture.FixtureSpec{Image: cfg.Target.DB, Migrations: cfg.Target.Schema.Migrations, Seed: cfg.Target.Schema.Seed})
	if err != nil {
		return err
	}
	db, seed, err := provisionReady(ctx, f, prepared, ledger)
	if err != nil {
		return err
	}
	if o.arm == "B0" {
		return nil
	}
	if len(cfg.Scenarios[o.scenario].Workers) != 2 {
		return errors.New("exactly two workers required")
	}
	n := 20
	if o.arm == "B2" {
		n = 100
	}
	if o.executions > 0 {
		n = o.executions
	}
	ledger["planned_executions"] = n
	for i := 1; i <= n; i++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		x := execution{Index: i}
		evaluator, err := executionOracles(cfg, &x, origin)
		if err != nil {
			return err
		}
		execctx, endexec := timing.Start(ctx, "execution")
		err = func() error {
			resetctx, end := timing.Start(execctx, "fixture_reset")
			err := f.Reset(resetctx)
			if err == nil {
				x.Seed, err = snapshot(resetctx, db)
			}
			end()
			if err != nil {
				return err
			}
			if !reflect.DeepEqual(x.Seed, seed) {
				return errors.New("reset seed differs")
			}
			x.ResetVerified = true
			return launch(execctx, o, cfg, db, &x, evaluator, origin)
		}()
		endexec()
		if err != nil {
			x.Error = err.Error()
			x.CancellationOnly = ctx.Err() != nil && err.Error() == ctx.Err().Error()
		}
		ledger["executions"] = i
		if saveErr := save(filepath.Join(o.out, fmt.Sprintf("execution-%03d.json", i)), x); saveErr != nil {
			return errors.Join(err, saveErr)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// completedOracle observes the end of one declaration without taking verdict
// composition or normalization away from oracle.Set.
type completedOracle struct {
	oracle.Oracle
	completed func(string)
}

func (o completedOracle) Evaluate(ctx context.Context, db oracle.DB, run oracle.RunContext) ([]oracle.Violation, error) {
	violations, err := o.Oracle.Evaluate(ctx, db, run)
	if err == nil {
		o.completed(o.ID())
	}
	return violations, err
}

func executionOracles(cfg config.Config, x *execution, origin time.Time) (*oracle.Set, error) {
	x.OracleEndsNS = make(map[string]int64)
	var assertions []oracle.Oracle
	for _, a := range cfg.Oracle.Assertions {
		v, err := sqlassert.NewZeroRow(a.ID, a.SQL)
		if err != nil {
			return nil, err
		}
		assertions = append(assertions, completedOracle{Oracle: v, completed: func(id string) {
			x.OracleEndsNS[id] = time.Since(origin).Nanoseconds()
		}})
	}
	return oracle.NewSet(assertions...)
}

func provisionReady(ctx context.Context, f fixture.Provisioner, prepared fixture.Prepared, ledger map[string]any) (*fixture.DB, map[string][]map[string]any, error) {
	envctx, end := timing.Start(ctx, "fixture_provision")
	defer end()
	db, err := f.Provision(envctx, prepared)
	if err != nil {
		return nil, nil, err
	}
	db.SQL.SetMaxOpenConns(1)
	var version, isolation string
	if err = db.SQL.QueryRowContext(envctx, "SELECT VERSION(), @@session.transaction_isolation").Scan(&version, &isolation); err != nil {
		return nil, nil, err
	}
	ledger["environment"] = map[string]any{"version": version, "isolation": isolation, "image": prepared.Image(), "schema": prepared.MigrationDigest(), "seed": prepared.SeedDigest()}
	seed, err := snapshot(envctx, db)
	if err != nil {
		return nil, nil, err
	}
	ledger["seed"] = seed
	if err = f.Ready(db); err != nil {
		return nil, nil, err
	}
	return db, seed, nil
}

func save(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	if err = os.WriteFile(path+".tmp", append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(path+".tmp", path)
}
