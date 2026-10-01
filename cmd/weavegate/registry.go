package main

import (
	"context"
	"fmt"
	"io/fs"
	"sort"

	matchingfixture "github.com/weavegate/weavegate/fixtures/matching-slice"
	matchingsut "github.com/weavegate/weavegate/fixtures/matching-slice/sut"
	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/orchestrator"
	"github.com/weavegate/weavegate/internal/sut"
	"github.com/weavegate/weavegate/internal/sut/gonative"
	"github.com/weavegate/weavegate/internal/syncpoint"
)

// entrypoint declares one built-in workflow the CLI can run. An entrypoint
// is a Go function, not a path (A-3): the gonative adapter cannot load a
// workflow dynamically, so target.sut.entrypoint names one of these IDs.
type entrypoint struct {
	NewAdapter func(syncpoint.Client) sut.Adapter
	Variants   []string
	Schedules  fs.FS
}

// builtinEntrypoints is the CLI's fixture registry. Only cmd/weavegate knows
// about fixtures; the engine packages never import them (AGENTS.md: fixtures
// are data).
var builtinEntrypoints = map[string]entrypoint{
	"matching-slice": {
		NewAdapter: func(client syncpoint.Client) sut.Adapter {
			return gonative.New(matchingsut.NewRegistry(client))
		},
		Variants:  []string{"vulnerable", "fixed"},
		Schedules: matchingfixture.ScheduleFS(),
	},
}

// composition is one bound adapter kind: the per-run factory the
// orchestrator calls, the variants that binding accepts, and the schedules a
// replay ID can be resolved against. Schedules is nil for a kind that ships
// none, which leaves stage ③ of the replay lookup empty rather than absent.
type composition struct {
	NewAdapter orchestrator.AdapterFactory
	Variants   []string
	Schedules  fs.FS
	Timeouts   *Timeouts
	SUTSHA256  string
}

// adapterKind declares one accepted target.sut.adapter value. Bind validates
// the adapter-specific part of the configuration and returns that run's
// composition. It provisions nothing: an error from Bind is always a
// configuration problem, never a fixture failure.
type adapterKind struct {
	Bind func(config.Config, config.Scenario, string) (composition, error)
}

// builtinAdapters is the CLI's adapter registry: target.sut.adapter selects
// the composition, and each kind owns how it locates the application under
// test. Adding an adapter is a registry entry plus that kind's own
// configuration keys — the engine packages keep seeing only sut.Adapter.
var builtinAdapters = map[string]adapterKind{
	config.SupportedAdapter: {Bind: bindGoNative},
	config.ExternalAdapter:  {Bind: bindExternal},
}

// bindGoNative composes the Go-native adapter around a built-in entrypoint.
// The workflow is compiled into this binary, so the entrypoint registry is
// this kind's application lookup and its declared variants are the ones the
// compiled workflow implements.
func bindGoNative(cfg config.Config, _ config.Scenario, _ string) (composition, error) {
	entry, ok := builtinEntrypoints[cfg.Target.SUT.Entrypoint]
	if !ok {
		return composition{}, ci.InputError(fmt.Errorf(
			"resolve entrypoint %q: not a known built-in ID; known IDs: %v",
			cfg.Target.SUT.Entrypoint,
			knownEntrypointIDs(),
		))
	}
	return composition{
		NewAdapter: func(_ context.Context, client syncpoint.Client) (sut.Adapter, error) {
			return entry.NewAdapter(client), nil
		},
		Variants:  entry.Variants,
		Schedules: entry.Schedules,
	}, nil
}

// knownEntrypointIDs returns the built-in entrypoint IDs in sorted order for
// use in error messages.
func knownEntrypointIDs() []string {
	ids := make([]string, 0, len(builtinEntrypoints))
	for id := range builtinEntrypoints {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// knownAdapterIDs returns the registered adapter IDs in sorted order for use
// in error messages.
func knownAdapterIDs() []string {
	ids := make([]string, 0, len(builtinAdapters))
	for id := range builtinAdapters {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
