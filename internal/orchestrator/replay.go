package orchestrator

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/weavegate/weavegate/internal/oracle"
	"github.com/weavegate/weavegate/internal/scenario"
	"github.com/weavegate/weavegate/internal/timing"
	"github.com/weavegate/weavegate/internal/trace"
)

// ReplayResult summarizes repeated executions of one saved schedule.
type ReplayResult struct {
	ScheduleID   string
	Repeat       int
	Fingerprints map[string]int
	MismatchRuns []int
	Flaky        bool
	Runs         []RunResult
	Elapsed      time.Duration
}

type normalizedRun struct {
	Oracle    string          `json:"oracle"`
	Terminals trace.Terminals `json:"terminals"`
	Trace     trace.Trace     `json:"trace"`
}

// Replay executes the same validated schedule repeat times. A logical
// fingerprint mismatch is returned as Flaky=true; run and Oracle failures
// remain errors and are never converted into flakiness.
func (o *Orchestrator) Replay(
	ctx context.Context,
	value scenario.Scenario,
	schedule scenario.Schedule,
	repeat int,
	evaluator oracle.Evaluator,
) (result ReplayResult, returnErr error) {
	ctx, finish := timing.Start(ctx, "replay")
	defer finish()
	startedAt := time.Now()
	result = ReplayResult{
		ScheduleID:   schedule.ID,
		Repeat:       repeat,
		Fingerprints: make(map[string]int),
	}
	defer func() {
		result.Elapsed = time.Since(startedAt)
	}()

	if repeat < 1 {
		return result, fmt.Errorf("replay schedule %q: repeat must be positive, got %d", schedule.ID, repeat)
	}
	if isNilEvaluator(evaluator) {
		return result, fmt.Errorf("replay schedule %q: Oracle evaluator is required", schedule.ID)
	}

	baseline := ""
	for index := 0; index < repeat; index++ {
		run, err := o.Run(ctx, value, schedule, evaluator)
		if err != nil {
			result.Flaky = false
			return result, fmt.Errorf(
				"replay schedule %q run %d/%d: %w",
				schedule.ID,
				index+1,
				repeat,
				err,
			)
		}

		fingerprint, err := normalizedFingerprint(run)
		if err != nil {
			result.Flaky = false
			return result, fmt.Errorf(
				"replay schedule %q run %d/%d: fingerprint: %w",
				schedule.ID,
				index+1,
				repeat,
				err,
			)
		}
		if run.Fingerprint == "" {
			result.Flaky = false
			return result, fmt.Errorf(
				"replay schedule %q run %d/%d: run fingerprint is empty",
				schedule.ID,
				index+1,
				repeat,
			)
		}
		if run.Fingerprint != fingerprint {
			result.Flaky = false
			return result, fmt.Errorf(
				"replay schedule %q run %d/%d: fingerprint mismatch: run=%q recomputed=%q",
				schedule.ID,
				index+1,
				repeat,
				run.Fingerprint,
				fingerprint,
			)
		}
		run.Fingerprint = fingerprint
		result.Runs = append(result.Runs, run)
		result.Fingerprints[fingerprint]++
		if index == 0 {
			baseline = fingerprint
		} else if fingerprint != baseline {
			result.MismatchRuns = append(result.MismatchRuns, index+1)
		}
	}

	result.Flaky = len(result.Fingerprints) > 1
	return result, nil
}

func normalizedFingerprint(run RunResult) (string, error) {
	evaluation, err := oracle.ValidateEvaluation(run.Evaluation)
	if err != nil {
		return "", fmt.Errorf("validate Oracle evaluation: %w", err)
	}
	canonical, err := json.Marshal(normalizedRun{
		Oracle:    evaluation.Fingerprint,
		Terminals: run.Terminals.Clone(),
		Trace:     run.Trace.Clone(),
	})
	if err != nil {
		return "", fmt.Errorf("marshal normalized run: %w", err)
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}
