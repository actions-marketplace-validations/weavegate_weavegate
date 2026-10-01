package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/weavegate/weavegate/internal/ci"
	"github.com/weavegate/weavegate/internal/config"
	"github.com/weavegate/weavegate/internal/fixture"
	"github.com/weavegate/weavegate/internal/scenario"
)

func TestRunPlanRejectsInputBeforeFixtureFactory(t *testing.T) {
	configPath := filepath.Join(repoRoot(t), "fixtures", "matching-slice", ".weavegate", "config.yaml")
	outDir := t.TempDir()

	incompatible, err := scenario.NewSchedule([]scenario.CoordinationStep{{Worker: "unknown", Point: "unknown"}})
	if err != nil {
		t.Fatalf("build incompatible schedule: %v", err)
	}
	incompatiblePath := filepath.Join(t.TempDir(), "incompatible.json")
	file, err := os.Create(incompatiblePath)
	if err != nil {
		t.Fatalf("create incompatible schedule: %v", err)
	}
	if err := scenario.WriteSchedule(file, incompatible); err != nil {
		_ = file.Close()
		t.Fatalf("write incompatible schedule: %v", err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("close incompatible schedule: %v", err)
	}

	oversizedPath := writeOversizedConfig(t, configPath)
	missingSourcePath := writeConfigWithBadMigrations(t, configPath)
	malformedSourcePath := writeMalformedFixtureConfig(t, configPath)
	tests := map[string]runFlags{
		"empty replay": {
			config: configPath, scenario: "concurrent-assign", replaySet: true, out: outDir,
		},
		"missing replay": {
			config: configPath, scenario: "concurrent-assign", replay: "sch_000000000000", replaySet: true, out: outDir,
		},
		"incompatible replay": {
			config: configPath, scenario: "concurrent-assign", replay: incompatiblePath, replaySet: true, out: outDir,
		},
		"oversized candidate plan": {
			config: oversizedPath, scenario: "concurrent-assign", out: outDir,
		},
		"empty variant override": {
			config: configPath, scenario: "concurrent-assign", variantSet: true, out: outDir,
		},
		"missing fixture source": {
			config: missingSourcePath, scenario: "concurrent-assign", out: outDir,
		},
		"malformed fixture source": {
			config: malformedSourcePath, scenario: "concurrent-assign", out: outDir,
		},
	}

	for name, flags := range tests {
		t.Run(name, func(t *testing.T) {
			factoryCalls := 0
			var stderr bytes.Buffer
			err := runScenario(context.Background(), &bytes.Buffer{}, &stderr, flags, func() fixture.Provisioner {
				factoryCalls++
				return nil
			})
			if got := exitCodeFromError(err); got != ci.ExitInput {
				t.Fatalf("exit = %d, want %d; stderr=%s", got, ci.ExitInput, stderr.String())
			}
			if factoryCalls != 0 {
				t.Fatalf("fixture factory calls = %d, want 0", factoryCalls)
			}
		})
	}

	t.Log("CLI_PLAN_RESULT empty_replay=5 missing_replay=5 incompatible_replay=5 oversized_candidates=5 empty_override=5 missing_fixture=5 malformed_fixture=5 fixture_factory_calls=0 docker=false")
}

func writeOversizedConfig(t *testing.T, sourcePath string) string {
	t.Helper()
	cfg, err := config.Load(sourcePath)
	if err != nil {
		t.Fatalf("load source config: %v", err)
	}
	configured := cfg.Scenarios["concurrent-assign"]
	configured.Workers = nil
	for index := 1; index <= 4; index++ {
		configured.Workers = append(configured.Workers, config.Worker{
			ID:      "w" + string(rune('0'+index)),
			Command: "assign",
			Args:    map[string]string{"request_id": "42"},
		})
	}
	configured.SyncPoints = []string{"p1", "p2", "p3", "p4"}
	cfg.Scenarios["concurrent-assign"] = configured

	content, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal oversized config: %v", err)
	}
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatalf("write oversized config: %v", err)
	}
	return path
}

func writeMalformedFixtureConfig(t *testing.T, sourcePath string) string {
	t.Helper()
	cfg, err := config.Load(sourcePath)
	if err != nil {
		t.Fatalf("load source config: %v", err)
	}
	seedPath := filepath.Join(t.TempDir(), "seed.sql")
	if err := os.WriteFile(seedPath, []byte("INSERT INTO broken VALUES ('unterminated);"), 0o644); err != nil {
		t.Fatalf("write malformed seed: %v", err)
	}
	cfg.Target.Schema.Seed = seedPath
	content, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal malformed fixture config: %v", err)
	}
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatalf("write malformed fixture config: %v", err)
	}
	return path
}

func TestBuildRunPlanUsesExplicitReplayPresence(t *testing.T) {
	configPath := filepath.Join(repoRoot(t), "fixtures", "matching-slice", ".weavegate", "config.yaml")
	_, err := buildRunPlan(runFlags{
		config: configPath, scenario: "concurrent-assign", replay: "   ", replaySet: true, out: t.TempDir(),
	})
	if err == nil || !strings.Contains(err.Error(), "--replay must not be empty") {
		t.Fatalf("explicit blank replay error = %v", err)
	}
}

type cancellationFixture struct {
	teardownCalls   int
	cleanupErr      error
	cleanupDeadline time.Time
}

func (f *cancellationFixture) Provision(ctx context.Context, _ fixture.Prepared) (*fixture.DB, error) {
	<-ctx.Done()
	return nil, ctx.Err()
}

func (*cancellationFixture) Reset(context.Context) error {
	return errors.New("reset must not be called")
}

func (*cancellationFixture) Ready(*fixture.DB) error { return nil }
func (*cancellationFixture) Quarantine(error)        {}

func (f *cancellationFixture) Teardown(ctx context.Context) error {
	f.teardownCalls++
	f.cleanupErr = ctx.Err()
	f.cleanupDeadline, _ = ctx.Deadline()
	return nil
}

func TestCanceledRunUsesIndependentBoundedCleanupOnce(t *testing.T) {
	configPath := filepath.Join(repoRoot(t), "fixtures", "matching-slice", ".weavegate", "config.yaml")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	fx := &cancellationFixture{}
	var stderr bytes.Buffer
	err := runScenario(ctx, &bytes.Buffer{}, &stderr, runFlags{
		config: configPath, scenario: "concurrent-assign", out: t.TempDir(),
	}, func() fixture.Provisioner { return fx })
	if got := exitCodeFromError(err); got != ci.ExitInterrupted {
		t.Fatalf("canceled run exit = %d, want %d; stderr=%s", got, ci.ExitInterrupted, stderr.String())
	}
	if fx.teardownCalls != 1 {
		t.Fatalf("teardown calls = %d, want exactly 1", fx.teardownCalls)
	}
	if fx.cleanupErr != nil {
		t.Fatalf("cleanup context error = %v, want operation cancellation detached", fx.cleanupErr)
	}
	remaining := time.Until(fx.cleanupDeadline)
	if fx.cleanupDeadline.IsZero() || remaining <= 0 || remaining > fixtureCleanupTimeout {
		t.Fatalf("cleanup deadline remaining = %v, want within (0, %v]", remaining, fixtureCleanupTimeout)
	}

	t.Log("CLI_INTERRUPT_RESULT exit=130 teardown_calls=1 cleanup_deadline=30s operation_cancel=detached second_signal=default")
}
