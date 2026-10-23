package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"testing"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/timing"
)

type callbackOracle struct {
	id       string
	evaluate func() ([]oracle.Violation, error)
}

func (o callbackOracle) ID() string { return o.id }
func (o callbackOracle) Evaluate(context.Context, oracle.DB, oracle.RunContext) ([]oracle.Violation, error) {
	return o.evaluate()
}

func TestAssertionCompletionPrecedesLaterOracle(t *testing.T) {
	for _, fail := range []bool{false, true} {
		ends := map[string]int{}
		clock := 0
		domain := callbackOracle{"domain", func() ([]oracle.Violation, error) {
			return []oracle.Violation{{OracleID: "domain", Kind: oracle.KindAssertion, Rows: []oracle.Row{{"id": int64(1)}}}}, nil
		}}
		receipt := callbackOracle{"receipt", func() ([]oracle.Violation, error) {
			if ends["domain"] != 1 {
				t.Fatal("domain completion deferred until after receipt query")
			}
			if fail {
				return nil, errors.New("receipt failed")
			}
			return nil, nil
		}}
		wrap := func(o oracle.Oracle) oracle.Oracle {
			return completedOracle{o, func(id string) { clock++; ends[id] = clock }}
		}
		set, err := oracle.NewSet(wrap(domain), wrap(receipt))
		if err != nil {
			t.Fatal(err)
		}
		evaluation, err := set.Evaluate(context.Background(), nil, oracle.RunContext{})
		if (err != nil) != fail {
			t.Fatalf("fail=%t error=%v", fail, err)
		}
		if !fail && (len(evaluation.Results) != 2 || ends["receipt"] != 2) {
			t.Fatalf("composition or completion missing: %+v %+v", evaluation, ends)
		}
		if fail && ends["receipt"] != 0 {
			t.Fatal("failed assertion acquired completion time")
		}
	}
}

type readinessFixture struct {
	fixture.Fixture
	ctx   context.Context
	db    *fixture.DB
	fail  string
	calls []string
}

func (f *readinessFixture) probe(ctx context.Context, name string) error {
	f.calls = append(f.calls, name)
	_, end := timing.Start(ctx, "probe_"+name)
	defer end()
	if name == f.fail {
		return errors.New(name)
	}
	return nil
}

func (f *readinessFixture) Provision(ctx context.Context, _ fixture.Prepared) (*fixture.DB, error) {
	f.ctx = ctx
	return f.db, f.probe(ctx, "provision")
}

func (f *readinessFixture) Ready(*fixture.DB) error { return f.probe(f.ctx, "ready") }

type readinessConnector struct{ f *readinessFixture }

func (c readinessConnector) Connect(context.Context) (driver.Conn, error) {
	return readinessConn(c), nil
}
func (c readinessConnector) Driver() driver.Driver { return readinessDriver(c) }

type readinessDriver struct{ f *readinessFixture }

func (d readinessDriver) Open(string) (driver.Conn, error) { return readinessConn(d), nil }

type readinessConn struct{ f *readinessFixture }

func (c readinessConn) Close() error { return nil }
func (c readinessConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("unexpected prepare")
}
func (c readinessConn) Begin() (driver.Tx, error) { return nil, errors.New("unexpected begin") }
func (c readinessConn) QueryContext(ctx context.Context, query string, _ []driver.NamedValue) (driver.Rows, error) {
	switch query {
	case "SELECT VERSION(), @@session.transaction_isolation":
		if err := c.f.probe(ctx, "version"); err != nil {
			return nil, err
		}
		return &readinessRows{columns: []string{"version", "isolation"}, values: []driver.Value{"constructed", "REPEATABLE-READ"}}, nil
	case "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME":
		if err := c.f.probe(ctx, "seed"); err != nil {
			return nil, err
		}
		return &readinessRows{columns: []string{"TABLE_NAME"}}, nil
	default:
		return nil, errors.New("unexpected query: " + query)
	}
}

type readinessRows struct {
	columns []string
	values  []driver.Value
}

func (r *readinessRows) Columns() []string { return r.columns }
func (r *readinessRows) Close() error      { return nil }
func (r *readinessRows) Next(dest []driver.Value) error {
	if r.values == nil {
		return io.EOF
	}
	copy(dest, r.values)
	r.values = nil
	return nil
}

func TestProvisionTimingIncludesReadinessAndFailures(t *testing.T) {
	for _, failure := range []string{"", "provision", "version", "seed", "ready"} {
		t.Run("failure="+failure, func(t *testing.T) {
			f := &readinessFixture{fail: failure}
			db := sql.OpenDB(readinessConnector{f})
			t.Cleanup(func() {
				if err := db.Close(); err != nil {
					t.Error(err)
				}
			})
			f.db = &fixture.DB{SQL: db}
			ctx, recorder := timing.New(context.Background())
			ctx, finish := timing.Start(ctx, "command")
			ledger := map[string]any{}
			_, seed, err := provisionReady(ctx, f, fixture.Prepared{}, ledger)
			if (failure == "") != (err == nil) {
				t.Fatalf("failure=%q err=%v", failure, err)
			}
			if failure == "" && (seed == nil || ledger["seed"] == nil || len(f.calls) != 4) {
				t.Fatal("readiness did not retain environment and seed")
			}
			_, teardown := timing.Start(ctx, "fixture_teardown")
			teardown()
			finish()
			spans := recorder.Snapshot()
			environment := spans[1]
			if environment.Name != "fixture_provision" || environment.DurationNS <= 0 {
				t.Fatal(environment)
			}
			end := environment.StartNS + environment.DurationNS
			for _, span := range spans[2 : len(spans)-1] {
				if span.ParentID != environment.ID || span.StartNS < environment.StartNS || span.StartNS+span.DurationNS > end {
					t.Fatalf("readiness work outside environment interval: %+v in %+v", span, environment)
				}
			}
			if spans[len(spans)-1].StartNS < end {
				t.Fatal("environment includes teardown")
			}
		})
	}
}
