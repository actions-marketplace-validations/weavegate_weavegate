// Package orchestrator executes validated saved schedules across fixture-backed
// SUT workers.
package orchestrator

import (
	"errors"
	"fmt"
	"time"

	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

// RuntimeFactory creates isolated sync-point state for one run.
type RuntimeFactory func() syncpoint.Runtime

// AdapterFactory creates a fresh adapter bound to one run's runtime client.
// A factory that cannot compose an adapter for this run returns the reason,
// so the run reports that cause instead of an anonymous factory failure.
type AdapterFactory func(syncpoint.Client) (sut.Adapter, error)

// Config supplies the already-provisioned fixture and bounded run dependencies.
type Config struct {
	Fixture    fixture.Fixture
	DB         *fixture.DB
	NewRuntime RuntimeFactory
	NewAdapter AdapterFactory

	BlockInferenceTimeout time.Duration
	StepTimeout           time.Duration
	RunTimeout            time.Duration
	StopTimeout           time.Duration
	OnEvent               EventObserver
}

// Orchestrator executes one validated scenario and schedule at a time.
type Orchestrator struct {
	config  Config
	runGate chan struct{}
}

// New validates dependencies and timeout budgets without starting a run.
func New(config Config) (*Orchestrator, error) {
	if config.Fixture == nil {
		return nil, errors.New("create orchestrator: fixture is required")
	}
	if config.DB == nil {
		return nil, errors.New("create orchestrator: database is required")
	}
	if config.NewRuntime == nil {
		return nil, errors.New("create orchestrator: runtime factory is required")
	}
	if config.NewAdapter == nil {
		return nil, errors.New("create orchestrator: adapter factory is required")
	}

	timeouts := []struct {
		name  string
		value time.Duration
	}{
		{name: "block inference", value: config.BlockInferenceTimeout},
		{name: "step", value: config.StepTimeout},
		{name: "run", value: config.RunTimeout},
		{name: "stop", value: config.StopTimeout},
	}
	for _, timeout := range timeouts {
		if timeout.value <= 0 {
			return nil, fmt.Errorf(
				"create orchestrator: %s timeout must be positive, got %s",
				timeout.name,
				timeout.value,
			)
		}
	}

	runGate := make(chan struct{}, 1)
	runGate <- struct{}{}
	return &Orchestrator{config: config, runGate: runGate}, nil
}
