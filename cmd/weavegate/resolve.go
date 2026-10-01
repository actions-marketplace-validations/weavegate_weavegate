package main

import (
	"fmt"
	"io/fs"
	"slices"
	"time"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/diagnostic"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/oracle/sqlassert"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/scenario"
	internalsut "github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
	shippedrules "github.com/weavegate/weavegate/rules"
)

var diagnosticRuleFS fs.FS = shippedrules.FS()

// Timeouts holds the four orchestrator timeouts derived from the
// configured arrive timeout (A-13).
type Timeouts struct {
	BlockInference time.Duration
	Step           time.Duration
	Run            time.Duration
	Stop           time.Duration
}

// Resolved is a configured scenario translated into the values the engine
// needs, without starting a container: a fixture spec, a validated
// scenario, an Oracle set, and orchestrator timeouts. It does not access a
// database or a fixture.
type Resolved struct {
	Fixture     fixture.FixtureSpec
	Scenario    scenario.Scenario
	Oracle      *oracle.Set
	NewRuntime  orchestrator.RuntimeFactory
	NewAdapter  orchestrator.AdapterFactory
	Timeouts    Timeouts
	Schedules   fs.FS
	Diagnostics diagnostic.Table
	SUTSHA256   string
}

// Resolve translates cfg's named scenario into engine dependencies. variant,
// when non-empty, overrides target.sut.variant. No container starts here:
// an error here is always a configuration problem (exit 5), never a fixture
// failure. A malformed embedded diagnostic table is an internal build defect;
// it is deliberately not wrapped as a user configuration error.
func Resolve(cfg config.Config, scenarioName, variant string) (Resolved, error) {
	diagnosticTable, err := diagnostic.Load(diagnosticRuleFS)
	if err != nil {
		return Resolved{}, fmt.Errorf("resolve embedded diagnostic rules: %w", err)
	}
	configuredScenario, ok := cfg.Scenarios[scenarioName]
	if !ok {
		return Resolved{}, ci.InputError(fmt.Errorf(
			"resolve scenario %q: not declared in config; known scenarios: %v",
			scenarioName,
			scenarioNames(cfg),
		))
	}

	selectedVariant := variant
	if selectedVariant == "" {
		selectedVariant = cfg.Target.SUT.Variant
	}

	kind, ok := builtinAdapters[cfg.Target.SUT.Adapter]
	if !ok {
		return Resolved{}, ci.InputError(fmt.Errorf(
			"resolve adapter %q: not a registered adapter; registered adapters: %v",
			cfg.Target.SUT.Adapter,
			knownAdapterIDs(),
		))
	}
	bound, err := kind.Bind(cfg, configuredScenario, selectedVariant)
	if err != nil {
		return Resolved{}, err
	}
	if !slices.Contains(bound.Variants, selectedVariant) {
		return Resolved{}, ci.InputError(fmt.Errorf(
			"resolve variant %q for entrypoint %q: not supported; supported variants: %v",
			selectedVariant,
			cfg.Target.SUT.Entrypoint,
			bound.Variants,
		))
	}

	workers := make([]scenario.Worker, 0, len(configuredScenario.Workers))
	var params map[string]string
	for _, worker := range configuredScenario.Workers {
		workers = append(workers, scenario.Worker{ID: worker.ID, Command: worker.Command})
		params = worker.Args
	}

	resolvedScenario := scenario.Scenario{
		Name:       scenarioName,
		Workers:    workers,
		SyncPoints: append([]string(nil), configuredScenario.SyncPoints...),
		SUTConfig: internalsut.SUTConfig{
			Variant: selectedVariant,
			Params:  params,
		},
	}

	oracles := make([]oracle.Oracle, 0, len(cfg.Oracle.Assertions))
	for _, assertion := range cfg.Oracle.Assertions {
		zeroRow, err := sqlassert.NewZeroRow(assertion.ID, assertion.SQL)
		if err != nil {
			return Resolved{}, ci.InputError(fmt.Errorf("resolve oracle %q: %w", assertion.ID, err))
		}
		oracles = append(oracles, zeroRow)
	}
	oracleSet, err := oracle.NewSet(oracles...)
	if err != nil {
		return Resolved{}, ci.InputError(fmt.Errorf("resolve oracle set: %w", err))
	}

	arrive := time.Duration(cfg.Run.ArriveTimeoutMS) * time.Millisecond

	timeouts := Timeouts{
		BlockInference: arrive,
		Step:           20 * arrive,
		Run:            60 * arrive,
		Stop:           20 * arrive,
	}
	if bound.Timeouts != nil {
		timeouts = *bound.Timeouts
	}

	return Resolved{
		Fixture: fixture.FixtureSpec{
			Image:      cfg.Target.DB,
			Migrations: cfg.Target.Schema.Migrations,
			Seed:       cfg.Target.Schema.Seed,
		},
		Scenario:    resolvedScenario,
		Oracle:      oracleSet,
		NewRuntime:  syncpoint.New,
		NewAdapter:  bound.NewAdapter,
		Timeouts:    timeouts,
		Schedules:   bound.Schedules,
		Diagnostics: diagnosticTable,
		SUTSHA256:   bound.SUTSHA256,
	}, nil
}

func scenarioNames(cfg config.Config) []string {
	names := make([]string, 0, len(cfg.Scenarios))
	for name := range cfg.Scenarios {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}
